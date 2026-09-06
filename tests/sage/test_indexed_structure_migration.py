"""Deriving a passage's structure relative to its document (CAS-ADR-049 §3).

A vault provisioned before the decision carries no derived structure and ranks
the whole heading path at the top keyword weight, so a title a source format
made its top-level heading is indexed into every passage of that document. The
migration derives the structure and rebuilds the vector that ranks it.

Two conditions are repaired and they can disagree, which is what most of this
module is about. A pass interrupted after its derivation leaves nothing to
derive and a vector still built from the address; a migration guarding on the
derivation alone would leave such a vault unrepaired forever.

The ``pre_change_vault`` fixture stands in for a deployed vault by restoring the
pre-decision generated expression over a table that already has the column --
the exact state the bootstrap leaves on the first open after deploy. It asserts
its own precondition before yielding, which is the anti-coincidental-pass guard
for the whole module: a fixture that silently failed to install the old shape
would start from the post-state and every assertion below would pass vacuously.
"""

import asyncio
from datetime import datetime, timezone
from pathlib import Path

import pytest

from sage.adapters.content_store_postgres import PostgresContentStore
from sage.adapters.interfaces import Chunk
from sage.models.enums import SourceType
from sage.models.schemas import Document
from sage.services.maintenance import BACKFILL_PASSAGE_INDEXED_STRUCTURE, MaintenanceService
from sage.services.passage_structure import indexed_structure
from sage.storage.postgres.schema import EMBEDDING_DIM, TEXT_SEARCH_CONFIG

pytestmark = pytest.mark.asyncio

_TITLE = "T-0001: Zztitleword and the retrieval surface"

# Paths as the markdown adapter produces them for a document whose title is its
# H1: every one rooted at the title, including the H1's own passage.
_PATHS = (_TITLE, f"{_TITLE} > Problem", f"{_TITLE} > Design notes")

_PRE_CHANGE_TSV = (
    "ALTER TABLE chunks DROP COLUMN IF EXISTS tsv;"
    " ALTER TABLE chunks ADD COLUMN tsv tsvector GENERATED ALWAYS AS ("
    f"setweight(to_tsvector('{TEXT_SEARCH_CONFIG}', heading_path), 'A')"
    f" || setweight(to_tsvector('{TEXT_SEARCH_CONFIG}', content), 'D')"
    ") STORED;"
    " CREATE INDEX IF NOT EXISTS idx_chunks_tsv_gin ON chunks USING GIN (tsv);"
)


@pytest.fixture
async def store(pg_pool):
    return PostgresContentStore(pg_pool)


def _doc(document_id: str, title: str) -> Document:
    now = datetime.now(timezone.utc)
    return Document(
        id=document_id,
        title=title,
        source_type=SourceType.MARKDOWN,
        source_path=f"imports/{document_id}.md",
        lifecycle_status="active",
        source_content_hash=f"sha256:{0:064x}",
        adapter_version="1",
        created_by="t",
        created_at=now,
        last_modified_by="t",
        updated_at=now,
        doc_type="ticket",
    )


def _maintenance(graph_store, store, config, tmp_vault_dir) -> MaintenanceService:
    return MaintenanceService(
        vault_id=config.vault.id,
        graph_store=graph_store,
        config=config,
        registry_service=None,
        content_store=store,
        vault_dir=Path(tmp_vault_dir),
    )


async def _generation_expression(pg_pool) -> str:
    async with pg_pool.connection() as conn:
        cur = await conn.execute(
            "SELECT generation_expression FROM information_schema.columns"
            " WHERE table_schema = current_schema()"
            " AND table_name = 'chunks' AND column_name = 'tsv'"
        )
        row = await cur.fetchone()
    return (row[0] if row else "") or ""


async def _stored_structure(pg_pool, document_id: str) -> dict[str, str | None]:
    async with pg_pool.connection() as conn:
        cur = await conn.execute(
            "SELECT heading_path, indexed_structure FROM chunks WHERE document_id = %s",
            (document_id,),
        )
        return {r[0]: r[1] for r in await cur.fetchall()}


