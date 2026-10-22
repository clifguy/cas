"""Repeatable model-call microbenchmarks, separate from service scheduling."""

from __future__ import annotations

import asyncio
import hashlib
import math
import os
import platform
import resource
import statistics
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from sage.instrumentation.performance import scope


def resources() -> dict[str, Any]:
    """Process peak is lifetime-cumulative; use a fresh process per matrix arm."""
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    row: dict[str, Any] = {
        "process_peak_bytes": peak * (1 if platform.system() == "Darwin" else 1024),
        "process_cpu_seconds": time.process_time(),
    }
    for name in (
        "cpu.stat",
        "cpu.max",
        "memory.current",
        "memory.peak",
        "memory.max",
        "memory.events",
    ):
        path = Path("/sys/fs/cgroup") / name
        if path.is_file():
            row[name] = path.read_text().strip()
    if hasattr(os, "sched_getaffinity"):
        row["cpu_affinity"] = sorted(os.sched_getaffinity(0))
    return row


def _validate(vectors: list, count: int, reference: list | None) -> None:
    import numpy as np

    values = np.asarray(vectors, dtype=float)
    if values.ndim != 2 or len(values) != count or not np.isfinite(values).all():
        raise ValueError("invalid vector count or values")
    if not np.allclose(np.linalg.norm(values, axis=1), 1.0, atol=1e-4):
        raise ValueError("vectors are not normalized")
    if reference is not None and (
        values.shape != np.asarray(reference).shape
        or not np.allclose(values, reference, atol=1e-4, rtol=1e-3)
    ):
        raise ValueError("vector association differs from singleton reference")


def benchmark_case(
    provider: Any,
    texts: list[str],
    *,
    batch_size: int,
    repeats: int,
    warmups: int,
    fair: bool = False,
    reference: list | None = None,
    emit: Callable[[dict], None] | None = None,
    instrument: bool = True,
) -> list[dict]:
    """Warm up then measure actual singleton dispatch or direct batched calls."""
    if not texts or not 1 <= batch_size <= 8 or not 1 <= repeats <= 10 or not 0 <= warmups <= 3:
        raise ValueError("invalid bounded benchmark parameters")
    if fair and batch_size != 1:
        raise ValueError("fair dispatch baseline uses singleton model calls")
    windows = [texts[i : i + batch_size] for i in range(0, len(texts), batch_size)]
    rows = []
    for iteration in range(warmups + repeats):
        before = resources()
        start = time.perf_counter_ns()
        with scope("benchmark", "embedding", "model_case", enabled=instrument) as trace:
            if fair:
                vectors = asyncio.run(provider.embed(texts))
            else:
                vectors = [v for window in windows for v in provider._embed_sync(window)]
        elapsed = (time.perf_counter_ns() - start) / 1e9
        _validate(vectors, len(texts), reference)
        if iteration < warmups:
            continue
        row = {
            "mode": "fair_dispatch" if fair else "direct_batch",
            "batch_size": batch_size,
            "actual_batch_sizes": [len(w) for w in windows],
            "invocations": len(windows),
            "repetition": iteration - warmups,
            "texts": len(texts),
            "elapsed_seconds": elapsed,
            "texts_per_second": len(texts) / elapsed,
            "instrumented": instrument,
            "counts": dict(trace.counts) if trace else {},
            "stages": dict(trace.stages) if trace else {},
            "ranges": dict(trace.ranges) if trace else {},
            "resources_before": before,
            "resources_after": resources(),
            "outcome": "completed",
        }
        tokens = row["counts"].get("embedding.tokens")
        row["tokens_per_second"] = tokens / elapsed if tokens is not None else None
        rows.append(row)
        if emit is not None:
            emit(row)
    return rows


def summarize(rows: list[dict]) -> dict:
    if not rows or any(r["outcome"] != "completed" for r in rows):
        raise ValueError("incomplete measurements cannot be summarized as success")
    times = [r["elapsed_seconds"] for r in rows]
    return {
        "repetitions": len(rows),
        "median_seconds": statistics.median(times),
        "minimum_seconds": min(times),
        "maximum_seconds": max(times),
        "p95_seconds": sorted(times)[math.ceil(len(times) * 0.95) - 1],
    }


