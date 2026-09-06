"""PostgreSQL graph store for SAGE documents, edges, and users (CAS-ADR-042).

A native async psycopg3 store over a connection pool: concurrency is served by
Postgres, so there is no thread-local connection and no single-writer ceiling --
the contrast with the embedded SQLite store, whose thread-pool + per-thread
connection model serializes writes. The SQL is dialect-substituted from the
embedded store -- ``%s`` placeholders, ``jsonb`` columns wrapped on write and
read back as Python objects, the ``->>`` text accessor in place of
``json_extract``, native booleans, ``ILIKE`` for case-insensitive match -- and
behaves identically per the cross-backend parity suite.

The bare schema (tables, indexes, natural-key UNIQUE indexes) is provisioned out
of band by :mod:`sage.storage.postgres.schema`; this module owns the two
behavioral pieces that schema deliberately leaves to the graph-store
implementation: the chain-head maintenance trigger (created in
:meth:`PostgresGraphStore.initialize`) and the per-vault tier3 partial-unique
indexes (created on demand by :meth:`ensure_tier3_unique_index`).

Recursive-CTE note: Postgres permits at most one reference to a recursive CTE
inside its own recursive term, where SQLite permits several. The bidirectional
walks (``traverse`` with ``direction="both"``, ``chain_walk``,
``head_with_hash_for_chain``) are therefore expressed with a single self-join
whose ``ON`` matches either endpoint and whose ``CASE`` follows the other --
equivalent to SQLite's two-branch ``UNION`` but legal under the one-reference
rule.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from datetime import datetime
from typing import Any, Final

from psycopg import errors as pg_errors
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from sage.adapters.interfaces import (
    DOCUMENT_FACET_FIELDS,
    NON_CANONICAL_SOURCE_PATH_PATTERN,
    FacetFieldCounts,
    GraphStore,
    NaturalKeyConflict,
    StorageQueryError,
)
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
from sage.storage.tier3_uniqueness import (
    TIER3_UNIQUE_INDEX_PREFIX,
    Tier3UniqueIndexBlockedError,
    Tier3UniqueViolation,
    tier3_unique_index_name,
)
from sage.utils.sql_patterns import escape_like

# Names of the natural-key UNIQUE indexes (provisioned by the schema module).
# Postgres reports a unique-index violation with ``diag.constraint_name`` set to
# the index name, so matching on these names keeps a natural-key collision
# distinct from a tier3 collision or a primary-key collision.
_EDGES_UNIQ_INDEX = "idx_edges_uniq_natural_key"
_STAGING_EDGES_UNIQ_INDEX = "idx_staging_edges_uniq_natural_key"

# Defense-in-depth fence for tier3 keys interpolated into a ``->>`` accessor or
# an expression-index DDL. The service layer validates the same keys against the
# doc_type's metadata_schema; this is the last-line guarantee no caller string
# can break out of the path.
_TIER3_KEY_FORMAT = re.compile(r"^[A-Za-z0-9_]+$")
_DOC_TYPE_FORMAT = re.compile(r"^[a-z][a-z0-9_]*$")

# Columns safe to use in ORDER BY (prevent SQL injection). Mirrors the embedded
# store's allowlist.
_SORTABLE_COLUMNS: frozenset[str] = frozenset(
    {"title", "doc_type", "document_date", "lifecycle_status"}
)

# Final sort term making an enumeration's ordering total. ``id`` is the primary
# key of each table it is appended for -- documents and staging edges alike --
# so appending it breaks every tie the sort columns leave without disturbing the
# primary sort a caller asked for. The direction is arbitrary: what the
# consumers need is that the order is the same one twice, not which of two tied
# rows comes first.
#
# It is applied where it is applied, and this names no more than that: the
# catalog clause built below, and the enumerations keyed on ``created_at``,
# which one batch can write identically. Other orderings here remain partial --
# the metadata search ranks on two tiers and takes a limit, so which rows
# survive its cut is not fixed -- and appending this term to one of them is a
# change of its own, not something to infer from its presence here.
_ORDER_TIEBREAK: Final[str] = ", id ASC"

# The default document enumeration order: active documents first, then the most
# recently dated, with undated documents last. This is a browsing order -- it
# arranges rows for a reader, and enumeration has no notion of a better row, so
# what it owes is a sensible arrangement and (with the tiebreak) a repeatable
# one. An undated document sorts last here rather than being given a substitute
# date, which keeps the documents that carry an authored date in one unbroken
# sequence.
_SALIENCE_ORDER: Final[str] = (
    "CASE WHEN lifecycle_status = 'active' THEN 0 ELSE 1 END, "
    "CASE WHEN document_date IS NULL THEN 1 ELSE 0 END, "
    "document_date DESC"
)

# The order the two boost helpers rank their match set by before truncating it.
# Deliberately not ``_SALIENCE_ORDER``, though it agrees with it on every
# document carrying an authored date: the two answer different questions, and
# the fallback below is where that shows.
#
# This one exists to mirror the retrieval layer's own reranking, because a cut
# taken before that reranking runs should keep the rows the reranking would go
# on to raise. That reranking resolves a document's date as ``document_date``
# and falls back to ``source_modified_at``, so an undated but recently ingested
# document is boosted there -- and ranking it last here would cut it before it
# ever reached the boost. Both columns hold ISO-8601 text, so truncating the
# timestamp to its date leaves a value that compares lexically against
# ``document_date``. A document with neither still sorts last.
#
# The divergence is the point rather than an oversight: a browsing order should
# not invent a date for a document that has none, and a cut that mirrors a
# ranking must follow that ranking's own fallback.
_BOOST_CUT_DATE: Final[str] = "COALESCE(document_date, LEFT(source_modified_at, 10))"
_BOOST_CUT_ORDER: Final[str] = (
    "CASE WHEN lifecycle_status = 'active' THEN 0 ELSE 1 END, "
    f"CASE WHEN {_BOOST_CUT_DATE} IS NULL THEN 1 ELSE 0 END, "
    f"{_BOOST_CUT_DATE} DESC"
)

# Chain-head maintenance trigger DDL (CAS-ADR-031 supersession-lineage rule).
# Mirrors the embedded store's ``trg_tier3_chain_head_on_supersedes``: any
# supersedes edge insertion flips the target's ``is_chain_head`` to false, so
# the partial tier3 unique index never fires against a superseded predecessor.
_CHAIN_HEAD_FN_DDL = """
CREATE OR REPLACE FUNCTION trg_fn_chain_head_on_supersedes() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    UPDATE documents SET is_chain_head = false WHERE id = NEW.target_id;
    RETURN NEW;
