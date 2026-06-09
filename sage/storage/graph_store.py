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
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, TypeVar

from sage.adapters.interfaces import GraphStore
from sage.instrumentation.timing import NULL_QUERY_TIMER, NullQueryTimer, QueryTimer
from sage.models.enums import (
    EdgeType,
    PipelineStatus,
    RationaleKind,
    ResolutionPolicy,
    SourceType,
    UserType,
)
from sage.models.graph_rows import EdgeQueryRow, LinkReadContext, OnConflict
from sage.models.schemas import Document, Edge, LinkRequest, StagingEdge, User
from sage.storage.migrations import (
    BACKFILL_PLAN,
    MIGRATION_PLAN,
    POST_MIGRATION_DDL,
    TABLES,
    TIER3_UNIQUE_INDEX_PREFIX,
    DuplicateEdgesPresentError,
    SchemaMigrationRequired,
    Tier3UniqueIndexBlockedError,
    Tier3UniqueViolation,
    pending_backfills,
    pending_migrations,
    tier3_unique_index_ddl,
    tier3_unique_index_drop_ddl,
    tier3_unique_index_name,
)

T = TypeVar("T")

# Identifies the unique-index IntegrityError raised when a caller
# attempts to insert an edge or staging edge whose natural-key triple
# already exists. SQLite's IntegrityError message embeds the index name;
# matching on the name keeps this distinct from other integrity failures
# (FK violations, NOT NULL violations).
_EDGES_UNIQ_INDEX = "idx_edges_uniq_natural_key"
_STAGING_EDGES_UNIQ_INDEX = "idx_staging_edges_uniq_natural_key"


def _is_unique_violation(exc: sqlite3.IntegrityError, index_name: str) -> bool:
    """Return True if exc is the unique-index violation on `index_name`.

    SQLite renders UNIQUE constraint failures as
    ``"UNIQUE constraint failed: <table>.<col1>, <table>.<col2>,..."``
    and includes the index name only via the column list. Match on the
    presence of "UNIQUE" plus the three natural-key columns to be robust
    against SQLite version differences in the message format.
    """
    msg = str(exc)
    if "UNIQUE constraint failed" not in msg:
        return False
    # Both unique indexes are on (source_id, target_id, edge_type); the
    # caller chooses which index to match against by table.
    table = "edges" if index_name == _EDGES_UNIQ_INDEX else "staging_edges"
    return (
        f"{table}.source_id" in msg and f"{table}.target_id" in msg and f"{table}.edge_type" in msg
    )


# SQLite renders partial expression-index UNIQUE violations as
# ``"UNIQUE constraint failed: index 'idx_name'"`` because there is no
# single column to name. The tier3 partial indexes are named
# ``idx_tier3_unique_<doc_type>_<field>``; the regex below extracts the
# tail token so the translator can recover (doc_type, field) by stripping
# the known doc_type prefix from the captured tail.
_TIER3_UNIQUE_INDEX_NAME_REGEX = re.compile(
    rf"index '({re.escape(TIER3_UNIQUE_INDEX_PREFIX)}[A-Za-z0-9_]+)'"
)


def _is_tier3_unique_violation(exc: sqlite3.IntegrityError) -> str | None:
    """Return the colliding tier3 index name or None.

    Format: ``idx_tier3_unique_<doc_type>_<field>``. The doc_type / field
    boundary is recovered by the caller by stripping the known doc_type
    prefix from the captured tail.
    """
    msg = str(exc)
    if TIER3_UNIQUE_INDEX_PREFIX not in msg:
        return None
    match = _TIER3_UNIQUE_INDEX_NAME_REGEX.search(msg)
    return match.group(1) if match else None


# Defense-in-depth gate for tier3 keys that get interpolated into the
# JSON path of a json_extract() expression. The service layer validates
# the same keys against the doc_type's metadata_schema; this regex is
# the last-line fence guaranteeing no caller-supplied string can break
# out of the path and inject SQL.
_TIER3_KEY_FORMAT = re.compile(r"^[A-Za-z0-9_]+$")