def workload(shape: str, count: int = 8) -> list[str]:
    """Stable synthetic inputs with dense varied words rather than huge unknown tokens."""
    if shape not in ("short", "long", "mixed") or not 1 <= count <= 64:
        raise ValueError("unknown or oversized workload")
    words = (
        "account operation update permission resource request event status timestamp identity"
    ).split()
    texts = []
    for index in range(count):
        length = 24 if shape == "short" or (shape == "mixed" and index % 2 == 0) else 900
        texts.append(
            "search_document: "
            + " ".join(
                f"{words[(i + index) % len(words)]} {index * 1000 + i}" for i in range(length)
            )
        )
    return texts


def workload_digest(texts: list[str]) -> str:
    import json

    return hashlib.sha256(json.dumps(texts, ensure_ascii=False).encode()).hexdigest()


def main() -> int:
    import argparse
    import json
    import logging

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--shape", choices=["short", "long", "mixed"], default="mixed")
    parser.add_argument("--batch-size", type=int, choices=[1, 2, 4, 8], default=1)
    parser.add_argument("--threads", type=int, choices=[1, 2, 4], default=2)
    parser.add_argument("--interop-threads", type=int, choices=[1, 2, 4], default=1)
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--warmups", type=int, default=1)
    parser.add_argument("--fair", action="store_true")
    parser.add_argument("--uninstrumented", action="store_true")
    args = parser.parse_args()
    import torch

    from sage.adapters.embedding_nomic import (
        MODEL_PINS,
        NOMIC_MODEL_NAME,
        NomicEmbeddingProvider,
    )

    torch.set_num_threads(args.threads)
    torch.set_num_interop_threads(args.interop_threads)
    logging.basicConfig(level=logging.WARNING)
    texts = workload(args.shape)
    # Exclusive creation preserves prior/partial evidence instead of overwriting it.
    with args.output.open("x") as output:

        def emit(row: dict) -> None:
            output.write(json.dumps(row) + "\n")
            output.flush()

        identity = {
            "kind": "identity",
            "architecture": platform.machine(),
            "model": NOMIC_MODEL_NAME,
            "model_revision": MODEL_PINS[NOMIC_MODEL_NAME].revision,
            "code_revision": MODEL_PINS[NOMIC_MODEL_NAME].code_revision,
            "image_digest": os.environ.get("BENCHMARK_IMAGE_DIGEST"),
            "revision": os.environ.get("CONTAINER_APP_REVISION"),
            "replica": os.environ.get("CONTAINER_APP_REPLICA_NAME"),
            "evidence_role": "azure_candidate"
            if os.environ.get("CONTAINER_APP_REVISION")
            else "local_support",
            "torch_version": torch.__version__,
            "threads": torch.get_num_threads(),
            "interop_threads": torch.get_num_interop_threads(),
            "shape": args.shape,
            "workload_sha256": workload_digest(texts),
            "resources": resources(),
        }
        emit(identity)
        try:
            start = time.perf_counter()
            provider = NomicEmbeddingProvider()
            emit(
                {
                    "kind": "model_load",
                    "elapsed_seconds": time.perf_counter() - start,
                    "resources": resources(),
                }
            )
            # Independent singleton vectors protect count/order/association for every arm.
            reference = [provider._embed_sync([text])[0] for text in texts]
            rows = benchmark_case(
                provider,
                texts,
                batch_size=args.batch_size,
                repeats=args.repeats,
                warmups=args.warmups,
                fair=args.fair,
                reference=reference,
                emit=emit,
                instrument=not args.uninstrumented,
            )
            if not args.uninstrumented and any(
                not r["counts"].get("embedding.tokens") for r in rows
            ):
                raise RuntimeError("actual model token accounting is missing")
            emit({"kind": "summary", **summarize(rows), "outcome": "completed"})
        except BaseException as exc:
            emit({"kind": "failure", "exception_class": type(exc).__name__, "outcome": "failed"})
            raise
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
