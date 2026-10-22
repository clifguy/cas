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
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import patch


class BulkReadiness:
    """Observe target preparation without mistaking queued work for inference."""

    def __init__(
        self,
        target_id: str | None = None,
        *,
        clock: Callable[[], float] = time.monotonic,
        failures: list[dict] | None = None,
    ) -> None:
        self.failures = failures if failures is not None else []
        self.target_id = target_id
        self.clock = clock
        self.started = threading.Event()
        self._scope = threading.local()
        self._lock = threading.Lock()
        self._stage = "waiting"
        self._sizing_completions = 0
        self._prepared_chunks = 0
        self._last_progress = clock()
        self._wait_start = None

    def _progress(self, stage: str) -> None:
        self._stage = stage
        self._last_progress = self.clock()

    def instrument(self, ingestion: object, provider: object) -> None:
        original_chunk = ingestion._chunk_projection
        original_count = provider.count_tokens
        original_embed = ingestion._embed_chunks

        def chunk(document_id: str, projection: object) -> list:
            # The recovery manifest supplies identity before startup; fresh
            # ingestion binds it from the actual largest-source marker.
            with self._lock:
                target = document_id == self.target_id or (
                    self.target_id is None and "LARGE_BULK_TRIAL" in projection.text
                )
                if target:
                    self.target_id = document_id
                    self._progress("preparing")
            previous = getattr(self._scope, "target", False)
            self._scope.target = target
            try:
                result = original_chunk(document_id, projection)
                if target:
                    with self._lock:
                        self._prepared_chunks = len(result)
                        self._progress("prepared")
                return result
            finally:
                self._scope.target = previous

        def count(text: str) -> int:
            result = original_count(text)
            if getattr(self._scope, "target", False):
                with self._lock:
                    self._sizing_completions += 1
                    self._progress("preparing")
            return result

        async def embed(chunks: list) -> None:
            with self._lock:
                if self.target_id is not None and any(
                    chunk.document_id == self.target_id for chunk in chunks
                ):
                    self._progress("submitted")
            return await original_embed(chunks)

        ingestion._chunk_projection = chunk
        provider.count_tokens = count
        ingestion._embed_chunks = embed

    def run_inference(
        self, texts: list[str], original: Callable[[list[str]], list[list[float]]]
    ) -> list[list[float]]:
        if any("LARGE_BULK_TRIAL" in text for text in texts):
            with self._lock:
                self._progress("inference")
                self.started.set()
        # Entry is evidence of contention, not acceptance of the result. The
        # caller still records and propagates any actual inference exception.
        try:
            return original(texts)
        except Exception as exc:
            self.failures.append(
                {
                    "kind": "model_inference",
                    "source_group": "largest"
                    if any("LARGE_BULK_TRIAL" in text for text in texts)
                    else "baseline_or_witness",
                    "error": repr(exc),
                }
            )
            raise

    def snapshot(self) -> dict:
        with self._lock:
            now = self.clock()
            return {
                "target_id": self.target_id,
                "stage": self._stage,
                "sizing_completions": self._sizing_completions,
                "prepared_chunks": self._prepared_chunks,
                "elapsed_seconds": None if self._wait_start is None else now - self._wait_start,
                "inactive_seconds": now - self._last_progress,
            }

    async def wait(
        self,
        checkpoint: Callable[[], Awaitable[None]],
        *,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        absolute_timeout: float = 300,
    ) -> None:
        # Bound asynchronous diagnostics too; the clock-driven checks below
        # enforce target inactivity and remain independently testable.
        async with asyncio.timeout(absolute_timeout):
            with self._lock:
                self._wait_start = self.clock()
                self._last_progress = max(self._last_progress, self._wait_start)
            last_checkpoint = -float("inf")
            while True:
                state = self.snapshot()
                if state["elapsed_seconds"] >= absolute_timeout or state["inactive_seconds"] >= 120:
                    raise TimeoutError(f"largest inference readiness expired: {state}")
                if self.started.is_set():
                    return
                if self.clock() - last_checkpoint >= 1:
                    await checkpoint()
                    last_checkpoint = self.clock()
                await sleep(0.05)


@asynccontextmanager
async def observed_lifespan(
    app: object,
    initializer_owner: object,
    readiness: BulkReadiness,
    on_initialized: Callable[[object], None],
) -> AsyncIterator[None]:
    """Install trial instrumentation before automatic startup recovery."""
    original = initializer_owner.initialize_services

    async def initialize(*args: object, **kwargs: object) -> object:
        services = await original(*args, **kwargs)
        readiness.instrument(services.ingestion_service, services.ingestion_service._embedding)
        on_initialized(services)
        return services

    with patch.object(initializer_owner, "initialize_services", initialize):
        async with app.router.lifespan_context(app):
            yield


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
                "LARGE_BULK_TRIAL"
                if size > 1024**2
                else ("RECOVERY_WITNESS" if size == 65536 else "AuditEvent"),
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


