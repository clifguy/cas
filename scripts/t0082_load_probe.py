#!/usr/bin/env python3
"""Synthetic-load probe for the semantic-mode tier3 pushdown A/B.

extends the ``json_extract`` pushdown of ``tier3_metadata``
filters from catalog mode into the shared ``_content_filters()`` resolver
that gates semantic and keyword retrieval. This probe captures latency
numbers for semantic-mode tier3 lookups so the AC's required A/B
comparison can be made against the same query shapes pre/post change.

Sibling to ``scripts/t0074_load_probe.py``. The t0074 probe deliberately
omits semantic mode (it skips the embedding provider entirely); this
probe is the semantic-mode counterpart and uses ``StubEmbeddingProvider``
so the cas vault's nomic-embed-text model never loads. The probe only
measures retrieval-service overhead (filter resolution + chunk pre-filter
+ result assembly), not embedding-model latency.

Procedure:

1. Pre-change baseline: temporarily revert the tier3 line in
   ``sage/services/retrieval.py::_content_filters`` so semantic-mode
   tier3 falls through to the legacy ``list_all_documents()`` post-filter
   path (or to the equivalent un-pushed query_documents call). Run::

       .venv/bin/python -m scripts.t0082_load_probe --label before

2. Post-change measurement: with the production pushdown restored, run::

       .venv/bin/python -m scripts.t0082_load_probe --label after

Both runs append to timing.log; ``--label`` distinguishes them.

The probe touches only the semantic discover surface plus a small set
of tier3 filter shapes drawn from the cas vault's seeded ticket
portfolio. ``StubAbstractionProvider`` is used so Qwen3/MLX never
loads. Postgres supports concurrent readers, so the running MCP server's
open connection does not block the probe.
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
from sage.models.schemas import (
    DiscoverRequest,
    RetrievalFilters,
)
from sage.vault_management import config_path_for_vault

# Tier3 shapes drawn from the cas vault's seeded ticket portfolio.
# Mirrors t0074_load_probe.TIER3_PROBES but framed for semantic-mode
# discover (so a query string is required).
TIER3_PROBES = (
    {"ticket_priority": "high"},
    {"ticket_type": "feature"},
    {"ticket_id": "T-0072"},
)

# Probe queries -- short generic strings that BM25 finds across many
# tickets so the candidate pool is large enough for the tier3 filter
# to do non-trivial work. Semantic search with the stub embedding
# provider returns zero-cosine matches but still walks the chunk
# table, which is exactly the latency we are measuring.
PROBE_QUERIES = (
    "filter pushdown",
    "tier3 metadata",
    "retrieval service",
)


def _percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    idx = min(len(s) - 1, int(round(pct * (len(s) - 1))))
    return s[idx]


def _stats(label: str, durations_ms: list[float]) -> str:
    if not durations_ms:
        return f"  {label}: (no samples)"
    return (
        f"  {label}: n={len(durations_ms)} "
        f"median={statistics.median(durations_ms):.3f}ms "
        f"p95={_percentile(durations_ms, 0.95):.3f}ms "
        f"min={min(durations_ms):.3f}ms max={max(durations_ms):.3f}ms"
    )


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

    print(f"=== T-0082 probe label={label} reps={reps} ===")
    try:
        # --- semantic discover with no filter (calibration baseline) ---
        for query in PROBE_QUERIES:
            durations: list[float] = []
            for _ in range(reps):
                req = DiscoverRequest(
                    mode=RetrievalMode.SEMANTIC,
                    query=query,
                    limit=20,
                )
                t0 = time.perf_counter()
                await services.retrieval_service.discover(req)
                durations.append((time.perf_counter() - t0) * 1000.0)
            print(_stats(f"semantic no-filter query='{query}'", durations))

        # --- semantic discover with tier3 filter (the target shape) ---
        for tier3 in TIER3_PROBES:
            for query in PROBE_QUERIES:
                durations = []
                for _ in range(reps):
                    req = DiscoverRequest(
                        mode=RetrievalMode.SEMANTIC,
                        query=query,
                        filters=RetrievalFilters(doc_type="ticket", tier3=tier3),
                        limit=20,
                    )
                    t0 = time.perf_counter()
                    await services.retrieval_service.discover(req)
                    durations.append((time.perf_counter() - t0) * 1000.0)
                print(
                    _stats(
                        f"semantic ticket tier3={tier3} query='{query}'",
                        durations,
                    )
                )

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
    parser.add_argument(
        "--label",
        required=True,
        help=(
            "Free-text label printed in the run header. Conventional "
            "values: 'before' for the pre-pushdown baseline, 'after' "
            "for the post-pushdown measurement."
        ),
    )
    parser.add_argument(
        "--reps",
        type=int,
        default=10,
        help="Repetitions per query shape. Default 10.",
    )
    args = parser.parse_args()

    if "pytest" in sys.modules:
        print(
            "scripts.t0082_load_probe is a one-shot probe, not a test. "
            "Refusing to run inside pytest.",
            file=sys.stderr,
        )
        raise SystemExit(2)

    asyncio.run(_run_probes(args.label, args.reps))


if __name__ == "__main__":
    main()


def cas_brain_root() -> Path:
    return Path("~/sage_vaults/cas/brain").expanduser()
