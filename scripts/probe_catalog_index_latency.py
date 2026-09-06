#!/usr/bin/env python3
"""Synthetic-load probe for the storage-index A/B baseline.

Runs a fixed set of query shapes against the cas vault and emits
timing records via the instrumentation. The probe is intended
to be run twice on the same code revision:

1. Pre-migration: capture the timing log against the current schema.
2. Post-migration: capture the timing log against the new index set.

Compare the two log windows to demonstrate that index
additions move query plans onto the new indexes without regressing
latency.

The probe deliberately:

- Hits only catalog-mode discover, traverse, and a small set of tier3
  filter shapes — the three query families a storage-index migration
  either adds an index for (doc_type, project, composite edges) or
  must not regress (tier3_metadata, which is left un-indexed).
- Forces emit_threshold_ms=0.0 so every call is logged individually
  rather than coalesced into a periodic summary record.
- Uses StubAbstractionProvider so Qwen3 / MLX never loads.
- Skips the embedding provider entirely (no semantic mode used).
- Runs against the real cas vault's Postgres store. Postgres supports
  concurrent readers, so the running MCP server's open connection
  does not block the probe.

Usage::

    # Pre-migration baseline (run first, before applying a schema change)
    .venv/bin/python -m scripts.probe_catalog_index_latency --label before

    # Post-migration A/B (run after migration applies via fresh init)
    .venv/bin/python -m scripts.probe_catalog_index_latency --label after

Both runs append to the same timing.log; use --label to grep them
apart in post-processing. The script prints a per-shape latency
summary to stdout from its own per-call timing, independent of the
timing.log emissions.
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
from sage.models.enums import EdgeType, RetrievalMode
from sage.models.schemas import (
    DiscoverRequest,
    RetrievalFilters,
    TraverseRequest,
)
from sage.vault_management import bound_vault_root, config_path_for_vault

DOC_TYPES = ("ticket", "adr", "failure_record", "tooling_entry", "steering_document")

# Deterministic start nodes for traversal probes. Picked for density:
# the audit document references eight related documents; the
# steering doc anchors many ticket → steering references.
TRAVERSE_STARTS = (
    (
        "8504a919_t_0072_sage_storage_layer_access_pattern_audit_and_remediation",
        EdgeType.REFERENCES,
        "outbound",
    ),
    (
        "daa245c7_cas_ticket_conventions",
        EdgeType.REFERENCES,
        "inbound",
    ),
    (
        "11cd227b_t_0073_phase_0_add_structured_query_timing_instrumentation_to_sage_storage_and_retrieval_layers",
        EdgeType.SUPERSEDES,
        "outbound",
    ),
    (
        "df7285ad_t_0074_phase_1_add_sqlite_indexes_on_documents_doc_type_documents_project_and_composite_edge_indexes",
        EdgeType.DEPENDS_ON,
        "outbound",
    ),
)

TIER3_PROBES = (
    {"ticket_priority": "high"},
    {"ticket_type": "feature"},
    {"ticket_id": "T-0072"},
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
    # Force every storage / content / retrieval call to emit its own
    # log record (no fast-path suppression) so the A/B comparison sees
    # the full per-call distribution rather than coalesced summaries.
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

    print(f"=== catalog-index latency probe label={label} reps={reps} ===")
    try:
        # --- catalog discover by doc_type ---
        for dt in DOC_TYPES:
            durations: list[float] = []
            for _ in range(reps):
                req = DiscoverRequest(
                    mode=RetrievalMode.CATALOG,
                    filters=RetrievalFilters(doc_type=dt),
                    limit=100,
                )
                t0 = time.perf_counter()
                await services.retrieval_service.discover(req)
                durations.append((time.perf_counter() - t0) * 1000.0)
            print(_stats(f"catalog doc_type={dt}", durations))

        # --- catalog discover by doc_type + lifecycle_status (composite-index target) ---
        for dt in ("ticket", "adr"):
            for status in ("active", "completed"):
                durations = []
                for _ in range(reps):
                    req = DiscoverRequest(
                        mode=RetrievalMode.CATALOG,
                        filters=RetrievalFilters(doc_type=dt, lifecycle_status=status),
                        limit=100,
                    )
                    t0 = time.perf_counter()
                    await services.retrieval_service.discover(req)
                    durations.append((time.perf_counter() - t0) * 1000.0)
                print(_stats(f"catalog doc_type={dt} lifecycle={status}", durations))

        # --- catalog discover with tier3 filters (regression check) ---
        for tier3 in TIER3_PROBES:
            durations = []
            for _ in range(reps):
                req = DiscoverRequest(
                    mode=RetrievalMode.CATALOG,
                    filters=RetrievalFilters(doc_type="ticket", tier3_metadata=tier3),
                    limit=100,
                )
                t0 = time.perf_counter()
                await services.retrieval_service.discover(req)
                durations.append((time.perf_counter() - t0) * 1000.0)
            print(_stats(f"catalog ticket tier3={tier3}", durations))

        # --- traverse with explicit edge_type (composite edge-index target) ---
        for start_id, edge_type, direction in TRAVERSE_STARTS:
            durations = []
            for _ in range(reps):
                req = TraverseRequest(
                    start_id=start_id,
                    edge_type=edge_type,
                    direction=direction,
                    depth=3,
                )
                t0 = time.perf_counter()
                await services.graph_ops_service.traverse(req)
                durations.append((time.perf_counter() - t0) * 1000.0)
            print(
                _stats(
                    f"traverse {edge_type.value} {direction} from {start_id.split('_', 1)[0]}",
                    durations,
                )
            )

    finally:
        # Flush any pending periodic summary so post-mortem grep on
        # timing.log captures everything this probe emitted.
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
            "Free-text label printed in the run header and visible in any "
            "shell wrapper that splits timing.log windows. Conventional "
            "values: 'before' for pre-migration, 'after' for post-migration."
        ),
    )
    parser.add_argument(
        "--reps",
        type=int,
        default=10,
        help="Repetitions per query shape. Default 10.",
    )
    args = parser.parse_args()

    # Sanity guard against accidental invocation from pytest.
    if "pytest" in sys.modules:
        print(
            "scripts.probe_catalog_index_latency is a one-shot probe, not a test. "
            "Refusing to run inside pytest.",
            file=sys.stderr,
        )
        raise SystemExit(2)

    asyncio.run(_run_probes(args.label, args.reps))


if __name__ == "__main__":
    main()


# Convenience for module-level runs.
def cas_brain_root() -> Path:
    return bound_vault_root() / "cas" / "brain"