def trial_passed(report: dict) -> bool:
    """Accept measured contention plus complete integrity/recovery evidence.

    The largest input may remain actively indexing at the measurement boundary;
    only the explicitly named, claimed job with measured progress qualifies.
    """
    files = report.get("files", [])
    successful = {"abstraction_complete", "abstraction_skipped"}
    active = {
        "projection_complete",
        "indexing_in_progress",
        "indexing_complete",
        "abstraction_in_progress",
    }
    if not files or files[0].get("pipeline_status") not in successful:
        return False
    allowed = report.get("allowed_incomplete_ids", [])
    if allowed != [files[-1]["id"]]:
        return False
    for entry in files:
        if entry.get("pipeline_error") or entry.get("retained_source_matches") is not True:
            return False
        status = entry.get("pipeline_status")
        if entry.get("stored_chunks", 0) > 0 and (
            entry.get("reconstruction_matches") is not True
            or entry.get("vectors_valid") is not True
        ):
            return False
        if status in successful:
            if (
                entry.get("reconstruction_matches") is not True
                or entry.get("vectors_valid") is not True
                or entry.get("stored_chunks", 0) <= 0
            ):
                return False
        elif (
            entry["id"] not in allowed
            or status not in active
            or entry.get("claim_active") is not True
        ):
            return False
    if report.get("phase") == "recover":
        control = report.get("recovery_control", {})
        required = (
            "interrupted",
            "claim_released_on_stop",
            "settled",
            "claim_released_after_settle",
            "reconstruction_matches",
            "vectors_valid",
            "retained_source_matches",
            "same_identity",
        )
        if not all(control.get(key) is True for key in required):
            return False
        shutdown = report.get("shutdown", {})
        if (
            report.get("duplicate_recovery_enqueues") != 0
            or shutdown.get("claims_released") is not True
            or shutdown.get("queue_empty") is not True
            or shutdown.get("failed_ids") != []
        ):
            return False
    return (
        not report["failures"]
        and report.get("semantic_max", 99) < 12
        and report.get("lightweight_p95", 99) <= 2
        and report.get("lightweight_max", 99) <= 5
        and report["large_bulk_progress_during_queries"] > 0
        and report["memory"]["process_peak_bytes"] <= 3 * 1024**3
        and int(report["memory"].get("memory.peak", 4 * 1024**3)) < 4 * 1024**3
    )


def unexpected_pipeline_errors(documents: list[dict], stopped_ids: set[str]) -> list[str]:
    """Permit only the exact deliberate worker-stop stamp for observed claims."""
    interruption = (
        "abstraction work was dropped before it completed: the queue draining it was stopped"
    )
    return [
        doc["id"]
        for doc in documents
        if doc["pipeline_status"] == "failed"
        or (
            doc.get("pipeline_error")
            and not (
                doc["id"] in stopped_ids
                and doc["pipeline_status"] == "abstraction_interrupted"
                and doc["pipeline_error"] == interruption
            )
        )
    ]


async def read_stored_vectors(store: object, document_id: str) -> list[list[float]]:
    """Read actual stored vectors; ordinary passage reads intentionally omit them."""
    rows = await store._fetchall(
        "SELECT embedding::text FROM chunks WHERE document_id = %s AND chunk_index >= 0 "
        "ORDER BY chunk_index",
        (document_id,),
    )
    return [json.loads(row[0]) if row[0] is not None else [] for row in rows]


