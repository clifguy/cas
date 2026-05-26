#!/usr/bin/env python3
"""Backfill the synthetic document-header chunk for every document in a vault (F9).

Rewrites the chunks of every document so that:
  - Body chunk[0]'s ``content`` no longer carries the inlined identity
    preamble produced by the prior ``_build_search_preamble`` (title +
    source filename stem + tags). The OLD preamble is reconstructed
    deterministically from current document metadata and stripped via
    prefix-match. Documents already migrated (chunk[0] does not start
    with the OLD preamble) skip the strip step.
  - A standalone synthetic header chunk lives at
    ``heading_path = SYNTHETIC_HEADER_HEADING_PATH``, carrying title +
    source filename stem + tags + semantic_abstract + a case-split
    identifier-token line. This chunk is what makes BM25 match natural-
    language queries against CamelCase compound identifiers.

Idempotent: re-running on an already-migrated vault writes the same
synthetic header (any drift in title/abstract gets picked up).

Usage::

    # Dry run
    .venv/bin/python -m scripts.rebuild_synthetic_headers VAULT_ID

    # Apply
    .venv/bin/python -m scripts.rebuild_synthetic_headers VAULT_ID --execute

    # Custom embed batch size
    .venv/bin/python -m scripts.rebuild_synthetic_headers VAULT_ID --execute --batch-size 32

Safe to run while the SAGE MCP server is running. Per-document chunk
replacement is atomic (``index_chunks`` does delete-then-insert).
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from dataclasses import replace
from datetime import datetime, timedelta, timezone

from sage.adapters.interfaces import SYNTHETIC_HEADER_HEADING_PATH, Chunk
from sage.adapters.stubs import StubAbstractionProvider
from sage.config import load_vault_config
from sage.mcp_init import initialize_services
from sage.services.ingestion import IngestionService
from sage.vault_management import config_path_for_vault


def _truncate(s: str | None, n: int) -> str:
    if not s:
        return ""
    return s if len(s) <= n else s[: n - 1] + "…"


def _build_old_preamble(doc) -> str:
    """Reconstruct the pre-``_build_search_preamble`` output from
    current document metadata. Used to strip the inlined preamble from
    chunk[0] during backfill.

    Mirrors the logic that previously lived in
    ``IngestionService._build_search_preamble``: title, source filename
    stem (extension stripped), tags. Joined with ``\\n`` and terminated
    by ``\\n\\n``.
    """
    parts: list[str] = []
    if doc.title:
        parts.append(f"Title: {doc.title}")
    if doc.source_path:
        filename = doc.source_path.rsplit("/", 1)[-1]
        stem = filename.rsplit(".", 1)[0] if "." in filename else filename
        parts.append(f"Source: {stem}")
    if doc.tags:
        parts.append(f"Tags: {', '.join(doc.tags)}")
    if not parts:
        return ""
    return "\n".join(parts) + "\n\n"


async def rebuild_with_services(
    *,
    graph,
    store,
    embedder,
    execute: bool,
    batch_size: int,
    label: str = "vault",
) -> int:
    """Plan and (optionally) apply the synthetic-header backfill for a vault."""
    documents = await graph.list_all_documents()

    plan: list[tuple[object, int, bool]] = []  # (doc, body_chunk_count, strip_old_preamble)
    skipped_no_chunks = 0

    for doc in documents:
        chunks = await store.get_all_chunks(doc.id)
        if not chunks:
            skipped_no_chunks += 1
            continue

        # Look for body chunk that would carry the inlined preamble (chunk_index 0).
        # Existing synthetic-header chunks (already-migrated docs) carry
        # heading_path == SYNTHETIC_HEADER_HEADING_PATH and chunk_index == -1.
        body_chunks = [c for c in chunks if c.heading_path != SYNTHETIC_HEADER_HEADING_PATH]
        if not body_chunks:
            skipped_no_chunks += 1
            continue
        first_body = min(body_chunks, key=lambda c: c.chunk_index)
        old_preamble = _build_old_preamble(doc)
        strip = bool(old_preamble) and first_body.content.startswith(old_preamble)
        plan.append((doc, len(body_chunks), strip))

    total = len(plan)
    print(f"Vault: {label}")
    print(f"Total documents: {len(documents)}")
    print(f"  skipped (no chunks): {skipped_no_chunks}")
    print(f"  to rewrite: {total} document(s)")
    print(f"  of which strip OLD inlined preamble: {sum(1 for _, _, strip in plan if strip)}")

    if total == 0:
        print("Nothing to do.")
        return 0

    for doc, n, strip in plan[:10]:
        flag = "strip" if strip else "header-only"
        print(
            f"  {doc.id:36s}  {doc.source_type:8s}  "
            f"{_truncate(doc.title, 40):40s}  "
            f"body_chunks={n:4d}  {flag}"
        )
    if total > 10:
        print(f"  ... and {total - 10} more")

    if not execute:
        print("\n(dry-run; pass --execute to apply)")
        return 0

    print("\nApplying...")
    started = datetime.now(timezone.utc)
    n_done = 0
    for i, (doc, _expected_body, strip) in enumerate(plan, 1):
        chunks = await store.get_all_chunks(doc.id)
        body_chunks_existing = [
            c for c in chunks if c.heading_path != SYNTHETIC_HEADER_HEADING_PATH
        ]
        if not body_chunks_existing:
            print(
                f"[{i:4d}/{total}]  {doc.id}  "
                f"{_truncate(doc.title, 40):40s}  "
                f"(no body chunks; skipped)"
            )
            continue
        body_chunks_existing.sort(key=lambda c: c.chunk_index)

        # Build a fresh body-chunk list. Strip the OLD inlined preamble
        # from chunk[0] if it's present; this is deterministic because
        # `_build_old_preamble` reconstructs the exact prefix.
        rebuilt: list[Chunk] = []
        re_embed_indices: list[int] = []
        first = body_chunks_existing[0]
        old_preamble = _build_old_preamble(doc) if strip else ""
        if strip and old_preamble and first.content.startswith(old_preamble):
            new_first_content = first.content[len(old_preamble) :]
            rebuilt.append(replace(first, content=new_first_content))
            re_embed_indices.append(0)
        else:
            rebuilt.append(first)
        rebuilt.extend(body_chunks_existing[1:])

        # Re-embed chunks that changed.
        if re_embed_indices:
            texts = [
                f"{rebuilt[idx].heading_path}\n\n{rebuilt[idx].content}"
                if rebuilt[idx].heading_path
                else rebuilt[idx].content
                for idx in re_embed_indices
            ]
            new_embeddings = await embedder.embed(texts)
            for idx, emb in zip(re_embed_indices, new_embeddings):
                rebuilt[idx] = replace(rebuilt[idx], embedding=emb)

        # Build the fresh synthetic header chunk.
        header = Chunk(
            document_id=doc.id,
            heading_path=SYNTHETIC_HEADER_HEADING_PATH,
            content=IngestionService._build_header_chunk_content(doc),
            chunk_index=-1,
            doc_type=doc.doc_type,
        )
        header_embed_text = (
            f"{header.heading_path}\n\n{header.content}" if header.heading_path else header.content
        )
        [header_embedding] = await embedder.embed([header_embed_text])
        header.embedding = header_embedding

        chunks_final = [header, *rebuilt]
        await store.index_chunks(doc.id, chunks_final)

        n_done += 1
        status_flag = "stripped+header" if strip else "header"
        print(
            f"[{i:4d}/{total}]  {doc.id}  "
            f"{_truncate(doc.title, 40):40s}  "
            f"chunks={len(chunks_final):5d}  {status_flag}  ✓"
        )

    elapsed = datetime.now(timezone.utc) - started
    print(f"\nDone. {n_done} document(s) rewritten in {elapsed.total_seconds():.1f}s.")

    # Compact LanceDB fragments and prune retained FTS version metadata.
    # The 1242-doc rewrite leaves ~285 GB of stale FTS index versions in
    # _indices/; an explicit optimize with cleanup_older_than=0 drops
    # everything except the current version. Use a freshly-opened table
    # handle so the optimize call is not blocked by version pinning from
    # the long-running mutation loop above.
    try:
        import lancedb

        brain_root = store._brain_root  # set in LanceDBContentStore.__init__
        print("\nCompacting LanceDB fragments and pruning old versions...")
        opt_started = datetime.now(timezone.utc)
        fresh_db = lancedb.connect(str(brain_root / "lancedb"))
        fresh_table = fresh_db.open_table("chunks")
        fresh_table.optimize(cleanup_older_than=timedelta(0))
        opt_elapsed = datetime.now(timezone.utc) - opt_started
        print(f"Compaction done in {opt_elapsed.total_seconds():.1f}s.")
    except Exception as exc:
        print(f"Compaction step failed (non-fatal): {exc!r}", file=sys.stderr)

    _ = batch_size  # reserved for future batched embed; current path embeds tiny lists
    return 0


async def rebuild_vault(vault_id: str, *, execute: bool, batch_size: int) -> int:
    """Plan and (optionally) apply the backfill for a vault. Production entry."""
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
        return await rebuild_with_services(
            graph=services.graph_store,
            store=services.content_store,
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
            "Backfill the synthetic document-header chunk for every "
            "document in a vault (T-0038, F9). See script docstring."
        )
    )
    parser.add_argument("vault_id", help="Vault id (e.g. example_vault)")
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Apply the rewrite. Without this flag, runs in dry-run mode.",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=64,
        help="Embedder batch size (default: 64).",
    )
    args = parser.parse_args()

    rc = asyncio.run(rebuild_vault(args.vault_id, execute=args.execute, batch_size=args.batch_size))
    raise SystemExit(rc)


if __name__ == "__main__":
    main()