@pytest.fixture
async def pre_change_vault(store, postgres_graph_store, pg_pool):
    """A vault with the column present, every value underived, the old vector.

    Asserts its own precondition, because every test here is evidence only if
    the pre-state is genuinely the one a deployed vault is in.
    """
    document_id = "00000001_pre"
    await postgres_graph_store.insert_document(_doc(document_id, _TITLE))
    await store.index_chunks(
        document_id,
        [
            Chunk(
                document_id=document_id,
                heading_path=path,
                content=f"body prose under section {index}",
                embedding=[0.0] * EMBEDDING_DIM,
                chunk_index=index,
            )
            for index, path in enumerate(_PATHS)
        ],
    )
    async with pg_pool.connection() as conn:
        await conn.execute("UPDATE chunks SET indexed_structure = NULL")
        await conn.execute(_PRE_CHANGE_TSV)

    assert not await store.passage_vector_ranks_indexed_structure(), (
        "the stand-in must carry the pre-decision vector, or this module proves nothing"
    )
    assert await store.passages_awaiting_indexed_structure(), (
        "the stand-in must carry underived passages"
    )
    hits = await store.search_bm25("zztitleword", limit=10)
    assert [r.document_id for r in hits] == [document_id], (
        "pre-state control: the title term reaches the document through its "
        "passage heading paths, which is the behaviour being changed"
    )
    return document_id


# ---------------------------------------------------------------------------
# What the migration repairs
# ---------------------------------------------------------------------------


async def test_the_migration_derives_every_passage(
    store, postgres_graph_store, minimal_config, tmp_vault_dir, pre_change_vault, pg_pool
):
    """Each passage carries its structure relative to its document.

    Anti-coincidental-pass: a migration writing ``indexed_structure =
    heading_path`` for every row satisfies "no NULLs" and leaves behaviour
    identical to today. The two positive controls below are what make this an
    assertion about the rule rather than about non-nullness -- one row must
    differ from its address, and one must be the empty string the H1's own
    passage carries.
    """
    await _maintenance(postgres_graph_store, store, minimal_config, tmp_vault_dir).migrate_vault()

    stored = await _stored_structure(pg_pool, pre_change_vault)
    assert None not in stored.values(), "every passage must carry a derived structure"
    assert stored[_TITLE] == "", "the top-level heading's own passage has no relative structure"
    assert stored[f"{_TITLE} > Problem"] == "Problem"
    assert any(structure != path for path, structure in stored.items()), (
        "control: at least one passage must differ from its address, or a "
        "migration that copied the path would satisfy this test"
    )


async def test_the_migration_rebuilds_the_vector_that_ranks_it(
    store, postgres_graph_store, minimal_config, tmp_vault_dir, pre_change_vault, pg_pool
):
    """Weight A stops reading the address and starts reading the structure."""
    before = await _generation_expression(pg_pool)
    assert "heading_path" in before and "indexed_structure" not in before

    await _maintenance(postgres_graph_store, store, minimal_config, tmp_vault_dir).migrate_vault()

    after = await _generation_expression(pg_pool)
    assert "indexed_structure" in after
    assert await store.passage_vector_ranks_indexed_structure()


async def test_the_migration_recreates_the_index_over_the_vector(
    store, postgres_graph_store, minimal_config, tmp_vault_dir, pre_change_vault, pg_pool
):
    """Dropping the column drops its index, so the rebuild restores it.

    Anti-coincidental-pass: a migration that dropped and re-added the column but
    forgot the index leaves keyword search *working* on a small corpus -- it
    just sequential-scans -- so no query-level assertion in this module would
    notice. Only a catalog read does.
    """
    await _maintenance(postgres_graph_store, store, minimal_config, tmp_vault_dir).migrate_vault()

    async with pg_pool.connection() as conn:
        cur = await conn.execute(
            "SELECT indexdef FROM pg_indexes WHERE schemaname = current_schema() "
            "AND indexname = 'idx_chunks_tsv_gin'"
        )
        row = await cur.fetchone()
    assert row is not None, "the GIN index over the keyword vector was not recreated"
    assert "tsv" in row[0]