def vectors_valid(vectors: list[list[float]], count: int, dimensions: int) -> bool:
    return (
        bool(count)
        and len(vectors) == count
        and all(
            len(vector) == dimensions
            and all(math.isfinite(value) for value in vector)
            and abs(sum(value * value for value in vector) - 1) < 0.001
            for vector in vectors
        )
    )


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
        report["large_bulk_readiness"] = readiness.snapshot()
        report["memory"] = memory()
        for kind in ("semantic", "lightweight"):
            values = sorted(report[f"{kind}_seconds"])
            if values:
                report[f"{kind}_p95"] = values[math.ceil(0.95 * len(values)) - 1]
                report[f"{kind}_max"] = max(values)
        path.write_text(json.dumps(report, indent=2))

    from sage import app as app_module

    manifest_path = root / "manifest.json"
    manifest = json.loads(manifest_path.read_text()) if args.phase == "recover" else []
    readiness = BulkReadiness(
        target_id=manifest[-1]["id"] if manifest else None, failures=report["failures"]
    )
    provider = None
    original = None
    completed = 0
    bulk_completed = 0
    witness_started = threading.Event()

    def observed(texts: list[str]) -> list[list[float]]:
        nonlocal completed, bulk_completed
        is_bulk = any("LARGE_BULK_TRIAL" in text for text in texts)
        if any("RECOVERY_WITNESS" in text for text in texts):
            witness_started.set()
        if is_bulk:
            if len(report["bulk_input_shapes"]) < 16:
                report["bulk_input_shapes"].extend(
                    {
                        "characters": len(text),
                        "bytes": len(text.encode()),
                        "tokens": provider.count_tokens(text),
                    }
                    for text in texts
                )
        result = readiness.run_inference(texts, original)
        completed += len(texts)
        if is_bulk:
            bulk_completed += len(texts)
        return result

    def observe_provider(services: object) -> None:
        nonlocal provider, original
        provider = services.ingestion_service._embedding
        original = provider._embed_sync
        provider._embed_sync = observed

    async with observed_lifespan(app, app_module, readiness, observe_provider):
        svc = app.state.vault_registry["embedding_trial"]
        provider = svc.ingestion_service._embedding

        async def checkpoint(stage: str) -> None:
            report["stage"] = stage
            report["live_jobs"] = [
                {
                    "id": doc.id,
                    "status": doc.pipeline_status,
                    "error": doc.pipeline_error,
                    "claimed": doc.id in svc.ingestion_service._inflight,
                }
                for doc in await svc.graph_store.list_all_documents()
            ]
            report["queue_size"] = (
                svc.ingestion_service._abstraction_queue.qsize()
                if svc.ingestion_service._abstraction_queue is not None
                else 0
            )
            save()

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

        async def readiness_checkpoint() -> None:
            await checkpoint("waiting for largest inference")

        try:
            await readiness.wait(readiness_checkpoint)
        except TimeoutError:
            report["failures"].append(
                {
                    "kind": "large_bulk_readiness",
                    "readiness": readiness.snapshot(),
                    # The timed-out checkpoint may itself be the graph read.
                    # Persist available diagnostics without re-entering it.
                    "documents": report.get("live_jobs"),
                    "documents_source": "last_successful_checkpoint"
                    if "live_jobs" in report
                    else "unavailable",
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
        await checkpoint("contention measurement complete; validating outputs")
        report["allowed_incomplete_ids"] = [manifest[-1]["id"]]
        for entry in manifest:
            doc = await svc.graph_store.get_document(entry["id"])
            entry["pipeline_status"] = doc.pipeline_status
            entry["pipeline_error"] = doc.pipeline_error
            entry["claim_active"] = entry["id"] in svc.ingestion_service._inflight
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
            entry["vectors_valid"] = vectors_valid(
                await read_stored_vectors(svc.content_store, entry["id"]),
                len(chunks),
                provider.dimensions,
            )
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
        if args.phase == "recover":
            # Exercise a bounded, genuinely interrupted inference job separately
            # from the ongoing largest-file measurement. Use only production
            # worker/recompute entry points and the original retained source/ID.
            ingestion = svc.ingestion_service
            await ingestion.stop_worker()
            require(not ingestion._inflight, "bulk shutdown stranded claims")
            witness = manifest[1]
            witness_id = witness["id"]
            witness_started.clear()
            await ingestion.recompute_pipeline(witness_id)
            await checkpoint("waiting for witness inference before interruption")

            async def wait_for_witness() -> None:
                while not witness_started.is_set():
                    await asyncio.sleep(0.01)

            await asyncio.wait_for(wait_for_witness(), 30)
            await ingestion.stop_worker()
            interrupted = await svc.graph_store.get_document(witness_id)
            control = {
                "document_id": witness_id,
                "source_sha256": witness["source_sha256"],
                "interrupted_status": interrupted.pipeline_status,
                "interrupted": interrupted.pipeline_status == "abstraction_interrupted",
                "claim_released_on_stop": witness_id not in ingestion._inflight,
            }
            require(
                control["interrupted"] and control["claim_released_on_stop"],
                "witness did not interrupt and release its claim",
            )
            await ingestion.recompute_pipeline(witness_id)
            await checkpoint("waiting for interrupted witness settlement")

            async def wait_for_settlement() -> None:
                while True:
                    doc = await svc.graph_store.get_document(witness_id)
                    report["witness_last_state"] = {
                        "id": doc.id,
                        "status": doc.pipeline_status,
                        "error": doc.pipeline_error,
                        "claimed": witness_id in ingestion._inflight,
                    }
                    save()
                    require(
                        not doc.pipeline_error and doc.pipeline_status != "failed",
                        f"witness failed: {doc.pipeline_error}",
                    )
                    if (
                        doc.pipeline_status in ("abstraction_complete", "abstraction_skipped")
                        and witness_id not in ingestion._inflight
                    ):
                        return
                    await asyncio.sleep(0.05)

            await asyncio.wait_for(wait_for_settlement(), 120)
            settled = await svc.graph_store.get_document(witness_id)
            chunks = await svc.content_store.get_all_chunks(witness_id)
            control.update(
                settled=settled.pipeline_status in ("abstraction_complete", "abstraction_skipped"),
                settled_status=settled.pipeline_status,
                claim_released_after_settle=witness_id not in ingestion._inflight,
                same_identity=(
                    settled.id == witness_id
                    and settled.source_content_hash == "sha256:" + witness["source_sha256"]
                ),
                reconstruction_matches=(
                    hashlib.sha256("".join(chunk.content for chunk in chunks).encode()).hexdigest()
                    == witness["projection_sha256"]
                ),
                vectors_valid=vectors_valid(
                    await read_stored_vectors(svc.content_store, witness_id),
                    len(chunks),
                    provider.dimensions,
                ),
                stored_chunks=len(chunks),
            )
            delivery = await svc.documents_service.get_document_content(witness_id)
            digest = hashlib.sha256()
            for block in delivery.chunks:
                digest.update(block)
            control["retained_source_matches"] = digest.hexdigest() == witness["source_sha256"]
            report["recovery_control"] = control
            report["remaining_recovery_enqueues"] = await ingestion.recover_incomplete_documents()
            require(
                report["remaining_recovery_enqueues"] == 1,
                "expected only the interrupted largest job to resume",
            )
            report["duplicate_recovery_enqueues"] = await ingestion.recover_incomplete_documents()
            require(report["duplicate_recovery_enqueues"] == 0, "duplicate resumed job")
            require(
                {doc.id for doc in await svc.graph_store.list_all_documents()}
                == {entry["id"] for entry in manifest},
                "recovery duplicated document identities",
            )
        if args.phase == "recover":
            intentionally_stopped = set(svc.ingestion_service._inflight)
            require(
                intentionally_stopped == {manifest[-1]["id"]},
                "unexpected claims at final largest-job interruption",
            )
            await svc.ingestion_service.stop_worker()
            completed_jobs = await asyncio.gather(
                *(asyncio.wrap_future(job) for job in provider._inference_jobs),
                return_exceptions=True,
            )
            for outcome in completed_jobs:
                if isinstance(outcome, BaseException):
                    report["failures"].append({"kind": "inference_drain", "error": repr(outcome)})
            final_documents = await svc.graph_store.list_all_documents()
            report["shutdown"] = {
                "claims_released": not svc.ingestion_service._inflight,
                "queue_empty": svc.ingestion_service._abstraction_queue.empty(),
                "failed_ids": unexpected_pipeline_errors(
                    [
                        {
                            "id": doc.id,
                            "pipeline_status": doc.pipeline_status,
                            "pipeline_error": doc.pipeline_error,
                        }
                        for doc in final_documents
                    ],
                    intentionally_stopped,
                ),
                "expected_interrupted_ids": sorted(intentionally_stopped),
                "document_states": [
                    {
                        "id": doc.id,
                        "pipeline_status": doc.pipeline_status,
                        "pipeline_error": doc.pipeline_error,
                    }
                    for doc in final_documents
                ],
            }
        if args.phase == "fresh":
            manifest_path.write_text(json.dumps(manifest, indent=2))
        save()
        report["passed"] = trial_passed(report)
        save()
        if args.phase == "fresh":
            # Persist an actual crash boundary without replaying any ingest.
            os._exit(0 if report["passed"] else 1)
    # The final peak includes source reconstruction and graceful teardown.
    save()
    report["passed"] = trial_passed(report)
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
                try:
                    run = subprocess.run(command, capture_output=True, text=True, timeout=600)
                except subprocess.TimeoutExpired as exc:

                    def decoded(value: str | bytes | None) -> str:
                        return (
                            value.decode(errors="replace")
                            if isinstance(value, bytes)
                            else value or ""
                        )

                    (root / f"{phase}.log").write_text(decoded(exc.stdout) + decoded(exc.stderr))
                    inspection = subprocess.run(
                        ["docker", "inspect", name], capture_output=True, text=True, check=False
                    )
                    (root / f"{phase}-inspect.json").write_text(
                        inspection.stdout + inspection.stderr
                    )
                    (root / f"{phase}-timeout.json").write_text(
                        json.dumps(
                            {
                                "phase": phase,
                                "container": name,
                                "timeout_seconds": 600,
                                "partial_report": str(root / f"{phase}.json"),
                                "diagnostic": "see retained live_jobs and progress",
                            },
                            indent=2,
                        )
                    )
                    raise RuntimeError(
                        f"{phase} timed out; partial report/log/inspect: {root}"
                    ) from exc
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
