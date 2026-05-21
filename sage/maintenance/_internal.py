"""Shared internals for the maintenance scripts.

This module holds the per-document purge primitive that
``sage.maintenance.purge_document`` (T-0105) and
``sage.maintenance.purge_batch`` (T-0106) both call. It is internal to
the maintenance package; nothing outside ``sage.maintenance.*`` should
import it. The architectural-boundary test (T-0107) excludes this
module from the importable surface of ``sage.mcp_server`` and the Core
API alongside the rest of the package.

Convention: the per-document cascade always writes the audit record
**before** mutating SQLite or LanceDB. The worst-case partial-failure
outcome is "audit record with no delete", never "delete with no audit
record".
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import lancedb


@dataclass(frozen=True)
class _PurgeOneResult:
    """Outcome of a single per-document purge.

    ``audit_written`` and ``sqlite_committed`` let the caller distinguish
    "audit-only" partial failure from "audit+sqlite, lancedb orphaned"
    partial failure when surfacing batch-level error messages.
    """

    document_id: str
    succeeded: bool
    error: str | None
    audit_written: bool
    sqlite_committed: bool
    lancedb_deleted: bool


def _escape_sql(value: str) -> str:
    """Single-quote escape for inline LanceDB SQL-like predicates."""
    return value.replace("'", "''")


def _fetch_document_row(conn: sqlite3.Connection, document_id: str) -> dict[str, Any] | None:
    """Return the documents row for ``document_id`` or ``None``.

    Returns the columns needed by both the dry-run summary and the audit
    record: ``id, title, source_path, source_content_hash, doc_type,
    pipeline_status, document_date, created_at``.
    """
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT id, title, source_path, source_content_hash, doc_type, "
        "pipeline_status, document_date, created_at "
        "FROM documents WHERE id = ?",
        (document_id,),
    ).fetchone()
    return dict(row) if row else None


def _list_staging_edge_ids(conn: sqlite3.Connection, document_id: str) -> list[str]:
    """IDs of staging edges that reference ``document_id`` at either end."""
    rows = conn.execute(
        "SELECT id FROM staging_edges WHERE source_id = ? OR target_id = ?",
        (document_id, document_id),
    ).fetchall()
    return [r[0] for r in rows]


def _lancedb_chunk_count(lancedb_dir: Path, document_id: str) -> int:
    """Count of LanceDB chunks for ``document_id`` (0 if no store/table)."""
    if not lancedb_dir.exists():
        return 0
    db = lancedb.connect(str(lancedb_dir))
    if "chunks" not in db.list_tables().tables:
        return 0
    return db.open_table("chunks").count_rows(filter=f"document_id = '{_escape_sql(document_id)}'")


def _write_audit_record(
    vault_dir: Path,
    doc_row: dict[str, Any],
    reason: str,
    batch_id: str | None = None,
) -> None:
    """Append a JSONL audit record to ``{vault_dir}/.maintenance_log.jsonl``.

    The ``batch_id`` field is included only when supplied. T-0105 single-
    doc callers pass ``None`` and the field is omitted; T-0106 batch
    callers pass a shared UUID and every entry in the batch carries it.
    """
    record: dict[str, Any] = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "document_id": doc_row["id"],
        "title": doc_row["title"],
        "source_path": doc_row["source_path"],
        "source_content_hash": doc_row["source_content_hash"],
        "doc_type": doc_row["doc_type"],
        "reason": reason,
    }
    if batch_id is not None:
        record["batch_id"] = batch_id
    audit_path = vault_dir / ".maintenance_log.jsonl"
    audit_path.parent.mkdir(parents=True, exist_ok=True)
    with open(audit_path, "a") as f:
        f.write(json.dumps(record) + "\n")


def _purge_one(
    *,
    document_id: str,
    conn: sqlite3.Connection,
    lancedb_dir: Path,
    vault_dir: Path,
    reason: str,
    batch_id: str | None = None,
) -> _PurgeOneResult:
    """Audit-first, SQLite cascade in one transaction, then LanceDB delete.

    The caller owns the connection and is responsible for all precondition
    checks (existence, staging edges, pipeline status, typed confirmation).
    The helper does not validate; it executes.
    """
    doc = _fetch_document_row(conn, document_id)
    if doc is None:
        return _PurgeOneResult(
            document_id=document_id,
            succeeded=False,
            error="document not found",
            audit_written=False,
            sqlite_committed=False,
            lancedb_deleted=False,
        )

    _write_audit_record(vault_dir, doc, reason, batch_id=batch_id)

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
        return _PurgeOneResult(
            document_id=document_id,
            succeeded=False,
            error=f"sqlite cascade failed: {exc}",
            audit_written=True,
            sqlite_committed=False,
            lancedb_deleted=False,
        )

    try:
        if lancedb_dir.exists():
            db = lancedb.connect(str(lancedb_dir))
            if "chunks" in db.list_tables().tables:
                db.open_table("chunks").delete(f"document_id = '{_escape_sql(document_id)}'")
    except Exception as exc:
        return _PurgeOneResult(
            document_id=document_id,
            succeeded=False,
            error=f"lancedb delete failed: {exc}",
            audit_written=True,
            sqlite_committed=True,
            lancedb_deleted=False,
        )

    return _PurgeOneResult(
        document_id=document_id,
        succeeded=True,
        error=None,
        audit_written=True,
        sqlite_committed=True,
        lancedb_deleted=True,
    )
