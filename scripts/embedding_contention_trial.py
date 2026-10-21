"""Run a disposable real-model contention/recovery trial in the candidate image.

The host arm creates a unique test database on SAGE_TEST_PG_DSN's server, runs
both phases at 2 CPU/4 GiB, retains reports/inspect evidence, then drops only
that database. No live vault or production credential is accepted or required.
"""

import argparse
import asyncio
import csv
import hashlib
import io
import json
import math
import os
import platform
import resource
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path


def require(condition: bool, message: object) -> None:
    if not condition:
        raise RuntimeError(str(message))


def memory() -> dict:
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    result = {"process_peak_bytes": peak * (1024 if sys.platform == "linux" else 1)}
    for name in ("memory.peak", "memory.max", "memory.events"):
        path = Path("/sys/fs/cgroup") / name
        if path.exists():
            result[name] = path.read_text().strip()
    return result


def fixture(path: Path, size: int) -> dict:
    output = io.StringIO(newline="")
    writer = csv.writer(output)
    writer.writerow(["Time", "Operation", "Payload"])
    row = 0
    while output.tell() < size:
        # Distinct short words/numbers/punctuation produce dense WordPiece
        # work rather than a huge repeated field collapsing into one [UNK].
        payload = {
            "event": row,
            "records": [
                {
                    "id": row * 32 + n,
                    "action": f"authenticate account {n}",
                    "status": "accepted",
                    "detail": "first line\nsecond line",
                }
                for n in range(25 if size == 9216 else 32)
            ],
        }
        writer.writerow(
            [
                "2026-10-21",
                "LARGE_BULK_TRIAL" if size > 1024**2 else "AuditEvent",
                json.dumps(payload) + "\nAudit continuation",
            ]
        )
        row += 1
    path.write_text(output.getvalue())
    raw = path.read_bytes()
    return {
        "file": path.name,
        "bytes": len(raw),
        "source_sha256": hashlib.sha256(raw).hexdigest(),
        "shape": "dense quoted JSON with multiline cell content",
        "rows": row,
    }


