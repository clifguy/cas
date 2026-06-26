"""Single-document purge (permanently out-of-band per CAS-ADR-029).

Removes one document and all its dependents from a SAGE vault: the
``documents`` row, ``document_tags``, ``edges`` (both directions),
``staging_edges`` (both directions), and the LanceDB chunks. Operator-
invoked only; this module is unreachable from the SAGE Core API and MCP
server by architectural invariant (import-topology test).

Safeguards (SAGE-Architecture v2.1 §6.4 No-Delete Invariant):
- Dry-run is the default. ``--apply`` is required for any state change.
- Target identification is by ``document_id`` only — no query interpretation.
- Refuses if any ``staging_edges`` row references the document at either end.
- Refuses if the document's ``pipeline_status`` is non-terminal (see
  ``sage.models.enums.TERMINAL_PIPELINE_STATUSES``).
- ``--apply`` requires typed confirmation of the document_id at the prompt.
- The SQLite cascade runs in one transaction. On failure, the transaction
  rolls back. The audit record is written **before** the cascade so the
  worst-case partial-failure outcome is "audit record with no delete",
  never "delete with no audit record".
- Appends a JSONL audit record to
  ``~/sage_vaults/{vault_id}/.maintenance_log.jsonl``.

If a new ``PipelineStatus`` enum value is added in the future, update
``sage.models.enums.TERMINAL_PIPELINE_STATUSES`` to declare its
terminality; this script reads that set rather than enumerating statuses
locally.

The per-document cascade lives in ``sage.maintenance._internal._purge_one``
and is shared with ``sage.maintenance.purge_batch``.

Usage::

    .venv/bin/python -m sage.maintenance.purge_document \\
        --vault VAULT --document-id ID --reason TEXT [--apply]
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

from sage.maintenance._internal import (
    _fetch_document_row,
    _lancedb_chunk_count,
    _list_staging_edge_ids,
    _purge_one,
)
from sage.models.enums import TERMINAL_PIPELINE_STATUSES

# Rebind point for tests that need to simulate SQLite failures.
_sqlite_connect = sqlite3.connect

_TERMINAL_STATUS_VALUES: frozenset[str] = frozenset(s.value for s in TERMINAL_PIPELINE_STATUSES)


def _resolve_paths(vault_id: str) -> tuple[Path, Path, Path] | None:
    """Return (vault_dir, sqlite_path, lancedb_dir) or None if config missing."""
    # CAS-ADR-043: locate and load the vault declaration through the active
    # profile's vault-source store rather than the filesystem directly.
    from sage.mcp_init import get_stack_config, resolve_stack_vault_source_store
    from sage.vault_source_binding import DiscoveredVault

    store = resolve_stack_vault_source_store(get_stack_config())
    config_path = store.config_locator(vault_id)
    if config_path is None or not config_path.exists():
        print(
            f"error: vault config not found for vault {vault_id!r}: {config_path}",
            file=sys.stderr,
        )
        return None
    config = store.load_config(DiscoveredVault(config_path=config_path))
    brain_root = Path(config.vault.brain_root).expanduser()
    return config_path.parent, brain_root / "graph.db", brain_root / "lancedb"


def _count_one(conn: sqlite3.Connection, sql: str, params: tuple) -> int:
    row = conn.execute(sql, params).fetchone()
    return row[0] if row else 0


def purge(
    *,
    vault_id: str,
    document_id: str,
    reason: str,
    apply: bool,
) -> int:
    """Service-level entry point. Returns 0 on success, non-zero on refusal.

    ``apply=False`` (default behaviour of the CLI without ``--apply``)
    prints the enumeration and exits 0. ``apply=True`` runs the full
    safeguard chain: preconditions → typed-confirmation prompt →
    audit-log append → SQLite cascade → LanceDB delete (the last three
    via ``_purge_one``).
    """
    paths = _resolve_paths(vault_id)
    if paths is None:
        return 2
    vault_dir, sqlite_path, lancedb_dir = paths

    if not sqlite_path.exists():
        print(
            f"error: graph.db not found for vault {vault_id!r}: {sqlite_path}",
            file=sys.stderr,
        )
        return 2

    conn = _sqlite_connect(sqlite_path)
    try:
        doc = _fetch_document_row(conn, document_id)
        if doc is None:
            print(
                f"error: document {document_id!r} not found in vault {vault_id!r}",
                file=sys.stderr,
            )
            return 2

        tag_count = _count_one(
            conn,
            "SELECT COUNT(*) FROM document_tags WHERE document_id = ?",
            (document_id,),
        )
        outbound_count = _count_one(
            conn,
            "SELECT COUNT(*) FROM edges WHERE source_id = ?",
            (document_id,),
        )
        inbound_count = _count_one(
            conn,
            "SELECT COUNT(*) FROM edges WHERE target_id = ?",
            (document_id,),
        )
        staging_ids = _list_staging_edge_ids(conn, document_id)
        chunk_count = _lancedb_chunk_count(lancedb_dir, document_id)

        print(f"Vault:        {vault_id}")
        print(f"Document:     {document_id}")
        print(f"  title:           {doc['title']}")
        print(f"  source_path:     {doc['source_path']}")
        print(f"  doc_type:        {doc['doc_type']}")
        print(f"  pipeline_status: {doc['pipeline_status']}")
        print(f"  reason:          {reason}")
        print()
        print("Will delete:")
        print("  documents row:           1")
        print(f"  document_tags rows:      {tag_count}")
        print(f"  edges (outbound):        {outbound_count}")
        print(f"  edges (inbound):         {inbound_count}")
        print(f"  staging edges:           {len(staging_ids)}")
        print(f"  lancedb chunks:          {chunk_count}")
        print()

        if not apply:
            print("(dry-run; pass --apply to execute)")
            return 0

        if staging_ids:
            print(
                f"refuse: pending staging edges reference {document_id!r}: "
                f"{', '.join(staging_ids)}. Resolve them (confirm or dismiss) "
                "before purging.",
                file=sys.stderr,
            )
            return 3

        status = doc["pipeline_status"]
        if status not in _TERMINAL_STATUS_VALUES:
            print(
                f"refuse: pipeline_status {status!r} is non-terminal for "
                f"document {document_id!r}. Wait for the pipeline to reach a "
                f"terminal status ({sorted(_TERMINAL_STATUS_VALUES)}) before "
                "purging.",
                file=sys.stderr,
            )
            return 3

        prompt = f"To confirm, retype the document_id ({document_id}): "
        typed = input(prompt)
        if typed != document_id:
            print(
                "refuse: typed confirmation did not match document_id. Aborting.",
                file=sys.stderr,
            )
            return 3

        result = _purge_one(
            document_id=document_id,
            conn=conn,
            lancedb_dir=lancedb_dir,
            vault_dir=vault_dir,
            reason=reason,
            batch_id=None,
        )
        if not result.succeeded:
            print(
                f"error: {result.error}; "
                f"audit_written={result.audit_written}, "
                f"sqlite_committed={result.sqlite_committed}, "
                f"lancedb_deleted={result.lancedb_deleted}",
                file=sys.stderr,
            )
            return 4

        print(f"purge complete: {document_id} removed from vault {vault_id!r}.")
        return 0
    finally:
        conn.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="sage.maintenance.purge_document",
        description=(
            "Remove one document and all its dependents from a SAGE vault. "
            "Operator-only; dry-run by default."
        ),
    )
    parser.add_argument("--vault", required=True, help="Vault id (e.g. example_vault).")
    parser.add_argument("--document-id", required=True, help="Document id to purge.")
    parser.add_argument(
        "--reason",
        required=True,
        help="Reason for the purge (recorded in the audit log).",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Apply the cascade. Without this flag, prints a plan and exits.",
    )
    args = parser.parse_args(argv)
    return purge(
        vault_id=args.vault,
        document_id=args.document_id,
        reason=args.reason,
        apply=args.apply,
    )


if __name__ == "__main__":
    raise SystemExit(main())
