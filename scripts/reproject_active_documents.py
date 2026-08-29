#!/usr/bin/env python3
"""Re-project active documents so chunk content carries the ATX
heading line.

A chunking fix changed the persisted shape of ``Chunk.content``: each
body chunk now holds its own ATX heading line ("#...", "##...",...)
followed by the body text. The fix lives in ``IngestionService._chunk_projection``
and is source-type-agnostic: it iterates ``projection.headings`` produced
by whatever adapter ran Stage 1. Every adapter that emits ``HeadingNode``
entries with populated ``level`` is therefore in scope — markdown, docx,
xlsx, and pdf (when the PDF has an outline or sectioning).

The fix only takes effect for documents ingested *after* it landed.
Documents ingested earlier still hold body-only chunk content and still
fail the projection round-trip property. This script refreshes those
existing records.

Per-document flow:

1. Look up the adapter for the document's source_type.
2. Re-read the source file from ``storage_root / source_path``.
3. Run the adapter to produce a fresh ``ProjectionResult``.
4. Verify the new content hash matches the document's stored
   ``source_content_hash``. On mismatch, the source file has drifted
   since original ingest; skip with a warning (override via
   ``--allow-hash-drift``).
5. Snapshot the document's current ``pipeline_status``.
6. Call ``IngestionService._stage2_indexing`` to re-chunk (now with the
   fix), re-embed, and atomically replace stored chunks. The
   synthetic header chunk is rebuilt as part of Stage 2.
7. Restore the snapshotted ``pipeline_status`` if it was a terminal
   state (``abstraction_complete``, ``abstraction_skipped``, ``failed``)
   so docs with existing abstracts keep their reporting status. The
   restore is written through the ingestion service's pipeline-status
   seam, so a restored successful terminal status clears any
   ``pipeline_error`` the document still carried, exactly as the same
   transition does during ingestion. A restored ``failed`` keeps its
   recorded error.

The script does NOT:

- Run Stage 3 (abstraction). ``semantic_abstract`` is preserved.
- Run Stage 1 record creation. The document id, version, lifecycle,
  tags, and tier3_metadata are unchanged.
- Re-ingest documents whose ``source_type`` has no registered adapter
  in the current ingestion service (e.g. legacy source types).
- Re-ingest non-active documents (lifecycle ``completed`` or
  ``archived``).

A ``StubAbstractionProvider`` is wired in so the Qwen3 MLX model is not
loaded by this script even when ``abstraction.enabled`` is true in the
vault config.

Usage::

    # Dry run across every vault under the bound vault root, all source types
    .venv/bin/python -m scripts.reproject_active_documents

    # Apply across every vault
    .venv/bin/python -m scripts.reproject_active_documents --execute

    # Single vault
    .venv/bin/python -m scripts.reproject_active_documents \\
        --vault example_vault --execute

    # Restrict to one source type (repeatable)
    .venv/bin/python -m scripts.reproject_active_documents \\
        --source-type markdown --source-type docx --execute

    # Re-project even when the source file has drifted since original ingest
    .venv/bin/python -m scripts.reproject_active_documents \\
        --execute --allow-hash-drift

Safe to run while the SAGE MCP server is running. The content store's
``index_chunks`` delete-then-insert runs in a single database transaction;
Postgres serializes concurrent writes to the same rows. There is no
DocumentLockManager coordination between this script and the MCP server,
so avoid running
this script in parallel with an MCP-initiated re-ingest of the same
document.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sage.adapters.stubs import StubAbstractionProvider
from sage.config import load_vault_config
from sage.mcp_init import SAGEServices, initialize_services
from sage.models.enums import TERMINAL_PIPELINE_STATUSES, SourceType
from sage.vault_management import bound_vault_root, config_path_for_vault


def _truncate(s: str | None, n: int) -> str:
    if not s:
        return ""
    return s if len(s) <= n else s[: n - 1] + "…"


def discover_vault_ids() -> list[str]:
    """Return sorted vault ids found under the root this process is bound to.

    A vault directory qualifies if it contains a ``vault_config.yaml`` file.
    Discovery resolves the root the same way ``config_path_for_vault`` does, so
    a vault this function reports is one whose config the loader below can then
    actually find; resolving the two differently would surface every discovered
    vault as a missing config under a redirected root.
    """
    root = bound_vault_root()
    if not root.exists():
        return []
    return sorted(
        p.name for p in root.iterdir() if p.is_dir() and (p / "vault_config.yaml").exists()
    )


async def reproject_vault_with_services(
    vault_id: str,
    services: SAGEServices,
    *,
    execute: bool,
    allow_hash_drift: bool,
    source_types: frozenset[str],
) -> tuple[int, int, int]:
    """Plan and (optionally) apply re-projection for one vault.

    ``source_types`` is the set of ``SourceType`` values to include
    (e.g. ``frozenset({"markdown", "docx"})``). Documents whose
    source_type is outside this set are silently ignored.

    Returns ``(n_done, n_skipped, n_failed)``.
    """
    graph = services.graph_store
    ingestion = services.ingestion_service
    content_store = services.content_store
    config = services.config

    storage_root = Path(config.vault.storage_root).expanduser().resolve()

    # Adapter map is private but documented; same pattern the existing
    # reindex_chunks_with_heading_context.py script uses for the embedder.
    # Per-document adapter lookup so we can mix source types in one sweep.
    all_docs = await graph.list_all_documents()
    candidates = [
        d for d in all_docs if d.source_type in source_types and d.lifecycle_status == "active"
    ]

    # Resolve source paths and split into actionable / missing buckets.
    # A missing adapter for a candidate's source_type counts as missing-
    # source (the document predates the current adapter registry).
    plan: list[tuple[object, Path, object]] = []  # (doc, src_path, adapter)
    missing_source: list[object] = []
    missing_adapter: list[object] = []
    for doc in candidates:
        if not doc.source_path:
            missing_source.append(doc)
            continue
        src_path = (storage_root / doc.source_path).resolve()
        if not src_path.exists():
            missing_source.append(doc)
            continue
        try:
            source_type_enum = SourceType(doc.source_type)
        except ValueError:
            missing_adapter.append(doc)
            continue
        adapter = ingestion._adapters.get(source_type_enum)
        if adapter is None:
            missing_adapter.append(doc)
            continue
        plan.append((doc, src_path, adapter))

    # Per-source-type breakdown for the header.
    type_counts: dict[str, int] = {}
    for d in candidates:
        type_counts[d.source_type] = type_counts.get(d.source_type, 0) + 1
    type_breakdown = ", ".join(f"{st}={n}" for st, n in sorted(type_counts.items())) or "(none)"

    print(f"\nVault: {vault_id}")
    print(f"  Total documents:                  {len(all_docs)}")
    print(f"  Active candidates:                {len(candidates)} ({type_breakdown})")
    print(f"  Source file present (actionable): {len(plan)}")
    print(f"  Source file missing (skipped):    {len(missing_source)}")
    print(f"  Adapter unavailable (skipped):    {len(missing_adapter)}")
    if missing_source:
        print("  Missing-source examples (first 5):")
        for d in missing_source[:5]:
            print(f"    {d.id}  source_type={d.source_type}  source_path={d.source_path!r}")
    if missing_adapter:
        print("  Missing-adapter examples (first 5):")
        for d in missing_adapter[:5]:
            print(f"    {d.id}  source_type={d.source_type}")

    skipped_pre = len(missing_source) + len(missing_adapter)

    if not plan:
        return (0, skipped_pre, 0)

    if not execute:
        print("  Plan preview (first 10):")
        for d, _src, _adapter in plan[:10]:
            print(f"    {d.id:42s}  {d.source_type:9s}  {_truncate(d.title, 50)}")
        if len(plan) > 10:
            print(f"    ... and {len(plan) - 10} more")
        print("  (dry-run; pass --execute to apply)")
        return (0, skipped_pre, 0)

    print("\n  Applying re-projection...")
    started = datetime.now(timezone.utc)
    n_done = 0
    n_failed = 0
    n_hash_drift_skipped = 0

    for i, (doc, src_path, adapter) in enumerate(plan, 1):
        label = (
            f"[{i:4d}/{len(plan)}]  {doc.id}  {doc.source_type:9s}  {_truncate(doc.title, 40):40s}"
        )
        try:
            # Stage-1-equivalent: project the source fresh.
            projection = await adapter.project(src_path)

            # Hash drift guard: source file changed since original ingest.
            # Per the canonical-hash convention, doc.source_content_hash is
            # `sha256:<hex>` and projection.content_hash is raw hex; compare
            # the hex parts.
            stored_hex = (doc.source_content_hash or "").removeprefix("sha256:")
            if (
                stored_hex
                and projection.content_hash
                and projection.content_hash != stored_hex
                and not allow_hash_drift
            ):
                n_hash_drift_skipped += 1
                print(
                    f"  {label}  SKIP (hash drift: stored={stored_hex[:12]}…, "
                    f"file={projection.content_hash[:12]}…); rerun with "
                    f"--allow-hash-drift to override"
                )
                continue

            # Snapshot pipeline_status so we can restore terminal states
            # after Stage 2 (which would otherwise mark INDEXING_COMPLETE).
            prior_status = doc.pipeline_status

            # Stage 2: re-chunk (fix), re-embed, atomic replace.
            await ingestion._stage2_indexing(doc.id, projection)

            # Restore prior terminal pipeline_status so that
            # `search filter pipeline_status=abstraction_complete`
            # continues to return this doc. The write goes through the
            # ingestion service's status seam rather than straight to the
            # store, so the rule that a successful terminal status clears
            # `pipeline_error` holds here as it does in the service.
            if prior_status in TERMINAL_PIPELINE_STATUSES:
                await ingestion._stamp_pipeline_status(doc.id, prior_status)
            n_done += 1
            print(f"  {label}  ✓")
        except Exception as exc:
            n_failed += 1
            print(f"  {label}  FAILED: {exc!r}", file=sys.stderr)

    elapsed = datetime.now(timezone.utc) - started
    skipped_total = skipped_pre + n_hash_drift_skipped
    print(
        f"\n  Done. {n_done} re-projected, "
        f"{n_hash_drift_skipped} skipped on hash drift, "
        f"{n_failed} failed in {elapsed.total_seconds():.1f}s."
    )

    # Reclaim the dead-tuple bloat this rewrite generated: each
    # index_chunks call is a delete-then-insert, and optimize's VACUUM
    # reclaims the superseded rows.
    if n_done > 0:
        try:
            print("  Reclaiming content-store bloat...")
            opt_started = datetime.now(timezone.utc)
            await content_store.optimize(cleanup_older_than=timedelta(0))
            opt_elapsed = datetime.now(timezone.utc) - opt_started
            print(f"  Reclaim done in {opt_elapsed.total_seconds():.1f}s.")
        except Exception as exc:
            print(f"  Reclaim step failed (non-fatal): {exc!r}", file=sys.stderr)

    return (n_done, skipped_total, n_failed)


async def reproject_vault(
    vault_id: str,
    *,
    execute: bool,
    allow_hash_drift: bool,
    source_types: frozenset[str],
) -> tuple[int, int, int]:
    """Initialize services for one vault and run re-projection.

    Uses ``StubAbstractionProvider`` so Qwen3 / MLX is not loaded — this
    script does not invoke Stage 3 abstraction anywhere.
    """
    config_path = config_path_for_vault(vault_id)
    if not config_path.exists():
        print(f"[{vault_id}] vault config not found: {config_path}", file=sys.stderr)
        return (0, 0, 1)

    config = load_vault_config(config_path)
    services = await initialize_services(
        config,
        config_path=config_path,
        abstraction_provider=StubAbstractionProvider(),
    )
    try:
        return await reproject_vault_with_services(
            vault_id,
            services,
            execute=execute,
            allow_hash_drift=allow_hash_drift,
            source_types=source_types,
        )
    finally:
        await services.graph_store.close()


async def reproject_all(
    vault_ids: list[str],
    *,
    execute: bool,
    allow_hash_drift: bool,
    source_types: frozenset[str],
) -> int:
    """Run re-projection across each vault sequentially. Returns process exit code."""
    grand_done = 0
    grand_skipped = 0
    grand_failed = 0
    for vault_id in vault_ids:
        try:
            done, skipped, failed = await reproject_vault(
                vault_id,
                execute=execute,
                allow_hash_drift=allow_hash_drift,
                source_types=source_types,
            )
            grand_done += done
            grand_skipped += skipped
            grand_failed += failed
        except Exception as exc:
            grand_failed += 1
            print(f"[{vault_id}] FAILED to process vault: {exc!r}", file=sys.stderr)

    print("\n=== Summary ===")
    print(f"  Vaults processed: {len(vault_ids)}")
    print(f"  Re-projected:     {grand_done}")
    print(f"  Skipped:          {grand_skipped}")
    print(f"  Failed:           {grand_failed}")

    return 0 if grand_failed == 0 else 1


_DEFAULT_SOURCE_TYPES = frozenset({st.value for st in SourceType})


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Re-project active documents so the current chunk-content shape "
            "applies. Covers every SourceType by default. See module docstring "
            "for full details."
        )
    )
    parser.add_argument(
        "--vault",
        help=(
            "Single vault id (e.g. example_vault). Default: every vault discovered "
            "under the vault root this process is bound to ($SAGE_VAULT_ROOT, "
            "else ~/sage_vaults/)."
        ),
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Apply the re-projection. Without this flag, runs in dry-run mode.",
    )
    parser.add_argument(
        "--source-type",
        action="append",
        choices=sorted(_DEFAULT_SOURCE_TYPES),
        dest="source_types",
        help=(
            "Restrict to a single source type. Repeatable (e.g. "
            "--source-type markdown --source-type docx). Default: all source "
            "types with registered adapters."
        ),
    )
    parser.add_argument(
        "--allow-hash-drift",
        action="store_true",
        help=(
            "Re-project even when the source file's hash differs from the "
            "document's stored source_content_hash. Default: skip with warning."
        ),
    )
    args = parser.parse_args()

    if args.vault:
        vault_ids = [args.vault]
    else:
        vault_ids = discover_vault_ids()
        if not vault_ids:
            print(f"No vaults discovered under {bound_vault_root()}", file=sys.stderr)
            raise SystemExit(2)
        print(f"Discovered vaults: {', '.join(vault_ids)}")

    source_types = frozenset(args.source_types) if args.source_types else _DEFAULT_SOURCE_TYPES
    print(f"Source types in scope: {', '.join(sorted(source_types))}")

    rc = asyncio.run(
        reproject_all(
            vault_ids,
            execute=args.execute,
            allow_hash_drift=args.allow_hash_drift,
            source_types=source_types,
        )
    )
    raise SystemExit(rc)


if __name__ == "__main__":
    main()
