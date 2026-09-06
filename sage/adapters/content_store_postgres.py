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
from collections.abc import Sequence
from datetime import timedelta
from typing import TYPE_CHECKING

from sage.adapters.interfaces import (
    HEADING_PATH_SEPARATOR,
    LEGACY_DOCUMENT_HEADER_CHUNK_INDEX,
    LEGACY_DOCUMENT_HEADER_HEADING_PATH,
    Chunk,
    ContentStore,
    ContentStoreOptimizeSnapshot,
    DocumentSurface,
    KeywordQueryParse,
    SearchResult,
)
from sage.instrumentation.timing import NULL_QUERY_TIMER, NullQueryTimer, QueryTimer
from sage.storage.postgres.schema import (
    CHUNKS_TSV_GENERATION_EXPRESSION_PROBE,
    CHUNKS_TSV_REBUILD,
    EMBEDDING_DIM,
    TEXT_SEARCH_CONFIG,
)
from sage.utils.sql_patterns import escape_like
from sage.utils.text_normalization import fold_for_query

if TYPE_CHECKING:  # pragma: no cover - typing only
    from psycopg import AsyncConnection
    from psycopg_pool import AsyncConnectionPool

# Chunk-level columns the port accepts as pre-filter predicates. Filter keys
# outside this allowlist are ignored, and only these fixed identifiers are ever
# interpolated into SQL -- predicate values are always parameterized.
_FILTERABLE_COLUMNS: tuple[str, ...] = ("doc_type", "document_id", "lifecycle_status", "project")

# Backfill batch size. The pairs are distinct (document_id, heading_path), so a
# vault of tens of thousands of passages yields far fewer of them than rows;
# batching keeps one statement's parameter list bounded all the same.
_BACKFILL_BATCH_SIZE = 1000

# Metadata columns update_chunk_metadata may touch (content, hence tsv, is never
# rewritten here).
_METADATA_COLUMNS: tuple[str, ...] = ("doc_type", "lifecycle_status", "project")

_INSERT_COLUMNS = (
    "document_id, heading_path, indexed_structure, content, chunk_index, "
    "embedding, doc_type, lifecycle_status, project"
)
_INSERT_SQL = (
    f"INSERT INTO chunks ({_INSERT_COLUMNS}) "  # noqa: S608 -- fixed column constant
    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)"
)

# ``indexed_structure`` is read back as well as written, because a passage read
# can be the first half of a write: the re-embedding maintenance script reads
# every chunk of a document, replaces the embeddings, and hands the same objects
# back to ``index_chunks``. Omitting the column here would null the derived
# structure on the way through that round-trip, and the generated column's
# coalesce would then quietly index the address again -- a silent reversion to
# the pre-decision behaviour rather than an error.
_SELECT_CHUNK_COLUMNS = (
    "document_id, heading_path, indexed_structure, content, chunk_index, "
    "doc_type, lifecycle_status, project"
)


def _passage_rows_only(alias: str = "") -> str:
    """Scope a read to the passage surface, as a ``WHERE`` fragment.

    This is the surface's own definition rather than a per-consumer exclusion of
    some particular row: nothing writes an index at or below the legacy marker's
    any more. It is expressed once, here, and every passage read on this binding
    calls it, so a site added without the scoping is visible as one.

    Why the scoping is kept at all -- the window in which a vault still holds a
    legacy document-level row, and why that window does not close -- is stated at
    ``LEGACY_DOCUMENT_HEADER_CHUNK_INDEX`` in the port, and is not restated here
    or at any call site.

    Rendered as ``>=`` against the first index above the marker rather than as
    ``>`` against the marker itself. The two are identical on an integer column,
    but the second spells the heading-path delimiter between two placeholders,
    which is a form the passage-structure scan reads as a restatement of that
    delimiter -- correctly, since it cannot tell a SQL comparison from a join.

    Args:
        alias: The table alias to qualify the column with, when the surrounding
            statement joins and an unqualified column would be ambiguous.
    """
    column = f"{alias}.chunk_index" if alias else "chunk_index"
    return f"{column} >= {LEGACY_DOCUMENT_HEADER_CHUNK_INDEX + 1}"


# A tsquery renders as quoted lexemes joined by operators: ``&`` conjunction,
# ``|`` alternation, ``<->`` phrase adjacency. ``!`` negates either a single
# lexeme (``!'beta'``) or a parenthesised group (``!( 'a' <-> 'b' )``, which a
# negated phrase produces). Embedded quotes are doubled.
_TSQUERY_LEXEME = re.compile(r"'((?:[^']|'')*)'")

