"""Read-only observer. Importing this module performs no network calls."""

import concurrent.futures
import fcntl
import json
import time
import uuid

from .config import bound, settings
from .recovery_checks import atomic, check_scope, probe_ok, utc


def client():
    from . import client as helper

    return helper


def probe(helper, run_id, label, path, method, body):
    started = utc().isoformat()
    began = time.monotonic()
    row = bound({"kind": "request", "run_id": run_id, "label": label, "started_at": started})
    try:
        result = helper.request(path, method, body, timeout=12)
        row.update(
            status=result["status"],
            seconds=time.monotonic() - began,
            http_seconds=result["seconds"],
            ok=probe_ok(label, result),
        )
        if label in {"semantic", "keyword"} and result["status"] == 200:
            hits = result["body"].get("results", [])
            row.update(
                results=len(hits),
                matched_document=hits[0].get("document", {}).get("id") if hits else None,
            )
    except Exception as error:
        # Do not retain response bodies, credentials, or arbitrary exception text.
        row.update(ok=False, seconds=time.monotonic() - began, error=type(error).__name__)
    row["at"] = utc().isoformat()
    return row


def main():
    with (settings().root / "monitor.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        check_scope(json.loads((settings().root / "backlog-ids.json").read_text()))
        helper = client()
        run_id = str(uuid.uuid4())
        atomic(
            settings().root / "monitor-run.json",
            bound(
                {
                    "run_id": run_id,
                    "start_utc": utc().isoformat(),
                    "ids": settings().expected_ids,
                }
            ),
        )
        tests = [
            ("health", "/health", "GET", None),
            ("inventory", "/sage_vaults", "GET", None),
            (
                "document",
                f"/sage_vaults/{settings().vault_id}/documents/" + settings().six_ids[0],
                "GET",
                None,
            ),
            (
                "catalog",
                f"/sage_vaults/{settings().vault_id}/discover",
                "POST",
                {
                    "mode": "catalog",
                    "limit": 6,
                    "response_mode": "light",
                    "filters": {"document_ids": settings().six_ids},
                },
            ),
            (
                "keyword",
                f"/sage_vaults/{settings().vault_id}/discover",
                "POST",
                {
                    "mode": "keyword",
                    "query": "SAGE",
                    "limit": 1,
                    "response_mode": "light",
                    "filters": {"document_ids": [settings().indexed_document]},
                },
            ),
            (
                "semantic",
                f"/sage_vaults/{settings().vault_id}/discover",
                "POST",
                {
                    "mode": "semantic",
                    "query": "Information security project instructions",
                    "limit": 1,
                    "response_mode": "light",
                    "filters": {"document_ids": [settings().indexed_document]},
                },
            ),
        ]
        with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
            while not (settings().root / "stop").exists():
                began = time.monotonic()
                rows = list(pool.map(lambda test: probe(helper, run_id, *test), tests))
                cycle = bound(
                    {
                        "kind": "cycle",
                        "run_id": run_id,
                        "at": utc().isoformat(),
                        "requests": rows,
                    }
                )
                with (settings().root / "cycles.jsonl").open("a") as stream:
                    stream.write(json.dumps(cycle) + "\n")
                    stream.flush()
                # A complete immutable cycle is published only after all six probes finish.
                atomic(settings().root / "monitor-latest.json", cycle)
                with (settings().root / "samples.jsonl").open("a") as stream:
                    for row in rows:
                        stream.write(json.dumps(row) + "\n")
                time.sleep(max(0, 10 - (time.monotonic() - began)))


if __name__ == "__main__":
    main()
