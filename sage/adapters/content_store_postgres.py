"""Postgres binding for the ContentStore port (CAS-ADR-042).

A ``ContentStore`` implementation on Postgres: semantic search via pgvector
(cosine HNSW), keyword search via native ``ts_rank`` over a generated
``tsvector`` column, and filters pushed down to SQL ``WHERE``. The layer above
the port (RRF fusion, salience/abstract/metadata boosts) is binding-invariant,
so it runs on this binding unchanged.

Bloat and size signals are read from real Postgres internals
(``pgstattuple`` for dead-tuple and free-space accounting,
``pg_total_relation_size`` for the on-disk footprint) and ``optimize`` reclaims
via ``VACUUM (FULL, ANALYZE)``, so the dashboard's content-store indicators stay
substrate-agnostic.

The store operates on the unqualified ``chunks`` table and relies on the pool's
``search_path`` to select the active vault's schema; each vault binds its own
``search_path``-scoped pool. The pool is expected to be pgvector-registered
(``register_vector_async`` runs per connection), so embeddings round-trip as
plain Python lists.
"""

from __future__ import annotations

import re
from datetime import timedelta
from typing import TYPE_CHECKING

from sage.adapters.interfaces import (
    SYNTHETIC_HEADER_HEADING_PATH,
    Chunk,
    ContentStore,
    ContentStoreOptimizeSnapshot,
    SearchResult,
)
from sage.instrumentation.timing import NULL_QUERY_TIMER, NullQueryTimer, QueryTimer
from sage.storage.postgres.schema import EMBEDDING_DIM, TEXT_SEARCH_CONFIG

if TYPE_CHECKING:  # pragma: no cover - typing only
    from psycopg import AsyncConnection
    from psycopg_pool import AsyncConnectionPool

# Chunk-level columns the port accepts as pre-filter predicates. Filter keys
# outside this allowlist are ignored, and only these fixed identifiers are ever
# interpolated into SQL -- predicate values are always parameterized.
_FILTERABLE_COLUMNS: tuple[str, ...] = ("doc_type", "document_id", "lifecycle_status", "project")

# Metadata columns update_chunk_metadata may touch (content, hence tsv, is never
# rewritten here).
_METADATA_COLUMNS: tuple[str, ...] = ("doc_type", "lifecycle_status", "project")

_INSERT_COLUMNS = (
    "document_id, heading_path, content, chunk_index, "
    "embedding, doc_type, lifecycle_status, project"
)
_INSERT_SQL = (
    f"INSERT INTO chunks ({_INSERT_COLUMNS}) "  # noqa: S608 -- fixed column constant
    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)"
)

_SELECT_CHUNK_COLUMNS = (
    "document_id, heading_path, content, chunk_index, doc_type, lifecycle_status, project"
)


# A tsquery renders as quoted lexemes joined by operators, with ``!`` marking a
# negation: ``'alpha' & !'beta'``. Embedded quotes are doubled. Only the
# un-negated lexemes are terms the caller must supply.
_TSQUERY_LEXEME = re.compile(r"(!\s*)?'((?:[^']|'')*)'")


