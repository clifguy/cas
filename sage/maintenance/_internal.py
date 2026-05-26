"""Shared internals for the maintenance scripts.

This module holds the per-document purge primitive that
``sage.maintenance.purge_document``,
``sage.maintenance.purge_batch``, and
``sage.maintenance.purge_chain`` all call, plus the chain-walk
helpers used exclusively by ``purge_chain``. It is internal to the
maintenance package; nothing outside ``sage.maintenance.*`` should
import it. The architectural-boundary test excludes this
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
    chain_id: str | None = None,
) -> None:
    """Append a JSONL audit record to ``{vault_dir}/.maintenance_log.jsonl``.

    The ``batch_id`` field is included only when supplied. single-
    doc callers pass ``None`` and the field is omitted; batch
    callers pass a shared UUID and every entry in the batch carries it;
    chain-purge callers pass ``chain_id`` instead so an auditor
    can distinguish chain-purges from time-window batch-purges.
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
    if chain_id is not None:
        record["chain_id"] = chain_id
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
    chain_id: str | None = None,
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

    _write_audit_record(vault_dir, doc, reason, batch_id=batch_id, chain_id=chain_id)

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


# ─── Chain-walk helpers ─────────────────────────────────────


def _walk_chain(
    conn: sqlite3.Connection,
    start_id: str,
    edge_type: str,
) -> dict[str, Any]:
    """Recursive-CTE walk of an edge chain from ``start_id``.

    Mirrors ``sage.storage.graph_store._chain_walk_sync`` but reimplemented
    against the maintenance package's own sync sqlite connection to keep
    the package's import surface small. Walks both directions; ``UNION``
    (not ``UNION ALL``) terminates on cycles.

    Returns ``{"documents": [{...},...], "edges": [{...},...]}`` where
    each document entry carries ``doc_id``, ``title``, ``version_label``,
    ``document_date``, ``doc_type``, ``pipeline_status`` and each edge
    entry carries ``source_id``, ``target_id``.
    """
    conn.row_factory = sqlite3.Row
    sql = """
        WITH RECURSIVE chain AS (
            SELECT ? AS doc_id

            UNION

            SELECT e.target_id AS doc_id
            FROM edges e
            INNER JOIN chain c ON e.source_id = c.doc_id
            WHERE e.edge_type = ?

            UNION

            SELECT e.source_id AS doc_id
            FROM edges e
            INNER JOIN chain c ON e.target_id = c.doc_id
            WHERE e.edge_type = ?
        )
        SELECT c.doc_id,
            d.title, d.version_label, d.document_date, d.doc_type,
            d.pipeline_status
        FROM chain c
        INNER JOIN documents d ON c.doc_id = d.id
    """
    doc_rows = conn.execute(sql, (start_id, edge_type, edge_type)).fetchall()
    documents = [
        {
            "doc_id": row["doc_id"],
            "title": row["title"],
            "version_label": row["version_label"],
            "document_date": row["document_date"],
            "doc_type": row["doc_type"],
            "pipeline_status": row["pipeline_status"],
        }
        for row in doc_rows
    ]

    doc_ids = [d["doc_id"] for d in documents]
    if len(doc_ids) <= 1:
        return {"documents": documents, "edges": []}

    placeholders = ",".join("?" * len(doc_ids))
    edge_sql = (
        f"SELECT source_id, target_id FROM edges "  # noqa: S608 -- ? placeholders, bound below
        f"WHERE edge_type = ? "
        f"AND source_id IN ({placeholders}) "
        f"AND target_id IN ({placeholders})"
    )
    edge_params: list[Any] = [edge_type] + doc_ids + doc_ids
    edge_rows = conn.execute(edge_sql, edge_params).fetchall()
    edges = [{"source_id": row["source_id"], "target_id": row["target_id"]} for row in edge_rows]
    return {"documents": documents, "edges": edges}


def _build_adjacency(
    documents: list[dict[str, Any]], edges: list[dict[str, str]]
) -> tuple[dict[str, set[str]], dict[str, set[str]]]:
    """Return (successors, predecessors) keyed by doc_id.

    Mirrors ``sage/services/graph_ops.py`` chain logic: for an edge
    ``source -> target``, ``target`` is a successor of ``source`` and
    ``source`` is a predecessor of ``target``. For supersedes the source
    is the newer version, so the head (newest) has no predecessors.
    """
    successors: dict[str, set[str]] = {d["doc_id"]: set() for d in documents}
    predecessors: dict[str, set[str]] = {d["doc_id"]: set() for d in documents}
    for e in edges:
        successors.setdefault(e["source_id"], set()).add(e["target_id"])
        predecessors.setdefault(e["target_id"], set()).add(e["source_id"])
    return successors, predecessors


def _chain_is_linear(documents: list[dict[str, Any]], edges: list[dict[str, str]]) -> bool:
    """True iff every node has at most one predecessor and at most one successor."""
    successors, predecessors = _build_adjacency(documents, edges)
    return all(
        len(successors[d["doc_id"]]) <= 1 and len(predecessors[d["doc_id"]]) <= 1 for d in documents
    )


def _chain_head_ids(documents: list[dict[str, Any]], edges: list[dict[str, str]]) -> list[str]:
    """Doc ids with zero inbound edges of the named type — the chain's heads.

    For a linear supersedes chain this is exactly one id (the newest version).
    For a branched chain (multiple roots) it can be multiple ids.
    """
    _, predecessors = _build_adjacency(documents, edges)
    return sorted(d["doc_id"] for d in documents if not predecessors.get(d["doc_id"]))


def _order_chain_from_head(
    documents: list[dict[str, Any]],
    edges: list[dict[str, str]],
    head_id: str,
) -> list[str]:
    """Head-first ordering of chain member ids via depth-first successor walk.

    Branches are visited in sorted-id order for deterministic output.
    """
    successors, _ = _build_adjacency(documents, edges)
    ordered: list[str] = []
    visited: set[str] = set()
    stack: list[str] = [head_id]
    while stack:
        node = stack.pop()
        if node in visited:
            continue
        visited.add(node)
        ordered.append(node)
        # Sorted in reverse so smaller ids are visited first when popped.
        for succ in sorted(successors.get(node, set()), reverse=True):
            if succ not in visited:
                stack.append(succ)
    # If the walk did not reach all chain members (disconnected via head),
    # append the rest in sorted order so the dry-run output still lists them.
    for d in documents:
        if d["doc_id"] not in visited:
            ordered.append(d["doc_id"])
            visited.add(d["doc_id"])
    return ordered