async def test_a_title_stops_reaching_the_passage_surface(
    store, postgres_graph_store, minimal_config, tmp_vault_dir, pre_change_vault
):
    """The caller-visible point of the change, on the passage surface alone.

    The fixture asserted the pre-state: the title term reached the document
    through its heading paths. After the migration it does not, while a term
    from a heading *within* the document still does -- CAS-ADR-049 keeps those
    at their ranking weight.

    No document surface is seeded here, so this is evidence about the passage
    surface by itself. In an ordinary vault the title is still reachable, from
    the surface that now carries it.
    """
    await _maintenance(postgres_graph_store, store, minimal_config, tmp_vault_dir).migrate_vault()

    assert await store.search_bm25("zztitleword", limit=10) == [], (
        "a document title that merely rooted every heading path must stop "
        "satisfying a match on the passage surface"
    )
    inner = await store.search_bm25("Design notes", limit=10)
    assert [r.document_id for r in inner] == [pre_change_vault], (
        "positive control: a heading within the document still matches, or this "
        "test would pass against a vault whose keyword arm stopped working"
    )


async def test_a_vault_that_took_the_column_but_not_the_migration_is_unchanged(
    store, postgres_graph_store, pg_pool
):
    """The window between the schema change and the migration is today's vault.

    An existing vault gains the column on its next open, from the bootstrap,
    and stays underived until an operator runs the migration. Through that
    window the coalesce reads the address, so the vault indexes exactly what it
    indexed before -- which is what lets the two ship independently.

    Anti-coincidental-pass: without the coalesce, ``NULL || content_tsv`` is
    ``NULL`` and the row matches *nothing*, so an assertion on the heading term
    alone could not tell "the fallback works" from "the fallback is gone and
    the whole vector is empty". The content term is the control that separates
    them.
    """
    document_id = "00000003_window"
    await postgres_graph_store.insert_document(_doc(document_id, _TITLE))
    await store.index_chunks(
        document_id,
        [
            Chunk(
                document_id=document_id,
                heading_path="Zzheadingword > Detail",
                content="zzbodyword prose",
                embedding=[0.0] * EMBEDDING_DIM,
                chunk_index=0,
            )
        ],
    )
    async with pg_pool.connection() as conn:
        await conn.execute("UPDATE chunks SET indexed_structure = NULL")
    stored = await _stored_structure(pg_pool, document_id)
    assert set(stored.values()) == {None}, "the row must be underived, or this proves nothing"

    heading = await store.search_bm25("zzheadingword", limit=10)
    assert [r.document_id for r in heading] == [document_id], (
        "an underived passage must go on being indexed by its address"
    )
    body = await store.search_bm25("zzbodyword", limit=10)
    assert [r.document_id for r in body] == [document_id], (
        "control: the content half of the vector survives too, so a failure "
        "above reads as a lost fallback rather than as an emptied vector"
    )


async def test_the_migration_takes_the_table_lock_before_it_decides(
    store, pre_change_vault, pg_pool
):
    """The decision is made under the lock the rebuild needs, not before it.

    Measured, and not what a reading of the code suggests: the probe reads a
    generated column's expression, which the catalog renders through
    ``pg_get_expr``, which opens the relation and takes a share lock. So the
    check already serializes against a concurrent rebuild on this server, and
    the double-rewrite it looks vulnerable to is not reachable here. The
    explicit lock is kept because that behaviour is an artifact of how a catalog
    view is evaluated rather than a documented guarantee, and this table is
    rebuilt on a different major version from the one these tests run against --
    which is the same hazard class the rest of this change exists to close.

    Anti-coincidental-pass, and the first version of this test failed it: with a
    second connection holding the table, the call does not finish either way,
    because an implementation that decided first would simply block later at the
    ``DROP``. Asserting that the call had not completed passed against both
    orderings; verified by mutation.

    What separates them is *which statement waits*. The assertion reads the
    blocked backend's current query: waiting at ``LOCK TABLE`` means the lock
    was taken first, and waiting at the catalog probe means it was not.
    """
    pending = await store.passages_awaiting_indexed_structure()
    derived = [(doc, path, indexed_structure(path, _TITLE)) for doc, path in pending]
    assert derived, "control: there is work for the pass to do"

    async def _blocked_statement() -> str:
        async with pg_pool.connection() as observer:
            cur = await observer.execute(
                "SELECT query FROM pg_stat_activity"
                " WHERE datname = current_database() AND wait_event_type = 'Lock'"
                " AND query ILIKE '%chunks%'"
            )
            rows = await cur.fetchall()
        return " | ".join(r[0] for r in rows)

    async def _wait_until_blocked(timeout: float = 10.0) -> str:
        """Poll until the migration is waiting on a lock, or give up.

        Polled rather than slept: a fixed pause is a bet on how quickly a loaded
        runner gets the second connection to its first statement, and the bet is
        wrong in the direction that reports a clean result -- observing before
        the migration has blocked at all finds nothing waiting, which the empty
        control below would report as "observing nothing" rather than as a race.
        """
        deadline = asyncio.get_running_loop().time() + timeout
        while asyncio.get_running_loop().time() < deadline:
            waiting = await _blocked_statement()
            if waiting:
                return waiting
            await asyncio.sleep(0.05)
        return ""

    migrating: asyncio.Task[int] | None = None
    try:
        async with pg_pool.connection() as blocker:
            async with blocker.transaction():
                await blocker.execute("LOCK TABLE chunks IN ACCESS EXCLUSIVE MODE")

                migrating = asyncio.create_task(store.migrate_indexed_structure(derived))
                waiting_on = await _wait_until_blocked()

                assert waiting_on, (
                    "control: the migration never blocked on the held lock, so "
                    "this test observed nothing"
                )
                assert "LOCK TABLE" in waiting_on.upper(), (
                    "the migration is waiting at "
                    f"{waiting_on!r}, past its own decision -- the check ran "
                    "against a catalog another caller was mid-change"
                )

            written = await asyncio.wait_for(migrating, timeout=30)
            migrating = None
    finally:
        # An assertion above leaves the migration blocked on a lock this test
        # holds; without this it outlives the test and the next one inherits a
        # connection stuck behind it.
        if migrating is not None:
            migrating.cancel()

    assert written == len(derived), "the pass completes once the lock is released"
    assert await store.passage_vector_ranks_indexed_structure()