class SqliteGraphStore(GraphStore):
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
        self._closed: bool = False

    def _get_connection(self) -> sqlite3.Connection:
        """Return the thread-local connection, creating one if needed."""
        conn: sqlite3.Connection | None = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(str(self._db_path), check_same_thread=False)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL;")
            conn.execute("PRAGMA foreign_keys=ON;")
            # Bounded wait instead of an immediate SQLITE_BUSY when another
            # connection holds the database briefly, and the WAL-safe
            # durability/throughput setting. Both are per-connection.
            conn.execute("PRAGMA busy_timeout=30000;")
            conn.execute("PRAGMA synchronous=NORMAL;")
            self._local.conn = conn
            with self._all_connections_lock:
                self._all_connections.append(conn)
        return conn

    async def _run(self, fn: Callable[..., T], *args: Any) -> T:
        """Dispatch a sync callable to the connection-pool executor.

        Per CAS-ADR-036, the dispatch boundary is the barrier check for
        terminator semantics: once ``close()`` has run, every operation
        through this method raises ``RuntimeError``. A single check here
        covers the entire public surface; placing it before the executor
        lookup defeats the silent-degrade path where
        ``loop.run_in_executor(None, ...)`` would otherwise fall through
        to asyncio's default executor and transparently re-open a SQLite
        handle on a fresh worker thread.
        """
        if self._closed:
            raise RuntimeError("SqliteGraphStore is closed")
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
        # 2. Detect and (optionally) apply pending ALTER TABLE migrations
        # and data backfills. Surface both classes of pending
        # work together so the operator sees one consolidated message.
        pending = pending_migrations(conn, MIGRATION_PLAN)
        pending_bf = pending_backfills(conn, BACKFILL_PLAN)
        if (pending or pending_bf) and not migrate:
            details_parts: list[str] = []
            if pending:
                details_parts.append(
                    "ALTER: " + ", ".join(f"{m.table}.{m.column}" for m in pending)
                )
            if pending_bf:
                details_parts.append("backfill: " + ", ".join(b.name for b in pending_bf))
            raise SchemaMigrationRequired(
                f"SqliteGraphStore at {self._db_path} has pending schema work "
                f"({'; '.join(details_parts)}). Re-run the server with "
                f"--migrate to apply."
            )
        for m in pending:
            try:
                conn.execute(m.ddl)
            except sqlite3.OperationalError:
                # Defensive: should not occur given the table_info check,
                # but tolerate concurrent migration in tests.
                pass
        # 3. Run data backfills before indexes; backfills usually need the
        # new table itself (created in step 1) but not the post-migration
        # indexes. Idempotent: apply() functions tolerate re-running.
        # Re-detect after migrations applied so backfills that depend on
        # columns added in step 2 are picked up — the initial
        # detect runs before migrations and swallows column-missing
        # OperationalErrors as "not pending."
        if pending:
            pending_bf = pending_backfills(conn, BACKFILL_PLAN)
        for bf in pending_bf:
            bf.apply(conn)
        # 4. Create indexes (may reference columns added by migrations).
        # The unique natural-key indexes in POST_MIGRATION_DDL
        # raise IntegrityError when the underlying table contains
        # duplicates. Translate that into DuplicateEdgesPresentError
        # with a remediation pointer to scripts/dedup_edges.py.
        for ddl in POST_MIGRATION_DDL:
            try:
                conn.execute(ddl)
            except sqlite3.IntegrityError as exc:
                conn.rollback()
                if "idx_edges_uniq_natural_key" in ddl:
                    table = "edges"
                elif "idx_staging_edges_uniq_natural_key" in ddl:
                    table = "staging_edges"
                else:
                    raise
                raise DuplicateEdgesPresentError(
                    f"Cannot create UNIQUE index on {table}: the table "
                    f"contains duplicate rows on the natural-key triple "
                    f"(source_id, target_id, edge_type). Run "
                    f"`python -m scripts.dedup_edges --vault <id> --apply` "
                    f"to remove duplicates, then re-initialize. "
                    f"Underlying error: {exc}"
                ) from exc
        conn.commit()

    async def close(self) -> None:
        """Terminate the store per CAS-ADR-036 barrier semantics.

        Marks the store closed *before* releasing resources so any
        operation racing with close either completes against pre-close
        state or fails at the dispatch boundary against post-close
        state; never silently observes a half-released store.
        Idempotent: a second call returns cleanly.
        """
        if self._closed:
            return
        self._closed = True
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
        try:
            self._exec_insert_document(conn, doc)
            conn.commit()
        except sqlite3.IntegrityError as exc:
            conn.rollback()
            self._maybe_raise_tier3_violation(conn, exc, doc)
            raise

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
        self._sync_document_tags(conn, doc.id, doc.tags)

    def _maybe_raise_tier3_violation(
        self,
        conn: sqlite3.Connection,
        exc: sqlite3.IntegrityError,
        doc: Document,
        supersedes_id: str | None = None,
    ) -> None:
        """If `exc` is a tier3 partial UNIQUE violation, raise
        `Tier3UniqueViolation` with structured detail. Otherwise return
        and let the caller re-raise the original IntegrityError.

        The caller has already issued `conn.rollback()` before invoking
        this method, so the SELECT used to hydrate the existing-holder
        document id runs against the committed-snapshot view of the
        documents table.

        When `supersedes_id` is supplied, a collision against the
        designated predecessor is the supersession-lineage exception and
        does NOT raise — the partial UNIQUE filter would not have fired
        for that case in the first place (the predecessor is flipped
        before the successor is inserted), so reaching this branch means
        the collision is against a *different* row and is a real violation.
        """
        index_name = _is_tier3_unique_violation(exc)
        if index_name is None or doc.doc_type is None:
            return
        # Recover (doc_type, field) from the index name. The doc_type is
        # known from the inserted Document; the field is the remainder of
        # the name after stripping `idx_tier3_unique_<doc_type>_`.
        prefix = f"{TIER3_UNIQUE_INDEX_PREFIX}{doc.doc_type}_"
        if not index_name.startswith(prefix):
            return  # different doc_type's index fired — not our row
        field = index_name[len(prefix) :]
        if not _TIER3_KEY_FORMAT.match(field):
            return
        if not doc.tier3_metadata or field not in doc.tier3_metadata:
            return
        colliding_value = doc.tier3_metadata[field]
        # Look up the existing holder using the same predicate the partial
        # index uses, so we report the row the constraint actually matched.
        # The doc_type and field are interpolated character-for-character
        # because parameterized identifiers cannot be bound. Both have been
        # validated against `^[a-z][a-z0-9_]*$` (doc_type via vault-config
        # schema) and `^[A-Za-z0-9_]+$` (field via the regex above).
        sql = (
            f"SELECT id FROM documents "  # noqa: S608 -- field validated by _TIER3_KEY_FORMAT regex
            f"WHERE doc_type = ? AND is_chain_head = 1 "
            f"AND json_extract(tier3_metadata, '$.{field}') = ? "
            f"LIMIT 1"
        )
        row = conn.execute(sql, (doc.doc_type, colliding_value)).fetchone()
        if row is None:
            return  # holder vanished between the failed write and the lookup
        existing_id = row["id"] if hasattr(row, "keys") else row[0]
        if supersedes_id is not None and existing_id == supersedes_id:
            return  # caller asserted supersession of the colliding row
        raise Tier3UniqueViolation(
            doc_type=doc.doc_type,
            field=field,
            colliding_value=colliding_value,
            existing_document_id=existing_id,
        )

    @staticmethod
    def _sync_document_tags(conn: sqlite3.Connection, doc_id: str, tags: list[str] | None) -> None:
        """Rewrite the document_tags join rows for ``doc_id`` to match ``tags``.

        Keeps the derived join table in sync with the JSON
        column. Called from _exec_insert_document and from
        _exec_update_document when the update touches `tags`. Runs in
        the caller's transaction; the caller commits.
        """
        conn.execute("DELETE FROM document_tags WHERE document_id = ?", (doc_id,))
        if tags:
            conn.executemany(
                "INSERT OR IGNORE INTO document_tags (document_id, tag) VALUES (?, ?)",
                [(doc_id, t) for t in tags],
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
        # Capture the resolved tag list before in-place serialization
        # so the join-table sync sees the Python list, not the JSON string.
        new_tags: list[str] | None = updates["tags"] if "tags" in updates else None
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
        if new_tags is not None:
            self._sync_document_tags(conn, doc_id, new_tags)

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
        *,
        default_exclude_failed: bool = True,
    ) -> tuple[list[Document], int]:
        """Query documents with SQL predicates. Returns (docs, total_count).

        Supported filter keys: doc_type, project, lifecycle_status,
        pipeline_status, tags (list[str], AND semantics), document_ids (list[str]),
        tier3_metadata (dict[str, object], AND semantics, pushed into SQL
        as ``json_extract(tier3_metadata, '$.<key>') = ?`` predicates per
        ).

        ``default_exclude_failed`` (default ``True``) controls the BH-020
        default-exclude clause: when the caller does not pass an explicit
        ``pipeline_status`` filter, failed-pipeline documents are dropped.
         the retrieval service's catalog mode passes ``False``
        so that filter-only enumeration sees every document; scoring
        modes (semantic, keyword) and all non-retrieval callers inherit
        ``True``. An explicit ``pipeline_status`` filter is honoured at
        both flag settings.

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
                default_exclude_failed,
            )

    def _query_documents_sync(
        self,
        filters: dict[str, object] | None,
        limit: int,
        offset: int,
        sort_by: str | None,
        sort_order: str | None,
        default_exclude_failed: bool,
    ) -> tuple[list[Document], int]:
        conn = self._get_connection()
        where_clauses: list[str] = []
        params: list[object] = []

        # BH-020 default-exclude is mode-scoped at the service
        # boundary; catalog mode passes default_exclude_failed=False so
        # filter-only enumeration sees failed-pipeline docs. Scoring
        # modes and all non-retrieval callers inherit True.
        if default_exclude_failed and (not filters or "pipeline_status" not in filters):
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
                # Filter via the document_tags join table so the
                # query is index-driven instead of a full table scan
                # through json_each. AND-of-tags semantics preserved by
                # adding one EXISTS clause per requested tag.
                for tag in filters["tags"]:
                    where_clauses.append(
                        "EXISTS (SELECT 1 FROM document_tags "
                        "WHERE document_id = documents.id AND tag = ?)"
                    )
                    params.append(tag)
            if "tier3_metadata" in filters and filters["tier3_metadata"]:
                tier3_metadata = filters["tier3_metadata"]
                if not isinstance(tier3_metadata, dict):
                    raise ValueError(
                        f"tier3_metadata filter must be a dict, got {type(tier3_metadata).__name__}"
                    )
                for key, value in tier3_metadata.items():
                    if not isinstance(key, str) or not _TIER3_KEY_FORMAT.fullmatch(key):
                        raise ValueError(
                            f"tier3_metadata filter key {key!r} is not safe for "
                            "SQL interpolation; keys must match [A-Za-z0-9_]+"
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
    # Tier3 uniqueness (CAS-ADR-031)
    # ------------------------------------------------------------------

    async def ensure_tier3_unique_index(self, doc_type: str, field: str) -> None:
        """Idempotently create the partial UNIQUE index for (doc_type, field).

        Wraps SQLite's CREATE UNIQUE INDEX IF NOT EXISTS so the call is a
        no-op when the index already exists. Raises
        ``Tier3UniqueIndexBlockedError`` (RuntimeError subclass) when the
        documents table holds rows that violate the constraint — the
        operator must resolve the collisions before the index can be
        activated (CAS-ADR-031 §5).
        """
        await self._run(self._ensure_tier3_unique_index_sync, doc_type, field)

    def _ensure_tier3_unique_index_sync(self, doc_type: str, field: str) -> None:
        self._validate_tier3_identifier(doc_type, field)
        conn = self._get_connection()
        try:
            conn.execute(tier3_unique_index_ddl(doc_type, field))
            conn.commit()
        except sqlite3.IntegrityError as exc:
            conn.rollback()
            raise Tier3UniqueIndexBlockedError(
                doc_type=doc_type,
                field=field,
                message=(
                    f"Cannot create tier3 UNIQUE index on "
                    f"({doc_type!r}, {field!r}): the documents table holds "
                    f"chain heads with colliding values. Resolve the "
                    f"collisions (renumber, supersede, or "
                    f"archive-and-recreate) before retrying. "
                    f"Underlying error: {exc}"
                ),
            ) from exc

    async def drop_tier3_unique_index(self, doc_type: str, field: str) -> None:
        """Drop the partial UNIQUE index for (doc_type, field) if it exists."""
        await self._run(self._drop_tier3_unique_index_sync, doc_type, field)

    def _drop_tier3_unique_index_sync(self, doc_type: str, field: str) -> None:
        self._validate_tier3_identifier(doc_type, field)
        conn = self._get_connection()
        conn.execute(tier3_unique_index_drop_ddl(doc_type, field))
        conn.commit()

    async def tier3_unique_index_exists(self, doc_type: str, field: str) -> bool:
        """True if the partial UNIQUE index for (doc_type, field) is present."""
        return await self._run(self._tier3_unique_index_exists_sync, doc_type, field)

    def _tier3_unique_index_exists_sync(self, doc_type: str, field: str) -> bool:
        self._validate_tier3_identifier(doc_type, field)
        conn = self._get_connection()
        name = tier3_unique_index_name(doc_type, field)
        row = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'index' AND name = ?",
            (name,),
        ).fetchone()
        return row is not None

    async def find_chain_heads_with_tier3_value(
        self, doc_type: str, field: str
    ) -> list[tuple[object, list[str]]]:
        """Return chain heads grouped by `tier3_metadata.<field>` value.

        Output: list of ``(value, [doc_id,...])`` tuples for chain heads
        (`is_chain_head = 1`) of `doc_type` where the named field is not
        null. Used by the migration scan to detect cross-chain collisions
        before activating a `unique_keys` declaration.
        """
        return await self._run(self._find_chain_heads_with_tier3_value_sync, doc_type, field)

    def _find_chain_heads_with_tier3_value_sync(
        self, doc_type: str, field: str
    ) -> list[tuple[object, list[str]]]:
        self._validate_tier3_identifier(doc_type, field)
        conn = self._get_connection()
        rows = conn.execute(
            (
                "SELECT id, json_extract(tier3_metadata, '$." + field + "') AS value "
                "FROM documents "
                "WHERE doc_type = ? AND is_chain_head = 1 "
                "  AND json_extract(tier3_metadata, '$." + field + "') IS NOT NULL"
            ),
            (doc_type,),
        ).fetchall()
        grouped: dict[object, list[str]] = {}
        for row in rows:
            value = row["value"]
            grouped.setdefault(value, []).append(row["id"])
        return [(v, ids) for v, ids in grouped.items()]

    @staticmethod
    def _validate_tier3_identifier(doc_type: str, field: str) -> None:
        """Defense-in-depth fence for the tier3 identifiers interpolated
        into expression-index DDL and json_extract paths. doc_type is
        constrained by the vault-config schema to ``^[a-z][a-z0-9_]*$``;
        field names are constrained by the cross-field validator in
        sage.config to property names declared in metadata_schema (which
        themselves are JSON Schema property names). The regex below is
        the last-line fence.
        """
        if not re.match(r"^[a-z][a-z0-9_]*$", doc_type):
            raise ValueError(f"Invalid doc_type identifier for tier3 index DDL: {doc_type!r}")
        if not _TIER3_KEY_FORMAT.match(field):
            raise ValueError(f"Invalid tier3 field identifier for index DDL: {field!r}")

    # ------------------------------------------------------------------
    # Edge operations
    # ------------------------------------------------------------------

    async def insert_edge(self, edge: Edge, on_conflict: OnConflict = "raise") -> tuple[Edge, bool]:
        """Insert an edge. Return ``(stored_edge, created)``.

        Under ``on_conflict="raise"`` (default), a duplicate natural-key
        triple raises ``sqlite3.IntegrityError`` and the caller is
        expected to handle it (or, for atomic operations, allow the
        transaction to roll back).

        Under ``on_conflict="noop"``, a duplicate is converted
        to a no-op: the pre-existing edge is loaded and returned with
        ``created=False``; the new edge payload (including its rationale)
        is discarded. Idempotency rationale: the existing rationale is
        provenance; overwriting it on a re-insert destroys audit trail.
        """
        return await self._run(self._insert_edge_sync, edge, on_conflict)

    def _insert_edge_sync(self, edge: Edge, on_conflict: OnConflict) -> tuple[Edge, bool]:
        conn = self._get_connection()
        try:
            self._exec_insert_edge(conn, edge)
            conn.commit()
        except sqlite3.IntegrityError as exc:
            # Roll back the failed insert before re-raising or resolving the
            # conflict. This method owns its transaction (it commits on
            # success), so a leftover open transaction would hold the WAL write
            # lock until close() and block sibling pool connections -- matching
            # _insert_document_sync's discipline.
            conn.rollback()
            if on_conflict == "noop" and _is_unique_violation(exc, _EDGES_UNIQ_INDEX):
                existing = self._find_edge_by_natural_key_sync(
                    edge.source_id, edge.target_id, edge.edge_type.value
                )
                if existing is None:
                    # Race or matcher false-positive: the row vanished
                    # between INSERT and lookup. Surface the original.
                    raise
                return existing, False
            raise
        return edge, True

    def _exec_insert_edge(self, conn: sqlite3.Connection, edge: Edge) -> None:
        """Issue the INSERT for an edge on the given connection without
        committing. Caller handles commit/rollback.

        Callers using compound atomic operations
        (``supersede_atomic``, ``insert_with_supersede_atomic``,
        ``merge_atomic``) must NOT swallow ``sqlite3.IntegrityError``
        from this path. The transaction must roll back so the
        predecessor update is not committed without the corresponding
        edge insert. Single-shot inserters (``insert_edge``,
        ``insert_staging_edge``) handle the noop translation themselves.
        """
        conn.execute(
            """INSERT INTO edges (
                id, source_id, target_id, edge_type, resolution_policy,
                source_valid_from_version, target_valid_from_version,
                valid_until_version, retracted_edge_id,
                created_at, notes, rationale, rationale_kind,
                synced_from_version, synced_from_content_hash
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
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
                edge.rationale_kind.value,
                edge.synced_from_version,
                edge.synced_from_content_hash,
            ),
        )

    async def find_edge_by_natural_key(
        self, source_id: str, target_id: str | None, edge_type: str
    ) -> Edge | None:
        """Async wrapper around the natural-key edge lookup."""
        return await self._run(self._find_edge_by_natural_key_sync, source_id, target_id, edge_type)

    def _find_edge_by_natural_key_sync(
        self, source_id: str, target_id: str | None, edge_type: str
    ) -> Edge | None:
        """Return the edge with the given natural-key triple, or None.

        Used by the ``on_conflict="noop"`` path after a unique-index
        IntegrityError to hydrate the pre-existing edge for the caller.
        ``target_id=None`` is treated literally (matches NULL); under
        the constraint with SQLite NULL-distinct semantics this
        path is unreachable from the noop branch (NULLs never collide),
        but the helper handles it for completeness.
        """
        conn = self._get_connection()
        if target_id is None:
            row = conn.execute(
                "SELECT * FROM edges "
                "WHERE source_id = ? AND target_id IS NULL AND edge_type = ? "
                "LIMIT 1",
                (source_id, edge_type),
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT * FROM edges "
                "WHERE source_id = ? AND target_id = ? AND edge_type = ? "
                "LIMIT 1",
                (source_id, target_id, edge_type),
            ).fetchone()
        return self._row_to_edge(row) if row else None

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
        does. Used by LifecycleService._set_lifecycle for the supersede
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
        # Flip the predecessor's is_chain_head flag alongside the
        # caller's lifecycle updates. The supersedes-edge insert below
        # would also trigger the chain-head flip via the substrate trigger
        # (`trg_tier3_chain_head_on_supersedes`), but doing it explicitly
        # here keeps the storage layer self-contained and idempotent.
        updates_with_chain_head = {**predecessor_updates, "is_chain_head": 0}
        try:
            self._exec_update_document(conn, predecessor_id, updates_with_chain_head)
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
        # Flip the predecessor's is_chain_head flag and apply the
        # caller-supplied lifecycle updates BEFORE inserting the new
        # document. If the predecessor were still a chain head at successor-
        # insert time, the partial UNIQUE index on (doc_type, tier3 field)
        # would fire against the predecessor + successor pair (the
        # supersession-lineage exception per CAS-ADR-031 §3 is realized by
        # excluding non-chain-heads from the partial filter).
        updates_with_chain_head = {**predecessor_updates, "is_chain_head": 0}
        try:
            self._exec_update_document(conn, predecessor_id, updates_with_chain_head)
            self._exec_insert_document(conn, new_doc)
            self._exec_insert_edge(conn, edge)
            conn.commit()
        except sqlite3.IntegrityError as exc:
            conn.rollback()
            self._maybe_raise_tier3_violation(conn, exc, new_doc, supersedes_id=predecessor_id)
            raise
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

    async def query_edges(
        self,
        *,
        filters: dict[str, object] | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> tuple[list[EdgeQueryRow], int]:
        """Enumerate edges with SQL predicates. Returns (rows, total_count).

        Supported filter keys: ``source_id``, ``target_id``, ``edge_type``.
        All three are exact-match; multiple keys AND together. An empty
        or ``None`` filter returns all edges paginated by limit/offset.

        Each row carries the hydrated ``Edge`` plus a computed retraction
        envelope (``retracted_at``, ``retracted_by_edge_id``) built via
        LEFT JOIN against the earliest ``retracts``-type edge that
        disclaims the row. When multiple retracts edges target the same
        row, the earliest by ``created_at`` wins.

        Ordering: ``edges.created_at DESC`` (most recently created first).
        """
        with self._query_timer.measure("query_edges"):
            return await self._run(self._query_edges_sync, filters, limit, offset)

    def _query_edges_sync(
        self,
        filters: dict[str, object] | None,
        limit: int,
        offset: int,
    ) -> tuple[list[EdgeQueryRow], int]:
        conn = self._get_connection()
        where_clauses: list[str] = []
        params: list[object] = []

        if filters:
            if "source_id" in filters and filters["source_id"]:
                where_clauses.append("e.source_id = ?")
                params.append(filters["source_id"])
            if "target_id" in filters and filters["target_id"]:
                where_clauses.append("e.target_id = ?")
                params.append(filters["target_id"])
            if "edge_type" in filters and filters["edge_type"]:
                where_clauses.append("e.edge_type = ?")
                params.append(filters["edge_type"])

        where_sql = " AND ".join(where_clauses) if where_clauses else "1=1"

        # Total (unpaginated) count.
        count_row = conn.execute(
            f"SELECT COUNT(*) FROM edges e WHERE {where_sql}",  # noqa: S608 -- where_sql built from trusted internal builder; values are ? placeholders
            params,
        ).fetchone()
        total_count = count_row[0]

        # Paged enumeration. The LEFT JOIN against the windowed
        # retracts-subquery attaches the earliest disclaiming retracts
        # edge (if any) per row. The window is partitioned by
        # retracted_edge_id and ordered by created_at ASC, so rn=1 is
        # always the earliest disclaimer.
        sql = f"""
            SELECT
                e.*,
                r.id AS retracted_by_edge_id,
                r.created_at AS retracted_at_iso
            FROM edges e
            LEFT JOIN (
                SELECT
                    retracted_edge_id,
                    id,
                    created_at,
                    ROW_NUMBER() OVER (
                        PARTITION BY retracted_edge_id
                        ORDER BY created_at ASC
                    ) AS rn
                FROM edges
                WHERE edge_type = 'retracts'
                  AND retracted_edge_id IS NOT NULL
            ) r ON r.retracted_edge_id = e.id AND r.rn = 1
            WHERE {where_sql}
            ORDER BY e.created_at DESC
            LIMIT ? OFFSET ?
        """  # noqa: S608 -- where_sql built from trusted internal builder; values are ? placeholders
        rows = conn.execute(sql, [*params, limit, offset]).fetchall()

        result: list[EdgeQueryRow] = []
        for row in rows:
            edge = self._row_to_edge(row)
            retracted_at_iso = row["retracted_at_iso"]
            result.append(
                EdgeQueryRow(
                    edge=edge,
                    retracted_at=(
                        datetime.fromisoformat(retracted_at_iso) if retracted_at_iso else None
                    ),
                    retracted_by_edge_id=row["retracted_by_edge_id"],
                )
            )
        return result, total_count

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
        self, request: LinkRequest, policy: ResolutionPolicy
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
        self, request: LinkRequest, policy: ResolutionPolicy
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
        """Return {retracted_edge_id: [retracts_edge,...]} for the given ids.

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

    async def insert_staging_edge(
        self, edge: StagingEdge, on_conflict: OnConflict = "raise"
    ) -> tuple[StagingEdge, bool]:
        """Insert a staging edge. Return ``(stored_edge, created)``.

        Under ``on_conflict="raise"`` (default), a duplicate natural-key
        triple raises ``sqlite3.IntegrityError``.

        Under ``on_conflict="noop"``, a duplicate is converted
        to a no-op: the pre-existing staging edge is loaded and returned
        with ``created=False``. Used by batch_inference to make
        auto-inferred staging edges idempotent under re-ingest.
        """
        return await self._run(self._insert_staging_edge_sync, edge, on_conflict)

    def _insert_staging_edge_sync(
        self, edge: StagingEdge, on_conflict: OnConflict
    ) -> tuple[StagingEdge, bool]:
        conn = self._get_connection()
        try:
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
        except sqlite3.IntegrityError as exc:
            # Roll back the failed insert before re-raising or resolving the
            # conflict, so the store's transaction never stays open holding the
            # WAL write lock (see _insert_edge_sync).
            conn.rollback()
            if on_conflict == "noop" and _is_unique_violation(exc, _STAGING_EDGES_UNIQ_INDEX):
                existing = self._find_staging_edge_by_natural_key_sync(
                    edge.source_id, edge.target_id, edge.edge_type.value
                )
                if existing is None:
                    raise
                return existing, False
            raise
        return edge, True

    def _find_staging_edge_by_natural_key_sync(
        self, source_id: str, target_id: str, edge_type: str
    ) -> StagingEdge | None:
        """Return the staging edge with the given natural-key triple, or None."""
        conn = self._get_connection()
        row = conn.execute(
            "SELECT * FROM staging_edges "
            "WHERE source_id = ? AND target_id = ? AND edge_type = ? "
            "LIMIT 1",
            (source_id, target_id, edge_type),
        ).fetchone()
        return self._row_to_staging_edge(row) if row else None

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
        # outbound: follow source_id -> target_id
        # inbound: follow target_id -> source_id
        # both: union of outbound and inbound
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
                f"e.notes, e.rationale, e.rationale_kind, e.source_id, e.target_id, "
                f"e.resolution_policy, e.source_valid_from_version, "
                f"e.target_valid_from_version, e.valid_until_version, "
                f"e.retracted_edge_id, "
                f"e.synced_from_version, e.synced_from_content_hash, "
                f"1 AS depth "
                f"FROM edges e "
                f"WHERE e.{match_col} = ?{type_filter}"
            )

        def _recursive_step(match_col: str, follow_col: str) -> str:
            type_filter = " AND e.edge_type = ?" if edge_type else ""
            return (
                f"SELECT e.id AS edge_id, e.{follow_col} AS doc_id, "  # noqa: S608 -- match_col/follow_col are trusted internal column-name literals passed by the caller
                f"e.edge_type, e.created_at AS edge_created_at, "
                f"e.notes, e.rationale, e.rationale_kind, e.source_id, e.target_id, "
                f"e.resolution_policy, e.source_valid_from_version, "
                f"e.target_valid_from_version, e.valid_until_version, "
                f"e.retracted_edge_id, "
                f"e.synced_from_version, e.synced_from_content_hash, "
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
                    "rationale_kind": row["rationale_kind"],
                    "retracted_edge_id": row["retracted_edge_id"],
                    "source_id": row["source_id"],
                    "target_id": row["target_id"],
                    "resolution_policy": row["resolution_policy"],
                    "source_valid_from_version": row["source_valid_from_version"],
                    "target_valid_from_version": row["target_valid_from_version"],
                    "valid_until_version": row["valid_until_version"],
                    "synced_from_version": row["synced_from_version"],
                    "synced_from_content_hash": row["synced_from_content_hash"],
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
        # only edges of the specified type. UNION (not UNION ALL)
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

    async def list_provenance_edges(self, edge_types: list[str]) -> list[dict]:
        """Return active edges of the given types carrying synced_from_*.

        Detector enumeration helper. "Active" = ``valid_until_version IS
        NULL``. Returns raw dicts with the fields the detector needs to
        classify each row; no Pydantic round-trip because the detector
        does its own DriftEntry construction.
        """
        with self._query_timer.measure("list_provenance_edges"):
            return await self._run(self._list_provenance_edges_sync, edge_types)

    def _list_provenance_edges_sync(self, edge_types: list[str]) -> list[dict]:
        if not edge_types:
            return []
        conn = self._get_connection()
        placeholders = ",".join("?" * len(edge_types))
        sql = (
            "SELECT id, edge_type, source_id, target_id, "  # noqa: S608 -- placeholders are ? markers; values are bound via parameters
            "synced_from_version, synced_from_content_hash "
            "FROM edges WHERE valid_until_version IS NULL "
            f"AND edge_type IN ({placeholders})"
        )
        rows = conn.execute(sql, edge_types).fetchall()
        return [
            {
                "id": r["id"],
                "edge_type": r["edge_type"],
                "source_id": r["source_id"],
                "target_id": r["target_id"],
                "synced_from_version": r["synced_from_version"],
                "synced_from_content_hash": r["synced_from_content_hash"],
            }
            for r in rows
        ]

    async def head_with_hash_for_chain(
        self,
        target_id: str,
        edge_type: str = "supersedes",
    ) -> dict:
        """Return the head of target_id's chain plus a linearity signal.

        Used by `MaintenanceService.detect_drift` to look up the current
        canonical revision for each candidate edge in one round-trip.

        Returns a dict with keys:
            head_id: str | None
            head_content_hash: str | None
            head_version_label: str | None
            heads_count: int
            is_linear: bool

        Head identification: a chain node is a head iff no chain-member
        supersedes it (no `edge_type` edge whose target is the candidate).
        For a fork (heads_count > 1), `head_id` / `head_content_hash` /
        `head_version_label` are all None; the detector reports the edge
        as `staleness_basis=chain_nonlinear` and the operator follows up
        via `chain`.
        """
        with self._query_timer.measure("head_with_hash_for_chain"):
            return await self._run(self._head_with_hash_for_chain_sync, target_id, edge_type)

    def _head_with_hash_for_chain_sync(
        self,
        target_id: str,
        edge_type: str,
    ) -> dict:
        conn = self._get_connection()

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
            SELECT d.id AS head_id,
                   d.source_content_hash AS head_content_hash,
                   d.version_label       AS head_version_label
            FROM chain c
            INNER JOIN documents d ON c.doc_id = d.id
            WHERE NOT EXISTS (
                SELECT 1 FROM edges e
                INNER JOIN chain c2 ON e.source_id = c2.doc_id
                WHERE e.edge_type = ? AND e.target_id = d.id
            )
        """
        params = [target_id, edge_type, edge_type, edge_type]
        rows = conn.execute(sql, params).fetchall()
        heads_count = len(rows)

        if heads_count == 1:
            row = rows[0]
            return {
                "head_id": row["head_id"],
                "head_content_hash": row["head_content_hash"],
                "head_version_label": row["head_version_label"],
                "heads_count": 1,
                "is_linear": True,
            }
        return {
            "head_id": None,
            "head_content_hash": None,
            "head_version_label": None,
            "heads_count": heads_count,
            "is_linear": False,
        }

    # ------------------------------------------------------------------
    # User operations
    # ------------------------------------------------------------------

    async def insert_user(self, user: User) -> None:
        await self._run(self._insert_user_sync, user)

    def _insert_user_sync(self, user: User) -> None:
        conn = self._get_connection()
        conn.execute(
            "INSERT INTO users (id, display_name, user_type, created_at) VALUES (?, ?, ?, ?)",
            (user.id, user.display_name, user.user_type.value, user.created_at.isoformat()),
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

    async def get_busy_timeout(self) -> int:
        with self._query_timer.measure("get_busy_timeout"):
            return await self._run(self._get_busy_timeout_sync)

    def _get_busy_timeout_sync(self) -> int:
        conn = self._get_connection()
        row = conn.execute("PRAGMA busy_timeout;").fetchone()
        return row[0]

    async def get_synchronous(self) -> int:
        with self._query_timer.measure("get_synchronous"):
            return await self._run(self._get_synchronous_sync)

    def _get_synchronous_sync(self) -> int:
        conn = self._get_connection()
        row = conn.execute("PRAGMA synchronous;").fetchone()
        return row[0]

    # ------------------------------------------------------------------
    # Row -> model conversions
    # ------------------------------------------------------------------

    @staticmethod
    def _row_to_document(row: sqlite3.Row) -> Document:
        # model_construct bypasses Pydantic validation: storage may carry legacy
        # values (e.g. ISO-with-time document_date written before) that
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
        """Build an ``Edge`` from a ``sqlite3.Row``.

        Single owner of the row-dict -> ``Edge`` projection per the *CAS
        Projection-Point Audit Conventions* steering document (cas vault,
        doc_type=steering_document). The exhaustive-fields test
        ``test_row_to_edge_populates_every_edge_field`` in
        ``tests/sage/test_graph_store.py`` fails closed if a field is added
        to ``Edge`` but not wired through this factory.

        The duplicate CTE-row construction path at
        ``sage/services/graph_ops.py`` is an architecturally excluded
        site (BH-101: per-row ``model_validate`` cost on the traversal
        hot path); its parity guarantee with this canonical factory is
        the subject of.

        Defensive ``"<col>" in row.keys()`` guards for
        ``resolution_policy``, ``rationale_kind``, the three anchor
        columns, and ``retracted_edge_id`` accommodate CTE projections
        that strip optional columns from the row dict.
        """
        keys = row.keys()
        policy_value = row["resolution_policy"] if "resolution_policy" in keys else None
        rationale_kind_value = row["rationale_kind"] if "rationale_kind" in keys else "manual"
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
            rationale_kind=RationaleKind(rationale_kind_value),
            synced_from_version=(
                row["synced_from_version"] if "synced_from_version" in keys else None
            ),
            synced_from_content_hash=(
                row["synced_from_content_hash"] if "synced_from_content_hash" in keys else None
            ),
        )

    @staticmethod
    def _row_to_staging_edge(row: sqlite3.Row) -> StagingEdge:
        """Build a ``StagingEdge`` from a ``sqlite3.Row``.

        Single owner of the row-dict -> ``StagingEdge`` projection per
        the *CAS Projection-Point Audit Conventions* steering document
        (cas vault, doc_type=steering_document). The exhaustive-fields
        test ``test_row_to_staging_edge_populates_every_staging_edge_field``
        in ``tests/sage/test_graph_store.py`` fails closed if a field is
        added to ``StagingEdge`` but not wired through this factory.
        """
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
            user_type=UserType(row["user_type"]),
            created_at=datetime.fromisoformat(row["created_at"]),
        )