class PostgresContentStore(ContentStore):
    """ContentStore backed by Postgres (pgvector + native ts_rank)."""

    def __init__(
        self,
        pool: AsyncConnectionPool,
        *,
        query_timer: QueryTimer | NullQueryTimer = NULL_QUERY_TIMER,
    ) -> None:
        self._pool = pool
        self._query_timer = query_timer

    # -- filter pushdown ----------------------------------------------------

    @staticmethod
    def _build_where(
        filters: dict[str, str | list[str]] | None,
    ) -> tuple[str | None, list[object]]:
        """Translate the filter dict into a parameterized WHERE fragment.

        Returns ``(clause_without_WHERE, params)`` or ``(None, [])`` when no
        recognized predicate is present. A scalar maps to ``col = %s``; a list
        maps to ``col = ANY(%s)``. Column names come from the fixed allowlist,
        so nothing caller-supplied is interpolated.
        """
        if not filters:
            return None, []
        clauses: list[str] = []
        params: list[object] = []
        for col in _FILTERABLE_COLUMNS:
            if col not in filters:
                continue
            value = filters[col]
            if isinstance(value, list):
                clauses.append(f"{col} = ANY(%s)")
                params.append(list(value))
            else:
                clauses.append(f"{col} = %s")
                params.append(value)
        if not clauses:
            return None, []
        return " AND ".join(clauses), params

    # -- write paths --------------------------------------------------------

    @staticmethod
    def _chunk_row(chunk: Chunk) -> tuple[object, ...]:
        embedding = chunk.embedding if chunk.embedding is not None else [0.0] * EMBEDDING_DIM
        return (
            chunk.document_id,
            chunk.heading_path,
            chunk.content,
            chunk.chunk_index,
            embedding,
            chunk.doc_type,
            chunk.lifecycle_status,
            chunk.project,
        )

    async def index_chunks(self, document_id: str, chunks: list[Chunk]) -> None:
        """Replace all chunks for a document (delete-then-insert, atomic)."""
        with self._query_timer.measure("index_chunks", params={"chunks": len(chunks)}):
            async with self._pool.connection() as conn, conn.transaction():
                await conn.execute("DELETE FROM chunks WHERE document_id = %s", (document_id,))
                if chunks:
                    async with conn.cursor() as cur:
                        await cur.executemany(_INSERT_SQL, [self._chunk_row(c) for c in chunks])

    async def replace_synthetic_header_chunk(self, document_id: str, chunk: Chunk) -> None:
        """Swap only the synthetic header chunk; body chunks are untouched."""
        with self._query_timer.measure("replace_synthetic_header_chunk"):
            async with self._pool.connection() as conn, conn.transaction():
                await conn.execute(
                    "DELETE FROM chunks WHERE document_id = %s AND heading_path = %s",
                    (document_id, SYNTHETIC_HEADER_HEADING_PATH),
                )
                await conn.execute(_INSERT_SQL, self._chunk_row(chunk))

    async def remove_document(self, document_id: str) -> None:
        """Remove all chunks for a document (idempotent)."""
        with self._query_timer.measure("remove_document"):
            async with self._pool.connection() as conn:
                await conn.execute("DELETE FROM chunks WHERE document_id = %s", (document_id,))

    async def update_chunk_metadata(
        self,
        document_id: str,
        metadata: dict[str, str | None],
    ) -> None:
        """Update metadata columns on all of a document's chunks."""
        with self._query_timer.measure("update_chunk_metadata"):
            cols = [c for c in _METADATA_COLUMNS if c in metadata]
            if not cols:
                return
            set_clause = ", ".join(f"{c} = %s" for c in cols)
            params: list[object] = [metadata[c] for c in cols]
            params.append(document_id)
            async with self._pool.connection() as conn:
                await conn.execute(
                    f"UPDATE chunks SET {set_clause} WHERE document_id = %s",  # noqa: S608
                    params,
                )

    # -- read paths ---------------------------------------------------------

    async def search_semantic(
        self,
        query_embedding: list[float],
        limit: int = 10,
        filters: dict[str, str | list[str]] | None = None,
    ) -> list[SearchResult]:
        """Vector similarity search; score is ``1 - cosine_distance``."""
        with self._query_timer.measure(
            "search_semantic", params={"limit": limit, "filtered": bool(filters)}
        ):
            where, where_params = self._build_where(filters)
            sql = (
                "SELECT document_id, heading_path, content, "
                "1 - (embedding <=> %s::vector) AS score FROM chunks"
            )
            params: list[object] = [query_embedding]
            if where:
                sql += f" WHERE {where}"
                params += where_params
            sql += " ORDER BY embedding <=> %s::vector LIMIT %s"
            params.append(query_embedding)
            params.append(limit)
            rows = await self._fetchall(sql, params)
            return [self._row_to_result(r) for r in rows]

    async def search_bm25(
        self,
        query: str,
        limit: int = 10,
        filters: dict[str, str | list[str]] | None = None,
    ) -> list[SearchResult]:
        """Keyword search via native ``ts_rank`` over the generated tsvector.

        The tsvector covers heading_path (weight A) and content (weight D), so
        a term present only in a heading is findable.
        """
        with self._query_timer.measure(
            "search_bm25", params={"limit": limit, "filtered": bool(filters)}
        ):
            if not query or not query.strip():
                return []
            where, where_params = self._build_where(filters)
            sql = (
                "SELECT document_id, heading_path, content, ts_rank(tsv, q) AS score "  # noqa: S608
                f"FROM chunks, websearch_to_tsquery('{TEXT_SEARCH_CONFIG}', %s) AS q "
                "WHERE tsv @@ q"
            )
            params: list[object] = [query]
            if where:
                sql += f" AND {where}"
                params += where_params
            sql += " ORDER BY score DESC LIMIT %s"
            params.append(limit)
            rows = await self._fetchall(sql, params)
            return [self._row_to_result(r) for r in rows]

    async def parse_keyword_query(self, query: str) -> list[str]:
        """The lexemes a keyword query requires, as the text-search config parses it.

        ``search_bm25`` builds its query with ``websearch_to_tsquery``, which
        joins bare terms with AND -- so a chunk matches only if it carries every
        lexeme. Reporting those lexemes lets a caller see why a query matched
        nothing, which the raw query text cannot tell them: stopwords are
        dropped and the rest are stemmed, so the terms actually required are
        neither the words typed nor a whitespace split of them.

        Negated terms (``-word``) are excluded from the result: they constrain
        the match but are not something the caller must supply.

        Returns an empty list for a blank query, matching ``search_bm25``.
        """
        with self._query_timer.measure("parse_keyword_query"):
            if not query or not query.strip():
                return []
            rows = await self._fetchall(
                f"SELECT websearch_to_tsquery('{TEXT_SEARCH_CONFIG}', %s)::text",  # noqa: S608
                [query],
            )
            rendered = rows[0][0] if rows and rows[0][0] else ""
            return [
                lexeme.replace("''", "'")
                for negated, lexeme in _TSQUERY_LEXEME.findall(rendered)
                if not negated
            ]

    async def get_chunks_by_heading_prefix(
        self, document_id: str, heading_prefix: str
    ) -> list[Chunk]:
        """Return chunks at the heading or any child heading, document order."""
        with self._query_timer.measure("get_chunks_by_heading_prefix"):
            child_pattern = self._escape_like(heading_prefix) + " > %"
            rows = await self._fetchall(
                f"SELECT {_SELECT_CHUNK_COLUMNS} FROM chunks "  # noqa: S608 -- fixed column constant
                "WHERE document_id = %s AND (heading_path = %s OR heading_path LIKE %s) "
                "ORDER BY chunk_index",
                (document_id, heading_prefix, child_pattern),
            )
            return [self._row_to_chunk(r) for r in rows]

    async def get_heading_paths(self, document_id: str) -> list[str]:
        """Return distinct heading paths in document order (marker excluded)."""
        with self._query_timer.measure("get_heading_paths"):
            rows = await self._fetchall(
                "SELECT heading_path FROM chunks WHERE document_id = %s AND heading_path <> %s "
                "GROUP BY heading_path ORDER BY MIN(chunk_index)",
                (document_id, SYNTHETIC_HEADER_HEADING_PATH),
            )
            return [r[0] for r in rows]

    async def has_chunks(self, document_id: str) -> bool:
        """Return True if at least one chunk exists for the document."""
        with self._query_timer.measure("has_chunks"):
            rows = await self._fetchall(
                "SELECT 1 FROM chunks WHERE document_id = %s LIMIT 1", (document_id,)
            )
            return bool(rows)

    async def get_all_chunks(self, document_id: str) -> list[Chunk]:
        """Return all chunks for a document in document order."""
        with self._query_timer.measure("get_all_chunks"):
            rows = await self._fetchall(
                f"SELECT {_SELECT_CHUNK_COLUMNS} FROM chunks "  # noqa: S608 -- fixed column constant
                "WHERE document_id = %s ORDER BY chunk_index",
                (document_id,),
            )
            return [self._row_to_chunk(r) for r in rows]

    # -- stats / bloat ------------------------------------------------------

    async def count_chunks(self) -> int:
        """Total chunk rows across all documents; 0 when the table is absent."""
        with self._query_timer.measure("count_chunks"):
            return await self._scalar_or_zero("SELECT count(*) FROM chunks")

    async def count_retained_versions(self) -> int:
        """Dead MVCC tuples awaiting VACUUM -- retained old row versions.

        Rises with un-optimized write churn (each re-index is a delete+insert),
        independent of corpus size, and is reset by ``optimize``. Read via
        ``pgstattuple``; 0 when the table is absent.
        """
        with self._query_timer.measure("count_retained_versions"):
            return await self._scalar_or_zero("SELECT dead_tuple_count FROM pgstattuple('chunks')")

    async def count_small_fragments(self) -> int:
        """Reclaimable free-space pages -- the un-compacted-fragment analog.

        After churn is vacuumed, dead tuples become in-page free space;
        ``optimize`` (VACUUM FULL) returns it to the OS. Reported as whole
        pages so a tightly packed store reads near zero. 0 when the table is
        absent.
        """
        with self._query_timer.measure("count_small_fragments"):
            import psycopg

            try:
                rows = await self._fetchall(
                    "SELECT free_space / current_setting('block_size')::bigint "
                    "FROM pgstattuple('chunks')"
                )
            except psycopg.errors.UndefinedTable:
                return 0
            return int(rows[0][0]) if rows and rows[0][0] is not None else 0

    async def measured_byte_size(self) -> int:
        """On-disk footprint of the chunks relation (heap + indexes + toast)."""
        with self._query_timer.measure("measured_byte_size"):
            return await self._scalar_or_zero("SELECT pg_total_relation_size('chunks'::regclass)")

    async def optimize(self, cleanup_older_than: timedelta) -> ContentStoreOptimizeSnapshot:
        """Reclaim bloat with ``VACUUM (FULL, ANALYZE)``; snapshot pre/post.

        Removes dead tuples, returns free space to the OS, and shrinks the
        relation, so the retained-version, fragment, and byte signals all drop.
        ``cleanup_older_than`` is accepted for the port contract but has no
        Postgres analog: VACUUM has no age threshold and reclaims every
        eligible dead tuple, a superset of the LanceDB age-based pruning.
        Runs on an autocommit connection (VACUUM cannot run inside a
        transaction block).
        """
        del cleanup_older_than  # no Postgres age-threshold analog; see docstring
        with self._query_timer.measure("optimize"):
            async with self._pool.connection() as conn:
                await conn.set_autocommit(True)
                pre = await self._bloat_snapshot(conn)
                await conn.execute("VACUUM (FULL, ANALYZE) chunks")
                post = await self._bloat_snapshot(conn)
            return ContentStoreOptimizeSnapshot(
                pre_bytes=pre["bytes"],
                post_bytes=post["bytes"],
                pre_versions=pre["versions"],
                post_versions=post["versions"],
                pre_fragments=pre["fragments"],
                post_fragments=post["fragments"],
                pre_small_fragments=pre["small_fragments"],
                post_small_fragments=post["small_fragments"],
            )

    # -- internals ----------------------------------------------------------

    @staticmethod
    async def _bloat_snapshot(conn: AsyncConnection) -> dict[str, int]:
        """Capture (bytes, versions, fragments, small_fragments) for chunks."""
        import psycopg

        try:
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT pg_total_relation_size('chunks'::regclass), "
                    "dead_tuple_count, "
                    "table_len / current_setting('block_size')::bigint, "
                    "free_space / current_setting('block_size')::bigint "
                    "FROM pgstattuple('chunks')"
                )
                row = await cur.fetchone()
        except psycopg.errors.UndefinedTable:
            row = None
        if row is None:
            return {"bytes": 0, "versions": 0, "fragments": 0, "small_fragments": 0}
        return {
            "bytes": int(row[0]),
            "versions": int(row[1]),
            "fragments": int(row[2]),
            "small_fragments": int(row[3]),
        }

    async def _fetchall(self, sql: str, params: object = ()) -> list[tuple]:
        async with self._pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(sql, params)
            return await cur.fetchall()

    async def _scalar_or_zero(self, sql: str, params: object = ()) -> int:
        import psycopg

        try:
            rows = await self._fetchall(sql, params)
        except psycopg.errors.UndefinedTable:
            return 0
        return int(rows[0][0]) if rows and rows[0][0] is not None else 0

    @staticmethod
    def _row_to_result(row: tuple) -> SearchResult:
        return SearchResult(
            document_id=row[0],
            heading_path=row[1],
            content=row[2],
            score=float(row[3]),
        )

    @staticmethod
    def _row_to_chunk(row: tuple) -> Chunk:
        return Chunk(
            document_id=row[0],
            heading_path=row[1],
            content=row[2],
            chunk_index=row[3],
            doc_type=row[4],
            lifecycle_status=row[5],
            project=row[6],
        )

    @staticmethod
    def _escape_like(value: str) -> str:
        """Escape LIKE wildcards so a literal prefix never acts as a pattern."""
        return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