async def test_a_backfill_does_not_hold_readers_out(store, pre_change_vault, pg_pool):
    """The lock stops rival migrators, not ordinary traffic.

    The decision's lock is followed by a plain ``UPDATE`` on the path where the
    vector is already current and only rows are outstanding. An exclusive lock
    there would hold every search and ingest on the vault behind a backfill that
    does not need it -- a regression against the pre-lock behaviour, where that
    path took ``ROW EXCLUSIVE`` and readers went through.

    Anti-coincidental-pass: a reader is held open in its own transaction for the
    whole of the backfill, so its ``ACCESS SHARE`` lock is live when the
    migration takes its own. Under ``ACCESS EXCLUSIVE`` the migration cannot
    proceed and this times out; under ``SHARE UPDATE EXCLUSIVE`` it completes.
    The two modes are distinguished by whether the call returns at all, which is
    why the reader is held rather than merely taken and released.
    """
    # Reach the backfill-only state: vector current, some rows underived.
    pending = await store.passages_awaiting_indexed_structure()
    await store.migrate_indexed_structure(
        [(doc, path, indexed_structure(path, _TITLE)) for doc, path in pending]
    )
    assert await store.passage_vector_ranks_indexed_structure(), "the vector is current"
    async with pg_pool.connection() as conn:
        await conn.execute("UPDATE chunks SET indexed_structure = NULL")
    still_pending = await store.passages_awaiting_indexed_structure()
    assert still_pending, "control: there is a backfill to run without a rebuild"

    async with pg_pool.connection() as reader:
        async with reader.transaction():
            cur = await reader.execute("SELECT count(*) FROM chunks")
            assert (await cur.fetchone())[0], "control: the reader holds a live lock"

            written = await asyncio.wait_for(
                store.migrate_indexed_structure(
                    [(doc, path, indexed_structure(path, _TITLE)) for doc, path in still_pending]
                ),
                timeout=10,
            )

    assert written == len(still_pending)


# ---------------------------------------------------------------------------
# Re-running it
# ---------------------------------------------------------------------------


