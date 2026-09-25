"""Reading authored text inside markup on the document surface (CAS-ADR-049 §9).

The text-search parser reads everything from ``<`` to the matching ``>`` as one
markup tag and indexes none of it, so a title such as ``Parsing <scan> reports``
could not be matched on ``scan``. The document surface's two keyword vectors now
parse their text with the angle brackets replaced by spaces; the stored text is
unchanged, and the match vector still reads authored text only.

The ``pre_markup_surface`` fixture stands in for a deployed vault by restoring
the earlier generated expressions over the surface table, and asserts its own
precondition before yielding: a fixture that silently failed to install the old
shape would start from the post-state and every assertion here would pass
vacuously.
"""

import asyncio
from pathlib import Path

import pytest

from sage.adapters.content_store_postgres import PostgresContentStore
from sage.adapters.interfaces import DocumentSurface
from sage.services.maintenance import (
    BACKFILL_DOCUMENT_SURFACE_KEYWORD_VECTORS,
    MaintenanceService,
)
from sage.storage.postgres.schema import (
    DOCUMENT_SURFACE_TSV_REBUILD,
    EMBEDDING_DIM,
    TEXT_SEARCH_CONFIG,
)

pytestmark = pytest.mark.asyncio

_TITLED = "00000001_markup_title"
_DERIVED = "00000002_markup_derived"
_MATCHABLE = "Parsing <scan> reports\n<zzmarkuptag>"
_ORIENTING = "An abstract that names <zzderivedterm> only here."

# The generated expressions a vault carried before its surface read markup text.
_PRE_MARKUP_SURFACE = (
    "ALTER TABLE document_surface DROP COLUMN IF EXISTS tsv_match;"
    " ALTER TABLE document_surface DROP COLUMN IF EXISTS tsv_rank;"
    " ALTER TABLE document_surface ADD COLUMN tsv_match tsvector GENERATED ALWAYS AS ("
    f"setweight(to_tsvector('{TEXT_SEARCH_CONFIG}', matchable), 'A')"
    ") STORED;"
    " ALTER TABLE document_surface ADD COLUMN tsv_rank tsvector GENERATED ALWAYS AS ("
    f"setweight(to_tsvector('{TEXT_SEARCH_CONFIG}', matchable), 'A')"
    f" || setweight(to_tsvector('{TEXT_SEARCH_CONFIG}', orienting), 'D')"
    ") STORED;"
    " CREATE INDEX IF NOT EXISTS idx_document_surface_tsv_match_gin"
    " ON document_surface USING GIN (tsv_match);"
    " CREATE INDEX IF NOT EXISTS idx_document_surface_tsv_rank_gin"
    " ON document_surface USING GIN (tsv_rank);"
)


@pytest.fixture
async def store(pg_pool):
    return PostgresContentStore(pg_pool)


def _maintenance(graph_store, store, config, tmp_vault_dir) -> MaintenanceService:
    return MaintenanceService(
        vault_id=config.vault.id,
        graph_store=graph_store,
        config=config,
        registry_service=None,
        content_store=store,
        vault_dir=Path(tmp_vault_dir),
    )


async def _seed(store: PostgresContentStore) -> None:
    await store.upsert_document_surface(
        DocumentSurface(
            document_id=_TITLED,
            matchable=_MATCHABLE,
            orienting="",
            embedding=[0.0] * EMBEDDING_DIM,
        )
    )
    await store.upsert_document_surface(
        DocumentSurface(
            document_id=_DERIVED,
            matchable="Quarterly summary",
            orienting=_ORIENTING,
            embedding=[0.0] * EMBEDDING_DIM,
        )
    )


async def _hits(store: PostgresContentStore, term: str) -> list[str]:
    return [r.document_id for r in await store.search_bm25(term, limit=10)]


async def _vector_holds(pg_pool, column: str, document_id: str, term: str) -> bool:
    async with pg_pool.connection() as conn:
        cur = await conn.execute(
            f"SELECT {column} @@ to_tsquery('{TEXT_SEARCH_CONFIG}', %s)"  # noqa: S608
            " FROM document_surface WHERE document_id = %s",
            (term, document_id),
        )
        return bool((await cur.fetchone())[0])


async def _stored_text(pg_pool) -> dict[str, tuple[str, str]]:
    async with pg_pool.connection() as conn:
        cur = await conn.execute("SELECT document_id, matchable, orienting FROM document_surface")
        return {r[0]: (r[1], r[2]) for r in await cur.fetchall()}


async def _column_identity(pg_pool) -> dict[str, int]:
    """The attribute number of each vector column: a drop-and-re-add changes it."""
    async with pg_pool.connection() as conn:
        cur = await conn.execute(
            "SELECT attname, attnum FROM pg_attribute"
            " WHERE attrelid = 'document_surface'::regclass"
            " AND attname IN ('tsv_match', 'tsv_rank') AND NOT attisdropped"
        )
        return {r[0]: r[1] for r in await cur.fetchall()}


