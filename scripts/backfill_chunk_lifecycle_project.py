"""Backfill `lifecycle_status` and `project` on LanceDB chunk rows.

The schema migration adds the two columns to the chunks table and
leaves them NULL on rows that predate the migration. This script reads
each document's current `lifecycle_status` and `project` from the graph
store and writes them to every chunk row for that document via
`ContentStore.update_chunk_metadata`.

Operator workflow:

    # Dry-run (no writes; shows the document count that would be touched).
    .venv/bin/python scripts/backfill_chunk_lifecycle_project.py --vault-id cas

    # Apply the backfill.
    .venv/bin/python scripts/backfill_chunk_lifecycle_project.py \\
        --vault-id cas --execute

The script also runs against multiple vaults sequentially when invoked
with `--all-vaults`. Run AFTER the schema migration completes (see
`sage --migrate` and ADR-008's --migrate convention).

Idempotent: a second run re-writes the same column values and no rows
are added or removed. Documents with no chunks (e.g., failed-pipeline
records) are silently skipped because `update_chunk_metadata` on a
zero-row match is a no-op.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sage.adapters.content_store_lancedb import LanceDBContentStore
from sage.config import load_vault_config
from sage.storage.graph_store import GraphStore
from sage.vault_management import config_path_for_vault

_VAULTS_ROOT = Path("~/sage_vaults").expanduser()


def discover_vault_ids() -> list[str]:
    """Return sorted vault ids that have a vault_config.yaml on disk."""
    if not _VAULTS_ROOT.exists():
        return []
    return sorted(
        p.name for p in _VAULTS_ROOT.iterdir() if p.is_dir() and (p / "vault_config.yaml").exists()
    )


async def run(
    *,
    graph_store: GraphStore,
    brain_root: Path,
    execute: bool,
    out=None,
) -> int:
    """Backfill chunk-level ``lifecycle_status`` and ``project`` for every
    document in the vault. Returns a Unix-style exit code (0 on success).

    Skips the chunk write when both fields on the parent doc are
    ``None``/empty — there is nothing to backfill.

    ``execute=False`` is dry-run mode: the script counts documents and
    prints what it would do, but does not call ``update_chunk_metadata``.
    """
    out = out if out is not None else sys.stdout

    content_store = LanceDBContentStore(brain_root)
    all_docs = await graph_store.list_all_documents()

    n_total = len(all_docs)
    n_actionable = sum(
        1 for d in all_docs if d.lifecycle_status is not None or d.project is not None
    )
    n_updated = 0

    print(f"Documents in vault:    {n_total}", file=out)
    print(f"Actionable (any of    {n_actionable}", file=out)
    print("  lifecycle / project)", file=out)
    print(f"Mode:                  {'EXECUTE' if execute else 'DRY-RUN'}", file=out)
    print("", file=out)

    if not execute:
        print(
            f"Dry-run. Pass --execute to push lifecycle_status / project "
            f"to chunk rows for {n_actionable} document(s).",
            file=out,
        )
        return 0

    started = datetime.now(timezone.utc)
    progress_every = 50  # log every N updated docs so Monitor can stream activity
    for doc in all_docs:
        chunk_updates: dict[str, str | None] = {}
        if doc.lifecycle_status is not None:
            chunk_updates["lifecycle_status"] = doc.lifecycle_status
        if doc.project is not None:
            chunk_updates["project"] = doc.project
        if not chunk_updates:
            continue
        await content_store.update_chunk_metadata(doc.id, chunk_updates)
        n_updated += 1
        if n_updated % progress_every == 0:
            print(
                f"  progress: {n_updated}/{n_actionable} documents updated",
                file=out,
                flush=True,
            )

    elapsed = datetime.now(timezone.utc) - started

    # Compact LanceDB fragments so the per-document update_chunk_metadata
    # writes don't accumulate version history indefinitely. Same call as
    # reproject_active_documents.py.
    try:
        table = content_store._get_table()
        if table is not None and n_updated > 0:
            print("Compacting LanceDB fragments...", file=out)
            opt_started = datetime.now(timezone.utc)
            table.optimize(cleanup_older_than=timedelta(0))
            opt_elapsed = datetime.now(timezone.utc) - opt_started
            print(f"  done in {opt_elapsed.total_seconds():.1f}s.", file=out)
    except Exception as exc:
        print(f"Compaction step failed (non-fatal): {exc!r}", file=sys.stderr)

    print(
        f"\nUpdated {n_updated} document(s) in {elapsed.total_seconds():.1f}s.",
        file=out,
    )
    return 0


async def _backfill_vault(vault_id: str, *, execute: bool) -> int:
    """CLI helper: load vault config, open the graph store, run the
    backfill, and close cleanly. Returns the run's exit code.
    """
    config_path = config_path_for_vault(vault_id)
    if not config_path.exists():
        print(f"[{vault_id}] vault config not found: {config_path}", file=sys.stderr)
        return 2

    config = load_vault_config(config_path)
    brain_root = Path(config.vault.brain_root).expanduser().resolve()

    graph_store = GraphStore(brain_root / "graph.db")
    await graph_store.initialize()
    try:
        print(f"=== Vault: {vault_id} ===")
        return await run(graph_store=graph_store, brain_root=brain_root, execute=execute)
    finally:
        await graph_store.close()


async def _backfill_all(vault_ids: list[str], *, execute: bool) -> int:
    rc = 0
    for vault_id in vault_ids:
        try:
            sub_rc = await _backfill_vault(vault_id, execute=execute)
            if sub_rc != 0:
                rc = sub_rc
        except Exception as exc:
            print(f"[{vault_id}] FAILED: {exc!r}", file=sys.stderr)
            rc = 1
    return rc


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Backfill lifecycle_status and project columns on LanceDB chunk "
            "rows. Run after the T-0077 schema migration."
        ),
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--vault-id", help="Single vault id to process.")
    group.add_argument(
        "--all-vaults",
        action="store_true",
        help="Process every vault under ~/sage_vaults/ sequentially.",
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Apply the backfill. Default is dry-run (no writes).",
    )
    args = parser.parse_args(argv)

    if args.all_vaults:
        vault_ids = discover_vault_ids()
        if not vault_ids:
            print(
                f"No vaults found under {_VAULTS_ROOT}. Nothing to do.",
                file=sys.stderr,
            )
            return 1
        return asyncio.run(_backfill_all(vault_ids, execute=args.execute))

    return asyncio.run(_backfill_vault(args.vault_id, execute=args.execute))


if __name__ == "__main__":
    sys.exit(main())
