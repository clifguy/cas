"""LanceDB ContentStore implementation.

Embedded columnar vector database with native FTS support.
Stores document chunks with embeddings for semantic and keyword search.
"""

import asyncio
import logging
from datetime import timedelta
from pathlib import Path

import lancedb
import pyarrow as pa
import pyarrow.parquet as pq

from sage.adapters.interfaces import (
    SYNTHETIC_HEADER_HEADING_PATH,
    Chunk,
    ContentStore,
    ContentStoreOptimizeSnapshot,
    SearchResult,
)
from sage.instrumentation.timing import NULL_QUERY_TIMER, NullQueryTimer, QueryTimer
from sage.storage.migrations import SchemaMigrationRequired

logger = logging.getLogger(__name__)

# Fixed schema for the chunks table.
# Vector dimension must match the embedding provider (768 for nomic-embed-text).
VECTOR_DIMENSIONS = 768

CHUNKS_TABLE = "chunks"

CHUNKS_SCHEMA = pa.schema(
    [
        pa.field("document_id", pa.utf8()),
        pa.field("heading_path", pa.utf8()),
        pa.field("content", pa.utf8()),
        pa.field("chunk_index", pa.int32()),
        pa.field("vector", pa.list_(pa.float32(), VECTOR_DIMENSIONS)),
        pa.field("doc_type", pa.utf8(), nullable=True),
        pa.field("lifecycle_status", pa.utf8(), nullable=True),
        pa.field("project", pa.utf8(), nullable=True),
    ]
)

# Columns that can be pre-filtered at query time.
_FILTERABLE_COLUMNS = {"doc_type", "document_id", "lifecycle_status", "project"}


