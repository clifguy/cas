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
    KeywordQueryParse,
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


# A tsquery renders as quoted lexemes joined by operators: ``&`` conjunction,
# ``|`` alternation, ``<->`` phrase adjacency. ``!`` negates either a single
# lexeme (``!'beta'``) or a parenthesised group (``!( 'a' <-> 'b' )``, which a
# negated phrase produces). Embedded quotes are doubled.
_TSQUERY_LEXEME = re.compile(r"'((?:[^']|'')*)'")

# A quoted span the query excludes, and one it requires. Adjacency has to be
# recognized from the caller's own text rather than from the rendered operator,
# because the tokenizer emits adjacency of its own for any compound it splits.
# The excluded spans come out first: a span the query asks to avoid imposes no
# adjacency the caller has to satisfy, and leaving it in would let it stand in
# for a required one. A surviving span counts only if it holds more than one
# word -- a single quoted word carries no adjacency, and reading it as a phrase
# is how a query pairing one with a hyphenated identifier reported adjacency
# neither of them asked for.
_EXCLUDED_QUOTED_SPAN = re.compile(r'-"[^"]*"')
_REQUIRED_PHRASE_SPAN = re.compile(r'"[^"\s]*\s[^"]*"')


def _skip_quoted(rendered: str, i: int) -> int:
    """Index just past the quoted lexeme starting at ``i``, doubled quotes included."""
    i += 1
    while i < len(rendered):
        if rendered[i] == "'":
            if rendered[i + 1 : i + 2] == "'":
                i += 2
                continue
            return i + 1
        i += 1
    return i


def _split_conjuncts(rendered: str) -> list[str] | None:
    """The top-level AND operands of a rendered tsquery, or None if it has none.

    Each operand becomes one document-id arm of the intersection that computes
    a document-scoped match, so the split has to be exact. ``None`` means the
    query's shape does not admit the decomposition and the caller must fall
    back to evaluating it against a single chunk.

    Two shapes are refused. A negation asks whether text is *absent*, and
    absence from one chunk is not absence from the document, so the two scopes
    genuinely disagree and no decision governs which one a caller means. A
    top-level alternation cannot be split at all: ``&`` binds tighter than
    ``|``, so ``a | b & c`` is ``a OR (b AND c)`` and cutting it at the ``&``
    would rewrite the query. An alternation nested *inside* an operand is fine
    -- a chunk satisfies ``a | b`` exactly when the document does -- so only
    the top level is checked.

    Lexemes are returned as rendered, including their quotes, because they are
    already stemmed: re-parsing them through ``to_tsquery`` would stem them a
    second time, and the English stemmer is not idempotent (``univers``, the
    lexeme for "university", stems again to ``univ`` and matches nothing).
    Casting the rendered text to ``tsquery`` takes it verbatim.
    """
    if "!" in rendered:
        return None
    operands: list[str] = []
    depth = 0
    start = 0
    i = 0
    while i < len(rendered):
        char = rendered[i]
        if char == "'":
            i = _skip_quoted(rendered, i)
            continue
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
        elif depth == 0 and char == "|":
            return None
        elif depth == 0 and char == "&":
            operands.append(rendered[start:i])
            start = i + 1
        i += 1
    operands.append(rendered[start:])
    trimmed = [operand.strip() for operand in operands]
    return trimmed if all(trimmed) else None


def _or_form(terms: tuple[str, ...]) -> str:
    """The query's required lexemes as a tsquery alternation, for ranking.

    Ranking cannot use the query itself: ``ts_rank`` against a conjunction a
    chunk does not satisfy returns a floor value, so every chunk of a document
    that matched across its chunks would tie at effectively zero. Against the
    alternation, a chunk scores by how much of the query it carries, which is
    the signal the ranking wants -- a chunk holding every term outranks one
    holding a subset.
    """
    return " | ".join("'" + term.replace("'", "''") + "'" for term in terms)