# A quoted span the query excludes. Removed before the required spans are read:
# a span the query asks to avoid imposes no adjacency the caller has to satisfy,
# and leaving it in would let it stand in for a required one.
_EXCLUDED_QUOTED_SPAN = re.compile(r'-"[^"]*"')


def _required_phrase_spans(query: str) -> list[str]:
    """The phrase spans the caller's own text requires, in order.

    Adjacency has to be recognized from the query text rather than from the
    rendered operator, because the tokenizer emits adjacency of its own for any
    compound it splits -- so a hyphenated identifier would otherwise read as a
    phrase the caller never wrote.

    Quotes are paired sequentially rather than matched by a pattern. A pattern
    looking for a quote, a word, whitespace and a closing quote will happily
    match from one span's *closing* quote to the next span's, so ``"a" "b"``
    reads as the phrase ``" "`` -- two single quoted words reported as one
    phrase. Splitting on the quote character makes the pairing positional and
    cannot make that mistake. A trailing unpaired quote still opens a span,
    which is how ``websearch_to_tsquery`` reads it: ``alpha "beta gamma``
    renders ``'alpha' & 'beta' <-> 'gamma'``, adjacency and all.
    """
    return _EXCLUDED_QUOTED_SPAN.sub("", query).split('"')[1::2]


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