class LanceDBContentStore(ContentStore):
    """Production content store backed by LanceDB.

    Data is persisted at brain_root on disk. The chunks table is created
    lazily on the first index_chunks call (AD-009). The FTS indexes are
    built once on the first populated write and maintained incrementally:
    the native backend covers newly added rows and honors deletes
    immediately, so no per-mutation rebuild is needed (AD-009).
    """

    def __init__(
        self,
        brain_root: str | Path,
        migrate: bool = False,
        *,
        query_timer: QueryTimer | NullQueryTimer = NULL_QUERY_TIMER,
    ) -> None:
        self._query_timer = query_timer
        # Serializes mutating operations to the single content table so
        # concurrent writers cannot race. Reads do not take this lock and
        # proceed on the event loop unimpeded. Per-instance; binds to the
        # running loop on first await.
        self._write_lock = asyncio.Lock()
        with self._query_timer.measure("initialize"):
            self._brain_root = Path(brain_root)
            self._brain_root.mkdir(parents=True, exist_ok=True)
            self._db = lancedb.connect(str(self._brain_root / "lancedb"))
            self._table_exists = CHUNKS_TABLE in self._db.list_tables().tables
            self._migrate_schema_if_needed(migrate=migrate)

    def pending_schema_columns(self) -> set[str]:
        """Return the set of columns that would be added by a migration.

        Empty set means no migration is pending. Read-only.
        """
        table = self._get_table()
        if table is None:
            return set()
        existing = set(table.schema.names)
        needed = set(CHUNKS_SCHEMA.names)
        return needed - existing

    def _get_table(self) -> lancedb.table.Table | None:
        """Return the chunks table, or None if it hasn't been created yet."""
        if not self._table_exists:
            # Recheck in case another call created it
            self._table_exists = CHUNKS_TABLE in self._db.list_tables().tables
        if self._table_exists:
            return self._db.open_table(CHUNKS_TABLE)
        return None

    def _ensure_table(self) -> lancedb.table.Table:
        """Return the chunks table, creating it if needed (AD-009)."""
        table = self._get_table()
        if table is not None:
            return table
        table = self._db.create_table(CHUNKS_TABLE, schema=CHUNKS_SCHEMA)
        self._table_exists = True
        logger.info("Created chunks table at %s", self._brain_root)
        return table

    def _migrate_schema_if_needed(self, *, migrate: bool) -> None:
        """Add missing metadata columns to an existing chunks table.

        When ``migrate`` is False and a migration is required, raises
        ``SchemaMigrationRequired`` rather than mutating the table.

        When ``migrate`` is True, materializes all rows, writes a parquet
        backup, then drops and recreates with the current schema. The
        backup is deleted only after successful recreation; if the process
        crashes mid-migration the parquet file survives for manual
        recovery, and a subsequent ``--migrate`` run will refuse to
        proceed until the operator removes it.
        """
        table = self._get_table()
        if table is None:
            return
        existing_names = set(table.schema.names)
        needed = set(CHUNKS_SCHEMA.names)
        missing = needed - existing_names
        if not missing:
            return

        if not migrate:
            raise SchemaMigrationRequired(
                f"LanceDB chunks table at {self._brain_root} is missing "
                f"columns {sorted(missing)}. Re-run the server with "
                f"--migrate to rebuild the table (destructive: drops and "
                f"recreates from a parquet backup)."
            )

        recovery_path = self._brain_root / "chunks_migration_backup.parquet"
        if recovery_path.exists():
            raise RuntimeError(
                f"A prior LanceDB schema migration appears to have failed: "
                f"{recovery_path} already exists. Inspect or remove this "
                f"backup before retrying --migrate so it is not overwritten."
            )

        # Materialize all rows before any destructive operation
        arrow_table = table.to_arrow()
        row_count = arrow_table.num_rows
        logger.info(
            "Migrating chunks table at %s: adding %s, %d rows to rewrite",
            self._brain_root,
            sorted(missing),
            row_count,
        )
        if row_count == 0:
            self._db.drop_table(CHUNKS_TABLE)
            self._table_exists = False
            return

        rows = arrow_table.to_pylist()
        for row in rows:
            for col in missing:
                row.setdefault(col, None)

        try:
            pq.write_table(arrow_table, recovery_path)
            self._db.drop_table(CHUNKS_TABLE)
            self._table_exists = False
            new_table = self._db.create_table(
                CHUNKS_TABLE,
                data=rows,
                schema=CHUNKS_SCHEMA,
            )
            self._table_exists = True
            self._ensure_fts_indexes(new_table)
            recovery_path.unlink(missing_ok=True)
            logger.info("Schema migration complete: %d rows migrated", len(rows))
        except Exception:
            logger.exception(
                "Schema migration failed. Recovery backup at %s",
                recovery_path,
            )
            raise

    @staticmethod
    def _build_where(filters: dict[str, str | list[str]]) -> str | None:
        """Build a SQL WHERE clause from a filters dict.

        Only columns in _FILTERABLE_COLUMNS are accepted to prevent
        injection via arbitrary column names. Values may be a single
        string (equality) or a list of strings (IN clause).
        """
        clauses = []
        for key, value in filters.items():
            if key not in _FILTERABLE_COLUMNS:
                continue
            if isinstance(value, list):
                escaped = ", ".join(f"'{_escape_sql(v)}'" for v in value)
                clauses.append(f"{key} IN ({escaped})")
            else:
                clauses.append(f"{key} = '{_escape_sql(value)}'")
        return " AND ".join(clauses) if clauses else None

    def _ensure_fts_indexes(self, table: lancedb.table.Table) -> None:
        """Create the content and heading_path FTS indexes if absent (AD-102).

        Two separate FTS indexes back BM25 search
        (``table.search(query, query_type="fts")``) over both the body
        content AND the heading_path tokens. The heading_path index lets
        agents find a section by its heading text alone (the equivalent of
        Word's Find on a heading) without needing to know document
        structure or call deterministic-mode lookups.

        The native FTS backend covers newly added rows via a scan of the
        unindexed delta and honors deletes immediately, so the indexes are
        built once and maintained incrementally rather than rebuilt after
        every mutation; ``table.optimize()`` folds the delta into the index
        on a maintenance cadence. Positions are not stored
        (``with_position=False``): only keyword/BM25 queries are ever
        issued, never phrase queries, so positional data would be dead
        weight that roughly doubles each index build.
        """
        indexed = {column for index in table.list_indices() for column in index.columns}
        for column in ("content", "heading_path"):
            if column in indexed:
                continue
            try:
                table.create_fts_index(
                    column,
                    use_tantivy=False,
                    with_position=False,
                    replace=False,
                )
            except Exception:
                # Index creation fails on an empty table; it is created on
                # the first write that adds rows.
                logger.debug("FTS index creation skipped for %s (likely empty table)", column)

    async def index_chunks(self, document_id: str, chunks: list[Chunk]) -> None:
        """Store embedded chunks for a document.

        Replaces any existing chunks for the same document_id (AD-025).
        The LanceDB add and the O(table size) FTS rebuild run off the
        event loop on a worker thread, serialized to the single content
        table by ``_write_lock`` so concurrent writers cannot race. A
        concurrent search on the loop is unaffected.
        """
        with self._query_timer.measure("index_chunks", params={"chunks": len(chunks)}):
            async with self._write_lock:
                await asyncio.to_thread(self._index_chunks_sync, document_id, chunks)

    def _index_chunks_sync(self, document_id: str, chunks: list[Chunk]) -> None:
        """Blocking body of index_chunks. Runs on a worker thread."""
        table = self._ensure_table()

        # Remove existing chunks for this document first (AD-025)
        try:
            table.delete(f"document_id = '{_escape_sql(document_id)}'")
        except Exception:  # noqa: S110 -- best-effort cleanup; absent rows are expected
            pass  # Table might be empty or document might not exist

        if not chunks:
            self._ensure_fts_indexes(table)
            return

        # Build rows as list of dicts for LanceDB
        rows = []
        for chunk in chunks:
            embedding = chunk.embedding or [0.0] * VECTOR_DIMENSIONS
            rows.append(
                {
                    "document_id": chunk.document_id,
                    "heading_path": chunk.heading_path,
                    "content": chunk.content,
                    "chunk_index": chunk.chunk_index,
                    "vector": embedding,
                    "doc_type": chunk.doc_type,
                    "lifecycle_status": chunk.lifecycle_status,
                    "project": chunk.project,
                }
            )

        table.add(rows)
        self._ensure_fts_indexes(table)

    async def replace_synthetic_header_chunk(self, document_id: str, chunk: Chunk) -> None:
        """Replace the synthetic document-header chunk for a document.

        Deletes any existing row for this document with
        ``heading_path == SYNTHETIC_HEADER_HEADING_PATH``, inserts the new
        header chunk, and rebuilds the FTS indexes. Body chunks are not
        touched.
        """
        with self._query_timer.measure("replace_synthetic_header_chunk"):
            async with self._write_lock:
                await asyncio.to_thread(
                    self._replace_synthetic_header_chunk_sync, document_id, chunk
                )

    def _replace_synthetic_header_chunk_sync(self, document_id: str, chunk: Chunk) -> None:
        """Blocking body of replace_synthetic_header_chunk. Worker thread."""
        table = self._ensure_table()

        doc_id_sql = _escape_sql(document_id)
        marker_sql = _escape_sql(SYNTHETIC_HEADER_HEADING_PATH)
        try:
            table.delete(f"document_id = '{doc_id_sql}' AND heading_path = '{marker_sql}'")
        except Exception:  # noqa: S110 -- best-effort cleanup; absent rows are expected
            pass

        embedding = chunk.embedding or [0.0] * VECTOR_DIMENSIONS
        row = {
            "document_id": chunk.document_id,
            "heading_path": chunk.heading_path,
            "content": chunk.content,
            "chunk_index": chunk.chunk_index,
            "vector": embedding,
            "doc_type": chunk.doc_type,
            "lifecycle_status": chunk.lifecycle_status,
            "project": chunk.project,
        }
        table.add([row])
        self._ensure_fts_indexes(table)

    async def count_chunks(self) -> int:
        """Return the total number of chunk rows across all documents.

        Returns 0 when the chunks table has not yet been created.
        """
        with self._query_timer.measure("count_chunks"):
            table = self._get_table()
            if table is None:
                return 0
            return table.count_rows()

    async def count_retained_versions(self) -> int:
        """Return the number of retained LanceDB dataset versions.

        Reads ``Table.list_versions()`` without mutating the store. Each
        write mints a dataset version (header / FTS rebuild included), so
        this rises monotonically with un-optimized churn. Returns 0 when
        the chunks table has not yet been created.
        """
        with self._query_timer.measure("count_retained_versions"):
            table = self._get_table()
            if table is None:
                return 0
            return len(table.list_versions())

    async def count_small_fragments(self) -> int:
        """Return the number of small (un-compacted) LanceDB fragments.

        Reads ``Table.stats()["fragment_stats"]["num_small_fragments"]``
        without mutating the store. Small fragments accumulate as small
        writes land and are merged by ``optimize``, so this rises with
        un-optimized churn while staying near zero on a healthy store.
        Returns 0 when the chunks table has not yet been created.
        """
        with self._query_timer.measure("count_small_fragments"):
            table = self._get_table()
            if table is None:
                return 0
            return table.stats()["fragment_stats"]["num_small_fragments"]

    async def measured_byte_size(self) -> int:
        """Return the recursive byte sum of the LanceDB directory.

        The on-disk footprint of the chunk store; 0 when nothing has been
        written yet. The canonical source for the dashboard's
        content-store size, read through the binding-agnostic port.
        """
        with self._query_timer.measure("measured_byte_size"):
            return self._lancedb_dir_bytes()

    def _lancedb_dir_bytes(self) -> int:
        """Sum of file sizes under the LanceDB directory (recursive walk).

        Backs both ``measured_byte_size`` and ``optimize``'s pre/post
        on-disk byte capture.
        """
        lancedb_dir = self._brain_root / "lancedb"
        if not lancedb_dir.exists():
            return 0
        return sum(f.stat().st_size for f in lancedb_dir.rglob("*") if f.is_file())

    async def optimize(self, cleanup_older_than: timedelta) -> ContentStoreOptimizeSnapshot:
        """Compact fragments and prune old dataset versions.

        Captures the chunks table's pre-optimize state (directory byte
        sum, Table.list_versions() length, Table.stats() fragment
        counts), calls Table.optimize(cleanup_older_than=...), then
        captures the post-optimize state and returns both. LanceDB's
        Table.optimize() return value is ignored; the snapshots are the
        caller-visible evidence of reclamation.

        cleanup_older_than is forwarded verbatim to LanceDB. The latest
        version is never removed regardless of the threshold.

        No-op when the chunks table has not yet been created (returns
        all-zero snapshot). delete_unverified is deliberately not
        exposed: the running SAGE process holds the dataset open, so
        LanceDB's safety floor (7-day age) must be respected.

        Runs off the event loop under ``_write_lock`` so compaction does
        not race a concurrent write to the table or freeze the loop.
        """
        async with self._write_lock:
            return await asyncio.to_thread(self._optimize_sync, cleanup_older_than)

    def _optimize_sync(self, cleanup_older_than: timedelta) -> ContentStoreOptimizeSnapshot:
        """Blocking body of optimize. Runs on a worker thread."""
        table = self._get_table()
        if table is None:
            return ContentStoreOptimizeSnapshot(
                pre_bytes=0,
                post_bytes=0,
                pre_versions=0,
                post_versions=0,
                pre_fragments=0,
                post_fragments=0,
                pre_small_fragments=0,
                post_small_fragments=0,
            )

        with self._query_timer.measure("optimize"):
            pre_bytes = self._lancedb_dir_bytes()
            pre_versions = len(table.list_versions())
            pre_stats = table.stats()
            pre_fragments = pre_stats["fragment_stats"]["num_fragments"]
            pre_small_fragments = pre_stats["fragment_stats"]["num_small_fragments"]

            table.optimize(cleanup_older_than=cleanup_older_than)

            post_bytes = self._lancedb_dir_bytes()
            post_versions = len(table.list_versions())
            post_stats = table.stats()
            post_fragments = post_stats["fragment_stats"]["num_fragments"]
            post_small_fragments = post_stats["fragment_stats"]["num_small_fragments"]

        return ContentStoreOptimizeSnapshot(
            pre_bytes=pre_bytes,
            post_bytes=post_bytes,
            pre_versions=pre_versions,
            post_versions=post_versions,
            pre_fragments=pre_fragments,
            post_fragments=post_fragments,
            pre_small_fragments=pre_small_fragments,
            post_small_fragments=post_small_fragments,
        )

    async def remove_document(self, document_id: str) -> None:
        """Remove all chunks for a document (AD-014, AD-015).

        Idempotent: removing a non-existent document is a no-op.
        """
        with self._query_timer.measure("remove_document"):
            async with self._write_lock:
                await asyncio.to_thread(self._remove_document_sync, document_id)

    def _remove_document_sync(self, document_id: str) -> None:
        """Blocking body of remove_document. Runs on a worker thread."""
        table = self._get_table()
        if table is None:
            return

        try:
            table.delete(f"document_id = '{_escape_sql(document_id)}'")
        except Exception:  # noqa: S110 -- best-effort cleanup; absent rows are expected
            pass  # No rows to delete is fine

        self._ensure_fts_indexes(table)

    async def update_chunk_metadata(
        self,
        document_id: str,
        metadata: dict[str, str | None],
    ) -> None:
        """Update metadata columns on all chunks for a document."""
        with self._query_timer.measure("update_chunk_metadata"):
            async with self._write_lock:
                await asyncio.to_thread(self._update_chunk_metadata_sync, document_id, metadata)

    def _update_chunk_metadata_sync(
        self,
        document_id: str,
        metadata: dict[str, str | None],
    ) -> None:
        """Blocking body of update_chunk_metadata. Runs on a worker thread."""
        table = self._get_table()
        if table is None:
            return

        updates = {k: v for k, v in metadata.items() if k in _FILTERABLE_COLUMNS}
        if not updates:
            return

        try:
            table.update(
                where=f"document_id = '{_escape_sql(document_id)}'",
                values=updates,
            )
        except Exception as exc:
            logger.warning("update_chunk_metadata failed for %s: %s", document_id, exc)

    async def search_semantic(
        self,
        query_embedding: list[float],
        limit: int = 10,
        filters: dict[str, str | list[str]] | None = None,
    ) -> list[SearchResult]:
        """Vector similarity search using cosine distance (AD-016, AD-017).

        Returns results ranked by descending cosine similarity.
        When filters are provided, only matching chunks are searched.
        """
        with self._query_timer.measure(
            "search_semantic",
            params={"limit": limit, "filtered": bool(filters)},
        ):
            table = self._get_table()
            if table is None:
                return []

            try:
                query = table.search(query_embedding, vector_column_name="vector").metric("cosine")
                if filters:
                    where = self._build_where(filters)
                    if where:
                        query = query.where(where)
                results = query.limit(limit).to_list()
            except Exception as exc:
                logger.warning("Semantic search failed: %s", exc)
                return []

            return [
                SearchResult(
                    document_id=row["document_id"],
                    heading_path=row["heading_path"],
                    content=row["content"],
                    # LanceDB returns _distance (lower = more similar for cosine);
                    # convert to similarity score (higher = more similar)
                    score=1.0 - row.get("_distance", 0.0),
                )
                for row in results
            ]

    async def search_bm25(
        self,
        query: str,
        limit: int = 10,
        filters: dict[str, str | list[str]] | None = None,
    ) -> list[SearchResult]:
        """BM25 keyword search using LanceDB native FTS (AD-018).

        Returns results ranked by relevance score, descending.
        When filters are provided, only matching chunks are searched.
        """
        with self._query_timer.measure(
            "search_bm25",
            params={"limit": limit, "filtered": bool(filters)},
        ):
            table = self._get_table()
            if table is None:
                return []

            try:
                search_query = table.search(query, query_type="fts")
                if filters:
                    where = self._build_where(filters)
                    if where:
                        search_query = search_query.where(where)
                results = search_query.limit(limit).to_list()
            except Exception as exc:
                logger.error("BM25 search failed: %s", exc)
                return []

            return [
                SearchResult(
                    document_id=row["document_id"],
                    heading_path=row["heading_path"],
                    content=row["content"],
                    score=row.get("_score", row.get("score", 0.0)),
                )
                for row in results
            ]

    async def get_chunks_by_heading_prefix(
        self, document_id: str, heading_prefix: str
    ) -> list[Chunk]:
        """Return chunks whose heading_path matches the prefix structurally (AD-020, AD-022).

        Matches exact heading or child headings via ' > ' separator.
        Does NOT match partial heading names (AD-022).
        """
        with self._query_timer.measure("get_chunks_by_heading_prefix"):
            table = self._get_table()
            if table is None:
                return []

            escaped_doc = _escape_sql(document_id)
            escaped_prefix = _escape_sql(heading_prefix)
            # Escape SQL LIKE wildcards in the prefix
            like_prefix = _escape_like(heading_prefix)

            # Exact match OR child match via ' > ' separator
            filter_expr = (
                f"document_id = '{escaped_doc}' AND "
                f"(heading_path = '{escaped_prefix}' OR "
                f"heading_path LIKE '{like_prefix} > %')"
            )

            try:
                results = table.search().where(filter_expr).to_list()
            except Exception as exc:
                logger.warning("Heading prefix query failed: %s", exc)
                return []

            chunks = [
                Chunk(
                    document_id=row["document_id"],
                    heading_path=row["heading_path"],
                    content=row["content"],
                    embedding=row["vector"] if "vector" in row else None,
                    chunk_index=row["chunk_index"],
                )
                for row in results
            ]
            chunks.sort(key=lambda c: c.chunk_index)
            return chunks

    async def get_heading_paths(self, document_id: str) -> list[str]:
        """Return distinct heading paths for a document in document order."""
        with self._query_timer.measure("get_heading_paths"):
            table = self._get_table()
            if table is None:
                return []

            escaped_doc = _escape_sql(document_id)
            try:
                rows = (
                    table.search()
                    .where(f"document_id = '{escaped_doc}'")
                    .select(["heading_path", "chunk_index"])
                    .to_list()
                )
            except Exception as exc:
                logger.warning("get_heading_paths failed: %s", exc)
                return []

            rows.sort(key=lambda r: r["chunk_index"])
            seen: set[str] = set()
            paths: list[str] = []
            for row in rows:
                hp = row["heading_path"]
                # Exclude the synthetic header chunk marker so it
                # does not appear in user-visible "available headings" lists
                # surfaced by HeadingNotFoundError.
                if hp == SYNTHETIC_HEADER_HEADING_PATH:
                    continue
                if hp not in seen:
                    seen.add(hp)
                    paths.append(hp)
            return paths

    async def has_chunks(self, document_id: str) -> bool:
        """Return True if at least one chunk exists for the document (AD-068)."""
        with self._query_timer.measure("has_chunks"):
            table = self._get_table()
            if table is None:
                return False

            escaped_doc = _escape_sql(document_id)
            try:
                rows = (
                    table.search()
                    .where(f"document_id = '{escaped_doc}'")
                    .select(["document_id"])
                    .limit(1)
                    .to_list()
                )
                return len(rows) > 0
            except Exception as exc:
                logger.warning("has_chunks failed: %s", exc)
                return False

    async def get_all_chunks(self, document_id: str) -> list[Chunk]:
        """Return all chunks for a document in document order (AD-011, AD-013)."""
        with self._query_timer.measure("get_all_chunks"):
            table = self._get_table()
            if table is None:
                return []

            escaped_doc = _escape_sql(document_id)

            try:
                results = table.search().where(f"document_id = '{escaped_doc}'").to_list()
            except Exception as exc:
                logger.warning("get_all_chunks failed: %s", exc)
                return []

            chunks = [
                Chunk(
                    document_id=row["document_id"],
                    heading_path=row["heading_path"],
                    content=row["content"],
                    embedding=row["vector"] if "vector" in row else None,
                    chunk_index=row["chunk_index"],
                    doc_type=row.get("doc_type"),
                    lifecycle_status=row.get("lifecycle_status"),
                    project=row.get("project"),
                )
                for row in results
            ]
            chunks.sort(key=lambda c: c.chunk_index)
            return chunks


def _escape_sql(value: str) -> str:
    """Escape single quotes for SQL string literals (AD-024)."""
    return value.replace("'", "''")


def _escape_like(value: str) -> str:
    """Escape SQL LIKE special characters and single quotes (AD-024).

    Escapes %, _, and ' to prevent incorrect matches or injection.
    """
    value = value.replace("'", "''")
    value = value.replace("%", "\\%")
    value = value.replace("_", "\\_")
    return value