END;
$$;
"""

_CHAIN_HEAD_TRIGGER_DDL = """
CREATE OR REPLACE TRIGGER trg_tier3_chain_head_on_supersedes
AFTER INSERT ON edges
FOR EACH ROW
WHEN (NEW.edge_type = 'supersedes' AND NEW.target_id IS NOT NULL)
EXECUTE FUNCTION trg_fn_chain_head_on_supersedes();
"""


def _tier3_equality_predicate(key: str, value: object) -> tuple[str, list[object]]:
    """Build an equality predicate for one tier3 field, routed by value type.

    ``tier3_metadata->>'<key>'`` extracts the member as ``text``. A Python
    ``int``, ``bool``, or ``float`` bound as a parameter adapts to a native
    Postgres type, and ``text = integer`` has no operator -- so a typed
    filter value could not be compared at all through the text accessor.

    Three branches, each load-bearing:

    * ``None`` keeps ``->> IS NULL``, which matches a stored JSON null and
      an absent key alike.
    * ``str`` keeps the text accessor. The canonical string keys are backed
      by expression indexes built on ``(tier3_metadata->>'<field>')``, and
      jsonb containment could not use them. It also preserves the string
      spelling of a typed field, which containment would reject as a type
      mismatch.
    * Every other value compares as jsonb through the ``->`` accessor,
      which yields the member itself rather than its text rendering.
      jsonb equality is typed and, for numbers, numeric: a stored ``1.0``
      renders as ``"1.0"`` and would not match a filter of ``1`` under
      text equality, but does here. It is equality and not containment
      (``@>``) on purpose -- containment is a subset test, so an array
      filter of ``[1, 2]`` would match a stored ``[1, 2, 3]``, which is
      not the exact-equality semantics this filter documents.

    ``key`` must already have passed ``_TIER3_KEY_FORMAT``; it is
    interpolated into the accessor on every branch.
    """
    if value is None:
        return f"tier3_metadata->>'{key}' IS NULL", []
    if isinstance(value, str):
        return f"tier3_metadata->>'{key}' = %s", [value]
    return f"tier3_metadata->'{key}' = %s::jsonb", [Jsonb(value)]


def _validate_tier3_identifier(doc_type: str, field: str) -> None:
    """Fence the tier3 identifiers interpolated into ``->>`` and index DDL."""
    if not _DOC_TYPE_FORMAT.match(doc_type):
        raise ValueError(f"Invalid doc_type identifier for tier3 index DDL: {doc_type!r}")
    if not _TIER3_KEY_FORMAT.match(field):
        raise ValueError(f"Invalid tier3 field identifier for index DDL: {field!r}")


def tier3_unique_index_ddl_pg(doc_type: str, field: str) -> str:
    """Postgres DDL for the partial UNIQUE expression index on (doc_type, field).

    The ``tier3_metadata->>'<field>'`` jsonb text accessor is the filter
    predicate; the boolean filter has no ``= 1`` comparison. Uniqueness is
    global within ``doc_type`` across all lifecycle statuses, excluding
    non-chain-heads (the supersession-lineage exception) and null values
    (so optional fields do not collide on null).
    """
    name = tier3_unique_index_name(doc_type, field)
    return (
        f"CREATE UNIQUE INDEX IF NOT EXISTS {name} "
        f"ON documents ((tier3_metadata->>'{field}')) "
        f"WHERE doc_type = '{doc_type}' "
        f"  AND is_chain_head "
        f"  AND (tier3_metadata->>'{field}') IS NOT NULL"
    )


def tier3_unique_index_drop_ddl_pg(doc_type: str, field: str) -> str:
    """DROP INDEX statement for the (doc_type, field) partial UNIQUE index."""
    return f"DROP INDEX IF EXISTS {tier3_unique_index_name(doc_type, field)}"


class PostgresGraphStore(GraphStore):
    """Async psycopg3 + connection-pool implementation of the GraphStore port.

    Constructed over an already-open ``AsyncConnectionPool`` (the caller owns the
    pool's lifecycle; ``close()`` marks the store closed but does not close an
    injected pool). Every operation acquires a pooled connection for its span, so
    independent writers run concurrently with no serialization.
    """

    def __init__(
        self,
        pool: Any,
        *,
        query_timer: QueryTimer | NullQueryTimer = NULL_QUERY_TIMER,
    ) -> None:
        self._pool = pool
        self._query_timer = query_timer
        self._closed = False

    # ------------------------------------------------------------------
    # Lifecycle + barrier
    # ------------------------------------------------------------------

    def _check_open(self) -> None:
        """Barrier check (CAS-ADR-036): once closed, every op raises."""
        if self._closed:
            raise RuntimeError("PostgresGraphStore is closed")

    async def initialize(self, migrate: bool = False) -> None:
        """Ensure the chain-head maintenance trigger exists.

        The bare schema (tables, indexes) is provisioned out of band by the
        schema module against the connection's search_path; this method
        idempotently installs the trigger that schema deliberately omits.
        ``migrate`` is accepted for port symmetry; the Postgres schema is
        provisioned externally and every statement here is replace-or-create, so
        the flag does not change behavior.
        """
        with self._query_timer.measure("initialize"):
            self._check_open()
            async with self._pool.connection() as conn:
                async with conn.transaction():
                    await conn.execute(_CHAIN_HEAD_FN_DDL)
                    await conn.execute(_CHAIN_HEAD_TRIGGER_DDL)

    async def close(self) -> None:
        """Mark the store closed (idempotent). Does not close an injected pool."""
        self._closed = True

    # ------------------------------------------------------------------
    # Low-level helpers
    # ------------------------------------------------------------------

    async def _fetch_rows(self, sql: str, params: Any = ()) -> list[dict]:
        self._check_open()
        async with self._pool.connection() as conn:
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(sql, params)
                return await cur.fetchall()

    async def _fetch_one(self, sql: str, params: Any = ()) -> dict | None:
        self._check_open()
        async with self._pool.connection() as conn:
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(sql, params)
                return await cur.fetchone()

    async def _fetch_scalar(self, sql: str, params: Any = ()) -> Any:
        self._check_open()
        async with self._pool.connection() as conn:
            cur = await conn.execute(sql, params)
            row = await cur.fetchone()
            return row[0] if row is not None else None

    async def _fetch_tuples(self, sql: str, params: Any = ()) -> list[tuple]:
        self._check_open()
        async with self._pool.connection() as conn:
            cur = await conn.execute(sql, params)
            return await cur.fetchall()

    async def _execute(self, sql: str, params: Any = ()) -> int:
        self._check_open()
        async with self._pool.connection() as conn:
            async with conn.transaction():
                cur = await conn.execute(sql, params)
                return cur.rowcount

    # ------------------------------------------------------------------
    # Document operations
    # ------------------------------------------------------------------

    async def insert_document(self, doc: Document) -> None:
        with self._query_timer.measure("insert_document"):
            try:
                async with self._pool.connection() as conn:
                    async with conn.transaction():
                        await self._exec_insert_document(conn, doc)
            except pg_errors.UniqueViolation as exc:
                await self._maybe_raise_tier3_violation(exc, doc)
                raise

    async def _exec_insert_document(self, conn: Any, doc: Document) -> None:
        """Issue the INSERT for a Document on ``conn`` without committing."""
        await conn.execute(
            """INSERT INTO documents (
                id, title, source_type, source_path, lifecycle_status,
                version_label, project, tags, authority_scope, doc_type,
                source_content_hash, stored_content_hash, adapter_version,
                created_by, created_at,
                last_modified_by, updated_at, projected_at, indexed_at,
                source_modified_at, document_date,
                semantic_abstract, pipeline_status, pipeline_error, tier3_metadata,
                metadata_confirmed
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                      %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
            (
                doc.id,
                doc.title,
                doc.source_type.value,
                doc.source_path,
                doc.lifecycle_status,
                doc.version_label,
                doc.project,
                Jsonb(doc.tags),
                doc.authority_scope,
                doc.doc_type,
                doc.source_content_hash,
                doc.stored_content_hash,
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
                Jsonb(doc.tier3_metadata) if doc.tier3_metadata else None,
                bool(doc.metadata_confirmed),
            ),
        )
        await self._sync_document_tags(conn, doc.id, doc.tags)

    async def _maybe_raise_tier3_violation(
        self,
        exc: pg_errors.UniqueViolation,
        doc: Document,
        supersedes_id: str | None = None,
    ) -> None:
        """Raise ``Tier3UniqueViolation`` if ``exc`` is a tier3 partial-unique
        violation; otherwise return and let the caller re-raise.

        Mirrors the embedded store: recover (doc_type, field) from the index
        name reported in ``diag.constraint_name``, then look up the existing
        holder via the same predicate the partial index uses. When
        ``supersedes_id`` is supplied, a collision against the designated
        predecessor is the supersession-lineage exception and does not raise.
        """
        index_name = exc.diag.constraint_name
        if not index_name or not index_name.startswith(TIER3_UNIQUE_INDEX_PREFIX):
            return
        if doc.doc_type is None:
            return
        prefix = f"{TIER3_UNIQUE_INDEX_PREFIX}{doc.doc_type}_"
        if not index_name.startswith(prefix):
            return  # a different doc_type's index fired
        field = index_name[len(prefix) :]
        if not _TIER3_KEY_FORMAT.match(field):
            return
        if not doc.tier3_metadata or field not in doc.tier3_metadata:
            return
        colliding_value = doc.tier3_metadata[field]
        # Same type-routing as the read path: a non-string unique key would
        # otherwise fail the holder lookup with `text = <native type>` while
        # reporting the collision it just caught.
        holder_clause, holder_params = _tier3_equality_predicate(field, colliding_value)
        row = await self._fetch_one(
            f"SELECT id FROM documents "  # noqa: S608 -- field validated by _TIER3_KEY_FORMAT
            f"WHERE doc_type = %s AND is_chain_head "
            f"AND {holder_clause} LIMIT 1",
            (doc.doc_type, *holder_params),
        )
        if row is None:
            return  # holder vanished between the failed write and the lookup
        existing_id = row["id"]
        if supersedes_id is not None and existing_id == supersedes_id:
            return
        raise Tier3UniqueViolation(
            doc_type=doc.doc_type,
            field=field,
            colliding_value=colliding_value,
            existing_document_id=existing_id,
        )

    async def _sync_document_tags(self, conn: Any, doc_id: str, tags: list[str] | None) -> None:
        """Rewrite the document_tags join rows for ``doc_id`` to match ``tags``."""
        await conn.execute("DELETE FROM document_tags WHERE document_id = %s", (doc_id,))
        if tags:
            async with conn.cursor() as cur:
                await cur.executemany(
                    "INSERT INTO document_tags (document_id, tag) VALUES (%s, %s) "
                    "ON CONFLICT DO NOTHING",
                    [(doc_id, t) for t in tags],
                )

    async def get_document(self, doc_id: str) -> Document | None:
        with self._query_timer.measure("get_document"):
            row = await self._fetch_one("SELECT * FROM documents WHERE id = %s", (doc_id,))
            return self._row_to_document(row) if row is not None else None

    async def update_document(self, doc_id: str, updates: dict) -> Document | None:
        if not updates:
            return await self.get_document(doc_id)
        async with self._pool.connection() as conn:
            async with conn.transaction():
                await self._exec_update_document(conn, doc_id, dict(updates))
        return await self.get_document(doc_id)

    async def _exec_update_document(self, conn: Any, doc_id: str, updates: dict) -> None:
        """Issue the UPDATE for a document on ``conn`` without committing.

        ``updates`` is a private copy the caller may mutate; collection and
        boolean fields are adapted in place to their Postgres wire forms.
        """
        if not updates:
            return
        new_tags: list[str] | None = updates["tags"] if "tags" in updates else None
        if "tags" in updates:
            updates["tags"] = Jsonb(updates["tags"])
        if "tier3_metadata" in updates:
            # Wrap unconditionally -- the embedded store json.dumps()es every
            # value on update (None -> "null", {} -> "{}"), so {} must round-trip
            # as {} (not collapse to NULL the way the insert path does). ``None``
            # and ``{}`` are stored as jsonb ``null`` / ``{}`` and read back as
            # Python None / {} respectively, matching SQLite exactly.
            updates["tier3_metadata"] = Jsonb(updates["tier3_metadata"])
        if "metadata_confirmed" in updates:
            updates["metadata_confirmed"] = bool(updates["metadata_confirmed"])
        if "is_chain_head" in updates:
            updates["is_chain_head"] = bool(updates["is_chain_head"])

        set_clause = ", ".join(f"{k} = %s" for k in updates)
        values = list(updates.values())
        values.append(doc_id)
        await conn.execute(
            f"UPDATE documents SET {set_clause} WHERE id = %s",  # noqa: S608 -- keys are trusted dict keys; values are %s params
            values,
        )
        if new_tags is not None:
            await self._sync_document_tags(conn, doc_id, new_tags)

    async def list_all_documents(self) -> list[Document]:
        with self._query_timer.measure("list_all_documents"):
            rows = await self._fetch_rows("SELECT * FROM documents")
            return [self._row_to_document(r) for r in rows]

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
        with self._query_timer.measure("query_documents"):
            where_sql, params = self._build_document_where(
                filters, default_exclude_failed=default_exclude_failed
            )

            # The filter predicates are the one part of this statement built
            # from caller input, so a driver rejection here is the caller's to
            # hear about -- but not in the driver's words, which quote the
            # statement and its hint. Scoped to this method rather than the
            # shared _fetch_* helpers: the write path relies on psycopg's
            # UniqueViolation propagating out of those to detect a tier3
            # collision, and a broader wrap would swallow it.
            try:
                total_count = await self._fetch_scalar(
                    f"SELECT COUNT(*) FROM documents WHERE {where_sql}",  # noqa: S608 -- builder-trusted; values are %s
                    params,
                )
                order_sql = self._build_order_clause(sort_by, sort_order)
                rows = await self._fetch_rows(
                    f"SELECT * FROM documents WHERE {where_sql} {order_sql} "  # noqa: S608 -- builder-trusted; values are %s
                    f"LIMIT %s OFFSET %s",
                    [*params, limit, offset],
                )
            except pg_errors.Error as exc:
                raise StorageQueryError("query_documents", str(exc)) from exc
            return [self._row_to_document(r) for r in rows], total_count

    @staticmethod
    def _build_document_where(
        filters: dict[str, object] | None,
        *,
        default_exclude_failed: bool,
    ) -> tuple[str, list[object]]:
        """Translate a document filter dict into a WHERE clause plus params.

        Shared by ``query_documents`` and ``query_document_facets`` so both
        surfaces resolve identical filter semantics. Returns ``("TRUE", [])``
        when nothing constrains the query. The tag predicate is a correlated
        ``EXISTS`` subquery qualified as ``documents.id``, so every consumer
        must keep the outer ``documents`` table unaliased.
        """
        where_clauses: list[str] = []
        params: list[object] = []

        if default_exclude_failed and (not filters or "pipeline_status" not in filters):
            where_clauses.append("pipeline_status != %s")
            params.append("failed")

        if filters:
            for col in (
                "doc_type",
                "project",
                "lifecycle_status",
                "pipeline_status",
                "source_type",
            ):
                if col in filters and filters[col]:
                    where_clauses.append(f"{col} = %s")
                    params.append(filters[col])
            if "document_ids" in filters and filters["document_ids"]:
                placeholders = ",".join("%s" for _ in filters["document_ids"])
                where_clauses.append(f"id IN ({placeholders})")
                params.extend(filters["document_ids"])
            if "tags" in filters and filters["tags"]:
                for tag in filters["tags"]:
                    where_clauses.append(
                        "EXISTS (SELECT 1 FROM document_tags "
                        "WHERE document_id = documents.id AND tag = %s)"
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
                    clause, clause_params = _tier3_equality_predicate(key, value)
                    where_clauses.append(clause)
                    params.extend(clause_params)

        where_sql = " AND ".join(where_clauses) if where_clauses else "TRUE"
        return where_sql, params

    @classmethod
    def _build_order_clause(cls, sort_by: str | None, sort_order: str | None) -> str:
        """Build a safe, total ORDER BY clause (active first, document_date desc default).

        Every branch orders on a column that admits ties -- hundreds of
        documents can share a ``document_date``, and the fallback sorts on
        ``title`` alone -- and Postgres is free to return tied rows in any
        order, and to choose a different one next time. Two things depend on it
        not doing that: ``limit``/``offset`` paging, which skips or repeats a
        row when the two queries disagree on where the page boundary falls, and
        the retrieval layer's budget hint, whose recommendation is measured on
        the premise that a lower limit returns the same rows truncated.

        ``_ORDER_TIEBREAK`` supplies what the sort columns cannot. Each branch
        contributes only its primary sort expression and every one of them
        returns through the single exit below, so a branch added later cannot
        be total-by-oversight -- which is how all three of these came to admit
        ties in the first place.
        """
        if sort_by is None:
            primary = _SALIENCE_ORDER
        elif sort_by not in _SORTABLE_COLUMNS:
            primary = "title"
        else:
            direction = "DESC" if sort_order == "desc" else "ASC"
            nulls_last = (
                "CASE WHEN document_date IS NULL THEN 1 ELSE 0 END, "
                if sort_by == "document_date"
                else ""
            )
            primary = f"{nulls_last}{sort_by} {direction}"
        return f"ORDER BY {primary}{_ORDER_TIEBREAK}"

    async def find_documents_by_title(self, title: str) -> list[Document]:
        with self._query_timer.measure("find_documents_by_title"):
            rows = await self._fetch_rows(
                "SELECT * FROM documents WHERE LOWER(title) = LOWER(%s)", (title,)
            )
            return [self._row_to_document(r) for r in rows]

    async def search_metadata(self, query: str, limit: int = 20) -> list[Document]:
        """Documents whose authored metadata carries the query as a substring.

        The title and the tags admit a document; the source path does not.
        A filename is incidental to how a document arrived rather than
        something an author wrote, and derived text ranks and orients but never
        satisfies a match (CAS-ADR-049 Decision 4) -- a rule that binds every
        path into a caller's result set, not the retrieval surfaces alone. The
        source path keeps its place in the ordering, where it can only arrange
        documents an authored field has already admitted.

        Beneath those two keys the match set is unranked -- containment either
        holds or does not, so two tag matches are equally good matches -- and
        the caller truncates. ``_BOOST_CUT_ORDER`` decides which of them
        survive and ``_ORDER_TIEBREAK`` makes that decision reproducible: the
        cut takes the documents the caller's own reranking would have raised
        anyway, rather than whichever ones the scan reached first. Those keys
        sit behind the match-quality keys, not in front, so a better match
        still outranks a more salient one.
        """
        with self._query_timer.measure("search_metadata"):
            # The query is text to find, not a pattern to apply: a caller's
            # own '%' or '_' would otherwise be read as this operator's
            # wildcards and silently widen the result.
            pattern = f"%{escape_like(query)}%"
            rows = await self._fetch_rows(
                "SELECT * FROM documents "  # noqa: S608 -- order from module constants; values are %s
                "WHERE title ILIKE %s "
                "   OR tags::text ILIKE %s "
                "ORDER BY "
                "  CASE WHEN title ILIKE %s THEN 0 ELSE 1 END, "
                "  CASE WHEN source_path ILIKE %s THEN 0 ELSE 1 END, "
                f" {_BOOST_CUT_ORDER}{_ORDER_TIEBREAK} "
                "LIMIT %s",
                (pattern, pattern, pattern, pattern, limit),
            )
            return [self._row_to_document(r) for r in rows]

    async def search_abstracts(self, query: str, limit: int = 20) -> list[Document]:
        """Documents whose generated abstract carries the query as a substring.

        Ordered on the same grounds as the sibling above and with the same two
        terms, but with nothing ahead of them: containment in an abstract
        admits a document and says nothing about how well it matched, so there
        is no match-quality key to rank the set by first. Without the ordering
        the truncation is a slice of whatever the scan reached, which Postgres
        need not choose the same way twice.
        """
        with self._query_timer.measure("search_abstracts"):
            # Escaped for the same reason the sibling above is: this result
            # feeds the abstract boost, which is another path into a caller's
            # result set, and a caller's own % or _ is text to find.
            pattern = f"%{escape_like(query)}%"
            rows = await self._fetch_rows(
                "SELECT * FROM documents WHERE semantic_abstract ILIKE %s "  # noqa: S608 -- order from module constants; values are %s
                f"ORDER BY {_BOOST_CUT_ORDER}{_ORDER_TIEBREAK} "
                "LIMIT %s",
                (pattern, limit),
            )
            return [self._row_to_document(r) for r in rows]

    # ------------------------------------------------------------------
    # Tier3 unique indexes (CAS-ADR-031)
    # ------------------------------------------------------------------

    async def ensure_tier3_unique_index(self, doc_type: str, field: str) -> None:
        _validate_tier3_identifier(doc_type, field)
        try:
            await self._execute(tier3_unique_index_ddl_pg(doc_type, field))
        except pg_errors.UniqueViolation as exc:
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
        _validate_tier3_identifier(doc_type, field)
        await self._execute(tier3_unique_index_drop_ddl_pg(doc_type, field))

    async def tier3_unique_index_exists(self, doc_type: str, field: str) -> bool:
        _validate_tier3_identifier(doc_type, field)
        name = tier3_unique_index_name(doc_type, field)
        # to_regclass resolves the unqualified name through the search_path and
        # returns NULL when no such relation exists.
        result = await self._fetch_scalar("SELECT to_regclass(%s) IS NOT NULL", (name,))
        return bool(result)

    async def find_chain_heads_with_tier3_value(
        self, doc_type: str, field: str
    ) -> list[tuple[object, list[str]]]:
        _validate_tier3_identifier(doc_type, field)
        rows = await self._fetch_rows(
            f"SELECT id, tier3_metadata->>'{field}' AS value "  # noqa: S608 -- field validated
            f"FROM documents "
            f"WHERE doc_type = %s AND is_chain_head "
            f"  AND tier3_metadata->>'{field}' IS NOT NULL",
            (doc_type,),
        )
        grouped: dict[object, list[str]] = {}
        for row in rows:
            grouped.setdefault(row["value"], []).append(row["id"])
        return [(v, ids) for v, ids in grouped.items()]

    # ------------------------------------------------------------------
    # Edge operations
    # ------------------------------------------------------------------

    async def insert_edge(self, edge: Edge, on_conflict: OnConflict = "raise") -> tuple[Edge, bool]:
        with self._query_timer.measure("insert_edge"):
            try:
                async with self._pool.connection() as conn:
                    async with conn.transaction():
                        await self._exec_insert_edge(conn, edge)
            except pg_errors.UniqueViolation as exc:
                if exc.diag.constraint_name == _EDGES_UNIQ_INDEX:
                    if on_conflict == "noop":
                        existing = await self.find_edge_by_natural_key(
                            edge.source_id, edge.target_id, edge.edge_type.value
                        )
                        if existing is not None:
                            return existing, False
                    raise NaturalKeyConflict(
                        edge.source_id, edge.target_id, edge.edge_type.value
                    ) from exc
                raise
            return edge, True

    async def _exec_insert_edge(self, conn: Any, edge: Edge) -> None:
        """Issue the INSERT for an edge on ``conn`` without committing."""
        await conn.execute(
            """INSERT INTO edges (
                id, source_id, target_id, edge_type, resolution_policy,
                source_valid_from_version, target_valid_from_version,
                valid_until_version, retracted_edge_id,
                created_at, notes, rationale, rationale_kind,
                synced_from_version, synced_from_content_hash
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
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
        if target_id is None:
            row = await self._fetch_one(
                "SELECT * FROM edges "
                "WHERE source_id = %s AND target_id IS NULL AND edge_type = %s LIMIT 1",
                (source_id, edge_type),
            )
        else:
            row = await self._fetch_one(
                "SELECT * FROM edges "
                "WHERE source_id = %s AND target_id = %s AND edge_type = %s LIMIT 1",
                (source_id, target_id, edge_type),
            )
        return self._row_to_edge(row) if row else None

    # --- Compound atomic operations ---

    async def supersede_atomic(
        self, predecessor_id: str, predecessor_updates: dict, edge: Edge
    ) -> Document | None:
        updates_with_chain_head = {**predecessor_updates, "is_chain_head": False}
        try:
            async with self._pool.connection() as conn:
                async with conn.transaction():
                    await self._exec_update_document(conn, predecessor_id, updates_with_chain_head)
                    await self._exec_insert_edge(conn, edge)
        except pg_errors.UniqueViolation as exc:
            if exc.diag.constraint_name == _EDGES_UNIQ_INDEX:
                raise NaturalKeyConflict(
                    edge.source_id, edge.target_id, edge.edge_type.value
                ) from exc
            raise
        return await self.get_document(predecessor_id)

    async def insert_with_supersede_atomic(
        self,
        new_doc: Document,
        predecessor_id: str,
        predecessor_updates: dict,
        edge: Edge,
    ) -> tuple[Document, Document]:
        updates_with_chain_head = {**predecessor_updates, "is_chain_head": False}
        try:
            async with self._pool.connection() as conn:
                async with conn.transaction():
                    await self._exec_update_document(conn, predecessor_id, updates_with_chain_head)
                    await self._exec_insert_document(conn, new_doc)
                    await self._exec_insert_edge(conn, edge)
        except pg_errors.UniqueViolation as exc:
            await self._maybe_raise_tier3_violation(exc, new_doc, supersedes_id=predecessor_id)
            if exc.diag.constraint_name == _EDGES_UNIQ_INDEX:
                raise NaturalKeyConflict(
                    edge.source_id, edge.target_id, edge.edge_type.value
                ) from exc
            raise
        inserted = await self.get_document(new_doc.id)
        updated_pred = await self.get_document(predecessor_id)
        return inserted, updated_pred

    async def get_edges_by_source(self, source_id: str, edge_type: str | None = None) -> list[Edge]:
        with self._query_timer.measure("get_edges_by_source"):
            if edge_type:
                rows = await self._fetch_rows(
                    "SELECT * FROM edges WHERE source_id = %s AND edge_type = %s",
                    (source_id, edge_type),
                )
            else:
                rows = await self._fetch_rows(
                    "SELECT * FROM edges WHERE source_id = %s", (source_id,)
                )
            return [self._row_to_edge(r) for r in rows]

    async def get_edges_by_target(self, target_id: str, edge_type: str | None = None) -> list[Edge]:
        with self._query_timer.measure("get_edges_by_target"):
            if edge_type:
                rows = await self._fetch_rows(
                    "SELECT * FROM edges WHERE target_id = %s AND edge_type = %s",
                    (target_id, edge_type),
                )
            else:
                rows = await self._fetch_rows(
                    "SELECT * FROM edges WHERE target_id = %s", (target_id,)
                )
            return [self._row_to_edge(r) for r in rows]

    async def query_edges(
        self,
        *,
        filters: dict[str, object] | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> tuple[list[EdgeQueryRow], int]:
        """Filtered, paginated edge enumeration with retraction state.

        Ordered by ``created_at`` descending, ties broken by edge id: the same
        total-order requirement ``_ORDER_TIEBREAK`` serves on the document
        query, for the same reason. Edges are written a transaction at a time
        and share a ``created_at`` freely, so without the tiebreak a caller
        paging this surface could skip an edge or receive one twice.

        The retraction pick below takes the same tiebreak on the same grounds.
        Nothing stops an edge being retracted twice -- a ``retracts`` edge
        carries a null target, so the natural-key index does not fire across
        two of them -- and the window reports whichever the row number ranks
        first. Ranking on ``created_at`` alone would let two retractions
        written together report either one, on different calls.
        """
        with self._query_timer.measure("query_edges"):
            where_clauses: list[str] = []
            params: list[object] = []
            if filters:
                for col in ("source_id", "target_id", "edge_type"):
                    if col in filters and filters[col]:
                        where_clauses.append(f"e.{col} = %s")
                        params.append(filters[col])
            where_sql = " AND ".join(where_clauses) if where_clauses else "TRUE"

            total_count = await self._fetch_scalar(
                f"SELECT COUNT(*) FROM edges e WHERE {where_sql}",  # noqa: S608 -- builder-trusted; values are %s
                params,
            )
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
                            ORDER BY created_at ASC, id ASC
                        ) AS rn
                    FROM edges
                    WHERE edge_type = 'retracts'
                      AND retracted_edge_id IS NOT NULL
                ) r ON r.retracted_edge_id = e.id AND r.rn = 1
                WHERE {where_sql}
                ORDER BY e.created_at DESC, e.id ASC
                LIMIT %s OFFSET %s
            """  # noqa: S608 -- builder-trusted; values are %s
            rows = await self._fetch_rows(sql, [*params, limit, offset])

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
        with self._query_timer.measure("get_supersedes_lineage"):
            self._check_open()
            exists = await self._fetch_scalar("SELECT 1 FROM documents WHERE id = %s", (doc_id,))
            if exists is None:
                return []
            # Single-direction recursive walk (source=newer -> target=older);
            # one self-reference, legal in Postgres. UNION (not UNION ALL)
            # terminates on cycles and dedupes diamonds.
            rows = await self._fetch_rows(
                "WITH RECURSIVE lineage(doc_id) AS ("
                "  SELECT %s::text"
                "  UNION"
                "  SELECT e.target_id "
                "  FROM edges e "
                "  INNER JOIN lineage l ON e.source_id = l.doc_id "
                "  WHERE e.edge_type = %s"
                ") "
                "SELECT doc_id FROM lineage",
                (doc_id, EdgeType.SUPERSEDES.value),
            )
            return [r["doc_id"] for r in rows]

    async def has_supersedes_successor(self, doc_id: str) -> bool:
        with self._query_timer.measure("has_supersedes_successor"):
            row = await self._fetch_scalar(
                "SELECT 1 FROM edges WHERE edge_type = %s AND target_id = %s LIMIT 1",
                (EdgeType.SUPERSEDES.value, doc_id),
            )
            return row is not None

    async def has_supersedes_predecessor(self, doc_id: str) -> bool:
        with self._query_timer.measure("has_supersedes_predecessor"):
            row = await self._fetch_scalar(
                "SELECT 1 FROM edges WHERE edge_type = %s AND source_id = %s LIMIT 1",
                (EdgeType.SUPERSEDES.value, doc_id),
            )
            return row is not None

    async def find_tombstone_candidates(self, lineage_ids: list[str]) -> list[str]:
        with self._query_timer.measure("find_tombstone_candidates"):
            if not lineage_ids:
                return []
            placeholders = ",".join("%s" for _ in lineage_ids)
            rows = await self._fetch_rows(
                f"SELECT id FROM edges "  # noqa: S608 -- ResolutionPolicy.NONE is an enum constant; ids are %s
                f"WHERE valid_until_version IS NULL "
                f"AND (resolution_policy IS NULL "
                f"     OR resolution_policy != '{ResolutionPolicy.NONE.value}') "
                f"AND (source_id IN ({placeholders}) OR target_id IN ({placeholders}))",
                [*lineage_ids, *lineage_ids],
            )
            return [row["id"] for row in rows]

    async def merge_atomic(
        self,
        merged_from_edge: Edge,
        tombstone_edge_ids: list[str],
        tombstone_version: str,
    ) -> None:
        with self._query_timer.measure("merge_atomic"):
            try:
                async with self._pool.connection() as conn:
                    async with conn.transaction():
                        await self._exec_insert_edge(conn, merged_from_edge)
                        if tombstone_edge_ids:
                            placeholders = ",".join("%s" for _ in tombstone_edge_ids)
                            await conn.execute(
                                f"UPDATE edges SET valid_until_version = %s "  # noqa: S608 -- ids are %s
                                f"WHERE id IN ({placeholders})",
                                [tombstone_version, *tombstone_edge_ids],
                            )
            except pg_errors.UniqueViolation as exc:
                if exc.diag.constraint_name == _EDGES_UNIQ_INDEX:
                    raise NaturalKeyConflict(
                        merged_from_edge.source_id,
                        merged_from_edge.target_id,
                        merged_from_edge.edge_type.value,
                    ) from exc
                raise

    async def read_link_context(
        self, request: LinkRequest, policy: ResolutionPolicy
    ) -> LinkReadContext:
        with self._query_timer.measure("read_link_context"):
            source_exists = (
                await self._fetch_scalar(
                    "SELECT 1 FROM documents WHERE id = %s", (request.source_id,)
                )
            ) is not None

            if request.target_id is None:
                target_exists = True
            else:
                target_exists = (
                    await self._fetch_scalar(
                        "SELECT 1 FROM documents WHERE id = %s", (request.target_id,)
                    )
                ) is not None

            retracted_edge: Edge | None = None
            if request.edge_type == EdgeType.RETRACTS and request.retracted_edge_id is not None:
                row = await self._fetch_one(
                    "SELECT * FROM edges WHERE id = %s", (request.retracted_edge_id,)
                )
                if row is not None:
                    retracted_edge = self._row_to_edge(row)

            source_anchor_exists = True
            if request.source_valid_from_version is not None:
                source_anchor_exists = (
                    await self._fetch_scalar(
                        "SELECT 1 FROM documents WHERE id = %s",
                        (request.source_valid_from_version,),
                    )
                ) is not None

            target_anchor_exists = True
            if request.target_valid_from_version is not None:
                target_anchor_exists = (
                    await self._fetch_scalar(
                        "SELECT 1 FROM documents WHERE id = %s",
                        (request.target_valid_from_version,),
                    )
                ) is not None

            source_lineage: frozenset[str] = frozenset()
            if source_exists and request.source_valid_from_version is not None:
                source_lineage = frozenset(await self.get_supersedes_lineage(request.source_id))

            target_lineage: frozenset[str] = frozenset()
            need_target_lineage = False
            if (
                policy in (ResolutionPolicy.TRANSITIVE_TARGET, ResolutionPolicy.TRANSITIVE_BOTH)
                and request.target_valid_from_version is not None
                and request.target_id is not None
            ):
                need_target_lineage = True
            if request.edge_type == EdgeType.MERGED_FROM and request.target_id is not None:
                need_target_lineage = True
            if need_target_lineage and target_exists:
                target_lineage = frozenset(await self.get_supersedes_lineage(request.target_id))

            has_sup_predecessor = False
            has_sup_successor = False
            tombstone_candidates: tuple[str, ...] = ()
            if request.edge_type == EdgeType.MERGED_FROM:
                has_sup_predecessor = (
                    await self._fetch_scalar(
                        "SELECT 1 FROM edges WHERE edge_type = %s AND source_id = %s LIMIT 1",
                        (EdgeType.SUPERSEDES.value, request.source_id),
                    )
                ) is not None
                if request.target_id is not None:
                    has_sup_successor = (
                        await self._fetch_scalar(
                            "SELECT 1 FROM edges WHERE edge_type = %s AND target_id = %s LIMIT 1",
                            (EdgeType.SUPERSEDES.value, request.target_id),
                        )
                    ) is not None
                if target_lineage:
                    tombstone_candidates = tuple(
                        await self.find_tombstone_candidates(list(target_lineage))
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
        with self._query_timer.measure("get_retracts_for_edges"):
            if not edge_ids:
                return {}
            placeholders = ",".join("%s" for _ in edge_ids)
            rows = await self._fetch_rows(
                f"SELECT * FROM edges "  # noqa: S608 -- ids are %s
                f"WHERE edge_type = %s AND retracted_edge_id IN ({placeholders})",
                [EdgeType.RETRACTS.value, *edge_ids],
            )
            grouped: dict[str, list[Edge]] = {}
            for row in rows:
                edge = self._row_to_edge(row)
                grouped.setdefault(edge.retracted_edge_id, []).append(edge)
            return grouped

    async def get_edge(self, edge_id: str) -> Edge | None:
        with self._query_timer.measure("get_edge"):
            row = await self._fetch_one("SELECT * FROM edges WHERE id = %s", (edge_id,))
            return self._row_to_edge(row) if row else None

    async def delete_edge(self, edge_id: str) -> bool:
        rowcount = await self._execute("DELETE FROM edges WHERE id = %s", (edge_id,))
        return rowcount > 0

    async def find_documents_by_hashes(self, hashes: list[str]) -> dict[str, str]:
        with self._query_timer.measure("find_documents_by_hashes"):
            if not hashes:
                return {}
            placeholders = ",".join("%s" for _ in hashes)
            rows = await self._fetch_rows(
                f"SELECT source_content_hash, id FROM documents "  # noqa: S608 -- ids are %s
                f"WHERE source_content_hash IN ({placeholders})",
                hashes,
            )
            return {row["source_content_hash"]: row["id"] for row in rows}

    async def find_documents_by_source_paths(self, source_paths: list[str]) -> dict[str, str]:
        with self._query_timer.measure("find_documents_by_source_paths"):
            if not source_paths:
                return {}
            # DISTINCT ON collapses the several-documents-one-path case to a
            # single representative row, and the id tie-break makes which one
            # deterministic rather than dependent on scan order.
            rows = await self._fetch_rows(
                "SELECT DISTINCT ON (source_path) source_path, source_content_hash "
                "FROM documents WHERE source_path = ANY(%s) "
                "ORDER BY source_path, id",
                (source_paths,),
            )
            return {row["source_path"]: row["source_content_hash"] for row in rows}

    async def find_document_ids_by_source_paths(
        self, source_paths: list[str]
    ) -> dict[str, list[str]]:
        with self._query_timer.measure("find_document_ids_by_source_paths"):
            if not source_paths:
                return {}
            # No DISTINCT ON here, unlike the method above: every id carrying a
            # path is the answer, and the id ordering makes the list stable.
            rows = await self._fetch_rows(
                "SELECT id, source_path FROM documents WHERE source_path = ANY(%s) "
                "ORDER BY source_path, id",
                (source_paths,),
            )
            found: dict[str, list[str]] = {}
            for row in rows:
                found.setdefault(row["source_path"], []).append(row["id"])
            return found

    async def list_non_canonical_source_paths(self) -> dict[str, str]:
        with self._query_timer.measure("list_non_canonical_source_paths"):
            # Passed as a parameter rather than embedded, so the one pattern the
            # port defines is the one the database matches on.
            rows = await self._fetch_rows(
                "SELECT id, source_path FROM documents WHERE source_path ~ %s",
                (NON_CANONICAL_SOURCE_PATH_PATTERN,),
            )
            return {row["id"]: row["source_path"] for row in rows}

    async def remove_document(self, document_id: str) -> None:
        with self._query_timer.measure("remove_document"):
            async with self._pool.connection() as conn:
                async with conn.transaction():
                    # Edges and staging edges reference documents with no ON
                    # DELETE CASCADE, so they must go before the documents row
                    # or the delete is FK-blocked. document_tags cascades, but
                    # is cleared explicitly for parity and independence from the
                    # cascade. All in one transaction: a single coordinated
                    # graph-store removal (CAS-ADR-042 weakest-binding).
                    await conn.execute(
                        "DELETE FROM document_tags WHERE document_id = %s", (document_id,)
                    )
                    await conn.execute(
                        "DELETE FROM edges WHERE source_id = %s OR target_id = %s",
                        (document_id, document_id),
                    )
                    await conn.execute(
                        "DELETE FROM staging_edges WHERE source_id = %s OR target_id = %s",
                        (document_id, document_id),
                    )
                    await conn.execute("DELETE FROM documents WHERE id = %s", (document_id,))

    async def find_documents_ingested_between(
        self, since: datetime, until: datetime | None = None
    ) -> list[Document]:
        with self._query_timer.measure("find_documents_ingested_between"):
            # created_at is stored as an ISO-8601 UTC string (see insert_document
            # and get_last_ingestion_at); consistent-format ISO strings compare
            # lexicographically in chronological order, so the window bounds are
            # bound as isoformat strings.
            if until is None:
                rows = await self._fetch_rows(
                    "SELECT * FROM documents WHERE created_at >= %s "  # noqa: S608 -- module constant; values are %s
                    f"ORDER BY created_at{_ORDER_TIEBREAK}",
                    (since.isoformat(),),
                )
            else:
                rows = await self._fetch_rows(
                    "SELECT * FROM documents WHERE created_at >= %s AND created_at < %s "  # noqa: S608 -- module constant; values are %s
                    f"ORDER BY created_at{_ORDER_TIEBREAK}",
                    (since.isoformat(), until.isoformat()),
                )
            return [self._row_to_document(r) for r in rows]

    # ------------------------------------------------------------------
    # Staging edge operations
    # ------------------------------------------------------------------

    async def list_staging_edges(self) -> list[StagingEdge]:
        with self._query_timer.measure("list_staging_edges"):
            rows = await self._fetch_rows(
                f"SELECT * FROM staging_edges ORDER BY created_at{_ORDER_TIEBREAK}"  # noqa: S608 -- module constant; no values
            )
            return [self._row_to_staging_edge(r) for r in rows]

    async def get_staging_edge(self, edge_id: str) -> StagingEdge | None:
        with self._query_timer.measure("get_staging_edge"):
            row = await self._fetch_one("SELECT * FROM staging_edges WHERE id = %s", (edge_id,))
            return self._row_to_staging_edge(row) if row else None

    async def insert_staging_edge(
        self, edge: StagingEdge, on_conflict: OnConflict = "raise"
    ) -> tuple[StagingEdge, bool]:
        with self._query_timer.measure("insert_staging_edge"):
            try:
                async with self._pool.connection() as conn:
                    async with conn.transaction():
                        await conn.execute(
                            """INSERT INTO staging_edges
                            (id, source_id, target_id, edge_type, inference_evidence,
                             confidence_tier, created_at)
                            VALUES (%s, %s, %s, %s, %s, %s, %s)""",
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
            except pg_errors.UniqueViolation as exc:
                if exc.diag.constraint_name == _STAGING_EDGES_UNIQ_INDEX:
                    if on_conflict == "noop":
                        existing = await self._find_staging_edge_by_natural_key(
                            edge.source_id, edge.target_id, edge.edge_type.value
                        )
                        if existing is not None:
                            return existing, False
                    raise NaturalKeyConflict(
                        edge.source_id, edge.target_id, edge.edge_type.value
                    ) from exc
                raise
            return edge, True

    async def _find_staging_edge_by_natural_key(
        self, source_id: str, target_id: str, edge_type: str
    ) -> StagingEdge | None:
        row = await self._fetch_one(
            "SELECT * FROM staging_edges "
            "WHERE source_id = %s AND target_id = %s AND edge_type = %s LIMIT 1",
            (source_id, target_id, edge_type),
        )
        return self._row_to_staging_edge(row) if row else None

    async def delete_staging_edge(self, edge_id: str) -> bool:
        rowcount = await self._execute("DELETE FROM staging_edges WHERE id = %s", (edge_id,))
        return rowcount > 0

    async def count_staging_edges(self) -> int:
        with self._query_timer.measure("count_staging_edges"):
            return await self._fetch_scalar("SELECT COUNT(*) FROM staging_edges")

    # ------------------------------------------------------------------
    # Statistics
    # ------------------------------------------------------------------

    async def query_document_facets(
        self,
        filters: dict[str, object] | None = None,
        *,
        fields: Sequence[str] | None = None,
        value_limit: int | None = None,
    ) -> tuple[dict[str, FacetFieldCounts], int]:
        with self._query_timer.measure("query_document_facets"):
            # Facets are an enumeration surface: no default failed-pipeline
            # exclusion, matching catalog document enumeration.
            where_sql, params = self._build_document_where(filters, default_exclude_failed=False)

            # Same containment as query_documents: the caller-built filter
            # predicates are what can make the backend refuse, and the
            # driver's wording is not the caller's to receive.
            try:
                return await self._collect_document_facets(where_sql, params, fields, value_limit)
            except pg_errors.Error as exc:
                raise StorageQueryError("query_document_facets", str(exc)) from exc

    async def _collect_document_facets(
        self,
        where_sql: str,
        params: list[object],
        fields: Sequence[str] | None,
        value_limit: int | None,
    ) -> tuple[dict[str, FacetFieldCounts], int]:
        """Run the per-field facet counts and the total for one WHERE clause.

        Each per-field aggregation is wrapped as a subquery so a single
        ``COUNT(*) OVER ()`` yields the true distinct-value total: the
        window is computed over every group before the outer ``ORDER BY``
        and ``LIMIT`` apply, so the total is cap-independent. ``LIMIT
        NULL`` is Postgres for "no limit", so one statement shape serves
        the capped and uncapped calls.
        """
        requested = (
            DOCUMENT_FACET_FIELDS
            if fields is None
            else tuple(f for f in DOCUMENT_FACET_FIELDS if f in set(fields))
        )
        facets: dict[str, FacetFieldCounts] = {}
        for field in requested:
            if field == "tags":
                # The tag filter predicate inside ``where_sql`` is a
                # correlated EXISTS subquery qualified as
                # ``documents.id``; the ``documents`` table inside this
                # subquery must stay unaliased for that correlation to
                # resolve. The join against ``document_tags`` (one row
                # per (document, tag) primary key) makes COUNT(*) the
                # per-tag distinct-document count.
                inner_sql = (
                    "SELECT document_tags.tag AS value, COUNT(*) AS doc_count "  # noqa: S608 -- builder-trusted; values are %s
                    "FROM document_tags JOIN documents "
                    "ON documents.id = document_tags.document_id "
                    f"WHERE {where_sql} "
                    "GROUP BY document_tags.tag"
                )
            else:
                inner_sql = (
                    f"SELECT {field} AS value, COUNT(*) AS doc_count "  # noqa: S608 -- field from DOCUMENT_FACET_FIELDS
                    "FROM documents "
                    f"WHERE ({where_sql}) AND {field} IS NOT NULL "
                    f"GROUP BY {field}"
                )
            rows = await self._fetch_tuples(
                f"SELECT value, doc_count, COUNT(*) OVER () FROM ({inner_sql}) AS facet_rows "  # noqa: S608 -- composed from builder-trusted parts
                "ORDER BY doc_count DESC, value ASC "
                "LIMIT %s",
                [*params, value_limit],
            )
            facets[field] = FacetFieldCounts(
                values={row[0]: row[1] for row in rows},
                total_distinct=rows[0][2] if rows else 0,
            )

        total = await self._fetch_scalar(
            f"SELECT COUNT(*) FROM documents WHERE {where_sql}",  # noqa: S608 -- builder-trusted; values are %s
            params,
        )
        return facets, total

    async def get_document_counts_by_field(self, field: str) -> dict[str, int]:
        with self._query_timer.measure("get_document_counts_by_field"):
            allowed = {"lifecycle_status", "doc_type", "source_type", "pipeline_status", "project"}
            if field not in allowed:
                return {}
            rows = await self._fetch_tuples(
                f"SELECT {field}, COUNT(*) FROM documents "  # noqa: S608 -- field checked against allowlist
                f"WHERE {field} IS NOT NULL GROUP BY {field}"
            )
            return {row[0]: row[1] for row in rows}

    async def get_edge_counts_by_type(self) -> dict[str, int]:
        with self._query_timer.measure("get_edge_counts_by_type"):
            rows = await self._fetch_tuples(
                "SELECT edge_type, COUNT(*) FROM edges GROUP BY edge_type"
            )
            return {row[0]: row[1] for row in rows}

    async def get_total_document_count(self) -> int:
        with self._query_timer.measure("get_total_document_count"):
            return await self._fetch_scalar("SELECT COUNT(*) FROM documents")

    async def get_total_edge_count(self) -> int:
        with self._query_timer.measure("get_total_edge_count"):
            return await self._fetch_scalar("SELECT COUNT(*) FROM edges")

    async def get_last_ingestion_at(self) -> datetime | None:
        with self._query_timer.measure("get_last_ingestion_at"):
            value = await self._fetch_scalar("SELECT MAX(created_at) FROM documents")
            return datetime.fromisoformat(value) if value is not None else None

    async def count_documents_by_pipeline_status(self, status: str) -> int:
        with self._query_timer.measure("count_documents_by_pipeline_status"):
            return await self._fetch_scalar(
                "SELECT COUNT(*) FROM documents WHERE pipeline_status = %s", (status,)
            )

    async def clear_pipeline_error_for_statuses(self, statuses: list[str]) -> int:
        if not statuses:
            return 0
        with self._query_timer.measure("clear_pipeline_error_for_statuses"):
            return await self._execute(
                "UPDATE documents SET pipeline_error = NULL "
                "WHERE pipeline_error IS NOT NULL AND pipeline_status = ANY(%s)",
                (list(statuses),),
            )

    async def list_pending_metadata_documents(self) -> list[Document]:
        with self._query_timer.measure("list_pending_metadata_documents"):
            rows = await self._fetch_rows(
                "SELECT * FROM documents WHERE metadata_confirmed = false"
            )
            return [self._row_to_document(r) for r in rows]

    async def measured_byte_size(self) -> int:
        """Live total relation size of the graph tables (heap + indexes + toast).

        Sums documents, edges, staging_edges, users, and document_tags --
        the tables that constitute the graph store. ``chunks`` (the content
        store) is deliberately excluded; that footprint is reported through
        ``ContentStore.measured_byte_size`` instead. ``to_regclass`` resolves
        against the connection's search_path and yields NULL for a table
        that has not been provisioned yet, so an unprovisioned schema sums
        to NULL and COALESCE reports 0 rather than raising.
        """
        with self._query_timer.measure("measured_byte_size"):
            value = await self._fetch_scalar(
                "SELECT COALESCE(SUM(pg_total_relation_size(to_regclass(t.name))), 0) "
                "FROM (VALUES ('documents'), ('edges'), ('staging_edges'), "
                "('users'), ('document_tags')) AS t(name)"
            )
            return int(value)

    async def storage_present(self, vault_id: str) -> bool:
        """Whether the vault's schema is still present in the shared database.

        The Postgres binding names each vault's schema by its vault id
        (CAS-ADR-042), so an out-of-band ``DROP SCHEMA`` is what this probe
        exists to see. It reads the schema catalog rather than resolving a
        table name: name resolution walks the connection's search_path and can
        fall through to a same-named table in a later entry, so a dropped
        schema could read as present -- the catalog probe cannot be masked
        that way.
        """
        with self._query_timer.measure("storage_present"):
            value = await self._fetch_scalar(
                "SELECT EXISTS(SELECT 1 FROM information_schema.schemata WHERE schema_name = %s)",
                (vault_id,),
            )
            return bool(value)

    # ------------------------------------------------------------------
    # Traversal
    # ------------------------------------------------------------------

    async def traverse(
        self, start_id: str, edge_type: str | None, direction: str, depth: int
    ) -> list[dict]:
        with self._query_timer.measure("traverse"):
            self._check_open()
            type_filter = " AND e.edge_type = %s" if edge_type else ""
            edge_cols = (
                "e.id AS edge_id, "
                "e.edge_type, e.created_at AS edge_created_at, "
                "e.notes, e.rationale, e.rationale_kind, e.source_id, e.target_id, "
                "e.resolution_policy, e.source_valid_from_version, "
                "e.target_valid_from_version, e.valid_until_version, "
                "e.retracted_edge_id, "
                "e.synced_from_version, e.synced_from_content_hash"
            )
            params: list = []
            if direction == "outbound":
                follow = "e.target_id"
                seed_match, recurse_join = "e.source_id = %s", "e.source_id = t.doc_id"
            elif direction == "inbound":
                follow = "e.source_id"
                seed_match, recurse_join = "e.target_id = %s", "e.target_id = t.doc_id"
            else:  # both -- single self-reference; follow the far endpoint
                follow = "CASE WHEN e.source_id = %s THEN e.target_id ELSE e.source_id END"
                seed_match = "(e.source_id = %s OR e.target_id = %s)"
                recurse_join = "(e.source_id = t.doc_id OR e.target_id = t.doc_id)"

            if direction == "both":
                seed_follow = follow  # references one %s (start_id)
                recurse_follow = (
                    "CASE WHEN e.source_id = t.doc_id THEN e.target_id ELSE e.source_id END"
                )
                seed = (
                    f"SELECT {edge_cols}, {seed_follow} AS doc_id, 1 AS depth "  # noqa: S608 -- trusted builders; values are %s
                    f"FROM edges e WHERE {seed_match}{type_filter}"
                )
                recurse = (
                    f"SELECT {edge_cols}, {recurse_follow} AS doc_id, t.depth + 1 AS depth "  # noqa: S608 -- trusted builders; values are %s
                    f"FROM edges e INNER JOIN traversal t ON {recurse_join} "
                    f"WHERE t.depth < %s{type_filter}"
                )
                params += [start_id, start_id, start_id]  # seed_follow CASE + seed_match OR
                if edge_type:
                    params.append(edge_type)
                params += [depth]
                if edge_type:
                    params.append(edge_type)
            else:
                seed = (
                    f"SELECT {edge_cols}, {follow} AS doc_id, 1 AS depth "  # noqa: S608 -- trusted builders; values are %s
                    f"FROM edges e WHERE {seed_match}{type_filter}"
                )
                recurse = (
                    f"SELECT {edge_cols}, {follow} AS doc_id, t.depth + 1 AS depth "  # noqa: S608 -- trusted builders; values are %s
                    f"FROM edges e INNER JOIN traversal t ON {recurse_join} "
                    f"WHERE t.depth < %s{type_filter}"
                )
                params += [start_id]
                if edge_type:
                    params.append(edge_type)
                params += [depth]
                if edge_type:
                    params.append(edge_type)

            sql = (
                f"WITH RECURSIVE traversal AS (\n"  # noqa: S608 -- fragments are trusted column/predicate builders; values are %s
                f"  {seed}\n"
                f"  UNION ALL\n"
                f"  {recurse}\n"
                f")\n"
                f"SELECT t.*, "
                f"d.id AS d_id, d.title, d.lifecycle_status, d.source_type, "
                f"d.source_path, d.version_label, d.project, d.doc_type, "
                f"d.tags::text AS tags, "
                f"d.document_date AS d_document_date, "
                f"d.source_modified_at AS d_source_modified_at "
                f"FROM traversal t "
                f"INNER JOIN documents d ON t.doc_id = d.id"
            )
            rows = await self._fetch_rows(sql, params)
            return [
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
                for row in rows
            ]

    async def chain_walk(self, start_id: str, edge_type: str) -> list[dict]:
        with self._query_timer.measure("chain_walk"):
            # Single self-reference: match either endpoint, follow the other.
            sql = """
                WITH RECURSIVE chain AS (
                    SELECT %s::text AS doc_id

                    UNION

                    SELECT CASE WHEN e.source_id = c.doc_id THEN e.target_id
                                ELSE e.source_id END AS doc_id
                    FROM edges e
                    INNER JOIN chain c
                        ON (e.source_id = c.doc_id OR e.target_id = c.doc_id)
                    WHERE e.edge_type = %s
                )
                SELECT c.doc_id,
                    d.title, d.lifecycle_status, d.version_label,
                    d.document_date
                FROM chain c
                INNER JOIN documents d ON c.doc_id = d.id
            """
            doc_rows = await self._fetch_rows(sql, (start_id, edge_type))
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
            doc_ids = [d["doc_id"] for d in documents]
            if len(doc_ids) <= 1:
                return {"documents": documents, "edges": []}

            placeholders = ",".join("%s" for _ in doc_ids)
            edge_rows = await self._fetch_rows(
                f"SELECT source_id, target_id FROM edges "  # noqa: S608 -- ids are %s
                f"WHERE edge_type = %s "
                f"AND source_id IN ({placeholders}) "
                f"AND target_id IN ({placeholders})",
                [edge_type, *doc_ids, *doc_ids],
            )
            edges = [
                {"source_id": row["source_id"], "target_id": row["target_id"]} for row in edge_rows
            ]
            return {"documents": documents, "edges": edges}

    async def list_provenance_edges(self, edge_types: list[str]) -> list[dict]:
        with self._query_timer.measure("list_provenance_edges"):
            if not edge_types:
                return []
            placeholders = ",".join("%s" for _ in edge_types)
            rows = await self._fetch_rows(
                f"SELECT id, edge_type, source_id, target_id, "  # noqa: S608 -- types are %s
                f"synced_from_version, synced_from_content_hash "
                f"FROM edges WHERE valid_until_version IS NULL "
                f"AND edge_type IN ({placeholders})",
                edge_types,
            )
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

    async def head_with_hash_for_chain(self, target_id: str, edge_type: str = "supersedes") -> dict:
        with self._query_timer.measure("head_with_hash_for_chain"):
            # Recursive term has one self-reference (the OR-join); the head-test
            # NOT EXISTS references the CTE only in the outer query, which is
            # unrestricted.
            sql = """
                WITH RECURSIVE chain AS (
                    SELECT %s::text AS doc_id

                    UNION

                    SELECT CASE WHEN e.source_id = c.doc_id THEN e.target_id
                                ELSE e.source_id END AS doc_id
                    FROM edges e
                    INNER JOIN chain c
                        ON (e.source_id = c.doc_id OR e.target_id = c.doc_id)
                    WHERE e.edge_type = %s
                )
                SELECT d.id AS head_id,
                       d.source_content_hash AS head_content_hash,
                       d.version_label       AS head_version_label
                FROM chain c
                INNER JOIN documents d ON c.doc_id = d.id
                WHERE NOT EXISTS (
                    SELECT 1 FROM edges e
                    INNER JOIN chain c2 ON e.source_id = c2.doc_id
                    WHERE e.edge_type = %s AND e.target_id = d.id
                )
            """
            rows = await self._fetch_rows(sql, (target_id, edge_type, edge_type))
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
        await self._execute(
            "INSERT INTO users (id, display_name, user_type, created_at) VALUES (%s, %s, %s, %s)",
            (user.id, user.display_name, user.user_type.value, user.created_at.isoformat()),
        )

    async def get_user(self, user_id: str) -> User | None:
        with self._query_timer.measure("get_user"):
            row = await self._fetch_one("SELECT * FROM users WHERE id = %s", (user_id,))
            return self._row_to_user(row) if row else None

    async def get_user_by_display_name(self, display_name: str) -> User | None:
        with self._query_timer.measure("get_user_by_display_name"):
            row = await self._fetch_one(
                "SELECT * FROM users WHERE display_name = %s", (display_name,)
            )
            return self._row_to_user(row) if row else None

    async def list_users(self) -> list[User]:
        with self._query_timer.measure("list_users"):
            rows = await self._fetch_rows("SELECT * FROM users")
            return [self._row_to_user(r) for r in rows]

    # ------------------------------------------------------------------
    # Row -> model conversions
    #
    # jsonb columns come back already parsed (tags as list, tier3_metadata as
    # dict); booleans as native bool; the ISO-text timestamp columns parse the
    # same as the embedded store. ``model_construct`` bypasses validation so a
    # repair workflow can read legacy values the request-side validators reject.
    # ------------------------------------------------------------------

    @staticmethod
    def _row_to_document(row: dict) -> Document:
        return Document.model_construct(
            id=row["id"],
            title=row["title"],
            source_type=SourceType(row["source_type"]),
            source_path=row["source_path"],
            lifecycle_status=row["lifecycle_status"],
            version_label=row["version_label"],
            project=row["project"],
            tags=row["tags"] or [],
            authority_scope=row["authority_scope"],
            doc_type=row["doc_type"],
            source_content_hash=row["source_content_hash"],
            # ``.get`` rather than ``[]``: a row selected before the additive
            # column reached this vault's schema carries no such key, and a
            # document predating the column is null there by definition.
            stored_content_hash=row.get("stored_content_hash"),
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
            tier3_metadata=row["tier3_metadata"],
            metadata_confirmed=bool(row["metadata_confirmed"]),
        )

    @staticmethod
    def _row_to_edge(row: dict) -> Edge:
        policy_value = row.get("resolution_policy")
        rationale_kind_value = row.get("rationale_kind") or "manual"
        return Edge(
            id=row["id"],
            source_id=row["source_id"],
            target_id=row["target_id"],
            edge_type=EdgeType(row["edge_type"]),
            resolution_policy=ResolutionPolicy(policy_value) if policy_value else None,
            source_valid_from_version=row.get("source_valid_from_version"),
            target_valid_from_version=row.get("target_valid_from_version"),
            valid_until_version=row.get("valid_until_version"),
            retracted_edge_id=row.get("retracted_edge_id"),
            created_at=datetime.fromisoformat(row["created_at"]),
            notes=row["notes"],
            rationale=row["rationale"],
            rationale_kind=RationaleKind(rationale_kind_value),
            synced_from_version=row.get("synced_from_version"),
            synced_from_content_hash=row.get("synced_from_content_hash"),
        )

    @staticmethod
    def _row_to_staging_edge(row: dict) -> StagingEdge:
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
    def _row_to_user(row: dict) -> User:
        return User(
            id=row["id"],
            display_name=row["display_name"],
            user_type=UserType(row["user_type"]),
            created_at=datetime.fromisoformat(row["created_at"]),
        )
