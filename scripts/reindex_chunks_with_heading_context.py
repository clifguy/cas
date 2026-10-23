#!/usr/bin/env python3
"""Re-index stored authored passages under bounded deterministic discovery policy.

Does not reproject or abstract. Authored content and stable addresses remain intact;
readable and keyword scope publish before bounded embedding increments. The source
ledger retains admitted work through retries, and completed repeated runs are no-ops.
An explicit --reprocessing-decision records operator authority for expanded scope.
The document adapter version remains the one that shaped its stored passages.

The historical --batch-size argument is retained as a compatibility hint; shared
policy's max_in_flight remains the dispatch ceiling. No source-specific scheduler
or provider tuning is introduced.

Usage::

    .venv/bin/python -m scripts.reindex_chunks_with_heading_context VAULT_ID
    .venv/bin/python -m scripts.reindex_chunks_with_heading_context VAULT_ID --execute
    .venv/bin/python -m scripts.reindex_chunks_with_heading_context VAULT_ID --execute \
        --reprocessing-decision "Owner requested expanded selection"

Only an explicit execute invocation writes. Publications use the current durable
attempt fence, so a concurrent newer generation rejects stale work.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import datetime, timedelta, timezone

from sage.config import load_vault_config
from sage.mcp_init import initialize_services
from sage.services.selective_embedding import reindex_passages
from sage.vault_management import config_path_for_vault


def _truncate(s: str | None, n: int) -> str:
    if not s:
        return ""
    return s if len(s) <= n else s[: n - 1] + "…"


async def reindex_with_services(
    *,
    graph,
    store,
    embedder,
    execute: bool,
    batch_size: int,
    label: str = "vault",
    reprocessing_decision: str | None = None,
) -> int:
    """Re-index every chunk in the given vault services.

    Factored out from ``reindex_vault`` so tests can supply stub services
    (graph store, content store, embedding provider) without exercising
    the full production initialization path. Returns 0 on success.
    """
    store.bind_processing_store(graph)
    documents = await graph.list_all_documents()

    plan: list[tuple[object, int]] = []  # (doc, chunk_count)
    skipped_no_chunks = 0

    for doc in documents:
        chunks = await store.get_all_chunks(doc.id)
        if not chunks:
            skipped_no_chunks += 1
            continue
        plan.append((doc, len(chunks)))

    total_chunks = sum(n for _, n in plan)
    print(f"Vault: {label}")
    print(f"Total documents: {len(documents)}")
    print(f"  skipped (no chunks): {skipped_no_chunks}")
    print(f"  to re-index: {len(plan)} document(s), {total_chunks} chunk(s)")

    if not plan:
        print("Nothing to do.")
        return 0

    for doc, n in plan[:10]:
        print(
            f"  {doc.id:36s}  {doc.source_type:8s}  {_truncate(doc.title, 40):40s}  chunks={n:5d}"
        )
    if len(plan) > 10:
        print(f"  ... and {len(plan) - 10} more")

    if not execute:
        print("\n(dry-run; pass --execute to apply)")
        return 0

    print("\nApplying...")
    started = datetime.now(timezone.utc)
    n_done = 0
    for i, (doc, _expected_chunks) in enumerate(plan, 1):
        chunks = await store.get_all_chunks(doc.id)
        if not chunks:
            # Could happen if chunks were removed between plan and apply.
            print(f"[{i:4d}/{len(plan)}]  {doc.id}  {_truncate(doc.title, 40):40s}  (no chunks)")
            continue

        changed = await reindex_passages(
            store,
            embedder,
            doc,
            chunks,
            decision=reprocessing_decision,
        )

        n_done += int(changed)
        print(
            f"[{i:4d}/{len(plan)}]  {doc.id}  "
            f"{_truncate(doc.title, 40):40s}  "
            f"chunks={len(chunks):5d}  {'updated' if changed else 'unchanged'}"
        )

    elapsed = datetime.now(timezone.utc) - started
    print(f"\nDone. {n_done} document(s) re-indexed in {elapsed.total_seconds():.1f}s.")

    # Reclaim the dead-tuple bloat the bulk re-index above generated: each
    # index_chunks call is a delete-then-insert, and optimize's VACUUM
    # reclaims the superseded rows.
    try:
        print("\nReclaiming content-store bloat...")
        opt_started = datetime.now(timezone.utc)
        await store.optimize(cleanup_older_than=timedelta(0))
        opt_elapsed = datetime.now(timezone.utc) - opt_started
        print(f"Reclaim done in {opt_elapsed.total_seconds():.1f}s.")
    except Exception as exc:
        print(f"Reclaim step failed (non-fatal): {exc!r}", file=sys.stderr)

    return 0


async def reindex_vault(
    vault_id: str,
    *,
    execute: bool,
    batch_size: int,
    reprocessing_decision: str | None = None,
) -> int:
    """Plan and (optionally) apply re-indexing for a vault. Production entry.

    Constructs services with a stub abstraction provider so this script
    does not double-load the Qwen3 model alongside any running MCP server
    (RAM budget per CLAUDE.md: ~38 GB with one Qwen3 instance loaded).
    Abstraction is not invoked anywhere in the re-index flow.
    """
    from sage.adapters.stubs import StubAbstractionProvider

    config_path = config_path_for_vault(vault_id)
    if not config_path.exists():
        print(f"vault config not found: {config_path}", file=sys.stderr)
        return 2

    config = load_vault_config(config_path)
    services = await initialize_services(
        config,
        config_path=config_path,
        abstraction_provider=StubAbstractionProvider(),
    )

    try:
        return await reindex_with_services(
            graph=services.graph_store,
            store=services.content_store,
            # NomicEmbeddingProvider constructed by initialize_services.
            embedder=services.ingestion_service._embedding,
            execute=execute,
            batch_size=batch_size,
            label=vault_id,
            reprocessing_decision=reprocessing_decision,
        )
    finally:
        await services.graph_store.close()


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Re-index existing chunks with heading-context embedding. "
            "See script docstring for full details."
        )
    )
    parser.add_argument("vault_id", help="Vault id (e.g. example_vault)")
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Apply the re-index. Without this flag, runs in dry-run mode.",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=64,
        help="Compatibility hint (default: 64); discovery policy bounds actual dispatch.",
    )
    parser.add_argument(
        "--reprocessing-decision",
        help=(
            "Explicit reason for expanding/resetting the current source allowance; "
            "omitted for retry."
        ),
    )
    args = parser.parse_args()

    rc = asyncio.run(
        reindex_vault(
            args.vault_id,
            execute=args.execute,
            batch_size=args.batch_size,
            reprocessing_decision=args.reprocessing_decision,
        )
    )
    raise SystemExit(rc)


if __name__ == "__main__":
    main()
