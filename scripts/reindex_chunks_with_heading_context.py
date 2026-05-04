#!/usr/bin/env python3
"""Re-index existing chunks with heading-context embedding.

Recomputes embeddings for every chunk in a vault so the embedder receives
``heading_path + content`` instead of ``content`` alone, and rebuilds the
LanceDB FTS indexes (which now cover both ``content`` and ``heading_path``
columns). Result: BM25 and semantic search both find chunks via heading-
text-only queries — the agent equivalent of Word's Find on a heading.

Does NOT re-run projection or abstraction. Only the chunk store is touched:
``heading_path`` and ``content`` fields stay the same; only the embedding
vectors change. Each document's ``adapter_version`` is bumped to the
current adapter ``VERSION`` so subsequent runs skip it (idempotent).

Skips documents that:
  - are already at the current adapter ``VERSION`` for their source_type
  - have a source_type with no registered adapter VERSION (no-op)
  - have no chunks in the content store

Usage::

    # Dry run: show the plan without modifying anything
    .venv/bin/python -m scripts.reindex_chunks_with_heading_context VAULT_ID

    # Apply
    .venv/bin/python -m scripts.reindex_chunks_with_heading_context \\
        VAULT_ID --execute

    # Custom batch size for embedding (default 64)
    .venv/bin/python -m scripts.reindex_chunks_with_heading_context \\
        VAULT_ID --execute --batch-size 32

The script is safe to run while the SAGE MCP server is running. Each
document's chunks are replaced atomically via ``index_chunks``'s existing
delete-then-insert path, and the FTS index is rebuilt incrementally.
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import datetime, timedelta, timezone

from sage.config import load_vault_config
from sage.mcp_init import initialize_services
from sage.models.enums import SourceType
from sage.source_adapters.docx_adapter import DocxAdapter
from sage.source_adapters.markdown_adapter import MarkdownAdapter
from sage.source_adapters.pdf_adapter import PdfAdapter
from sage.source_adapters.xlsx_adapter import XlsxAdapter
from sage.vault_management import _config_path_for_vault


# Source-type to current adapter VERSION. Documents with adapter_version
# below this for their source_type are candidates for re-indexing.
SOURCE_TYPE_TO_VERSION: dict[str, str] = {
    SourceType.DOCX.value: DocxAdapter.VERSION,
    SourceType.MARKDOWN.value: MarkdownAdapter.VERSION,
    SourceType.XLSX.value: XlsxAdapter.VERSION,
    SourceType.PDF.value: PdfAdapter.VERSION,
}


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
) -> int:
    """Re-index every chunk in the given vault services.

    Factored out from ``reindex_vault`` so tests can supply stub services
    (graph store, content store, embedding provider) without exercising
    the full production initialization path. Returns 0 on success.
    """
    documents = await graph.list_all_documents()

    plan: list[tuple[object, int, str]] = []  # (doc, chunk_count, target_version)
    skipped_current = 0
    skipped_no_adapter = 0
    skipped_no_chunks = 0

    for doc in documents:
        target_version = SOURCE_TYPE_TO_VERSION.get(doc.source_type)
        if target_version is None:
            skipped_no_adapter += 1
            continue
        if doc.adapter_version == target_version:
            skipped_current += 1
            continue
        chunks = await store.get_all_chunks(doc.id)
        if not chunks:
            skipped_no_chunks += 1
            continue
        plan.append((doc, len(chunks), target_version))

    total_chunks = sum(n for _, n, _ in plan)
    print(f"Vault: {label}")
    print(f"Total documents: {len(documents)}")
    print(f"  skipped (already at current version): {skipped_current}")
    print(f"  skipped (no adapter version registered): {skipped_no_adapter}")
    print(f"  skipped (no chunks): {skipped_no_chunks}")
    print(f"  to re-index: {len(plan)} document(s), {total_chunks} chunk(s)")

    if not plan:
        print("Nothing to do.")
        return 0

    for doc, n, target in plan[:10]:
        print(
            f"  {doc.id:36s}  {doc.source_type:8s}  "
            f"{_truncate(doc.title, 40):40s}  "
            f"chunks={n:5d}  {doc.adapter_version}→{target}"
        )
    if len(plan) > 10:
        print(f"  ... and {len(plan) - 10} more")

    if not execute:
        print("\n(dry-run; pass --execute to apply)")
        return 0

    print("\nApplying...")
    started = datetime.now(timezone.utc)
    n_done = 0
    for i, (doc, _expected_chunks, target_version) in enumerate(plan, 1):
        chunks = await store.get_all_chunks(doc.id)
        if not chunks:
            # Could happen if chunks were removed between plan and apply.
            await graph.update_document(
                doc.id, {"adapter_version": target_version}
            )
            print(
                f"[{i:4d}/{len(plan)}]  {doc.id}  "
                f"{_truncate(doc.title, 40):40s}  "
                f"(no chunks; version bumped)"
            )
            n_done += 1
            continue

        for start in range(0, len(chunks), batch_size):
            batch = chunks[start : start + batch_size]
            texts = [
                f"{c.heading_path}\n\n{c.content}"
                if c.heading_path
                else c.content
                for c in batch
            ]
            new_embeddings = await embedder.embed(texts)
            for c, emb in zip(batch, new_embeddings):
                c.embedding = emb

        await store.index_chunks(doc.id, chunks)
        await graph.update_document(
            doc.id, {"adapter_version": target_version}
        )
        n_done += 1
        print(
            f"[{i:4d}/{len(plan)}]  {doc.id}  "
            f"{_truncate(doc.title, 40):40s}  "
            f"chunks={len(chunks):5d}  ✓"
        )

    elapsed = datetime.now(timezone.utc) - started
    print(
        f"\nDone. {n_done} document(s) re-indexed in "
        f"{elapsed.total_seconds():.1f}s."
    )

    # Compact LanceDB fragments and prune old version metadata.
    # ``cleanup_older_than=timedelta(0)`` removes every version except
    # the latest. Without this, the FTS-index version churn from
    # _rebuild_fts (called per index_chunks) accumulates dramatically —
    # we observed 121 GB of _indices/ retained as version history when
    # the actual chunk data was 591 MB.
    try:
        table = store._get_table()
        if table is not None:
            print("\nCompacting LanceDB fragments and pruning old versions...")
            opt_started = datetime.now(timezone.utc)
            table.optimize(cleanup_older_than=timedelta(0))
            opt_elapsed = datetime.now(timezone.utc) - opt_started
            print(f"Compaction done in {opt_elapsed.total_seconds():.1f}s.")
    except Exception as exc:
        print(f"Compaction step failed (non-fatal): {exc!r}", file=sys.stderr)

    return 0


async def reindex_vault(
    vault_id: str, *, execute: bool, batch_size: int
) -> int:
    """Plan and (optionally) apply re-indexing for a vault. Production entry.

    Constructs services with a stub abstraction provider so this script
    does not double-load the Qwen3 model alongside any running MCP server
    (RAM budget per CLAUDE.md: ~38 GB with one Qwen3 instance loaded).
    Abstraction is not invoked anywhere in the re-index flow.
    """
    from sage.adapters.stubs import StubAbstractionProvider

    config_path = _config_path_for_vault(vault_id)
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
    parser.add_argument("vault_id", help="Vault id (e.g. pim_health)")
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Apply the re-index. Without this flag, runs in dry-run mode.",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=64,
        help="Embedder batch size (default: 64).",
    )
    args = parser.parse_args()

    rc = asyncio.run(
        reindex_vault(
            args.vault_id, execute=args.execute, batch_size=args.batch_size
        )
    )
    raise SystemExit(rc)


if __name__ == "__main__":
    main()
