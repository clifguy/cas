"""SQLite graph store for SAGE documents, edges, and users.

WAL mode enabled per-connection (BH-004). Each thread in the executor
pool gets its own connection via threading.local(), allowing concurrent
reads under WAL mode while SQLite serializes writes internally.
"""

import asyncio
import json
import re
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, TypeVar

from sage.instrumentation.timing import NULL_QUERY_TIMER, NullQueryTimer, QueryTimer
from sage.models.enums import (
    EdgeType,
    PipelineStatus,
    ResolutionPolicy,
    SourceType,
    UserType,
)
from sage.models.schemas import Document, Edge, LinkRequest, StagingEdge, User
from sage.storage.migrations import (
    MIGRATION_PLAN,
    POST_MIGRATION_DDL,
    TABLES,
    SchemaMigrationRequired,
    pending_migrations,
)

T = TypeVar("T")

# Defense-in-depth gate for tier3 keys that get interpolated into the
# JSON path of a json_extract() expression. The service layer validates
# the same keys against the doc_type's metadata_schema; this regex is
# the last-line fence guaranteeing no caller-supplied string can break
# out of the path and inject SQL.
_TIER3_KEY_FORMAT = re.compile(r"^[A-Za-z0-9_]+$")


@dataclass(frozen=True)
class LinkReadContext:
    """Pre-fetched state needed to validate and execute a LinkRequest.

    Populated by `GraphStore.read_link_context` in a single executor
    submission so the service layer can validate without issuing further
    per-query round-trips. Fields that are not applicable to the request's
    edge type are left at their default (empty / False / None).
    """

    source_exists: bool
    target_exists: bool
    retracted_edge: Edge | None = None
    source_lineage: frozenset[str] = field(default_factory=frozenset)
    target_lineage: frozenset[str] = field(default_factory=frozenset)
    source_anchor_exists: bool = True
    target_anchor_exists: bool = True
    has_sup_predecessor: bool = False
    has_sup_successor: bool = False
    tombstone_candidates: tuple[str, ...] = ()


