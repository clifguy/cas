#!/usr/bin/env python3
"""Re-project and re-chunk every document in a vault to apply the
empty-content-heading chunker fix without re-running abstraction.

Background. Earlier today the SAGE chunker silently dropped any heading
whose immediate next paragraph was another heading (parent with empty
body content). The heading text vanished from every searchable surface
so agents could not find the section the way Word's Find can. The fix
(commit included) emits one chunk per heading regardless of body
content. This script propagates the fix to existing data: it re-runs
Stage 1 (projection) and Stage 2 (chunking + embedding + indexing) but
deliberately skips Stage 3 (abstraction) so existing Qwen3-generated
``semantic_abstract`` values stay intact.

Idempotent: documents whose ``adapter_version`` equals the current
adapter ``VERSION`` are skipped. After this script applies the new
chunking, ``adapter_version`` is bumped on each document.

Usage::

    # Dry-run: enumerate the work
    .venv/bin/python -m scripts.rechunk_with_empty_heading_chunks VAULT_ID

    # Apply
    .venv/bin/python -m scripts.rechunk_with_empty_heading_chunks \\
        VAULT_ID --execute

    # Custom embedder batch size (default 64)
    .venv/bin/python -m scripts.rechunk_with_empty_heading_chunks \\
        VAULT_ID --execute --batch-size 32
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sage.adapters.stubs import StubAbstractionProvider
from sage.config import load_vault_config
from sage.mcp_init import initialize_services
from sage.models.enums import SourceType
from sage.source_adapters.docx_adapter import DocxAdapter
from sage.source_adapters.markdown_adapter import MarkdownAdapter
from sage.source_adapters.pdf_adapter import PdfAdapter
from sage.source_adapters.xlsx_adapter import XlsxAdapter
from sage.vault_management import _config_path_for_vault


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


async def rechunk_vault(
    vault_id: str, *, execute: bool, batch_size: int
) -> int:
    """Re-project + re-chunk + re-embed every eligible doc. Returns 0 on success.

    Constructs services with a stub abstraction provider so the script
    does not double-load Qwen3 alongside any running MCP server. The
    script never invokes abstraction; the stub is just a placeholder
    initialize_services requires.
    """
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
        graph = services.graph_store
        store = services.content_store
        ingestion = services.ingestion_service
        embedder = ingestion._embedding
        adapters = ingestion._adapters
        storage_root = Path(config.vault.storage_root).expanduser().resolve()

        documents = await graph.list_all_documents()

        plan: list[tuple[object, str]] = []  # (doc, target_version)
        skipped_current = 0
        skipped_no_adapter = 0
        skipped_missing_source = 0

        for doc in documents:
            target = SOURCE_TYPE_TO_VERSION.get(doc.source_type)
            if target is None:
                skipped_no_adapter += 1
                continue
            source_path = storage_root / doc.source_path
            if not source_path.exists():
                skipped_missing_source += 1
                continue
            # Skip only if both the version is current AND chunks are
            # actually present in the content store. This guards against
            # the case where adapter_version was bumped before chunks
            # finished being written (e.g. a prior run was interrupted by
            # disk-full, or chunk store was deleted out-of-band) — in those
            # cases reprocessing is necessary even though the version says
            # otherwise.
            if doc.adapter_version == target:
                existing = await store.get_all_chunks(doc.id)
                if existing:
                    skipped_current += 1
                    continue
            plan.append((doc, target))

        print(f"Vault: {vault_id}")
        print(f"Total documents: {len(documents)}")
        print(f"  skipped (already at current version): {skipped_current}")
        print(f"  skipped (no adapter version registered): {skipped_no_adapter}")
        print(f"  skipped (source file missing): {skipped_missing_source}")
        print(f"  to re-chunk: {len(plan)}")
        for doc, target in plan[:10]:
            print(
                f"  {doc.id:36s}  {doc.source_type:8s}  "
                f"{_truncate(doc.title, 40):40s}  "
                f"{doc.adapter_version}→{target}"
            )
        if len(plan) > 10:
            print(f"  ... and {len(plan) - 10} more")

        if not plan:
            print("Nothing to do.")
            return 0

        if not execute:
            print("\n(dry-run; pass --execute to apply)")
            return 0

        print("\nApplying...")
        started = datetime.now(timezone.utc)
        n_done = 0
        n_failed = 0
        for i, (doc, target) in enumerate(plan, 1):
            try:
                source_path = storage_root / doc.source_path
                adapter = adapters.get(SourceType(doc.source_type))
                if adapter is None:
                    print(
                        f"[{i:4d}/{len(plan)}]  {doc.id}  no adapter; skipped"
                    )
                    n_failed += 1
                    continue

                # Stage 1: re-project the source. Use the merged vault adapter
                # config so heading_style_map (USPTO Section etc.) applies.
                merged = ingestion._merge_adapter_config(
                    SourceType(doc.source_type), None
                )
                projection = await adapter.project(source_path, merged)

                # Build chunks using the new chunking logic (one chunk per
                # heading, including empty-content headings). Apply the BH-058
                # search preamble, mirroring the production pipeline.
                preamble = ingestion._build_search_preamble(doc)
                chunks = ingestion._chunk_projection(doc.id, projection, preamble)

                # Stamp doc_type on every chunk (mirrors ingestion pipeline).
                if doc.doc_type and chunks:
                    for c in chunks:
                        c.doc_type = doc.doc_type

                # Stage 2: embed the chunks. Combined heading_path + content
                # text is built inside the production pipeline; mirror it here.
                if chunks:
                    for start in range(0, len(chunks), batch_size):
                        batch = chunks[start : start + batch_size]
                        texts = [
                            f"{c.heading_path}\n\n{c.content}"
                            if c.heading_path
                            else c.content
                            for c in batch
                        ]
                        embeddings = await embedder.embed(texts)
                        for c, emb in zip(batch, embeddings):
                            c.embedding = emb

                # Replace existing chunks for this document.
                await store.index_chunks(doc.id, chunks)

                # Bump adapter_version on the document record so subsequent
                # runs skip it.
                await graph.update_document(doc.id, {"adapter_version": target})

                n_done += 1
                print(
                    f"[{i:4d}/{len(plan)}]  {doc.id}  "
                    f"{_truncate(doc.title, 40):40s}  "
                    f"chunks={len(chunks):5d}  ✓"
                )
            except Exception as exc:
                n_failed += 1
                print(
                    f"[{i:4d}/{len(plan)}]  {doc.id}  FAILED: {exc!r}",
                    file=sys.stderr,
                )

        elapsed = datetime.now(timezone.utc) - started
        print(
            f"\nDone. {n_done} re-chunked, {n_failed} failed, in "
            f"{elapsed.total_seconds():.1f}s."
        )

        # Compact LanceDB fragments and prune old version metadata.
        # Each index_chunks call writes a new fragment AND triggers
        # _rebuild_fts which writes new tantivy index versions. Without
        # pruning, vault-wide rewrites accumulate stale FTS index versions
        # — we measured 121 GB of _indices/ for an in-flight rewrite where
        # the actual chunk data was 591 MB. ``cleanup_older_than=timedelta(0)``
        # removes every version except the latest, reclaiming that space.
        # Safe for one-shot maintenance scripts where time-travel is not
        # needed.
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

        return 0 if n_failed == 0 else 1
    finally:
        await services.graph_store.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("vault_id")
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--batch-size", type=int, default=64)
    args = parser.parse_args()
    rc = asyncio.run(
        rechunk_vault(
            args.vault_id, execute=args.execute, batch_size=args.batch_size
        )
    )
    raise SystemExit(rc)


if __name__ == "__main__":
    main()
