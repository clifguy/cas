"""Stream baseline microbenchmark records into the private trial evidence store."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path


def main():
    from performance_runtime import Evidence

    if datetime.now(UTC) >= datetime.fromisoformat(os.environ["PERF_EXPIRES_AT"]):
        raise TimeoutError("benchmark/probe expiry reached before startup")
    probe = os.environ.get("PERF_PROBE_NONCE")
    if probe:
        import re

        if not re.fullmatch(r"[a-f0-9]{32}", probe):
            raise ValueError("invalid evidence probe nonce")
        store = Evidence(os.environ["PERF_EXPORT_URL"], os.environ["PERF_RUN_ID"])
        value = {"kind": "evidence_probe", "run_id": os.environ["PERF_RUN_ID"], "nonce": probe}
        response = store.request(
            "PUT",
            "probe-" + probe + ".json",
            content=json.dumps(value).encode(),
            headers={"x-ms-blob-type": "BlockBlob", "If-None-Match": "*"},
        )
        response.raise_for_status()
        return
    started = time.monotonic()
    per_run = int(os.environ["PERF_PER_RUN_SECONDS"])
    if not 1 <= per_run <= 900:
        raise ValueError("invalid approved per-run limit")
    case = json.loads(os.environ["PERF_BENCH_CASE"])
    if (
        case["shape"] not in ("short", "long", "mixed")
        or case["batch"] not in (1, 2, 4, 8)
        or case["threads"] not in (1, 2, 4)
        or case["mode"] not in ("direct", "fair")
        or (case["mode"] == "fair" and case["batch"] != 1)
    ):
        raise ValueError("invalid bounded benchmark case")
    deadline = started + min(
        per_run,
        (datetime.fromisoformat(os.environ["PERF_EXPIRES_AT"]) - datetime.now(UTC)).total_seconds(),
    )
    if time.monotonic() >= deadline:
        raise TimeoutError("microbenchmark preparation deadline exceeded")
    store = Evidence(os.environ["PERF_EXPORT_URL"], os.environ["PERF_RUN_ID"])
    with tempfile.TemporaryDirectory() as tmp:
        output = Path(tmp) / "measurements.jsonl"
        command = [
            sys.executable,
            "-m",
            "sage.utils.embedding_benchmark",
            "--shape",
            case["shape"],
            "--batch-size",
            str(case["batch"]),
            "--threads",
            str(case["threads"]),
            "--interop-threads",
            "1",
            "--warmups",
            "1",
            "--repeats",
            "3",
            "--output",
            str(output),
        ]
        if case["mode"] == "fair":
            command += ["--fair"]
        if case.get("uninstrumented"):
            command += ["--uninstrumented"]
        store.emit({"kind": "benchmark_start", "case": case, "at_epoch": time.time()})
        if time.monotonic() >= deadline:
            raise TimeoutError("microbenchmark preparation deadline exceeded")
        child = subprocess.Popen(  # noqa: S603 - fixed executable, bounded argv
            command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
        offset = 0
        try:
            while True:
                if output.exists():
                    with output.open() as handle:
                        handle.seek(offset)
                        while True:
                            line = handle.readline()
                            if not line or not line.endswith("\n"):
                                break
                            row = json.loads(line)
                            offset = handle.tell()
                            store.emit({"kind": "benchmark_record", "case": case, "record": row})
                status = child.poll()
                if status is not None:
                    # A final drain follows the child exit to retain its summary.
                    if output.exists():
                        with output.open() as handle:
                            handle.seek(offset)
                            for line in handle:
                                if not line.endswith("\n"):
                                    raise ValueError("partial final benchmark record")
                                store.emit(
                                    {
                                        "kind": "benchmark_record",
                                        "case": case,
                                        "record": json.loads(line),
                                    }
                                )
                    if status:
                        raise RuntimeError("baseline benchmark failed; partial records retained")
                    store.emit(
                        {"kind": "benchmark_complete", "case": case, "at_epoch": time.time()}
                    )
                    return
                if time.monotonic() >= deadline:
                    raise TimeoutError("microbenchmark deadline exceeded")
                time.sleep(1)
        finally:
            if child.poll() is None:
                child.terminate()
                try:
                    child.wait(timeout=30)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait()


if __name__ == "__main__":
    main()