class GraphStore:
    def __init__(
        self,
        db_path: Path,
        max_connections: int = 4,
        *,
        query_timer: QueryTimer | NullQueryTimer = NULL_QUERY_TIMER,
    ) -> None:
        self._db_path = db_path
        self._max_connections = max_connections
        self._executor: ThreadPoolExecutor | None = None
        self._local = threading.local()
        self._all_connections: list[sqlite3.Connection] = []
        self._all_connections_lock = threading.Lock()
        self._query_timer = query_timer

    def _get_connection(self) -> sqlite3.Connection:
        """Return the thread-local connection, creating one if needed."""
        conn: sqlite3.Connection | None = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(str(self._db_path), check_same_thread=False)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL;")
            conn.execute("PRAGMA foreign_keys=ON;")
            self._local.conn = conn
            with self._all_connections_lock:
                self._all_connections.append(conn)
        return conn

    async def _run(self, fn: Callable[..., T], *args: Any) -> T:
        """Dispatch a sync callable to the connection-pool executor."""
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._executor, fn, *args)

    async def initialize(self, migrate: bool = False) -> None:
        """Create database, start executor pool, optionally run migrations.

        Args:
            migrate: If True, apply any pending ALTER TABLE migrations.
                If False (default) and migrations are pending against an
                existing database, raise ``SchemaMigrationRequired``
                rather than silently mutating the schema.
        """
        with self._query_timer.measure("initialize"):
            self._db_path.parent.mkdir(parents=True, exist_ok=True)
            self._executor = ThreadPoolExecutor(
                max_workers=self._max_connections,
                thread_name_prefix="sage-graph",
            )
            await self._run(self._initialize_sync, migrate)

    def _initialize_sync(self, migrate: bool) -> None:
        conn = self._get_connection()
        # 1. Create tables (IF NOT EXISTS -- no-op for existing databases)
        for ddl in TABLES:
            conn.execute(ddl)
        # 2. Detect and (optionally) apply pending ALTER TABLE migrations.
        pending = pending_migrations(conn, MIGRATION_PLAN)
        if pending and not migrate:
            details = ", ".join(f"{m.table}.{m.column}" for m in pending)
            raise SchemaMigrationRequired(
                f"GraphStore at {self._db_path} has {len(pending)} pending "
                f"schema migration(s): {details}. Re-run the server with "
                f"--migrate to apply them."
            )
        for m in pending:
            try:
                conn.execute(m.ddl)
            except sqlite3.OperationalError:
                # Defensive: should not occur given the table_info check,
                # but tolerate concurrent migration in tests.
                pass
        # 3. Create indexes (may reference columns added by migrations)
        for ddl in POST_MIGRATION_DDL:
            conn.execute(ddl)
        conn.commit()

    async def close(self) -> None:
        if self._executor is not None:
            executor = self._executor
            self._executor = None
            executor.shutdown(wait=True)
        with self._all_connections_lock:
            for conn in self._all_connections:
                conn.close()
            self._all_connections.clear()

    # ------------------------------------------------------------------
    # Document operations
    # ------------------------------------------------------------------

    async def insert_document(self, doc: Document) -> None:
        await self._run(self._insert_document_sync, doc)

    def _insert_document_sync(self, doc: Document) -> None:
        conn = self._get_connection()
        self._exec_insert_document(conn, doc)
        conn.commit()

    def _exec_insert_document(self, conn: sqlite3.Connection, doc: Document) -> None:
        """Issue the INSERT for a Document on the given connection without
        committing. Used by both _insert_document_sync (commit-per-call)
        and the compound atomic methods (multi-write transactions).
        """
        conn.execute(
            """INSERT INTO documents (
                id, title, source_type, source_path, lifecycle_status,
                version_label, project, tags, authority_scope, doc_type,
                source_content_hash, adapter_version, created_by, created_at,
                last_modified_by, updated_at, projected_at, indexed_at,
                source_modified_at, document_date,
                semantic_abstract, pipeline_status, pipeline_error, tier3_metadata,
                metadata_confirmed
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                doc.id,
                doc.title,
                doc.source_type.value,
                doc.source_path,
                doc.lifecycle_status,
                doc.version_label,
                doc.project,
                json.dumps(doc.tags),
                doc.authority_scope,
                doc.doc_type,
                doc.source_content_hash,
                doc.adapter_version,
                doc.created_by,
                doc.created_at.isoformat(),
                doc.last_modified_by,
                doc.updated_at.isoformat(),
                doc.projected_at.isoformat() if doc.projected_at else None,
                doc.indexed_at.isoformat() if doc.indexed_at else None,
                doc.source_modified_at.isoformat() if doc.source_modified_at else None,
                doc.document_date,
                doc.semantic_abstract,
                doc.pipeline_status.value,
                doc.pipeline_error,
                json.dumps(doc.tier3_metadata) if doc.tier3_metadata else None,
                1 if doc.metadata_confirmed else 0,
            ),
        )

    async def get_document(self, doc_id: str) -> Document | None:
        with self._query_timer.measure("get_document"):
            return await self._run(self._get_document_sync, doc_id)

    def _get_document_sync(self, doc_id: str) -> Document | None:
        conn = self._get_connection()
        row = conn.execute("SELECT * FROM documents WHERE id = ?", (doc_id,)).fetchone()
        if row is None:
            return None
        return self._row_to_document(row)

    async def update_document(self, doc_id: str, updates: dict) -> Document | None:
        return await self._run(self._update_document_sync, doc_id, updates)

    def _update_document_sync(self, doc_id: str, updates: dict) -> Document | None:
        conn = self._get_connection()
        if not updates:
            return self._get_document_sync(doc_id)
        self._exec_update_document(conn, doc_id, updates)
        conn.commit()
        return self._get_document_sync(doc_id)

    def _exec_update_document(self, conn: sqlite3.Connection, doc_id: str, updates: dict) -> None:
        """Issue the UPDATE for a document on the given connection without
        committing. Caller is responsible for commit/rollback. Mutates the
        `updates` dict in place to JSON-serialize collection fields.
        """
        if not updates:
            return
        # Serialize JSON fields if present
        if "tags" in updates:
            updates["tags"] = json.dumps(updates["tags"])
        if "tier3_metadata" in updates:
            updates["tier3_metadata"] = json.dumps(updates["tier3_metadata"])
        if "metadata_confirmed" in updates:
            updates["metadata_confirmed"] = 1 if updates["metadata_confirmed"] else 0

        set_clause = ", ".join(f"{k} = ?" for k in updates)
        values = list(updates.values())
        values.append(doc_id)
        conn.execute(
            f"UPDATE documents SET {set_clause} WHERE id = ?",  # noqa: S608 -- set_clause is built from trusted dict keys; all values pass through ? placeholders
            values,
        )

    async def list_all_documents(self) -> list[Document]:
        """Return all documents in the graph store."""
        with self._query_timer.measure("list_all_documents"):
            return await self._run(self._list_all_documents_sync)

    def _list_all_documents_sync(self) -> list[Document]:
        conn = self._get_connection()
        rows = conn.execute("SELECT * FROM documents").fetchall()
        return [self._row_to_document(r) for r in rows]

    # Columns safe to use in ORDER BY (prevent SQL injection).
    _SORTABLE_COLUMNS: frozenset[str] = frozenset(
        {
            "title",
            "doc_type",
            "document_date",
            "lifecycle_status",
        }
    )

    async def query_documents(
        self,
        filters: dict[str, object] | None = None,
        limit: int = 100,
        offset: int = 0,
        sort_by: str | None = None,
        sort_order: str | None = None,
    ) -> tuple[list[Document], int]:
        """Query documents with SQL predicates. Returns (docs, total_count).

        Supported filter keys: doc_type, project, lifecycle_status,
        pipeline_status, tags (list[str], AND semantics), document_ids (list[str]),
        tier3 (dict[str, object], AND semantics, pushed into SQL as
        ``json_extract(tier3_metadata, '$.<key>') = ?`` predicates per
        T-0075). Failed-pipeline documents are excluded by default
        unless pipeline_status is explicitly set.

        Tier3 keys must match ``[A-Za-z0-9_]+``; an offending key raises
        ``ValueError`` before any SQL is built. The service layer
        validates the same keys against the doc_type's metadata_schema;
        the storage-layer check is defense-in-depth so the JSON path
        interpolation is always safe.

        sort_by: column to sort on (title, document_date, lifecycle_status).
        sort_order: 'asc' or 'desc'. Default varies by sort_by.
        If sort_by is None, uses default sort: active lifecycle first,
        then document_date descending.
        """
        with self._query_timer.measure("query_documents"):
            return await self._run(
                self._query_documents_sync,
                filters,
                limit,
                offset,
                sort_by,
                sort_order,
            )

    def _query_documents_sync(
        self,
        filters: dict[str, object] | None,
        limit: int,
        offset: int,
        sort_by: str | None,
        sort_order: str | None,
    ) -> tuple[list[Document], int]:
        conn = self._get_connection()
        where_clauses: list[str] = []
        params: list[object] = []

        # Default: exclude failed pipeline unless explicitly filtering for it
        if not filters or "pipeline_status" not in filters:
            where_clauses.append("pipeline_status != ?")
            params.append("failed")

        if filters:
            if "doc_type" in filters and filters["doc_type"]:
                where_clauses.append("doc_type = ?")
                params.append(filters["doc_type"])
            if "project" in filters and filters["project"]:
                where_clauses.append("project = ?")
                params.append(filters["project"])
            if "lifecycle_status" in filters and filters["lifecycle_status"]:
                where_clauses.append("lifecycle_status = ?")
                params.append(filters["lifecycle_status"])
            if "pipeline_status" in filters and filters["pipeline_status"]:
                where_clauses.append("pipeline_status = ?")
                params.append(filters["pipeline_status"])
            if "document_ids" in filters and filters["document_ids"]:
                placeholders = ",".join("?" for _ in filters["document_ids"])
                where_clauses.append(f"id IN ({placeholders})")
                params.extend(filters["document_ids"])
            if "tags" in filters and filters["tags"]:
                for tag in filters["tags"]:
                    where_clauses.append("EXISTS (SELECT 1 FROM json_each(tags) WHERE value = ?)")
                    params.append(tag)
            if "tier3" in filters and filters["tier3"]:
                tier3 = filters["tier3"]
                if not isinstance(tier3, dict):
                    raise ValueError(f"tier3 filter must be a dict, got {type(tier3).__name__}")
                for key, value in tier3.items():
                    if not isinstance(key, str) or not _TIER3_KEY_FORMAT.fullmatch(key):
                        raise ValueError(
                            f"tier3 filter key {key!r} is not safe for SQL interpolation; "
                            "keys must match [A-Za-z0-9_]+"
                        )
                    path_expr = f"json_extract(tier3_metadata, '$.{key}')"
                    if value is None:
                        # SQLite json_extract returns SQL NULL for both
                        # missing keys and JSON null values, matching the
                        # _tier3_matches helper's None semantics.
                        where_clauses.append(f"{path_expr} IS NULL")
                    else:
                        where_clauses.append(f"{path_expr} = ?")
                        params.append(value)

        where_sql = " AND ".join(where_clauses) if where_clauses else "1=1"

        # Get total count
        count_row = conn.execute(
            f"SELECT COUNT(*) FROM documents WHERE {where_sql}",  # noqa: S608 -- where_sql built from trusted internal builder; values are ? placeholders
            params,
        ).fetchone()
        total_count = count_row[0]

        # Build ORDER BY clause
        order_sql = self._build_order_clause(sort_by, sort_order)

        # Get paged results
        rows = conn.execute(
            f"SELECT * FROM documents WHERE {where_sql} {order_sql} LIMIT ? OFFSET ?",  # noqa: S608 -- where_sql/order_sql built from trusted internal builders; values are ? placeholders
            [*params, limit, offset],
        ).fetchall()
        return [self._row_to_document(r) for r in rows], total_count

    @classmethod
    def _build_order_clause(cls, sort_by: str | None, sort_order: str | None) -> str:
        """Build a safe ORDER BY clause.

        Default (no sort_by): active lifecycle first, then document_date desc.
        Explicit sort_by: validated against allowlist, nulls sort last.
        """
        if sort_by is None:
            # Default: active first, then by document_date desc (nulls last)
            return (
                "ORDER BY "
                "CASE WHEN lifecycle_status = 'active' THEN 0 ELSE 1 END, "
                "CASE WHEN document_date IS NULL THEN 1 ELSE 0 END, "
                "document_date DESC"
            )

        if sort_by not in cls._SORTABLE_COLUMNS:
            return "ORDER BY title"

        direction = "DESC" if sort_order == "desc" else "ASC"
        nulls_last = (
            "CASE WHEN document_date IS NULL THEN 1 ELSE 0 END, "
            if sort_by == "document_date"
            else ""
        )
        return f"ORDER BY {nulls_last}{sort_by} {direction}"

    async def find_by_source_path(self, source_path: str) -> list[Document]:
        with self._query_timer.measure("find_by_source_path"):
            return await self._run(self._find_by_source_path_sync, source_path)

    def _find_by_source_path_sync(self, source_path: str) -> list[Document]:
        conn = self._get_connection()
        rows = conn.execute(
            "SELECT * FROM documents WHERE source_path = ?", (source_path,)
        ).fetchall()
        return [self._row_to_document(r) for r in rows]

    async def find_documents_by_title(self, title: str) -> list[Document]:
        """Find all documents with a matching title (case-insensitive)."""
        with self._query_timer.measure("find_documents_by_title"):
            return await self._run(self._find_documents_by_title_sync, title)

    def _find_documents_by_title_sync(self, title: str) -> list[Document]:
        conn = self._get_connection()
        rows = conn.execute(
            "SELECT * FROM documents WHERE LOWER(title) = LOWER(?)",
            (title,),
        ).fetchall()
        return [self._row_to_document(r) for r in rows]

    async def search_metadata(self, query: str, limit: int = 20) -> list[Document]:
        """Search documents by substring match in title, source_path, and tags.

        Returns documents where the query appears in any identity field,
        ordered by source_path match first (most specific), then title,
        then tags. Case-insensitive.
        """
        with self._query_timer.measure("search_metadata"):
            return await self._run(self._search_metadata_sync, query, limit)

    def _search_metadata_sync(self, query: str, limit: int) -> list[Document]:
        conn = self._get_connection()
        pattern = f"%{query}%"
        rows = conn.execute(
            "SELECT * FROM documents "
            "WHERE source_path LIKE ? COLLATE NOCASE "
            "   OR title LIKE ? COLLATE NOCASE "
            "   OR tags LIKE ? COLLATE NOCASE "
            "ORDER BY "
            "  CASE WHEN source_path LIKE ? COLLATE NOCASE THEN 0 ELSE 1 END, "
            "  CASE WHEN title LIKE ? COLLATE NOCASE THEN 0 ELSE 1 END "
            "LIMIT ?",
            (pattern, pattern, pattern, pattern, pattern, limit),
        ).fetchall()
        return [self._row_to_document(r) for r in rows]

    async def search_abstracts(self, query: str, limit: int = 20) -> list[Document]:
        """Search documents by substring match in semantic_abstract.

        Returns documents whose semantic_abstract contains query terms
        (case-insensitive). Only returns documents that have a non-null
        abstract. Used by the abstract prefilter in the retrieval pipeline.
        """
        with self._query_timer.measure("search_abstracts"):
            return await self._run(self._search_abstracts_sync, query, limit)

    def _search_abstracts_sync(self, query: str, limit: int) -> list[Document]:
        conn = self._get_connection()
        pattern = f"%{query}%"
        rows = conn.execute(
            "SELECT * FROM documents WHERE semantic_abstract LIKE ? COLLATE NOCASE LIMIT ?",
            (pattern, limit),
        ).fetchall()
        return [self._row_to_document(r) for r in rows]

    # ------------------------------------------------------------------
    # Edge operations
    # ------------------------------------------------------------------

    async def insert_edge(self, edge: Edge) -> None:
        await self._run(self._insert_edge_sync, edge)

    def _insert_edge_sync(self, edge: Edge) -> None:
        conn = self._get_connection()
        self._exec_insert_edge(conn, edge)
        conn.commit()

    def _exec_insert_edge(self, conn: sqlite3.Connection, edge: Edge) -> None:
        """Issue the INSERT for an edge on the given connection without
        committing. Caller handles commit/rollback.
        """
        conn.execute(
            """INSERT INTO edges (
                id, source_id, target_id, edge_type, resolution_policy,
                source_valid_from_version, target_valid_from_version,
                valid_until_version, retracted_edge_id,
                created_at, notes, rationale
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                edge.id,
                edge.source_id,
                edge.target_id,
                edge.edge_type.value,
                edge.resolution_policy.value if edge.resolution_policy else None,
                edge.source_valid_from_version,
                edge.target_valid_from_version,
                edge.valid_until_version,
                edge.retracted_edge_id,
                edge.created_at.isoformat(),
                edge.notes,
                edge.rationale,
            ),
        )

    # ------------------------------------------------------------------
    # Compound atomic operations (BH-135, BH-136)
    # ------------------------------------------------------------------

    async def supersede_atomic(
        self,
        predecessor_id: str,
        predecessor_updates: dict,
        edge: Edge,
    ) -> Document | None:
        """Update the predecessor document and insert a supersedes edge in
        a single SQLite transaction. Either both writes commit or neither
        does. Used by LifecycleService.set_lifecycle for the supersede
        action so a mid-operation failure cannot leave the predecessor
        archived without the corresponding edge (BH-135).

        Returns the updated predecessor document.
        """
        return await self._run(
            self._supersede_atomic_sync, predecessor_id, predecessor_updates, edge
        )

    def _supersede_atomic_sync(
        self, predecessor_id: str, predecessor_updates: dict, edge: Edge
    ) -> Document | None:
        conn = self._get_connection()
        try:
            self._exec_update_document(conn, predecessor_id, predecessor_updates)
            self._exec_insert_edge(conn, edge)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        return self._get_document_sync(predecessor_id)

    async def insert_with_supersede_atomic(
        self,
        new_doc: Document,
        predecessor_id: str,
        predecessor_updates: dict,
        edge: Edge,
    ) -> tuple[Document, Document]:
        """Insert a new document, update the predecessor, and insert the
        supersedes edge in a single transaction. Used by IngestionService
        to prevent the orphan class where a successor record exists but
        the predecessor is still active (BH-136).

        Returns (new_doc, updated_predecessor).
        """
        return await self._run(
            self._insert_with_supersede_atomic_sync,
            new_doc,
            predecessor_id,
            predecessor_updates,
            edge,
        )

    def _insert_with_supersede_atomic_sync(
        self,
        new_doc: Document,
        predecessor_id: str,
        predecessor_updates: dict,
        edge: Edge,
    ) -> tuple[Document, Document]:
        conn = self._get_connection()
        try:
            self._exec_insert_document(conn, new_doc)
            self._exec_update_document(conn, predecessor_id, predecessor_updates)
            self._exec_insert_edge(conn, edge)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        inserted = self._get_document_sync(new_doc.id)
        updated_pred = self._get_document_sync(predecessor_id)
        return inserted, updated_pred

    async def get_edges_by_source(self, source_id: str, edge_type: str | None = None) -> list[Edge]:
        with self._query_timer.measure("get_edges_by_source"):
            return await self._run(self._get_edges_by_source_sync, source_id, edge_type)

    def _get_edges_by_source_sync(self, source_id: str, edge_type: str | None = None) -> list[Edge]:
        conn = self._get_connection()
        if edge_type:
            rows = conn.execute(
                "SELECT * FROM edges WHERE source_id = ? AND edge_type = ?",
                (source_id, edge_type),
            ).fetchall()
        else:
            rows = conn.execute("SELECT * FROM edges WHERE source_id = ?", (source_id,)).fetchall()
        return [self._row_to_edge(r) for r in rows]

    async def get_edges_by_target(self, target_id: str, edge_type: str | None = None) -> list[Edge]:
        with self._query_timer.measure("get_edges_by_target"):
            return await self._run(self._get_edges_by_target_sync, target_id, edge_type)

    def _get_edges_by_target_sync(self, target_id: str, edge_type: str | None = None) -> list[Edge]:
        conn = self._get_connection()
        if edge_type:
            rows = conn.execute(
                "SELECT * FROM edges WHERE target_id = ? AND edge_type = ?",
                (target_id, edge_type),
            ).fetchall()
        else:
            rows = conn.execute("SELECT * FROM edges WHERE target_id = ?", (target_id,)).fetchall()
        return [self._row_to_edge(r) for r in rows]

    async def get_supersedes_lineage(self, doc_id: str) -> list[str]:
        """Return doc_id and all supersedes-predecessors (unordered).

        Walks supersedes edges outbound (source=newer, target=older)
        recursively from doc_id. Inclusive of doc_id. Empty list if
        doc_id is not in the documents table (caller treats as missing).
        Callers treat the result as a set; order is not preserved.
        """
        with self._query_timer.measure("get_supersedes_lineage"):
            return await self._run(self._get_supersedes_lineage_sync, doc_id)

    def _get_supersedes_lineage_sync(self, doc_id: str) -> list[str]:
        conn = self._get_connection()
        exists = conn.execute("SELECT 1 FROM documents WHERE id = ?", (doc_id,)).fetchone()
        if exists is None:
            return []
        # UNION (not UNION ALL) so the recursive CTE terminates on
        # cycles and dedupes diamond fan-outs. Real vault data has
        # developed 2-cycles (paired reciprocal supersedes edges) that
        # would loop forever under UNION ALL; diamonds produced
        # exponential duplicate paths.
        sql = (
            "WITH RECURSIVE lineage(doc_id) AS ("
            "  SELECT ?"
            "  UNION"
            "  SELECT e.target_id "
            "  FROM edges e "
            "  INNER JOIN lineage l ON e.source_id = l.doc_id "
            "  WHERE e.edge_type = ?"
            ") "
            "SELECT doc_id FROM lineage"
        )
        rows = conn.execute(sql, (doc_id, EdgeType.SUPERSEDES.value)).fetchall()
        return [r["doc_id"] for r in rows]

    async def has_supersedes_successor(self, doc_id: str) -> bool:
        """True if any supersedes edge points at doc_id (doc is not chain head).

        Used by the merged_from write-time invariant: predecessor target
        must be a chain head (no newer version supersedes it).
        """
        with self._query_timer.measure("has_supersedes_successor"):
            return await self._run(self._has_supersedes_successor_sync, doc_id)

    def _has_supersedes_successor_sync(self, doc_id: str) -> bool:
        conn = self._get_connection()
        row = conn.execute(
            "SELECT 1 FROM edges WHERE edge_type = ? AND target_id = ? LIMIT 1",
            (EdgeType.SUPERSEDES.value, doc_id),
        ).fetchone()
        return row is not None

    async def has_supersedes_predecessor(self, doc_id: str) -> bool:
        """True if doc_id supersedes something (doc is not chain first/oldest).

        Used by the merged_from write-time invariant: successor source
        must be the first version of its chain (nothing older).
        """
        with self._query_timer.measure("has_supersedes_predecessor"):
            return await self._run(self._has_supersedes_predecessor_sync, doc_id)

    def _has_supersedes_predecessor_sync(self, doc_id: str) -> bool:
        conn = self._get_connection()
        row = conn.execute(
            "SELECT 1 FROM edges WHERE edge_type = ? AND source_id = ? LIMIT 1",
            (EdgeType.SUPERSEDES.value, doc_id),
        ).fetchone()
        return row is not None

    async def find_tombstone_candidates(self, lineage_ids: list[str]) -> list[str]:
        """Return edge_ids that should be tombstoned when a chain terminates.

        Selects non-policy-none edges whose source_id or target_id sits
        in `lineage_ids` and which are not already tombstoned. Caller
        passes the supersedes lineage of the terminal predecessor; each
        returned edge will receive `valid_until_version = predecessor_terminal`
        in the same transaction as the merged_from insert.
        """
        with self._query_timer.measure("find_tombstone_candidates"):
            return await self._run(self._find_tombstone_candidates_sync, lineage_ids)

    def _find_tombstone_candidates_sync(self, lineage_ids: list[str]) -> list[str]:
        if not lineage_ids:
            return []
        conn = self._get_connection()
        placeholders = ",".join("?" for _ in lineage_ids)
        # Policy-none edges (supersedes, retracts, merged_from) are
        # exempt: lineage and meta-facts must remain navigable.
        sql = (
            f"SELECT id FROM edges "  # noqa: S608 -- ResolutionPolicy.NONE.value is an enum constant; placeholders are ? markers
            f"WHERE valid_until_version IS NULL "
            f"AND (resolution_policy IS NULL "
            f"     OR resolution_policy != '{ResolutionPolicy.NONE.value}') "
            f"AND (source_id IN ({placeholders}) OR target_id IN ({placeholders}))"
        )
        rows = conn.execute(sql, [*lineage_ids, *lineage_ids]).fetchall()
        return [row["id"] for row in rows]

    async def merge_atomic(
        self,
        merged_from_edge: Edge,
        tombstone_edge_ids: list[str],
        tombstone_version: str,
    ) -> None:
        """Insert the merged_from edge and tombstone predecessor-downstream
        edges in a single SQLite transaction. Either both writes commit or
        neither does (CAS-ADR-017, Chunk 6, CR-032).
        """
        await self._run(
            self._merge_atomic_sync,
            merged_from_edge,
            tombstone_edge_ids,
            tombstone_version,
        )

    def _merge_atomic_sync(
        self,
        merged_from_edge: Edge,
        tombstone_edge_ids: list[str],
        tombstone_version: str,
    ) -> None:
        conn = self._get_connection()
        try:
            self._exec_insert_edge(conn, merged_from_edge)
            if tombstone_edge_ids:
                placeholders = ",".join("?" for _ in tombstone_edge_ids)
                conn.execute(
                    f"UPDATE edges SET valid_until_version = ? "  # noqa: S608 -- placeholders are ? markers; values are bound via parameters
                    f"WHERE id IN ({placeholders})",
                    [tombstone_version, *tombstone_edge_ids],
                )
            conn.commit()
        except Exception:
            conn.rollback()
            raise

    async def read_link_context(
        self, request: "LinkRequest", policy: ResolutionPolicy
    ) -> LinkReadContext:
        """Fetch all state needed to validate a LinkRequest in one submission.

        Collapses what used to be up to 7 separate executor submissions
        (document existence checks, anchor existence checks, two lineage
        walks, merged_from chain-position probes, tombstone scan) into
        one sync callable running on one thread against one connection.
        The service layer then performs domain validation in Python
        without further round-trips.

        Fields not relevant to the request's edge type / policy are
        populated with safe defaults and should not be inspected. See
        `LinkReadContext` for the field taxonomy.
        """
        with self._query_timer.measure("read_link_context"):
            return await self._run(self._read_link_context_sync, request, policy)

    def _read_link_context_sync(
        self, request: "LinkRequest", policy: ResolutionPolicy
    ) -> LinkReadContext:
        conn = self._get_connection()

        source_exists = (
            conn.execute("SELECT 1 FROM documents WHERE id = ?", (request.source_id,)).fetchone()
            is not None
        )

        if request.target_id is None:
            target_exists = True
        else:
            target_exists = (
                conn.execute(
                    "SELECT 1 FROM documents WHERE id = ?", (request.target_id,)
                ).fetchone()
                is not None
            )

        retracted_edge: Edge | None = None
        if request.edge_type == EdgeType.RETRACTS and request.retracted_edge_id is not None:
            row = conn.execute(
                "SELECT * FROM edges WHERE id = ?", (request.retracted_edge_id,)
            ).fetchone()
            if row is not None:
                retracted_edge = self._row_to_edge(row)

        source_anchor_exists = True
        if request.source_valid_from_version is not None:
            source_anchor_exists = (
                conn.execute(
                    "SELECT 1 FROM documents WHERE id = ?",
                    (request.source_valid_from_version,),
                ).fetchone()
                is not None
            )

        target_anchor_exists = True
        if request.target_valid_from_version is not None:
            target_anchor_exists = (
                conn.execute(
                    "SELECT 1 FROM documents WHERE id = ?",
                    (request.target_valid_from_version,),
                ).fetchone()
                is not None
            )

        # Source lineage: needed whenever a source-side anchor is present
        # (transitive_source / transitive_both / retracts).
        source_lineage: frozenset[str] = frozenset()
        if source_exists and request.source_valid_from_version is not None:
            source_lineage = frozenset(self._get_supersedes_lineage_sync(request.source_id))

        # Target lineage: needed for anchor validation of transitive_target /
        # transitive_both, and also for merged_from tombstone scanning.
        target_lineage: frozenset[str] = frozenset()
        need_target_lineage = False
        if (
            policy
            in (
                ResolutionPolicy.TRANSITIVE_TARGET,
                ResolutionPolicy.TRANSITIVE_BOTH,
            )
            and request.target_valid_from_version is not None
            and request.target_id is not None
        ):
            need_target_lineage = True
        if request.edge_type == EdgeType.MERGED_FROM and request.target_id is not None:
            need_target_lineage = True
        if need_target_lineage and target_exists:
            target_lineage = frozenset(self._get_supersedes_lineage_sync(request.target_id))

        has_sup_predecessor = False
        has_sup_successor = False
        tombstone_candidates: tuple[str, ...] = ()
        if request.edge_type == EdgeType.MERGED_FROM:
            has_sup_predecessor = (
                conn.execute(
                    "SELECT 1 FROM edges WHERE edge_type = ? AND source_id = ? LIMIT 1",
                    (EdgeType.SUPERSEDES.value, request.source_id),
                ).fetchone()
                is not None
            )
            if request.target_id is not None:
                has_sup_successor = (
                    conn.execute(
                        "SELECT 1 FROM edges WHERE edge_type = ? AND target_id = ? LIMIT 1",
                        (EdgeType.SUPERSEDES.value, request.target_id),
                    ).fetchone()
                    is not None
                )
            if target_lineage:
                tombstone_candidates = tuple(
                    self._find_tombstone_candidates_sync(list(target_lineage))
                )

        return LinkReadContext(
            source_exists=source_exists,
            target_exists=target_exists,
            retracted_edge=retracted_edge,
            source_lineage=source_lineage,
            target_lineage=target_lineage,
            source_anchor_exists=source_anchor_exists,
            target_anchor_exists=target_anchor_exists,
            has_sup_predecessor=has_sup_predecessor,
            has_sup_successor=has_sup_successor,
            tombstone_candidates=tombstone_candidates,
        )

    async def get_retracts_for_edges(self, edge_ids: list[str]) -> dict[str, list[Edge]]:
        """Return {retracted_edge_id: [retracts_edge, ...]} for the given ids.

        Batch lookup used by the resolver to decide whether candidate
        edges have been retracted. Only edges of type `retracts` whose
        `retracted_edge_id` is in the input set are returned. Edges with
        no retractions are omitted from the dict.
        """
        with self._query_timer.measure("get_retracts_for_edges"):
            return await self._run(self._get_retracts_for_edges_sync, edge_ids)

    def _get_retracts_for_edges_sync(self, edge_ids: list[str]) -> dict[str, list[Edge]]:
        if not edge_ids:
            return {}
        conn = self._get_connection()
        placeholders = ",".join("?" for _ in edge_ids)
        rows = conn.execute(
            f"SELECT * FROM edges "  # noqa: S608 -- placeholders are ? markers; values are bound via parameters
            f"WHERE edge_type = ? AND retracted_edge_id IN ({placeholders})",
            [EdgeType.RETRACTS.value, *edge_ids],
        ).fetchall()
        grouped: dict[str, list[Edge]] = {}
        for row in rows:
            edge = self._row_to_edge(row)
            grouped.setdefault(edge.retracted_edge_id, []).append(edge)
        return grouped

    async def get_edge(self, edge_id: str) -> Edge | None:
        """Get a production edge by ID. Returns None if not found."""
        with self._query_timer.measure("get_edge"):
            return await self._run(self._get_edge_sync, edge_id)

    def _get_edge_sync(self, edge_id: str) -> Edge | None:
        conn = self._get_connection()
        row = conn.execute("SELECT * FROM edges WHERE id = ?", (edge_id,)).fetchone()
        return self._row_to_edge(row) if row else None

    async def delete_edge(self, edge_id: str) -> bool:
        """Delete a production edge. Returns True if it existed."""
        return await self._run(self._delete_edge_sync, edge_id)

    def _delete_edge_sync(self, edge_id: str) -> bool:
        conn = self._get_connection()
        cursor = conn.execute("DELETE FROM edges WHERE id = ?", (edge_id,))
        conn.commit()
        return cursor.rowcount > 0

    # ------------------------------------------------------------------
    # Hash check (BE-007)
    # ------------------------------------------------------------------

    async def find_documents_by_hashes(self, hashes: list[str]) -> dict[str, str]:
        """Return {hash: document_id} for hashes that exist in the store."""
        with self._query_timer.measure("find_documents_by_hashes"):
            return await self._run(self._find_documents_by_hashes_sync, hashes)

    def _find_documents_by_hashes_sync(self, hashes: list[str]) -> dict[str, str]:
        if not hashes:
            return {}
        conn = self._get_connection()
        placeholders = ",".join("?" for _ in hashes)
        rows = conn.execute(
            f"SELECT source_content_hash, id FROM documents "  # noqa: S608 -- placeholders are ? markers; values are bound via parameters
            f"WHERE source_content_hash IN ({placeholders})",
            hashes,
        ).fetchall()
        return {row["source_content_hash"]: row["id"] for row in rows}

    # ------------------------------------------------------------------
    # Staging edge operations (BE-010 through BE-013)
    # ------------------------------------------------------------------

    async def list_staging_edges(self) -> list[StagingEdge]:
        with self._query_timer.measure("list_staging_edges"):
            return await self._run(self._list_staging_edges_sync)

    def _list_staging_edges_sync(self) -> list[StagingEdge]:
        conn = self._get_connection()
        rows = conn.execute("SELECT * FROM staging_edges ORDER BY created_at").fetchall()
        return [self._row_to_staging_edge(r) for r in rows]

    async def get_staging_edge(self, edge_id: str) -> StagingEdge | None:
        with self._query_timer.measure("get_staging_edge"):
            return await self._run(self._get_staging_edge_sync, edge_id)

    def _get_staging_edge_sync(self, edge_id: str) -> StagingEdge | None:
        conn = self._get_connection()
        row = conn.execute("SELECT * FROM staging_edges WHERE id = ?", (edge_id,)).fetchone()
        if row is None:
            return None
        return self._row_to_staging_edge(row)

    async def insert_staging_edge(self, edge: StagingEdge) -> None:
        await self._run(self._insert_staging_edge_sync, edge)

    def _insert_staging_edge_sync(self, edge: StagingEdge) -> None:
        conn = self._get_connection()
        conn.execute(
            """INSERT INTO staging_edges
            (id, source_id, target_id, edge_type, inference_evidence,
             confidence_tier, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                edge.id,
                edge.source_id,
                edge.target_id,
                edge.edge_type.value,
                edge.inference_evidence,
                edge.confidence_tier,
                edge.created_at.isoformat(),
            ),
        )
        conn.commit()

    async def delete_staging_edge(self, edge_id: str) -> bool:
        """Delete a staging edge. Returns True if it existed."""
        return await self._run(self._delete_staging_edge_sync, edge_id)

    def _delete_staging_edge_sync(self, edge_id: str) -> bool:
        conn = self._get_connection()
        cursor = conn.execute("DELETE FROM staging_edges WHERE id = ?", (edge_id,))
        conn.commit()
        return cursor.rowcount > 0

    async def count_staging_edges(self) -> int:
        with self._query_timer.measure("count_staging_edges"):
            return await self._run(self._count_staging_edges_sync)

    def _count_staging_edges_sync(self) -> int:
        conn = self._get_connection()
        row = conn.execute("SELECT COUNT(*) FROM staging_edges").fetchone()
        return row[0]

    # ------------------------------------------------------------------
    # Statistics (BE-003 through BE-005)
    # ------------------------------------------------------------------

    async def get_document_counts_by_field(self, field: str) -> dict[str, int]:
        """Return {value: count} for a given document column."""
        with self._query_timer.measure("get_document_counts_by_field"):
            return await self._run(self._get_document_counts_by_field_sync, field)

    def _get_document_counts_by_field_sync(self, field: str) -> dict[str, int]:
        # Allowlist of valid fields to prevent SQL injection
        allowed = {"lifecycle_status", "doc_type", "source_type", "pipeline_status", "project"}
        if field not in allowed:
            return {}
        conn = self._get_connection()
        rows = conn.execute(
            f"SELECT {field}, COUNT(*) as cnt FROM documents "  # noqa: S608 -- field is checked against the trusted `allowed` whitelist above
            f"WHERE {field} IS NOT NULL GROUP BY {field}"
        ).fetchall()
        return {row[0]: row[1] for row in rows}

    async def get_edge_counts_by_type(self) -> dict[str, int]:
        with self._query_timer.measure("get_edge_counts_by_type"):
            return await self._run(self._get_edge_counts_by_type_sync)

    def _get_edge_counts_by_type_sync(self) -> dict[str, int]:
        conn = self._get_connection()
        rows = conn.execute(
            "SELECT edge_type, COUNT(*) as cnt FROM edges GROUP BY edge_type"
        ).fetchall()
        return {row[0]: row[1] for row in rows}

    async def get_total_document_count(self) -> int:
        with self._query_timer.measure("get_total_document_count"):
            return await self._run(self._get_total_document_count_sync)

    def _get_total_document_count_sync(self) -> int:
        conn = self._get_connection()
        return conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0]

    async def get_total_edge_count(self) -> int:
        with self._query_timer.measure("get_total_edge_count"):
            return await self._run(self._get_total_edge_count_sync)

    def _get_total_edge_count_sync(self) -> int:
        conn = self._get_connection()
        return conn.execute("SELECT COUNT(*) FROM edges").fetchone()[0]

    async def get_last_ingestion_at(self) -> datetime | None:
        with self._query_timer.measure("get_last_ingestion_at"):
            return await self._run(self._get_last_ingestion_at_sync)

    def _get_last_ingestion_at_sync(self) -> datetime | None:
        conn = self._get_connection()
        row = conn.execute("SELECT MAX(created_at) FROM documents").fetchone()
        if row[0] is None:
            return None
        return datetime.fromisoformat(row[0])

    async def count_documents_by_pipeline_status(self, status: str) -> int:
        with self._query_timer.measure("count_documents_by_pipeline_status"):
            return await self._run(self._count_documents_by_pipeline_status_sync, status)

    def _count_documents_by_pipeline_status_sync(self, status: str) -> int:
        conn = self._get_connection()
        return conn.execute(
            "SELECT COUNT(*) FROM documents WHERE pipeline_status = ?",
            (status,),
        ).fetchone()[0]

    # ------------------------------------------------------------------
    # Pending metadata (BE-014)
    # ------------------------------------------------------------------

    async def list_pending_metadata_documents(self) -> list[Document]:
        """Return documents where metadata_confirmed is false."""
        with self._query_timer.measure("list_pending_metadata_documents"):
            return await self._run(self._list_pending_metadata_documents_sync)

    def _list_pending_metadata_documents_sync(self) -> list[Document]:
        conn = self._get_connection()
        rows = conn.execute("SELECT * FROM documents WHERE metadata_confirmed = 0").fetchall()
        return [self._row_to_document(r) for r in rows]

    # ------------------------------------------------------------------
    # Graph traversal
    # ------------------------------------------------------------------

    async def traverse(
        self,
        start_id: str,
        edge_type: str | None,
        direction: str,
        depth: int,
    ) -> list[dict]:
        """Recursive CTE traversal returning raw dicts for service-layer dedup."""
        with self._query_timer.measure("traverse"):
            return await self._run(self._traverse_sync, start_id, edge_type, direction, depth)

    def _traverse_sync(
        self,
        start_id: str,
        edge_type: str | None,
        direction: str,
        depth: int,
    ) -> list[dict]:
        conn = self._get_connection()

        # Build direction-specific column references:
        #   outbound: follow source_id -> target_id
        #   inbound:  follow target_id -> source_id
        #   both:     union of outbound and inbound
        def _edge_select(match_col: str, follow_col: str) -> str:
            """SQL fragment selecting edges in one direction.

            Args:
                match_col: Column to match against (find edges from this node).
                follow_col: Column to follow (the next node in traversal).
            """
            type_filter = " AND e.edge_type = ?" if edge_type else ""
            return (
                f"SELECT e.id AS edge_id, e.{follow_col} AS doc_id, "  # noqa: S608 -- match_col/follow_col are trusted internal column-name literals passed by the caller
                f"e.edge_type, e.created_at AS edge_created_at, "
                f"e.notes, e.rationale, e.source_id, e.target_id, "
                f"e.resolution_policy, e.source_valid_from_version, "
                f"e.target_valid_from_version, e.valid_until_version, "
                f"1 AS depth "
                f"FROM edges e "
                f"WHERE e.{match_col} = ?{type_filter}"
            )

        def _recursive_step(match_col: str, follow_col: str) -> str:
            type_filter = " AND e.edge_type = ?" if edge_type else ""
            return (
                f"SELECT e.id AS edge_id, e.{follow_col} AS doc_id, "  # noqa: S608 -- match_col/follow_col are trusted internal column-name literals passed by the caller
                f"e.edge_type, e.created_at AS edge_created_at, "
                f"e.notes, e.rationale, e.source_id, e.target_id, "
                f"e.resolution_policy, e.source_valid_from_version, "
                f"e.target_valid_from_version, e.valid_until_version, "
                f"t.depth + 1 AS depth "
                f"FROM edges e "
                f"INNER JOIN traversal t ON e.{match_col} = t.doc_id "
                f"WHERE t.depth < ?{type_filter}"
            )

        params: list = []

        if direction == "outbound":
            seed = _edge_select("source_id", "target_id")
            params += [start_id] + ([edge_type] if edge_type else [])
            recurse = _recursive_step("source_id", "target_id")
            params += [depth] + ([edge_type] if edge_type else [])
        elif direction == "inbound":
            seed = _edge_select("target_id", "source_id")
            params += [start_id] + ([edge_type] if edge_type else [])
            recurse = _recursive_step("target_id", "source_id")
            params += [depth] + ([edge_type] if edge_type else [])
        else:  # both
            seed_out = _edge_select("source_id", "target_id")
            seed_in = _edge_select("target_id", "source_id")
            seed = f"{seed_out} UNION ALL {seed_in}"
            params += [start_id] + ([edge_type] if edge_type else [])
            params += [start_id] + ([edge_type] if edge_type else [])
            recurse_out = _recursive_step("source_id", "target_id")
            recurse_in = _recursive_step("target_id", "source_id")
            recurse = f"{recurse_out} UNION ALL {recurse_in}"
            params += [depth] + ([edge_type] if edge_type else [])
            params += [depth] + ([edge_type] if edge_type else [])

        sql = (
            f"WITH RECURSIVE traversal AS (\n"
            f"  {seed}\n"
            f"  UNION ALL\n"
            f"  {recurse}\n"
            f")\n"
            f"SELECT t.*, "
            f"d.id AS d_id, d.title, d.lifecycle_status, d.source_type, "
            f"d.source_path, d.version_label, d.project, d.doc_type, d.tags, "
            f"d.document_date AS d_document_date, "
            f"d.source_modified_at AS d_source_modified_at "
            f"FROM traversal t "
            f"INNER JOIN documents d ON t.doc_id = d.id"
        )

        rows = conn.execute(sql, params).fetchall()
        results = []
        for row in rows:
            results.append(
                {
                    "edge_id": row["edge_id"],
                    "doc_id": row["doc_id"],
                    "edge_type": row["edge_type"],
                    "edge_created_at": row["edge_created_at"],
                    "notes": row["notes"],
                    "rationale": row["rationale"],
                    "source_id": row["source_id"],
                    "target_id": row["target_id"],
                    "resolution_policy": row["resolution_policy"],
                    "source_valid_from_version": row["source_valid_from_version"],
                    "target_valid_from_version": row["target_valid_from_version"],
                    "valid_until_version": row["valid_until_version"],
                    "depth": row["depth"],
                    "d_title": row["title"],
                    "d_lifecycle_status": row["lifecycle_status"],
                    "d_source_type": row["source_type"],
                    "d_source_path": row["source_path"],
                    "d_version_label": row["version_label"],
                    "d_project": row["project"],
                    "d_doc_type": row["doc_type"],
                    "d_tags": row["tags"],
                    "d_document_date": row["d_document_date"],
                    "d_source_modified_at": row["d_source_modified_at"],
                }
            )
        return results

    # ------------------------------------------------------------------
    # Chain walk
    # ------------------------------------------------------------------

    async def chain_walk(
        self,
        start_id: str,
        edge_type: str,
    ) -> list[dict]:
        """Walk an edge chain to both ends from start_id.

        Uses a recursive CTE following edges in both directions (outbound
        via source_id->target_id, inbound via target_id->source_id).
        Returns raw dicts with document metadata for all reachable nodes,
        including the start node itself.
        """
        with self._query_timer.measure("chain_walk"):
            return await self._run(self._chain_walk_sync, start_id, edge_type)

    def _chain_walk_sync(
        self,
        start_id: str,
        edge_type: str,
    ) -> dict:
        """Return chain documents and edges between them.

        Returns:
            dict with "documents" (list of doc dicts) and "edges"
            (list of {source_id, target_id} dicts).
        """
        conn = self._get_connection()

        # Recursive CTE: walk both directions from start_id, following
        # only edges of the specified type.  UNION (not UNION ALL)
        # prevents infinite loops on cycles.
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
                d.title, d.lifecycle_status, d.version_label,
                d.document_date
            FROM chain c
            INNER JOIN documents d ON c.doc_id = d.id
        """
        params = [start_id, edge_type, edge_type]
        doc_rows = conn.execute(sql, params).fetchall()

        documents = [
            {
                "doc_id": row["doc_id"],
                "title": row["title"],
                "lifecycle_status": row["lifecycle_status"],
                "version_label": row["version_label"],
                "document_date": row["document_date"],
            }
            for row in doc_rows
        ]

        # Fetch all edges of this type between chain members
        doc_ids = [d["doc_id"] for d in documents]
        if len(doc_ids) <= 1:
            return {"documents": documents, "edges": []}

        placeholders = ",".join("?" * len(doc_ids))
        edge_sql = (
            f"SELECT source_id, target_id FROM edges "  # noqa: S608 -- placeholders are ? markers; values are bound via parameters
            f"WHERE edge_type = ? "
            f"AND source_id IN ({placeholders}) "
            f"AND target_id IN ({placeholders})"
        )
        edge_params: list = [edge_type] + doc_ids + doc_ids
        edge_rows = conn.execute(edge_sql, edge_params).fetchall()

        edges = [
            {"source_id": row["source_id"], "target_id": row["target_id"]} for row in edge_rows
        ]

        return {"documents": documents, "edges": edges}

    # ------------------------------------------------------------------
    # User operations
    # ------------------------------------------------------------------

    async def insert_user(self, user: User) -> None:
        await self._run(self._insert_user_sync, user)

    def _insert_user_sync(self, user: User) -> None:
        conn = self._get_connection()
        conn.execute(
            "INSERT INTO users (id, display_name, type, created_at) VALUES (?, ?, ?, ?)",
            (user.id, user.display_name, user.type.value, user.created_at.isoformat()),
        )
        conn.commit()

    async def get_user(self, user_id: str) -> User | None:
        with self._query_timer.measure("get_user"):
            return await self._run(self._get_user_sync, user_id)

    def _get_user_sync(self, user_id: str) -> User | None:
        conn = self._get_connection()
        row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        if row is None:
            return None
        return self._row_to_user(row)

    async def get_user_by_display_name(self, display_name: str) -> User | None:
        with self._query_timer.measure("get_user_by_display_name"):
            return await self._run(self._get_user_by_display_name_sync, display_name)

    def _get_user_by_display_name_sync(self, display_name: str) -> User | None:
        conn = self._get_connection()
        row = conn.execute("SELECT * FROM users WHERE display_name = ?", (display_name,)).fetchone()
        if row is None:
            return None
        return self._row_to_user(row)

    async def list_users(self) -> list[User]:
        with self._query_timer.measure("list_users"):
            return await self._run(self._list_users_sync)

    def _list_users_sync(self) -> list[User]:
        conn = self._get_connection()
        rows = conn.execute("SELECT * FROM users").fetchall()
        return [self._row_to_user(r) for r in rows]

    # ------------------------------------------------------------------
    # Utility: query journal mode for BH-004
    # ------------------------------------------------------------------

    async def get_journal_mode(self) -> str:
        with self._query_timer.measure("get_journal_mode"):
            return await self._run(self._get_journal_mode_sync)

    def _get_journal_mode_sync(self) -> str:
        conn = self._get_connection()
        row = conn.execute("PRAGMA journal_mode;").fetchone()
        return row[0]

    # ------------------------------------------------------------------
    # Row -> model conversions
    # ------------------------------------------------------------------

    @staticmethod
    def _row_to_document(row: sqlite3.Row) -> Document:
        # model_construct bypasses Pydantic validation: storage may carry legacy
        # values (e.g. ISO-with-time document_date written before T-0026) that
        # the request-side validators now reject, and the repair workflow must
        # be able to read those records to fix them. Per the Typed-Alias
        # Boundary Conventions, storage does not police shape on read.
        return Document.model_construct(
            id=row["id"],
            title=row["title"],
            source_type=SourceType(row["source_type"]),
            source_path=row["source_path"],
            lifecycle_status=row["lifecycle_status"],
            version_label=row["version_label"],
            project=row["project"],
            tags=json.loads(row["tags"]) if row["tags"] else [],
            authority_scope=row["authority_scope"],
            doc_type=row["doc_type"],
            source_content_hash=row["source_content_hash"],
            adapter_version=row["adapter_version"],
            created_by=row["created_by"],
            created_at=datetime.fromisoformat(row["created_at"]),
            last_modified_by=row["last_modified_by"],
            updated_at=datetime.fromisoformat(row["updated_at"]),
            projected_at=(
                datetime.fromisoformat(row["projected_at"]) if row["projected_at"] else None
            ),
            indexed_at=(datetime.fromisoformat(row["indexed_at"]) if row["indexed_at"] else None),
            source_modified_at=(
                datetime.fromisoformat(row["source_modified_at"])
                if row["source_modified_at"]
                else None
            ),
            document_date=row["document_date"],
            semantic_abstract=row["semantic_abstract"],
            pipeline_status=PipelineStatus(row["pipeline_status"]),
            pipeline_error=row["pipeline_error"],
            tier3_metadata=(json.loads(row["tier3_metadata"]) if row["tier3_metadata"] else None),
            metadata_confirmed=bool(row["metadata_confirmed"]),
        )

    @staticmethod
    def _row_to_edge(row: sqlite3.Row) -> Edge:
        keys = row.keys()
        policy_value = row["resolution_policy"] if "resolution_policy" in keys else None
        return Edge(
            id=row["id"],
            source_id=row["source_id"],
            target_id=row["target_id"],
            edge_type=EdgeType(row["edge_type"]),
            resolution_policy=ResolutionPolicy(policy_value) if policy_value else None,
            source_valid_from_version=(
                row["source_valid_from_version"] if "source_valid_from_version" in keys else None
            ),
            target_valid_from_version=(
                row["target_valid_from_version"] if "target_valid_from_version" in keys else None
            ),
            valid_until_version=(
                row["valid_until_version"] if "valid_until_version" in keys else None
            ),
            retracted_edge_id=(row["retracted_edge_id"] if "retracted_edge_id" in keys else None),
            created_at=datetime.fromisoformat(row["created_at"]),
            notes=row["notes"],
            rationale=row["rationale"],
        )

    @staticmethod
    def _row_to_staging_edge(row: sqlite3.Row) -> StagingEdge:
        return StagingEdge(
            id=row["id"],
            source_id=row["source_id"],
            target_id=row["target_id"],
            edge_type=EdgeType(row["edge_type"]),
            inference_evidence=row["inference_evidence"],
            confidence_tier=row["confidence_tier"],
            created_at=datetime.fromisoformat(row["created_at"]),
        )

    @staticmethod
    def _row_to_user(row: sqlite3.Row) -> User:
        return User(
            id=row["id"],
            display_name=row["display_name"],
            type=UserType(row["type"]),
            created_at=datetime.fromisoformat(row["created_at"]),
        )