async def worker(args: argparse.Namespace) -> int:
    from dataclasses import asdict

    import httpx

    from sage import build_info
    from sage.adapters.embedding_nomic import MODEL_PINS, NOMIC_MODEL_NAME
    from sage.app import create_app
    from sage.config import SageCoreConfig, VaultConfig
    from sage.models.schemas import IngestRequest
    from sage.services.vault_registry import VaultRegistryService
    from sage.source_adapters.structured_data_adapter import StructuredDataAdapter

    root = Path(args.evidence)
    root.mkdir(exist_ok=True, parents=True)
    cfg = VaultRegistryService.get_default_config("embedding_trial", "Synthetic trial", "trial")
    cfg["vault"].update(storage_root=str(root / "sources"), brain_root=str(root / "brain"))
    cfg["abstraction"] = {"enabled": False}
    cfg["timing"] = {"enabled": False}
    cfg["document_types"] = {
        "doc_types": [
            {"value": "reference_document", "label": "Trial", "source_types": ["structured_data"]}
        ]
    }
    stack = SageCoreConfig.model_validate(
        {
            "profile": "local",
            "storage_backend": "postgres",
            "abstraction": {"provider": "stub"},
            "postgres": {
                "host": "127.0.0.1",
                "port": int(os.environ.get("TRIAL_PG_PORT", "5432")),
                "database": os.environ["TRIAL_PG_DATABASE"],
                "user": os.environ["TRIAL_PG_USER"],
            },
        }
    )
    app = create_app(config=VaultConfig.model_validate(cfg), stack_config=stack)
    report = {
        "phase": args.phase,
        "architecture": platform.machine(),
        "memory": memory(),
        "semantic_seconds": [],
        "lightweight_seconds": [],
        "failures": [],
        "progress": [],
    }
    report["model_pin"] = asdict(MODEL_PINS[NOMIC_MODEL_NAME])
    report["build"] = (
        build_info.BUILD_VERSION
        if hasattr(build_info, "BUILD_VERSION")
        else build_info.RELEASE_VERSION
    )
    report["bulk_input_shapes"] = []
    path = root / f"{args.phase}.json"

    def save() -> None:
        report["memory"] = memory()
        for kind in ("semantic", "lightweight"):
            values = sorted(report[f"{kind}_seconds"])
            if values:
                report[f"{kind}_p95"] = values[math.ceil(0.95 * len(values)) - 1]
                report[f"{kind}_max"] = max(values)
        path.write_text(json.dumps(report, indent=2))

    async with app.router.lifespan_context(app):
        svc = app.state.vault_registry["embedding_trial"]
        provider = svc.ingestion_service._embedding
        completed = 0
        bulk_completed = 0
        bulk_started = threading.Event()
        original = provider._embed_sync

        def observed(texts: list[str]) -> list[list[float]]:
            nonlocal completed, bulk_completed
            is_bulk = any("LARGE_BULK_TRIAL" in text for text in texts)
            if is_bulk:
                bulk_started.set()
                if len(report["bulk_input_shapes"]) < 16:
                    report["bulk_input_shapes"].extend(
                        {
                            "characters": len(text),
                            "bytes": len(text.encode()),
                            "tokens": provider.count_tokens(text),
                        }
                        for text in texts
                    )
            result = original(texts)
            completed += len(texts)
            if is_bulk:
                bulk_completed += len(texts)
            return result

        provider._embed_sync = observed
        manifest_path = root / "manifest.json"
        if args.phase == "fresh":
            manifest = []
            for index, size in enumerate((9216, 65536, 27262976)):
                source = root / f"audit-{index}.csv"
                entry = fixture(source, size)
                projected = await StructuredDataAdapter().project(source)
                entry["projection_sha256"] = hashlib.sha256(projected.text.encode()).hexdigest()
                request = IngestRequest(
                    source=str(source),
                    source_type="structured_data",
                    metadata={"title": source.name, "doc_type": "reference_document"},
                )
                result = await svc.ingestion_service.ingest(request, wait_for_pipeline=index == 0)
                entry["id"] = result.document.id
                manifest.append(entry)
                manifest_path.write_text(json.dumps(manifest, indent=2))
        else:
            manifest = json.loads(manifest_path.read_text())
        report["files"] = manifest
        if args.phase == "recover":
            report["recovered_ids"] = [
                entry["id"]
                for entry in manifest
                if entry.get("pipeline_status")
                not in ("abstraction_complete", "abstraction_skipped")
            ]
            require(
                manifest[-1]["id"] in report["recovered_ids"], "largest job was not interrupted"
            )
            report[
                "duplicate_recovery_enqueues"
            ] = await svc.ingestion_service.recover_incomplete_documents()
            require(report["duplicate_recovery_enqueues"] == 0, "duplicate recovery claim")

        async def wait_for_bulk() -> None:
            while not bulk_started.is_set():
                await asyncio.sleep(0.05)

        try:
            await asyncio.wait_for(wait_for_bulk(), 120)
        except TimeoutError:
            report["failures"].append(
                {
                    "kind": "large_bulk_readiness",
                    "documents": [
                        {"id": doc.id, "status": doc.pipeline_status, "error": doc.pipeline_error}
                        for doc in await svc.graph_store.list_all_documents()
                    ],
                }
            )
            report["passed"] = False
            save()
            raise
        bulk_at_start = bulk_completed
        baseline_id = manifest[0]["id"]
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://trial"
        ) as client:
            for _ in range(args.samples):
                started = time.monotonic()
                try:
                    response = await asyncio.wait_for(
                        client.post(
                            "/sage_vaults/embedding_trial/discover",
                            json={
                                "mode": "semantic",
                                "query": "authenticate account accepted audit event",
                                "filters": {"document_ids": [baseline_id]},
                                "min_relevance": 0,
                            },
                        ),
                        12,
                    )
                    response.raise_for_status()
                    require(
                        response.json()["results"], "already-indexed baseline returned no results"
                    )
                except Exception as exc:
                    report["failures"].append({"kind": "semantic", "error": repr(exc)})
                report["semantic_seconds"].append(time.monotonic() - started)
                for route in ("/health", "/sage_vaults"):
                    started = time.monotonic()
                    try:
                        response = await asyncio.wait_for(client.get(route), 5)
                        response.raise_for_status()
                    except Exception as exc:
                        report["failures"].append({"kind": "lightweight", "error": repr(exc)})
                    report["lightweight_seconds"].append(time.monotonic() - started)
                report["progress"].append(
                    {
                        "completed_inputs": completed,
                        "large_bulk_completed_inputs": bulk_completed,
                        "time": time.time(),
                    }
                )
                save()
                await asyncio.sleep(0.1)
        report["large_bulk_progress_during_queries"] = bulk_completed - bulk_at_start
        records = await svc.graph_store.list_all_documents()
        require(
            {record.id for record in records} == {entry["id"] for entry in manifest},
            "document identities changed or duplicate ingestion occurred",
        )
        for entry in manifest:
            doc = await svc.graph_store.get_document(entry["id"])
            entry["pipeline_status"] = doc.pipeline_status
            entry["pipeline_error"] = doc.pipeline_error
            entry["persisted_source_hash"] = doc.source_content_hash
            require(
                doc.source_content_hash == "sha256:" + entry["source_sha256"],
                "persisted source hash changed",
            )
            delivery = await svc.documents_service.get_document_content(entry["id"])
            digest = hashlib.sha256()
            for block in delivery.chunks:
                digest.update(block)
            entry["retained_source_matches"] = digest.hexdigest() == entry["source_sha256"]
            require(entry["retained_source_matches"], "retained source bytes changed")
            chunks = await svc.content_store.get_all_chunks(entry["id"])
            entry["stored_chunks"] = len(chunks)
            if chunks:
                entry["reconstruction_matches"] = (
                    hashlib.sha256("".join(c.content for c in chunks).encode()).hexdigest()
                    == entry["projection_sha256"]
                )
            require(
                hashlib.sha256((root / entry["file"]).read_bytes()).hexdigest()
                == entry["source_sha256"],
                "source changed during trial",
            )
        if args.phase == "fresh":
            manifest_path.write_text(json.dumps(manifest, indent=2))
        report["memory"] = memory()
        report["passed"] = (
            not report["failures"]
            and report.get("semantic_max", 99) < 12
            and report.get("lightweight_p95", 99) <= 2
            and report.get("lightweight_max", 99) <= 5
            and report["large_bulk_progress_during_queries"] > 0
            and report["memory"]["process_peak_bytes"] <= 3 * 1024**3
            and int(report["memory"].get("memory.peak", 4 * 1024**3)) < 4 * 1024**3
            and manifest[0].get("reconstruction_matches") is True
        )
        save()
        if args.phase == "fresh":
            # A process interruption, preserving exact source/document IDs for
            # startup recovery. No ingest is replayed in the recovery phase.
            os._exit(0 if report["passed"] else 1)
    save()
    report["passed"] = (
        report["passed"]
        and report["memory"]["process_peak_bytes"] <= 3 * 1024**3
        and int(report["memory"].get("memory.peak", 4 * 1024**3)) < 4 * 1024**3
    )
    save()
    return 0 if report["passed"] else 1


