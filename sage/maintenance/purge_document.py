"""Single-document purge (T-0105, permanently out-of-band per CAS-ADR-029).

Removes one document and all its dependents from a SAGE vault: the
``documents`` row, ``document_tags``, ``edges`` (both directions),
``staging_edges`` (both directions), and the LanceDB chunks. Operator-
invoked only; this module is unreachable from the SAGE Core API and MCP
server by architectural invariant (T-0107 import-topology test).

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

Usage::

    .venv/bin/python -m sage.maintenance.purge_document \\
        --vault VAULT --document-id ID --reason TEXT [--apply]
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import lancedb

from sage.config import load_vault_config
from sage.models.enums import TERMINAL_PIPELINE_STATUSES
from sage.vault_management import config_path_for_vault

# Rebind point for tests that need to simulate SQLite failures.
_sqlite_connect = sqlite3.connect

_TERMINAL_STATUS_VALUES: frozenset[str] = frozenset(s.value for s in TERMINAL_PIPELINE_STATUSES)


def _escape_sql(value: str) -> str:
    """Single-quote escape for inline LanceDB SQL-like predicates."""
    return value.replace("'", "''")


def _resolve_paths(vault_id: str) -> tuple[Path, Path, Path] | None:
    """Return (vault_dir, sqlite_path, lancedb_dir) or None if config missing."""
    config_path = config_path_for_vault(vault_id)
    if not config_path.exists():
        print(
            f"error: vault config not found for vault {vault_id!r}: {config_path}",
            file=sys.stderr,
        )
        return None
    config = load_vault_config(config_path)
    brain_root = Path(config.vault.brain_root).expanduser()
    return config_path.parent, brain_root / "graph.db", brain_root / "lancedb"


def _fetch_document(conn: sqlite3.Connection, document_id: str) -> dict[str, Any] | None:
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT id, title, source_path, source_content_hash, doc_type, "
        "pipeline_status FROM documents WHERE id = ?",
        (document_id,),
    ).fetchone()
    return dict(row) if row else None


def _count_one(conn: sqlite3.Connection, sql: str, params: tuple) -> int:
    row = conn.execute(sql, params).fetchone()
    return row[0] if row else 0


def _list_staging_edge_ids(conn: sqlite3.Connection, document_id: str) -> list[str]:
    rows = conn.execute(
        "SELECT id FROM staging_edges WHERE source_id = ? OR target_id = ?",
        (document_id, document_id),
    ).fetchall()
    return [r[0] for r in rows]


def _lancedb_chunk_count(lancedb_dir: Path, document_id: str) -> int:
    if not lancedb_dir.exists():
        return 0
    db = lancedb.connect(str(lancedb_dir))
    if "chunks" not in db.list_tables().tables:
        return 0
    return db.open_table("chunks").count_rows(filter=f"document_id = '{_escape_sql(document_id)}'")


def _write_audit_record(vault_dir: Path, doc_row: dict[str, Any], reason: str) -> None:
    """Append a JSONL audit record to {vault_dir}/.maintenance_log.jsonl."""
    record = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "document_id": doc_row["id"],
        "title": doc_row["title"],
        "source_path": doc_row["source_path"],
        "source_content_hash": doc_row["source_content_hash"],
        "doc_type": doc_row["doc_type"],
        "reason": reason,
    }
    audit_path = vault_dir / ".maintenance_log.jsonl"
    audit_path.parent.mkdir(parents=True, exist_ok=True)
    with open(audit_path, "a") as f:
        f.write(json.dumps(record) + "\n")


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
    audit-log append → SQLite cascade → LanceDB delete.
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
        doc = _fetch_document(conn, document_id)
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

        # Audit record first — a failure here leaves no delete behind.
        _write_audit_record(vault_dir, doc, reason)

        # SQLite cascade in a single transaction.
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "DELETE FROM document_tags WHERE document_id = ?",
                (document_id,),
            )
            conn.execute(
                "DELETE FROM edges WHERE source_id = ? OR target_id = ?",
                (document_id, document_id),
            )
            conn.execute(
                "DELETE FROM staging_edges WHERE source_id = ? OR target_id = ?",
                (document_id, document_id),
            )
            conn.execute(
                "DELETE FROM documents WHERE id = ?",
                (document_id,),
            )
            conn.commit()
        except Exception as exc:
            conn.rollback()
            print(
                f"error: SQLite cascade failed; audit record retained, "
                f"document row preserved: {exc}",
                file=sys.stderr,
            )
            return 4

        # LanceDB delete after SQLite commit. Failure here leaves orphan
        # chunks; surface but do not undo the SQLite cascade.
        if chunk_count > 0:
            db = lancedb.connect(str(lancedb_dir))
            table = db.open_table("chunks")
            table.delete(f"document_id = '{_escape_sql(document_id)}'")

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
    parser.add_argument("--vault", required=True, help="Vault id (e.g. pim_health).")
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
