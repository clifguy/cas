#!/usr/bin/env python3
"""Synthetic-load probe for the T-0078 tag-filter A/B baseline.

Mirrors scripts/t0074_load_probe.py but targets the tag-filter path
specifically. Run twice on the same code revision:

1. Pre-rewrite: capture timing against the json_each(tags) query.
2. Post-rewrite: capture timing against the document_tags join table.

Usage::

    .venv/bin/python -m scripts.t0078_load_probe --label before
    .venv/bin/python -m scripts.t0078_load_probe --label after
"""

from __future__ import annotations

import argparse
import asyncio
import statistics
import sys
import time
from pathlib import Path

from sage.adapters.stubs import StubAbstractionProvider, StubEmbeddingProvider
from sage.config import load_vault_config
from sage.instrumentation.timing import TimingConfig
from sage.mcp_init import initialize_services
from sage.models.enums import RetrievalMode
from sage.models.schemas import DiscoverRequest, RetrievalFilters
from sage.vault_management import config_path_for_vault

TAG_PROBES = (
    ["ticket"],
    ["adr"],
    ["sage"],
    ["ticket", "sage"],
    ["phase-2"],
)


def _stats(label: str, durations_ms: list[float]) -> str:
    if not durations_ms:
        return f"  {label}: (no samples)"
    return (
        f"  {label}: n={len(durations_ms)} "
        f"median={statistics.median(durations_ms):.3f}ms "
        f"p95={_percentile(durations_ms, 0.95):.3f}ms "
        f"min={min(durations_ms):.3f}ms max={max(durations_ms):.3f}ms"
    )


def _percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    idx = min(len(s) - 1, int(round(pct * (len(s) - 1))))
    return s[idx]


async def _run_probes(label: str, reps: int) -> None:
    config_path = config_path_for_vault("cas")
    if not config_path.exists():
        print(f"cas vault config not found at {config_path}", file=sys.stderr)
        raise SystemExit(2)

    config = load_vault_config(config_path)
    config.timing = TimingConfig(
        enabled=True,
        emit_threshold_ms=0.0,
        warn_threshold_ms=10_000.0,
        summary_interval_seconds=config.timing.summary_interval_seconds,
        log_path=config.timing.log_path,
    )

    services = await initialize_services(
        config,
        config_path=config_path,
        embedding_provider=StubEmbeddingProvider(),
        abstraction_provider=StubAbstractionProvider(),
    )

    print(f"=== T-0078 probe label={label} reps={reps} ===")
    try:
        for tag_set in TAG_PROBES:
            durations: list[float] = []
            count = 0
            for _ in range(reps):
                req = DiscoverRequest(
                    mode=RetrievalMode.CATALOG,
                    filters=RetrievalFilters(tags=tag_set),
                    limit=100,
                )
                t0 = time.perf_counter()
                result = await services.retrieval_service.discover(req)
                durations.append((time.perf_counter() - t0) * 1000.0)
                count = result.total_available
            print(_stats(f"catalog tags={tag_set} (n_results={count})", durations))
    finally:
        for timer in (
            services.graph_store._query_timer,
            getattr(services.content_store, "_query_timer", None),
            services.retrieval_service._query_timer,
        ):
            if timer is not None and hasattr(timer, "flush"):
                timer.flush()
        if services.timing_thread is not None:
            services.timing_thread.stop()
        await services.graph_store.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--label", required=True)
    parser.add_argument("--reps", type=int, default=20)
    args = parser.parse_args()

    if "pytest" in sys.modules:
        print(
            "t0078_load_probe is a one-shot probe; refusing to run inside pytest.", file=sys.stderr
        )
        raise SystemExit(2)

    asyncio.run(_run_probes(args.label, args.reps))


if __name__ == "__main__":
    main()


def cas_brain_root() -> Path:
    return Path("~/sage_vaults/cas/brain").expanduser()