def host(args: argparse.Namespace) -> int:
    import psycopg
    from psycopg import sql
    from psycopg.conninfo import conninfo_to_dict

    dsn = os.environ["SAGE_TEST_PG_DSN"]
    conninfo = conninfo_to_dict(dsn)
    require(
        conninfo.get("host") in ("localhost", "127.0.0.1"), "trial requires local test Postgres"
    )
    require(conninfo.get("dbname") in ("sage_test", "postgres"), "not a test maintenance database")
    database = "embedding_trial_" + uuid.uuid4().hex[:12]
    root = Path(args.evidence).resolve()
    root.mkdir(parents=True, exist_ok=True)
    root.chmod(0o777)
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(database)))
    try:
        for phase in ("fresh", "recover"):
            name = database + "_" + phase
            command = [
                "docker",
                "run",
                "--name",
                name,
                "--network",
                "host",
                "--cpus",
                "2",
                "--memory",
                "4g",
                "--memory-swap",
                "4g",
                "--entrypoint",
                "python",
                "-v",
                f"{Path(__file__).resolve()}:/trial.py:ro",
                "-v",
                f"{root}:/evidence",
                "-e",
                f"TRIAL_PG_DATABASE={database}",
                "-e",
                f"TRIAL_PG_USER={conninfo.get('user', os.environ.get('USER', 'postgres'))}",
                "-e",
                f"TRIAL_PG_PORT={conninfo.get('port', '5432')}",
                "-e",
                f"SAGE_PG_PASSWORD={conninfo.get('password', '')}",
                args.image,
                "/trial.py",
                "--phase",
                phase,
                "--evidence",
                "/evidence",
                "--samples",
                str(args.samples),
            ]
            try:
                run = subprocess.run(command, capture_output=True, text=True, timeout=600)
                (root / f"{phase}.log").write_text(run.stdout + run.stderr)
                inspect = subprocess.run(
                    ["docker", "inspect", name], capture_output=True, text=True, check=True
                )
                (root / f"{phase}-inspect.json").write_text(inspect.stdout)
                state = json.loads(inspect.stdout)[0]
                require(
                    not state["State"]["OOMKilled"] and state["RestartCount"] == 0,
                    "OOM or unexpected restart",
                )
                require(state["Config"]["Image"] == args.image, "wrong image")
                require(run.returncode == 0, f"{phase} failed: see {root}/{phase}.log")
                report = json.loads((root / f"{phase}.json").read_text())
                require(report["architecture"] == "x86_64" and report["passed"], report)
            finally:
                subprocess.run(["docker", "rm", "-f", name], check=False, capture_output=True)
    finally:
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(database)))
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--phase", choices=["fresh", "recover"])
    parser.add_argument("--evidence", required=True)
    parser.add_argument("--image", default="cas-sage:ci")
    parser.add_argument("--samples", type=int, default=30)
    arguments = parser.parse_args()
    sys.exit(asyncio.run(worker(arguments)) if arguments.phase else host(arguments))