def _parse_rendered(query: str, rendered: str) -> KeywordQueryParse:
    """Read a rendered tsquery, alongside the text it came from, as a parse.

    Split out from ``parse_keyword_query`` so the dispatcher can reuse a
    rendering it already has rather than asking the backend for a second one.
    Everything here is pure; the round-trip is the caller's.
    """
    required, excluded = _split_negation(rendered)
    spans = _required_phrase_spans(query)
    return KeywordQueryParse(
        terms=tuple(lexeme.replace("''", "'") for lexeme in _TSQUERY_LEXEME.findall(required)),
        excluded=tuple(excluded),
        all_required="|" not in required,
        # Both halves are load-bearing, and each is read from its own source.
        # The rendered operator alone over-reports: the tokenizer emits
        # adjacency for every compound it splits, so "CAS-ADR-048" would read
        # as a phrase. The caller's quotes alone over-report too: a quoted span
        # that rendered nothing imposes no adjacency. A span counts only if it
        # holds more than one word, since a single quoted word has nothing to
        # be adjacent to. Matching on "<" rather than "<->" catches the
        # distances a dropped stopword produces.
        adjacent=any(len(span.split()) > 1 for span in spans) and "<" in required,
    )


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
            chunk.indexed_structure,
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

    async def upsert_document_surface(self, surface: DocumentSurface) -> None:
        """Write a document's document-level row; its passages are untouched."""
        with self._query_timer.measure("upsert_document_surface"):
            async with self._pool.connection() as conn, conn.transaction():
                await conn.execute(
                    "DELETE FROM document_surface WHERE document_id = %s",
                    (surface.document_id,),
                )
                await conn.execute(
                    "INSERT INTO document_surface"
                    " (document_id, matchable, orienting, embedding,"
                    "  doc_type, lifecycle_status, project)"
                    " VALUES (%s, %s, %s, %s, %s, %s, %s)",
                    (
                        surface.document_id,
                        surface.matchable,
                        surface.orienting,
                        surface.embedding,
                        surface.doc_type,
                        surface.lifecycle_status,
                        surface.project,
                    ),
                )

    async def remove_document_surface(self, document_id: str) -> None:
        """Remove a document's document-level row (idempotent)."""
        with self._query_timer.measure("remove_document_surface"):
            async with self._pool.connection() as conn:
                await conn.execute(
                    "DELETE FROM document_surface WHERE document_id = %s", (document_id,)
                )

    async def update_document_surface_text(
        self, document_id: str, matchable: str, orienting: str
    ) -> bool:
        """Rewrite a document-level row's text, leaving its vector in place."""
        with self._query_timer.measure("update_document_surface_text"):
            async with self._pool.connection() as conn:
                cur = await conn.execute(
                    "UPDATE document_surface SET matchable = %s, orienting = %s"
                    " WHERE document_id = %s",
                    (matchable, orienting, document_id),
                )
                return bool(cur.rowcount)

    async def update_indexed_structure(
        self, document_id: str, derived: Sequence[tuple[str, str]]
    ) -> int:
        """Rewrite one document's derived structure, by ``(heading_path, structure)``."""
        if not derived:
            return 0
        with self._query_timer.measure("update_indexed_structure", params={"paths": len(derived)}):
            async with self._pool.connection() as conn, conn.transaction():
                async with conn.cursor() as cur:
                    await cur.executemany(
                        "UPDATE chunks SET indexed_structure = %s"
                        " WHERE document_id = %s AND heading_path = %s",
                        [(structure, document_id, path) for path, structure in derived],
                    )
                    return cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0

    async def remove_document(self, document_id: str) -> None:
        """Remove a document's passages and document surface (idempotent)."""
        with self._query_timer.measure("remove_document"):
            async with self._pool.connection() as conn, conn.transaction():
                await conn.execute("DELETE FROM chunks WHERE document_id = %s", (document_id,))
                await conn.execute(
                    "DELETE FROM document_surface WHERE document_id = %s", (document_id,)
                )

    # -- migration off the single-surface layout (CAS-ADR-049) ---------------

    async def legacy_document_header_rows(self) -> list[tuple[str, list[float] | None]]:
        """Return ``(document_id, embedding)`` for each legacy header row."""
        with self._query_timer.measure("legacy_document_header_rows"):
            rows = await self._fetchall(
                "SELECT document_id, embedding FROM chunks WHERE heading_path = %s",
                (LEGACY_DOCUMENT_HEADER_HEADING_PATH,),
            )
            return [(r[0], list(r[1]) if r[1] is not None else None) for r in rows]

    async def delete_legacy_document_header_rows(self) -> int:
        """Delete every legacy header row; returns the number removed."""
        with self._query_timer.measure("delete_legacy_document_header_rows"):
            async with self._pool.connection() as conn:
                cur = await conn.execute(
                    "DELETE FROM chunks WHERE heading_path = %s",
                    (LEGACY_DOCUMENT_HEADER_HEADING_PATH,),
                )
                return cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0

    # -- migration to the relative indexed structure (CAS-ADR-049 Decision 3) --

    async def passages_awaiting_indexed_structure(self) -> list[tuple[str, str]]:
        """Return the distinct ``(document_id, heading_path)`` still underived."""
        with self._query_timer.measure("passages_awaiting_indexed_structure"):
            rows = await self._fetchall(
                "SELECT DISTINCT document_id, heading_path FROM chunks "
                "WHERE indexed_structure IS NULL"
            )
            return [(r[0], r[1]) for r in rows]

    async def passage_vector_ranks_indexed_structure(self) -> bool:
        """Whether the keyword vector already ranks the relative structure."""
        with self._query_timer.measure("passage_vector_ranks_indexed_structure"):
            async with self._pool.connection() as conn:
                return await self._vector_ranks_indexed_structure(conn)

    @staticmethod
    async def _vector_ranks_indexed_structure(conn: AsyncConnection) -> bool:
        """Read the stored generation expression on an open connection."""
        cur = await conn.execute(CHUNKS_TSV_GENERATION_EXPRESSION_PROBE)
        row = await cur.fetchone()
        if row is None:
            raise RuntimeError(
                "the passage table carries no generated keyword vector; a rebuild "
                "was interrupted outside a transaction and the table needs repair "
                "before a migration can proceed"
            )
        return "indexed_structure" in (row[0] or "")

    async def migrate_indexed_structure(self, derived: Sequence[tuple[str, str, str]]) -> int:
        """Apply derived structure and, if needed, rebuild the keyword vector.

        One transaction, because the statement order is what makes this
        affordable and a half-applied order is worse than either end of it. The
        vector column is dropped *before* the backfill and re-added after, so
        the backfill maintains one fewer index -- dropping the column takes the
        GIN over it along -- and does not recompute a vector it is about to
        invalidate. The re-add then rewrites the table over final values,
        compacting the dead tuples the backfill created instead of leaving them
        for a later optimize.

        The rebuild is skipped when the stored expression already names the
        column, so an ordinary re-run costs a catalog read. The table is locked
        explicitly before that check, which is belt-and-braces rather than a
        repair. Reading a generated column's expression out of the catalog
        renders it through ``pg_get_expr``, which opens the relation and takes a
        share lock of its own -- measured, on a server where the probe blocks
        under a concurrent exclusive lock and so reads a settled answer. But
        that serialization is an artifact of how a catalog view happens to be
        evaluated rather than a documented guarantee, and this table is rebuilt
        on a different major version from the one the tests run against. The
        contention it covers is not two operators but one, re-invoking after a
        client timed out on a call still running.

        ``SHARE UPDATE EXCLUSIVE``, and the mode is load-bearing rather than
        incidental -- do not "strengthen" it. What this lock has to do is stop
        two migrators from both deciding to rebuild, and that mode conflicts
        with itself, so it does exactly that. What it must *not* do is stop
        anyone else: the decision is followed by a plain ``UPDATE`` whenever the
        vector is already current and only rows are outstanding, and taking an
        exclusive lock there would hold every search and ingest on the vault
        behind a backfill that does not need it. On the rebuild path the
        ``DROP`` escalates to ``ACCESS EXCLUSIVE`` on its own, so the stronger
        lock is still taken exactly where it is needed and no earlier.

        Expensive and exclusive when the rebuild does run: the re-add rewrites
        the passage table and every index over it, including the HNSW index
        over the embeddings, which dominates the cost on a large vault.

        Returns the number of rows the backfill wrote.
        """
        with self._query_timer.measure("migrate_indexed_structure", params={"pairs": len(derived)}):
            written = 0
            async with self._pool.connection() as conn, conn.transaction():
                await conn.execute("LOCK TABLE chunks IN SHARE UPDATE EXCLUSIVE MODE")
                rebuilding = not await self._vector_ranks_indexed_structure(conn)
                drop, add, index = CHUNKS_TSV_REBUILD

                if rebuilding:
                    await conn.execute(drop)

                if derived:
                    async with conn.cursor() as cur:
                        for start in range(0, len(derived), _BACKFILL_BATCH_SIZE):
                            batch = derived[start : start + _BACKFILL_BATCH_SIZE]
                            # Restricted to rows still awaiting derivation, so a
                            # re-run cannot overwrite a value a later ingest
                            # already wrote from a newer title.
                            await cur.executemany(
                                "UPDATE chunks SET indexed_structure = %s "
                                "WHERE document_id = %s AND heading_path = %s "
                                "AND indexed_structure IS NULL",
                                [(structure, doc, path) for doc, path, structure in batch],
                            )
                            written += cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0

                if rebuilding:
                    await conn.execute(add)
                    await conn.execute(index)
            return written

    async def update_chunk_metadata(
        self,
        document_id: str,
        metadata: dict[str, str | None],
    ) -> None:
        """Update metadata columns on both of a document's retrieval surfaces.

        Both surfaces carry the same filter columns so a caller's predicates
        apply identically to each (CAS-ADR-049), which holds only while both
        are kept current. Updating passages alone would leave a document
        matchable by its title under the lifecycle, doc_type or project it had
        before the change -- archiving a document would not stop it answering
        an ``active``-filtered query. Both writes share one transaction, so the
        two surfaces cannot disagree even if the second fails.

        Content, and hence every generated vector, is never rewritten here.
        """
        with self._query_timer.measure("update_chunk_metadata"):
            cols = [c for c in _METADATA_COLUMNS if c in metadata]
            if not cols:
                return
            set_clause = ", ".join(f"{c} = %s" for c in cols)
            params: list[object] = [metadata[c] for c in cols]
            params.append(document_id)
            async with self._pool.connection() as conn, conn.transaction():
                await conn.execute(
                    f"UPDATE chunks SET {set_clause} WHERE document_id = %s",  # noqa: S608
                    params,
                )
                await conn.execute(
                    f"UPDATE document_surface SET {set_clause} WHERE document_id = %s",  # noqa: S608
                    params,
                )

    # -- read paths ---------------------------------------------------------

    async def search_semantic(
        self,
        query_embedding: list[float],
        limit: int = 10,
        filters: dict[str, str | list[str]] | None = None,
    ) -> list[SearchResult]:
        """Vector similarity search; score is ``1 - cosine_distance``.

        Covers both retrieval surfaces (CAS-ADR-049): a document's passages and
        its document-level row compete in one ranking. Similarity is not
        matching, so derived text is in scope here -- it exists to make a
        document findable and triageable, which is a ranking and orientation
        value this preserves. A document-level hit carries an empty heading
        path, marking it as document-level rather than a passage.

        Each arm orders by distance and takes its own ``limit`` *before* the
        union, and the outer query sorts the survivors. Two properties depend
        on that shape. It is the form pgvector's HNSW index serves -- ordering
        the union by a computed score instead makes the planner scan both
        tables in full, since neither index can supply that order. And a row
        with no embedding yields a NaN distance, which sorts above every real
        number under ``score DESC`` but below every real distance under the
        ascending order each arm uses, so an unembedded row is the last thing
        an arm keeps rather than the first. It can still reach the result when
        an arm returns fewer rows than its limit -- there is then nothing for
        the ordering to displace it behind -- which is why nothing writes a
        row without an embedding.
        """
        with self._query_timer.measure(
            "search_semantic", params={"limit": limit, "filtered": bool(filters)}
        ):
            where, where_params = self._build_where(filters)
            predicate = f" WHERE {where}" if where else ""
            sql = (
                "SELECT document_id, heading_path, content, score,"  # noqa: S608
                " is_document_surface FROM ("
                " (SELECT document_id, heading_path, content,"
                " 1 - (embedding <=> %s::vector) AS score,"
                " false AS is_document_surface FROM chunks"
                f" WHERE {_passage_rows_only()}{' AND ' + where if where else ''}"
                " ORDER BY embedding <=> %s::vector LIMIT %s)"
                " UNION ALL"
                # The stored halves are widened to a superset of the document's
                # renderings so a query reaching for one form finds the other.
                # That widening is for the index to match on; served back as an
                # excerpt it reads as duplicated tokens rather than as anything
                # the document says. A document-level row is not a passage and
                # carries no excerpt (CAS-ADR-049).
                " (SELECT document_id, '' AS heading_path,"
                " '' AS content,"
                " 1 - (embedding <=> %s::vector) AS score,"
                " true AS is_document_surface FROM document_surface"
                f"{predicate}"
                " ORDER BY embedding <=> %s::vector LIMIT %s)"
                " ) s WHERE score IS NOT NULL ORDER BY score DESC LIMIT %s"
            )
            params: list[object] = [
                query_embedding,
                *where_params,
                query_embedding,
                limit,
                query_embedding,
                *where_params,
                query_embedding,
                limit,
                limit,
            ]
            rows = await self._fetchall(sql, params)
            return [self._row_to_semantic_result(r) for r in rows]

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
        present only in a heading is findable.

        Each operand is satisfied by the document's *authored* text wherever it
        lives: a passage, or the document surface carrying its title and tags
        (CAS-ADR-049). The arm is therefore a union of the two surfaces, and
        the intersection across operands runs over that union -- so a query
        whose terms are split between a title and a body still matches, which a
        pair of independently-ranked surfaces could not express. Derived text
        reaches only the document surface's ranking vector and so can never
        satisfy an operand.

        Two matching paths answer a query, and one arm grafts onto the first.
        The whole set is stated here and only here: each path's own docstring
        carries its internals, none of them carries its placement relative to
        the others, and a reader who needs the shape should not have to
        assemble it from three of them.

        ``_search_bm25_across_document``. Scope: the document. Tokenization:
        the query as the text-search configuration renders it, split into
        top-level operands and cast back verbatim, since re-parsing a rendered
        lexeme would stem it a second time. Admitted when the query requires at
        least one lexeme and ``_split_conjuncts`` accepts its shape.

        The folded arm. Not a third scope: the same document scope reached by a
        different tokenization, spliced into the path above as an extra
        ``UNION`` over the document surface alone. Admitted when folding
        changes the query and the folded text still renders. Confining it to
        the document surface is what keeps passage matching on literal
        tokenization.

        Two directions need it, for two different reasons. A caller typing
        ``documentLevelTextHandling`` reaches a title written ``Document Level
        Text Handling`` only through this arm, because no index-side widening
        can: the expansion adds a compound's folded and split forms and never
        synthesizes a joining of words the author wrote apart. A caller typing
        ``epsilon-level`` reaches ``Epsilon Level`` only through this arm too,
        because the configuration reads a hyphenated pair as the hyphenated
        whole *followed by* its parts -- so the query demands a lexeme the
        expansion never produced.

        The underscore is the separator that needs no arm: it renders as the
        parts alone, which the expansion already writes into adjacent
        positions. Hyphen and underscore are one character class to the folding
        transform and two different things to the tokenizer, and the hyphenated
        form is the one nearly every identifier-shaped title carries.

        ``_search_bm25_within_chunk``. Scope: one unit of text -- a passage, or
        the document surface as a second such unit. Tokenization: the whole
        query, rendered in SQL and never decomposed. Admitted when the query
        requires no lexeme at all, or when ``_split_conjuncts`` refuses its
        shape: a negation, or a top-level alternation.

        The set is exhaustive because the dispatch is two decisions over the
        parse, taken in order and jointly total -- whether the query requires a
        lexeme, and whether its rendered shape decomposes. Nothing else is
        consulted, and the folded arm answers nothing on its own: it widens
        what the first path matches and is unreachable without it.

        The two are ordered, not independent. Every query requiring no lexeme
        also fails the decomposition -- an empty rendering yields no operand,
        and a rendering of only negations carries the character that refuses
        one -- so the first decision routes nothing the second would not.
        It stays because it names a different thing: nothing to intersect on
        rather than a shape that will not split, and it says so before a
        rendering is scanned for a shape it does not have.

        The two paths stay two because merging them changes what a caller sees,
        not merely how the answer is computed. The document-scoped path ranks
        on the query's required lexemes joined by ``|`` rather than on the
        query itself, which is empty for a query asking only for absences and
        would collapse every row to a document-level one; its excerpt is the
        best-ranking chunk under that alternation, which for a negated query
        can be a chunk holding the very term the caller excluded; its
        ``matched_chunk_count`` counts chunks carrying a required lexeme, which
        a negation-only query has none of; and its ``limit`` budgets documents
        where the fallback budgets rows. The port admits both row shapes for
        exactly this reason.

        The fallback covers two shapes rather than one because only the
        negation's scope is genuinely undecided. A top-level alternation could
        instead be distributed -- ``a | (b & c)`` as an arm for ``a`` unioned
        with the intersection of arms for ``b`` and ``c`` -- which would make
        it document-scoped and leave the fallback to negation alone. That is a
        recall change no decision has taken: it would newly let a document
        carrying ``b`` in one passage and ``c`` in another satisfy the branch.
        """
        with self._query_timer.measure(
            "search_bm25", params={"limit": limit, "filtered": bool(filters)}
        ):
            if not query or not query.strip():
                return []
            # Both renderings the dispatch can need, asked for once. Reading
            # the parse from the rendering rather than through
            # ``parse_keyword_query`` is what keeps it to one: that method
            # renders the query itself, and rendering is the only thing either
            # decision below needs the backend for.
            rendered, folded = await self._render_query_forms(query)
            parse = _parse_rendered(query, rendered)
            if not parse.terms:
                # Nothing to rank against, and nothing to intersect on: either
                # every word was discarded, or the query asked only for
                # absences, which the decomposition refuses.
                return await self._search_bm25_within_chunk(query, limit, filters)
            operands = _split_conjuncts(rendered)
            if operands is None:
                return await self._search_bm25_within_chunk(query, limit, filters)
            return await self._search_bm25_across_document(
                operands,
                _or_form(parse.terms),
                limit,
                filters,
                folded,
            )

    async def _render_query_forms(self, query: str) -> tuple[str, str | None]:
        """Both renderings a keyword search needs, in one round-trip.

        Returns the query as the text-search configuration reads it, and its
        separator-folded rendering -- the latter ``None`` when folding changes
        nothing or leaves no lexemes, so the caller drops the extra union arm
        rather than repeating one it already has.

        Folding is a pure text transform, so whether a second rendering is
        wanted is known before the statement is built. A query with nothing to
        fold therefore asks for one column rather than two and costs what it
        did when the renderings were separate calls; one with something to fold
        pays a column instead of a round-trip.
        """
        folded = fold_for_query(query)
        wants_folded = bool(folded.strip()) and folded != query
        column = f"websearch_to_tsquery('{TEXT_SEARCH_CONFIG}', %s)::text"
        rows = await self._fetchall(
            f"SELECT {', '.join([column] * (2 if wants_folded else 1))}",  # noqa: S608
            [query, folded] if wants_folded else [query],
        )
        if not rows:
            return "", None
        return rows[0][0] or "", (rows[0][1] or None) if wants_folded else None

    async def _search_bm25_across_document(
        self,
        operands: list[str],
        or_form: str,
        limit: int,
        filters: dict[str, str | list[str]] | None,
        folded: str | None = None,
    ) -> list[SearchResult]:
        """Resolve each operand across both surfaces, intersect, then rank.

        What this path matches, in what tokenization, when it is admitted, and
        how it sits relative to the other one are stated at ``search_bm25`` and
        are not restated here. What follows is this path's own shape.

        ``limit`` bounds documents rather than rows: a matching document is
        returned once, carrying its best-ranking passage as the excerpt and a
        count of how many of its passages carry a query term. A document
        matched only through its document surface is returned with no excerpt
        and a passage count of zero -- the count names passages, so a
        document-level hit must not inflate it.
        """
        where, where_params = self._build_where(filters)
        # Interpolated into the fragments below. Column names come from a fixed
        # allowlist and every value stays bound, so the S608 suppressions below
        # cover a clause with nothing caller-supplied in its text.
        predicate = f" AND {where}" if where else ""

        # One arm per operand, each spanning both authored surfaces. DISTINCT
        # rather than relying on the set operators to deduplicate: a
        # single-operand query has no INTERSECT, and duplicate document ids
        # would multiply every row the ranking join produces.
        arm = (
            "(SELECT DISTINCT document_id FROM chunks"  # noqa: S608
            f" WHERE {_passage_rows_only()} AND tsv @@ %s::tsquery{predicate}"
            " UNION"
            " SELECT DISTINCT document_id FROM document_surface"
            f" WHERE tsv_match @@ %s::tsquery{predicate})"
        )
        params: list[object] = []
        for operand in operands:
            params += [operand, *where_params, operand, *where_params]
        matched = "\nINTERSECT\n".join([arm] * len(operands))

        # The normalized whole-query arm, document surface only.
        if folded:
            # ``predicate`` is built from a fixed column allowlist and every
            # value stays bound, so nothing caller-supplied reaches the text.
            matched = (
                f"({matched})\nUNION\n"  # noqa: S608
                "(SELECT DISTINCT document_id FROM document_surface"
                f" WHERE tsv_match @@ %s::tsquery{predicate})"
            )
            params += [folded, *where_params]

        sql = (
            f"WITH matched AS (\n{matched}\n),"  # noqa: S608
            " surf AS ("
            " SELECT document_id, ts_rank(tsv_rank, %s::tsquery) AS surf_score"
            " FROM document_surface WHERE document_id IN (SELECT document_id FROM matched)"
            " ), ranked AS ("
            " SELECT c.document_id, c.heading_path, c.content,"
            " max(ts_rank(c.tsv, %s::tsquery)) OVER (PARTITION BY c.document_id) AS chunk_score,"
            " count(*) OVER (PARTITION BY c.document_id) AS matched_chunks,"
            " row_number() OVER (PARTITION BY c.document_id ORDER BY"
            " ts_rank(c.tsv, %s::tsquery) DESC, c.chunk_index) AS rn"
            " FROM chunks c JOIN matched USING (document_id)"
            f" WHERE {_passage_rows_only('c')} AND c.tsv @@ %s::tsquery{predicate}"
            " ) SELECT m.document_id,"
            " COALESCE(r.heading_path, ''), COALESCE(r.content, ''),"
            " GREATEST(COALESCE(r.chunk_score, 0), COALESCE(s.surf_score, 0)) AS doc_score,"
            " COALESCE(r.matched_chunks, 0) AS matched_chunks,"
            # No surviving passage row means nothing but the document surface
            # answered, which is what makes the row a document-level one.
            " (r.document_id IS NULL) AS is_document_surface"
            " FROM matched m"
            " LEFT JOIN (SELECT * FROM ranked WHERE rn = 1) r USING (document_id)"
            " LEFT JOIN surf s USING (document_id)"
            " ORDER BY doc_score DESC, m.document_id LIMIT %s"
        )
        params += [or_form, or_form, or_form, or_form, *where_params, limit]
        rows = await self._fetchall(sql, params)
        return [
            SearchResult(
                document_id=row[0],
                heading_path=row[1],
                content=row[2],
                score=float(row[3]),
                matched_chunk_count=int(row[4]),
                is_document_surface=bool(row[5]),
            )
            for row in rows
        ]

    async def _search_bm25_within_chunk(
        self,
        query: str,
        limit: int,
        filters: dict[str, str | list[str]] | None,
    ) -> list[SearchResult]:
        """Evaluate the whole query against a single text unit.

        Which queries reach this path, and why it and the document-scoped one
        are the whole set, are stated at ``search_bm25`` and are not restated
        here. What follows is why the scope is the one it is.

        What is unsettled for the shapes that arrive here is the *scope* of the
        match, so this keeps the scope they already had rather than inventing
        one: the query is satisfied within one unit of text, not assembled
        across a document.

        The document surface is a second such unit, not a second scope. A
        document's authored text spans both surfaces (CAS-ADR-049 Decision 7),
        so reading passages alone left a title unreachable by any query of
        these shapes -- a hole in the guarantee that a document is findable by
        its own name, and one no ordinary conjunction would reveal. Adding the
        surface as another unit closes it while leaving the open question --
        whether a negation should be document-scoped -- exactly as open.

        Provenance does not lapse here either. The surface arm matches on
        ``tsv_match``, which covers the authored half alone, and ranks on
        ``tsv_rank``, which also covers the derived half: derived text ranks
        and orients, and never satisfies a match (Decision 4).
        """
        where, where_params = self._build_where(filters)
        chunk_arm = (
            # The interpolations are a module constant and a predicate built
            # from a fixed column allowlist; every value stays bound.
            "SELECT document_id, heading_path, content, ts_rank(tsv, q) AS score,"  # noqa: S608
            " false AS is_document_surface"
            f" FROM chunks, websearch_to_tsquery('{TEXT_SEARCH_CONFIG}', %s) AS q"
            f" WHERE tsv @@ q AND {_passage_rows_only()}"
        )
        # A document-level row is not a passage, so it carries no excerpt and
        # no heading, exactly as it does on the arms above.
        surface_arm = (
            "SELECT document_id, '' AS heading_path, '' AS content,"  # noqa: S608
            " ts_rank(tsv_rank, q) AS score, true AS is_document_surface"
            f" FROM document_surface, websearch_to_tsquery('{TEXT_SEARCH_CONFIG}', %s) AS q"
            " WHERE tsv_match @@ q"
        )
        params: list[object] = [query]
        if where:
            chunk_arm += f" AND {where}"
            params += where_params
        params.append(query)
        if where:
            surface_arm += f" AND {where}"
            params += where_params
        sql = (
            f"SELECT document_id, heading_path, content, score, is_document_surface FROM ("  # noqa: S608
            f" ({chunk_arm}) UNION ALL ({surface_arm})"
            " ) u ORDER BY score DESC LIMIT %s"
        )
        params.append(limit)
        rows = await self._fetchall(sql, params)
        return [self._row_to_semantic_result(r) for r in rows]

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
            return _parse_rendered(query, await self._render_tsquery(query))

    async def get_chunks_by_heading_prefix(
        self, document_id: str, heading_prefix: str
    ) -> list[Chunk]:
        """Return chunks at the heading or any child heading, document order."""
        with self._query_timer.measure("get_chunks_by_heading_prefix"):
            child_pattern = self._escape_like(heading_prefix) + HEADING_PATH_SEPARATOR + "%"
            rows = await self._fetchall(
                f"SELECT {_SELECT_CHUNK_COLUMNS} FROM chunks "  # noqa: S608 -- fixed column constant
                "WHERE document_id = %s AND (heading_path = %s OR heading_path LIKE %s) "
                "ORDER BY chunk_index",
                (document_id, heading_prefix, child_pattern),
            )
            return [self._row_to_chunk(r) for r in rows]

    async def get_heading_paths(self, document_id: str) -> list[str]:
        """Return distinct heading paths in document order.

        Scoped to the passage surface by ``_passage_rows_only``, which is where
        that scoping is explained.
        """
        with self._query_timer.measure("get_heading_paths"):
            rows = await self._fetchall(
                "SELECT heading_path FROM chunks "  # noqa: S608 -- fixed predicate constant
                f"WHERE document_id = %s AND {_passage_rows_only()} "
                "GROUP BY heading_path ORDER BY MIN(chunk_index)",
                (document_id,),
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
        """Return a document's passages in document order.

        Scoped to the passage surface by ``_passage_rows_only``, which is what
        keeps a legacy document-level row out of reconstructed projection text
        and out of the abstraction stage's input.
        """
        with self._query_timer.measure("get_all_chunks"):
            rows = await self._fetchall(
                f"SELECT {_SELECT_CHUNK_COLUMNS} FROM chunks "  # noqa: S608 -- fixed constants
                f"WHERE document_id = %s AND {_passage_rows_only()} ORDER BY chunk_index",
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
    def _row_to_semantic_result(row: tuple) -> SearchResult:
        """A row from the semantic union, which spans both surfaces.

        The passage count follows from the discriminant rather than being
        selected alongside it: the arm ranks row by row, so a passage row
        stands for one passage and a document-level row for none
        (CAS-ADR-049 Decision 5).
        """
        is_document_surface = bool(row[4])
        return SearchResult(
            document_id=row[0],
            heading_path=row[1],
            content=row[2],
            score=float(row[3]),
            matched_chunk_count=0 if is_document_surface else 1,
            is_document_surface=is_document_surface,
        )

    @staticmethod
    def _row_to_chunk(row: tuple) -> Chunk:
        return Chunk(
            document_id=row[0],
            heading_path=row[1],
            indexed_structure=row[2],
            content=row[3],
            chunk_index=row[4],
            doc_type=row[5],
            lifecycle_status=row[6],
            project=row[7],
        )

    _escape_like = staticmethod(escape_like)
