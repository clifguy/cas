"""Fail-closed checks shared by the private recovery observer and controller."""

import datetime as dt
import json
import math
import os
from pathlib import Path

from .config import require_binding, settings

LABELS = {"health", "inventory", "document", "catalog", "keyword", "semantic"}
LIGHT = {"health", "inventory", "document", "catalog"}


class Unsafe(RuntimeError):
    pass


def require(condition, message):
    if not condition:
        raise Unsafe(message)


def utc():
    return dt.datetime.now(dt.timezone.utc)


def timestamp(value):
    result = dt.datetime.fromisoformat(value)
    require(result.tzinfo is not None, "timestamp lacks timezone")
    return result


def atomic(path, value):
    path = Path(path)
    tmp = path.with_name(path.name + f".{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(value, stream, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def check_scope(ids):
    require(
        len(ids) == len(settings().expected_ids) and set(ids) == set(settings().expected_ids),
        "backlog scope differs from authorized sixteen IDs",
    )


def check_backlog(documents, complete=False):
    check_scope([item["id"] for item in documents])
    for item in documents:
        require(item.get("lifecycle_status") == "active", "backlog lifecycle changed")
        if complete:
            require(
                item.get("pipeline_status") == "abstraction_complete"
                and not item.get("pipeline_error"),
                "backlog not successfully complete",
            )
    return all(
        item.get("pipeline_status") == "abstraction_complete" and not item.get("pipeline_error")
        for item in documents
    )


def check_replica(rows, expected=None):
    require(len(rows) == 1, "expected exactly one replica")
    row = rows[0]
    require(expected is None or row["name"] == expected, "replica identity changed")
    containers = row["properties"]["containers"]
    require(len(containers) == 1, "expected exactly one container")
    for container in containers:
        require(
            container.get("ready") is True
            and container.get("runningState") == "Running"
            and container.get("restartCount") == 0,
            "container readiness/running/restart condition",
        )
    return row["name"]


def probe_ok(label, response):
    if response["status"] != 200:
        return False
    if label in {"semantic", "keyword"}:
        results = response["body"].get("results", [])
        return (
            bool(results)
            and results[0].get("document", {}).get("id") == settings().indexed_document
        )
    return True


def check_sample(row):
    require_binding(row)
    require(row["ok"] is True, "probe failed: " + row["label"])
    seconds = row["seconds"]
    require(
        isinstance(seconds, (int, float)) and math.isfinite(seconds) and 0 <= seconds <= 5,
        "probe exceeded five-second budget",
    )
    if row["label"] in {"semantic", "keyword"}:
        require(
            row.get("matched_document") == settings().indexed_document,
            "positive indexed search result missing",
        )


def check_snapshot(snapshot, run_id, now=None):
    now = now or utc()
    require_binding(snapshot)
    require(snapshot.get("run_id") == run_id, "observer run changed")
    age = (now - timestamp(snapshot["at"])).total_seconds()
    require(0 <= age <= 45, "observer missing or stale")
    rows = snapshot["requests"]
    require(
        len(rows) == 6 and {x["label"] for x in rows} == LABELS,
        "incomplete probe cycle",
    )
    for row in rows:
        require_binding(row)
        require(row.get("run_id") == run_id, "probe belongs to another observer run")
        require(
            0 <= (now - timestamp(row["started_at"])).total_seconds() <= 60,
            "stale probe",
        )
    return rows


def check_window(cycles, run_id, start, end):
    require((end - start).total_seconds() >= 1800, "window is shorter than thirty minutes")
    for cycle in cycles:
        if cycle.get("run_id") == run_id:
            require_binding(cycle)
    for label in LABELS:
        rows = [
            r
            for c in cycles
            if c.get("run_id") == run_id
            for r in c["requests"]
            if r["label"] == label
            and timestamp(r["started_at"]) >= start
            and timestamp(r["started_at"]) <= end
        ]
        require(bool(rows), "no window samples for " + label)
        rows.sort(key=lambda r: timestamp(r["started_at"]))
        points = [start] + [timestamp(r["started_at"]) for r in rows] + [end]
        require(
            max((b - a).total_seconds() for a, b in zip(points, points[1:])) <= 45,
            "probe coverage gap: " + label,
        )
        for row in rows:
            require(row.get("run_id") == run_id, "window probe belongs to another run")
            check_sample(row)
        if label in LIGHT:
            values = sorted(r["seconds"] for r in rows)
            require(
                values[math.ceil(0.95 * len(values)) - 1] <= 2,
                "light probe p95 exceeds two seconds",
            )
    return {
        "start_utc": start.isoformat(),
        "end_utc": end.isoformat(),
        "cycles": len(cycles),
        "run_id": run_id,
    }


def originals():
    import hashlib

    manifest = settings().manifest.read_bytes()
    ledger = settings().ledger.read_bytes()
    require(
        hashlib.sha256(manifest).hexdigest() == settings().manifest_sha256,
        "original manifest changed",
    )
    require(
        hashlib.sha256(ledger).hexdigest() == settings().ledger_sha256,
        "original source ledger changed",
    )
    expected, prior = json.loads(manifest), json.loads(ledger)
    require(len(expected) == len(prior) == 6, "expected exactly six original CSVs")
    require(len({x["filename"] for x in expected}) == 6, "duplicate expected filename")
    require(
        {x["filename"] for x in expected} == {x["filename"] for x in prior},
        "six-source filenames differ",
    )
    require(
        len({x["document_id"] for x in prior}) == 6
        and {x["document_id"] for x in prior} == set(settings().six_ids),
        "six-source identities differ",
    )
    return expected, prior