def _split_negation(rendered: str) -> tuple[str, list[str]]:
    """Separate a rendered tsquery into what it requires and what it excludes.

    Scans rather than pattern-matches because a negation's scope can be a
    parenthesised group holding several lexemes. Matching the ``!`` against an
    adjacent quote instead would keep every lexeme of a negated phrase after
    the first, reporting terms the caller explicitly excluded as required.

    Returns the text with negated spans removed, plus the lexemes those spans
    held. The excluded list matters beyond being dropped: a query that renders
    only negations searched for something, which an empty required set alone
    cannot distinguish from a query the backend discarded entirely.
    """
    kept: list[str] = []
    excluded: list[str] = []
    i = 0
    while i < len(rendered):
        if rendered[i] != "!":
            if rendered[i] == "'":
                end = _skip_quoted(rendered, i)
                kept.append(rendered[i:end])
                i = end
                continue
            kept.append(rendered[i])
            i += 1
            continue
        # A negation: consume its whole scope, collecting the lexemes in it.
        i += 1
        while i < len(rendered) and rendered[i].isspace():
            i += 1
        if i < len(rendered) and rendered[i] == "'":
            end = _skip_quoted(rendered, i)
            excluded.append(rendered[i + 1 : end - 1])
            i = end
        elif i < len(rendered) and rendered[i] == "(":
            depth = 0
            while i < len(rendered):
                if rendered[i] == "'":
                    end = _skip_quoted(rendered, i)
                    excluded.append(rendered[i + 1 : end - 1])
                    i = end
                    continue
                if rendered[i] == "(":
                    depth += 1
                elif rendered[i] == ")":
                    depth -= 1
                    if depth == 0:
                        i += 1
                        break
                i += 1
    return "".join(kept), [lexeme.replace("''", "'") for lexeme in excluded]


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
        """Keyword search scoped to the document (CAS-ADR-048).

        The match unit is the document: one intersected arm per top-level
        operand of the parsed query, each asking whether *some* chunk of the
        document satisfies that operand. Intersecting on document id makes the
        conjunction hold across the document's chunks rather than within any
        one of them, which is what the document-shaped result already claims.
        The shape also carries the phrase exception without a special case: an
        operand that is a phrase is satisfied only by a chunk holding its terms
        adjacently, so adjacency stays chunk-scoped while bare terms do not.

        Each arm is an indexed predicate over the generated tsvector, which
        covers heading_path (weight A) and content (weight D), so a term
        present only in a heading is findable. Queries whose shape does not
        decompose fall back to evaluating the whole query against one chunk.

        The synthetic document-header row is barred from the arms: it carries
        machine-generated and incidental text, which ranks and orients but
        never satisfies a match (CAS-ADR-049). It stays in the ranking pool, so
        a document's score still reflects it, but the excerpt is drawn from an
        authored passage -- otherwise a query for a word in the header's own
        scaffolding would answer with that scaffolding.
        """
        with self._query_timer.measure(
            "search_bm25", params={"limit": limit, "filtered": bool(filters)}
        ):
            if not query or not query.strip():
                return []
            parse = await self.parse_keyword_query(query)
            if not parse.terms:
                # Nothing to rank against, and nothing to intersect on: either
                # every word was discarded, or the query asked only for
                # absences, which the decomposition refuses.
                return await self._search_bm25_within_chunk(query, limit, filters)
            rendered = await self._render_tsquery(query)
            operands = _split_conjuncts(rendered)
            if operands is None:
                return await self._search_bm25_within_chunk(query, limit, filters)
            return await self._search_bm25_across_document(
                operands, _or_form(parse.terms), limit, filters
            )

    async def _search_bm25_across_document(
        self,
        operands: list[str],
        or_form: str,
        limit: int,
        filters: dict[str, str | list[str]] | None,
    ) -> list[SearchResult]:
        """Resolve each operand to document ids, intersect, then rank chunks.

        ``limit`` bounds documents rather than rows: a matching document is
        returned once, carrying its best-ranking authored chunk as the excerpt
        and a count of how many of its chunks carry a query term.
        """
        where, where_params = self._build_where(filters)
        # Interpolated into the fragments below. Column names come from a fixed
        # allowlist and every value stays bound, so the S608 suppressions below
        # cover a clause with nothing caller-supplied in its text.
        predicate = f" AND {where}" if where else ""

        # DISTINCT rather than relying on INTERSECT to deduplicate: a
        # single-operand query has no INTERSECT, and duplicate document ids
        # would multiply every row the ranking join produces.
        arm = (
            "SELECT DISTINCT document_id FROM chunks"  # noqa: S608
            f" WHERE heading_path <> %s AND tsv @@ %s::tsquery{predicate}"
        )
        params: list[object] = []
        for operand in operands:
            params += [SYNTHETIC_HEADER_HEADING_PATH, operand, *where_params]
        matched = "\nINTERSECT\n".join([arm] * len(operands))

        # The header is excluded from the arms above but present here, so it
        # lifts a document's score without being eligible as its excerpt --
        # ``rn`` orders authored chunks ahead of it.
        sql = (
            f"WITH matched AS (\n{matched}\n), ranked AS ("  # noqa: S608
            " SELECT c.document_id, c.heading_path, c.content,"
            " max(ts_rank(c.tsv, %s::tsquery)) OVER (PARTITION BY c.document_id) AS doc_score,"
            " count(*) FILTER (WHERE c.heading_path <> %s)"
            " OVER (PARTITION BY c.document_id) AS matched_chunks,"
            " row_number() OVER (PARTITION BY c.document_id ORDER BY"
            " (c.heading_path = %s), ts_rank(c.tsv, %s::tsquery) DESC, c.chunk_index) AS rn"
            " FROM chunks c JOIN matched USING (document_id)"
            f" WHERE c.tsv @@ %s::tsquery{predicate}"
            ") SELECT document_id, heading_path, content, doc_score, matched_chunks"
            " FROM ranked WHERE rn = 1 ORDER BY doc_score DESC, document_id LIMIT %s"
        )
        params += [
            or_form,
            SYNTHETIC_HEADER_HEADING_PATH,
            SYNTHETIC_HEADER_HEADING_PATH,
            or_form,
            or_form,
            *where_params,
            limit,
        ]
        rows = await self._fetchall(sql, params)
        return [
            SearchResult(
                document_id=row[0],
                heading_path=row[1],
                content=row[2],
                score=float(row[3]),
                matched_chunk_count=int(row[4]),
            )
            for row in rows
        ]

    async def _search_bm25_within_chunk(
        self,
        query: str,
        limit: int,
        filters: dict[str, str | list[str]] | None,
    ) -> list[SearchResult]:
        """Evaluate the whole query against a single chunk.

        The fallback for queries the document-scoped decomposition refuses --
        those carrying a negation or a top-level alternation. Neither scope is
        settled for them, so this keeps the behaviour they already had rather
        than inventing one.
        """
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

    async def _render_tsquery(self, query: str) -> str:
        """The query as the text-search configuration parses it."""
        rows = await self._fetchall(
            f"SELECT websearch_to_tsquery('{TEXT_SEARCH_CONFIG}', %s)::text",  # noqa: S608
            [query],
        )
        return rows[0][0] if rows and rows[0][0] else ""

    async def parse_keyword_query(self, query: str) -> KeywordQueryParse:
        """How the text-search configuration read this query.

        ``search_bm25`` builds its query with ``websearch_to_tsquery``, which
        joins bare terms with AND -- so a document matches only if its chunks
        between them carry every lexeme. Reporting those lexemes lets a caller
        see why a query matched nothing, which the raw query text cannot tell
        them: stopwords are dropped and the rest are stemmed, so the terms
        actually required are neither the words typed nor a whitespace split
        of them.

        The forms the query language admits beyond bare terms are reported
        rather than assumed away, because each makes a different sentence true
        of an empty result. Terms the query excludes (``-word``, ``-"a
        phrase"``) are not something the caller must supply, so they leave
        ``terms`` and are reported in ``excluded``. A query using ``or``
        renders an alternation, which makes it non-conjunctive. A quoted
        phrase renders adjacency, which is stronger than carrying every term
        and is the one predicate still scoped to a single chunk: a document
        can hold them all, apart, and still not match.
        """
        with self._query_timer.measure("parse_keyword_query"):
            if not query or not query.strip():
                return KeywordQueryParse(terms=(), excluded=(), all_required=True, adjacent=False)
            rendered = await self._render_tsquery(query)
            required, excluded = _split_negation(rendered)
            return KeywordQueryParse(
                terms=tuple(
                    lexeme.replace("''", "'") for lexeme in _TSQUERY_LEXEME.findall(required)
                ),
                excluded=tuple(excluded),
                all_required="|" not in required,
                # Both halves are load-bearing, and each is read from its own
                # source. The rendered operator alone over-reports: the
                # tokenizer emits adjacency for every compound it splits, so
                # "CAS-ADR-048" would read as a phrase. The caller's quotes
                # alone over-report too: a quoted span that rendered nothing
                # imposes no adjacency. Matching on "<" rather than "<->"
                # catches the distances a dropped stopword produces.
                adjacent=bool(_REQUIRED_PHRASE_SPAN.search(_EXCLUDED_QUOTED_SPAN.sub("", query)))
                and "<" in required,
            )

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
