#!/usr/bin/env python3
"""Restore chunk.doc_type column on LanceDB after the heading-context reindex
script wiped it.

Cause (one-time defect): ``get_all_chunks`` in
``sage/adapters/content_store_lancedb.py`` historically did not populate
``Chunk.doc_type`` from the stored row, leaving it ``None``. The
heading-context reindex script (``scripts/reindex_chunks_with_heading_context``)
read chunks via ``get_all_chunks``, re-embedded them, and wrote them back
via ``index_chunks`` — propagating ``doc_type=None`` to every chunk in
the vault. Search queries with ``filters={"doc_type": "..."}`` then
returned zero hits because no chunks had a doc_type value to match.

This script repairs the column by issuing per-document
``table.update(values={"doc_type": ...}, where="document_id = '...'")``
calls. The doc_type column is not FTS-indexed, so updates do not trigger
an FTS rebuild — the operation is fast (no embedding compute, no FTS).

The defect itself is fixed in ``get_all_chunks`` (it now populates
doc_type), so this script is a one-time recovery tool, not an ongoing
maintenance need.

Usage::

    # Dry-run: show how many chunks would be patched per doc_type
    .venv/bin/python -m scripts.restore_chunk_doc_type VAULT_ID

    # Apply
    .venv/bin/python -m scripts.restore_chunk_doc_type VAULT_ID --execute
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone

from sage.adapters.stubs import StubAbstractionProvider
from sage.config import load_vault_config
from sage.mcp_init import initialize_services
from sage.vault_management import config_path_for_vault


async def restore_doc_type(vault_id: str, *, execute: bool) -> int:
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
        graph = services.graph_store
        store = services.content_store
        table = store._get_table()
        if table is None:
            print("LanceDB chunks table not present; nothing to do.")
            return 0

        documents = await graph.list_all_documents()
        # Build per-doc-type counts and per-document doc_type lookup.
        type_counts: Counter[str] = Counter()
        targets: list[tuple[str, str]] = []  # (doc_id, doc_type)

        for doc in documents:
            if not doc.doc_type:
                continue
            # Count chunks for this doc_id (cheap via SQL count).
            try:
                rows = (
                    table.search()
                    .where(f"document_id = '{_escape(doc.id)}'")
                    .select(["doc_type"])
                    .limit(1)
                    .to_list()
                )
            except Exception:
                rows = []
            if not rows:
                continue
            existing = rows[0].get("doc_type")
            if existing == doc.doc_type:
                continue  # already correct, skip
            targets.append((doc.id, doc.doc_type))
            type_counts[doc.doc_type] += 1

        print(f"Vault: {vault_id}")
        print(f"Total documents in graph: {len(documents)}")
        print(f"Documents with chunks needing doc_type restore: {len(targets)}")
        print("By doc_type:")
        for dtype, n in sorted(type_counts.items(), key=lambda kv: -kv[1]):
            print(f"  {dtype:30s}  {n}")

        if not targets:
            print("Nothing to do.")
            return 0

        if not execute:
            print("\n(dry-run; pass --execute to apply)")
            return 0

        print("\nApplying...")
        started = datetime.now(timezone.utc)
        for i, (doc_id, doc_type) in enumerate(targets, 1):
            table.update(
                where=f"document_id = '{_escape(doc_id)}'",
                values={"doc_type": doc_type},
            )
            if i % 50 == 0 or i == len(targets):
                print(f"  [{i:4d}/{len(targets)}]  {doc_id}  {doc_type}")

        elapsed = datetime.now(timezone.utc) - started
        print(
            f"\nDone. {len(targets)} document(s) patched in "
            f"{elapsed.total_seconds():.1f}s."
        )

        # Compact LanceDB fragments and prune old version metadata.
        # ``cleanup_older_than=timedelta(0)`` removes every version
        # except the latest. table.update writes a new fragment per call;
        # without pruning, version metadata accumulates dramatically.
        try:
            print("\nCompacting LanceDB fragments and pruning old versions...")
            opt_started = datetime.now(timezone.utc)
            table.optimize(cleanup_older_than=timedelta(0))
            opt_elapsed = datetime.now(timezone.utc) - opt_started
            print(f"Compaction done in {opt_elapsed.total_seconds():.1f}s.")
        except Exception as exc:
            print(f"Compaction step failed (non-fatal): {exc!r}", file=sys.stderr)

        return 0
    finally:
        await services.graph_store.close()


def _escape(value: str) -> str:
    return value.replace("'", "''")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("vault_id")
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()
    rc = asyncio.run(restore_doc_type(args.vault_id, execute=args.execute))
    raise SystemExit(rc)


if __name__ == "__main__":
    main()