async def test_the_migration_is_idempotent(
    store, postgres_graph_store, minimal_config, tmp_vault_dir, pre_change_vault, pg_pool
):
    """A second run repairs nothing and changes nothing.

    Follows the shape the document-surface migration established: the first run
    must actually repair something, stated as a control, or the second run's
    silence proves nothing.
    """
    service = _maintenance(postgres_graph_store, store, minimal_config, tmp_vault_dir)

    first = await service.migrate_vault()
    assert BACKFILL_PASSAGE_INDEXED_STRUCTURE in first.backfills_applied, (
        "control: the first run must repair something, or the second run's "
        "silence is not evidence of idempotency"
    )
    after_first = await _stored_structure(pg_pool, pre_change_vault)
    expression_after_first = await _generation_expression(pg_pool)

    second = await service.migrate_vault()
    assert BACKFILL_PASSAGE_INDEXED_STRUCTURE not in second.backfills_applied
    assert await _stored_structure(pg_pool, pre_change_vault) == after_first
    assert await _generation_expression(pg_pool) == expression_after_first


async def test_a_completed_derivation_still_repairs_a_stale_vector(
    store, postgres_graph_store, minimal_config, tmp_vault_dir, pre_change_vault, pg_pool
):
    """The interrupted-pass case: nothing to derive, and a vector still stale.

    Anti-coincidental-pass: this is the test a migration guarded solely on "are
    there rows awaiting derivation?" fails. The fixture below leaves zero such
    rows and the pre-decision expression, which is precisely the state a pass
    interrupted between its two halves leaves behind -- and which every other
    test in this module would report as a clean vault.
    """
    titles = {pre_change_vault: _TITLE}
    async with pg_pool.connection() as conn:
        cur = await conn.execute(
            "SELECT document_id, heading_path FROM chunks WHERE indexed_structure IS NULL"
        )
        pending = await cur.fetchall()
        for document_id, heading_path in pending:
            await conn.execute(
                "UPDATE chunks SET indexed_structure = %s "
                "WHERE document_id = %s AND heading_path = %s",
                (indexed_structure(heading_path, titles[document_id]), document_id, heading_path),
            )

    assert not await store.passages_awaiting_indexed_structure(), "nothing left to derive"
    assert not await store.passage_vector_ranks_indexed_structure(), "the vector is still stale"

    report = await _maintenance(
        postgres_graph_store, store, minimal_config, tmp_vault_dir
    ).migrate_vault()

    assert BACKFILL_PASSAGE_INDEXED_STRUCTURE in report.backfills_applied, (
        "a pass that repaired the vector must say so; a silent return reads as "
        "a vault that needed nothing"
    )
    assert await store.passage_vector_ranks_indexed_structure()


async def test_the_migration_reports_nothing_on_a_clean_vault(
    store, postgres_graph_store, minimal_config, tmp_vault_dir
):
    """A vault provisioned after the decision has no derivation to report."""
    document_id = "00000002_fresh"
    await postgres_graph_store.insert_document(_doc(document_id, _TITLE))
    await store.index_chunks(
        document_id,
        [
            Chunk(
                document_id=document_id,
                heading_path=_PATHS[1],
                content="body prose",
                embedding=[0.0] * EMBEDDING_DIM,
                chunk_index=0,
                indexed_structure="Problem",
            )
        ],
    )

    report = await _maintenance(
        postgres_graph_store, store, minimal_config, tmp_vault_dir
    ).migrate_vault()
    assert BACKFILL_PASSAGE_INDEXED_STRUCTURE not in report.backfills_applied


# ---------------------------------------------------------------------------
# The address does not change (CAS-ADR-049 Decision 3)
# ---------------------------------------------------------------------------


async def test_stored_addresses_survive_the_migration_byte_for_byte(
    store, postgres_graph_store, minimal_config, tmp_vault_dir, pre_change_vault, pg_pool
):
    """Heading enumeration and section reads resolve exactly as before.

    Anti-coincidental-pass: asserting merely that the paths are "still
    title-rooted" would pass a migration that rewrote them to some other
    title-rooted form. A byte-for-byte snapshot of the whole address column
    cannot.
    """

    async def _addresses():
        async with pg_pool.connection() as conn:
            cur = await conn.execute(
                "SELECT document_id, chunk_index, heading_path FROM chunks ORDER BY 1, 2"
            )
            return await cur.fetchall()

    before = await _addresses()
    assert before, "control: there are addresses to preserve"

    await _maintenance(postgres_graph_store, store, minimal_config, tmp_vault_dir).migrate_vault()

    assert await _addresses() == before
    assert await store.get_heading_paths(pre_change_vault) == list(_PATHS), (
        "enumeration returns the addresses the source produced, title root included"
    )