async def _generation_expressions(pg_pool) -> dict[str, str]:
    async with pg_pool.connection() as conn:
        cur = await conn.execute(
            "SELECT column_name, generation_expression FROM information_schema.columns"
            " WHERE table_schema = current_schema() AND table_name = 'document_surface'"
            " AND column_name IN ('tsv_match', 'tsv_rank')"
        )
        return {r[0]: r[1] or "" for r in await cur.fetchall()}


@pytest.fixture
async def pre_markup_surface(store, pg_pool):
    """A vault whose surface vectors parse markup brackets in place.

    Asserts its own precondition, because every test here is evidence only if
    the pre-state is genuinely the one a deployed vault is in. The schema
    outlives the test, so the current vectors are restored on teardown whatever
    the test did; otherwise a failure here would hand a later test the old ones.
    """
    await _seed(store)
    async with pg_pool.connection() as conn:
        await conn.execute(_PRE_MARKUP_SURFACE)
    try:
        await _assert_pre_markup_state(store, pg_pool)
        yield
    finally:
        async with pg_pool.connection() as conn:
            for statement in DOCUMENT_SURFACE_TSV_REBUILD:
                await conn.execute(statement)


async def _assert_pre_markup_state(store: PostgresContentStore, pg_pool) -> None:
    """The stand-in is the pre-change state: stale vectors, markup terms unread."""
    assert not await store.document_surface_vector_is_current(), (
        "the stand-in must carry the vectors that drop markup, or this proves nothing"
    )
    assert _TITLED not in await _hits(store, "scan"), (
        "pre-state control: the title term is inside a tag the old vector drops"
    )
    assert not await _vector_holds(pg_pool, "tsv_rank", _DERIVED, "zzderivedterm"), (
        "pre-state control: the derived term is inside a tag the old rank vector drops"
    )


async def test_a_title_term_inside_markup_matches_after_the_rebuild_and_not_before(
    store, postgres_graph_store, minimal_config, tmp_vault_dir, pre_markup_surface
):
    """A term inside angle brackets in a title or tag matches once the vault is
    migrated; the fixture shows it does not before."""
    report = await _maintenance(
        postgres_graph_store, store, minimal_config, tmp_vault_dir
    ).migrate_vault()

    assert BACKFILL_DOCUMENT_SURFACE_KEYWORD_VECTORS in report.backfills_applied
    assert await store.document_surface_vector_is_current()
    for term in ("scan", "zzmarkuptag"):
        assert await _hits(store, term) == [_TITLED], term


async def test_a_derived_term_inside_markup_ranks_but_does_not_match(
    store, postgres_graph_store, minimal_config, tmp_vault_dir, pre_markup_surface, pg_pool
):
    """Derived text inside markup is read by the rank vector and never by the
    match vector, so it orients without satisfying a caller's term.

    Anti-coincidental-pass: the fixture shows the rank vector lacked the term, so
    its presence here is the rebuild's doing, and the match assertion would go
    red if the orienting text were admitted to the match vector.
    """
    await _maintenance(postgres_graph_store, store, minimal_config, tmp_vault_dir).migrate_vault()

    assert await _vector_holds(pg_pool, "tsv_rank", _DERIVED, "zzderivedterm")
    assert not await _vector_holds(pg_pool, "tsv_match", _DERIVED, "zzderivedterm")
    assert _DERIVED not in await _hits(store, "zzderivedterm")


async def test_stored_matchable_and_orienting_text_is_unchanged(
    store, postgres_graph_store, minimal_config, tmp_vault_dir, pre_markup_surface, pg_pool
):
    """Only the indexed form changes: the stored text keeps its brackets."""
    before = await _stored_text(pg_pool)
    assert before[_TITLED][0] == _MATCHABLE, "control: the stored text carries markup"

    await _maintenance(postgres_graph_store, store, minimal_config, tmp_vault_dir).migrate_vault()

    assert await _stored_text(pg_pool) == before


async def test_the_rebuild_runs_once_and_a_second_migration_is_a_no_op(
    store, postgres_graph_store, minimal_config, tmp_vault_dir, pre_markup_surface, pg_pool
):
    """The currency check reads Postgres's normalized expression, so a migrated
    vault is recognized as current and its vectors are not rebuilt again."""
    maintenance = _maintenance(postgres_graph_store, store, minimal_config, tmp_vault_dir)
    first = await maintenance.migrate_vault()
    assert BACKFILL_DOCUMENT_SURFACE_KEYWORD_VECTORS in first.backfills_applied
    identity = await _column_identity(pg_pool)
    assert set(identity) == {"tsv_match", "tsv_rank"}
    expressions = await _generation_expressions(pg_pool)
    assert all("'<>'::text" in e for e in expressions.values()), (
        "control: the stored expressions are Postgres's normalization, not the source text"
    )

    second = await maintenance.migrate_vault()

    assert BACKFILL_DOCUMENT_SURFACE_KEYWORD_VECTORS not in second.backfills_applied
    assert await _column_identity(pg_pool) == identity, "a second run dropped a column"


async def test_a_vault_bootstrapped_fresh_is_current(
    store, postgres_graph_store, minimal_config, tmp_vault_dir
):
    """A vault the current bootstrap provisioned already carries the current
    vectors, so the migration has nothing to rebuild on it."""
    await _seed(store)
    assert await store.document_surface_vector_is_current()

    report = await _maintenance(
        postgres_graph_store, store, minimal_config, tmp_vault_dir
    ).migrate_vault()

    assert BACKFILL_DOCUMENT_SURFACE_KEYWORD_VECTORS not in report.backfills_applied
    assert await _hits(store, "scan") == [_TITLED]


# A rank vector that reads authored text substituted and derived text raw: the
# state the rank marker's derived-text element exists to recognize.
_HALF_SUBSTITUTED_RANK = (
    "ALTER TABLE document_surface DROP COLUMN IF EXISTS tsv_rank;"
    " ALTER TABLE document_surface ADD COLUMN tsv_rank tsvector GENERATED ALWAYS AS ("
    f"setweight(to_tsvector('{TEXT_SEARCH_CONFIG}', translate(matchable, '<>', '  ')), 'A')"
    f" || setweight(to_tsvector('{TEXT_SEARCH_CONFIG}', orienting), 'D')"
    ") STORED;"
    " CREATE INDEX IF NOT EXISTS idx_document_surface_tsv_rank_gin"
    " ON document_surface USING GIN (tsv_rank);"
)


async def test_a_rank_vector_reading_derived_text_raw_is_stale(
    store, postgres_graph_store, minimal_config, tmp_vault_dir, pg_pool
):
    """Each half of the rank vector is checked, not only the authored half.

    Anti-coincidental-pass: the match vector here is current and the rank
    vector's authored half is too, so a currency check reading any one marker
    per column would call this vault migrated and leave derived text inside
    markup unread.
    """
    await _seed(store)
    try:
        async with pg_pool.connection() as conn:
            await conn.execute(_HALF_SUBSTITUTED_RANK)
        assert not await _vector_holds(pg_pool, "tsv_rank", _DERIVED, "zzderivedterm"), (
            "control: the derived term is unread by this rank vector"
        )

        assert not await store.document_surface_vector_is_current()
        report = await _maintenance(
            postgres_graph_store, store, minimal_config, tmp_vault_dir
        ).migrate_vault()

        assert BACKFILL_DOCUMENT_SURFACE_KEYWORD_VECTORS in report.backfills_applied
        assert await _vector_holds(pg_pool, "tsv_rank", _DERIVED, "zzderivedterm")
    finally:
        async with pg_pool.connection() as conn:
            for statement in DOCUMENT_SURFACE_TSV_REBUILD:
                await conn.execute(statement)


async def test_the_rebuild_takes_the_table_lock_before_it_decides(
    store, pre_markup_surface, pg_pool
):
    """The decision is made under the lock the rebuild needs, not before it.

    The observable is which statement waits behind a lock another session
    holds: waiting at ``LOCK TABLE`` means the lock was taken before the
    currency check, and waiting at the catalog probe means it was not.
    """

    async def _blocked_statement() -> str:
        async with pg_pool.connection() as observer:
            cur = await observer.execute(
                "SELECT query FROM pg_stat_activity"
                " WHERE datname = current_database() AND wait_event_type = 'Lock'"
                " AND query ILIKE '%document_surface%'"
            )
            rows = await cur.fetchall()
        return " | ".join(r[0] for r in rows)

    async def _wait_until_blocked(timeout: float = 10.0) -> str:
        """Poll until the rebuild is waiting on a lock, or give up."""
        deadline = asyncio.get_running_loop().time() + timeout
        while asyncio.get_running_loop().time() < deadline:
            waiting = await _blocked_statement()
            if waiting:
                return waiting
            await asyncio.sleep(0.05)
        return ""

    rebuilding: asyncio.Task[bool] | None = None
    try:
        async with pg_pool.connection() as blocker:
            async with blocker.transaction():
                await blocker.execute("LOCK TABLE document_surface IN ACCESS EXCLUSIVE MODE")

                rebuilding = asyncio.create_task(store.rebuild_document_surface_vector())
                waiting_on = await _wait_until_blocked()

                assert waiting_on, (
                    "control: the rebuild never blocked on the held lock, so this "
                    "test observed nothing"
                )
                assert "LOCK TABLE" in waiting_on.upper(), (
                    f"the rebuild is waiting at {waiting_on!r}, past its own decision"
                )

            rebuilt = await asyncio.wait_for(rebuilding, timeout=30)
            rebuilding = None
    finally:
        # A failed assertion leaves the rebuild blocked on this test's lock.
        if rebuilding is not None:
            rebuilding.cancel()

    assert rebuilt, "the rebuild runs once the lock is released"
    assert await store.document_surface_vector_is_current()
