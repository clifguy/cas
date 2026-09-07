"""PostgresContentStore: behaviour, stats/bloat, and RRF fusion (CAS-ADR-042).

Runs against a real Postgres named by ``SAGE_TEST_PG_DSN`` via the ``pg_pool``
harness (a disposable per-session schema, truncated per test); skips cleanly
when no server is configured.
"""

from __future__ import annotations

import asyncio
import inspect
import math
import os
import re
import time
from datetime import timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

from sage.adapters.content_store_postgres import (
    _CONTENT_STORE_SURFACES,
    PostgresContentStore,
)
from sage.adapters.interfaces import (
    LEGACY_DOCUMENT_HEADER_CHUNK_INDEX,
    LEGACY_DOCUMENT_HEADER_HEADING_PATH,
    Chunk,
    DocumentSurface,
)
from sage.storage.postgres.schema import EMBEDDING_DIM
from sage.utils.rrf import rrf_fuse

if TYPE_CHECKING:
    from psycopg_pool import AsyncConnectionPool

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _emb(index: int) -> list[float]:
    """A unit one-hot embedding -- orthogonal vectors give exact cosine 0 or 1."""
    vec = [0.0] * EMBEDDING_DIM
    vec[index % EMBEDDING_DIM] = 1.0
    return vec


def _graded_emb(cos: float, off: int) -> list[float]:
    """A unit vector at cosine ``cos`` to ``_emb(0)`` (``cos*e0 + sin*e_off``).

    Lets a corpus carry strictly distinct cosine similarities to one query
    vector, so the semantic ranking is fully deterministic and identical
    across bindings.
    """
    vec = [0.0] * EMBEDDING_DIM
    vec[0] = cos
    vec[off] = math.sqrt(max(0.0, 1.0 - cos * cos))
    return vec


def _chunk(
    document_id: str,
    *,
    content: str,
    heading_path: str = "Section",
    chunk_index: int = 0,
    embedding: list[float] | None = None,
    doc_type: str | None = None,
    lifecycle_status: str | None = None,
    project: str | None = None,
) -> Chunk:
    return Chunk(
        document_id=document_id,
        heading_path=heading_path,
        content=content,
        embedding=embedding if embedding is not None else [0.0] * EMBEDDING_DIM,
        chunk_index=chunk_index,
        doc_type=doc_type,
        lifecycle_status=lifecycle_status,
        project=project,
    )


def _dense_embedding(seed: int) -> list[float]:
    """A dense, low-compressibility 768-d vector (a distinct value per dimension).

    Churned rows then carry real TOAST weight and the heap spans whole pages, so
    the relation-size and free-space bloat signals register. A one-hot vector
    compresses to almost nothing and collapses the heap below a single page."""
    return [0.05 + 0.0001 * ((seed + j) % 997) for j in range(EMBEDDING_DIM)]


def _fat_chunks(doc_id: str, k: int) -> list[Chunk]:
    """Sizeable chunks: ~900 bytes of inline content plus a dense embedding, so a
    large batch spans many heap pages (and TOAST segments)."""
    body = "lorem ipsum dolor sit amet bloat token padding " * 19  # ~900 chars
    return [
        _chunk(
            doc_id,
            content=f"{i} {body}",
            heading_path=f"H{i}",
            chunk_index=i,
            embedding=_dense_embedding(i),
        )
        for i in range(k)
    ]


async def _churn(store: PostgresContentStore, doc_id: str = "bloat"):
    """Create measurable, vacuum-reclaimable bloat: a large write followed by a
    tiny replacement, leaving most rows dead. index_chunks is delete-then-insert,
    so the 200 prior rows become dead MVCC tuples awaiting reclamation.

    Scoped to the passage surface. The document surface has its own churn helper
    below, and the tests that discriminate the two surfaces need each reachable
    without the other."""
    await store.index_chunks(doc_id, _fat_chunks(doc_id, k=200))
    await store.index_chunks(doc_id, _fat_chunks(doc_id, k=5))


def _fat_surface(doc_id: str, generation: int) -> DocumentSurface:
    """A document-level row heavy enough to register on the bloat signals.

    The same weight argument as ``_fat_chunks``: a sparse embedding compresses to
    almost nothing and a short text keeps the heap under a single page, so a
    churned surface built from either would read as zero bloat however the
    accounting is written -- a fixture that cannot distinguish the two
    implementations this file is about."""
    body = "lorem ipsum dolor sit amet surface padding " * 21  # ~900 chars
    return DocumentSurface(
        document_id=doc_id,
        matchable=f"Title {doc_id}",
        orienting=f"{generation} {body}",
        embedding=_dense_embedding(generation),
    )


async def _churn_document_surface(
    store: PostgresContentStore, *, docs: int = 40, rewrites: int = 5
) -> None:
    """Create vacuum-reclaimable bloat on the document surface alone.

    ``upsert_document_surface`` is delete-then-insert, which is the write the
    surface takes on every metadata change and every abstract refresh, so each
    rewrite past the first leaves one dead row behind: ``docs * (rewrites - 1)``
    of them. The passage surface is not touched."""
    for generation in range(rewrites):
        for i in range(docs):
            await store.upsert_document_surface(_fat_surface(f"surface-{i}", generation))


# Every other backend that currently pins part of the reclaim horizon. The two
# columns the predicate reads are named rather than positional: ``xid`` is an
# assigned transaction id, which pins from any database; ``pins_snapshot`` is a
# snapshot held in *this* database, which is the only scope a snapshot pins in.
_HORIZON_HOLDERS_SQL = """
    SELECT pid,
           backend_xid::text AS xid,
           (backend_xmin IS NOT NULL
            AND datname IS NOT DISTINCT FROM current_database()) AS pins_snapshot,
           datname,
           backend_type,
           state,
           left(coalesce(query, ''), 80) AS query
    FROM pg_stat_activity
    WHERE pid <> pg_backend_pid()
      AND (backend_xid IS NOT NULL OR backend_xmin IS NOT NULL)
"""


async def _horizon_holders(pg_pool: AsyncConnectionPool) -> list[Any]:
    """Current horizon holders, as rows carrying ``pid`` / ``xid`` / ``pins_snapshot``."""
    from psycopg.rows import namedtuple_row

    async with pg_pool.connection() as conn, conn.cursor(row_factory=namedtuple_row) as cur:
        await cur.execute(_HORIZON_HOLDERS_SQL)
        return await cur.fetchall()


async def _horizon_diagnostics(pg_pool: AsyncConnectionPool) -> str:
    """Everything that can pin the horizon, including the classes the wait cannot see.

    Prepared transactions have no ``pg_stat_activity`` row at all, and a
    replication slot's ``xmin`` pins without any backend attached, so a reclaim
    that fails anyway fails with the wait reporting nothing to wait for. Dumping
    all three here turns that into a named holder instead of the opaque
    ``post_versions`` mismatch this file was debugged from once already."""
    import psycopg

    parts = [f"horizon holders: {await _horizon_holders(pg_pool)}"]
    probes = (
        ("prepared transactions", "SELECT gid, database, transaction::text FROM pg_prepared_xacts"),
        (
            "replication slots",
            "SELECT slot_name, database, xmin::text, catalog_xmin::text FROM pg_replication_slots",
        ),
    )
    async with pg_pool.connection() as conn:
        for label, sql in probes:
            try:
                rows: object = await (await conn.execute(sql)).fetchall()
            except psycopg.Error as exc:
                rows = f"unavailable ({exc})"
            parts.append(f"{label}: {rows}")
    return "\n".join(parts)


async def _await_reclaimable_horizon(pg_pool: AsyncConnectionPool, timeout: float = 60.0) -> None:
    """Wait until ``VACUUM FULL`` can actually reclaim this test's dead tuples.

    The rewrite keeps every dead tuple that anything might still see, so a
    reclaim assertion made while the horizon is pinned fails for reasons that
    have nothing to do with the store. Two different holders pin it, and they
    differ in scope -- measured against PostgreSQL 17, not assumed:

    * A backend holding a **snapshot** in *this* database. An autovacuum
      ANALYZE worker is enough. The same snapshot held from another database
      does not pin ours.
    * A transaction holding an **assigned transaction id**, in *any* database
      on the server, that was assigned before this test deleted its tuples.
      The rewrite's cutoff is bounded by the oldest transaction id still
      running cluster-wide, so a writer in an unrelated database blocks the
      reclaim just as effectively as a local one.

    The second is why this must be called after the churn rather than before,
    and why a parallel run needs it where a serial run did not: sibling workers
    provision their own databases and write to them, and every one of those
    writes holds a transaction id. Waiting for the whole server to fall quiet
    would never return under a full parallel suite. Waiting only for the
    writers that were *already* open does return, because a transaction that
    starts later carries a newer id and cannot hold tuples this test has
    already deleted. That set is finite and short-lived.

    The xid half is deliberately not restricted by database, so the incumbents
    can include a backend the suite does not own. Routine work cannot hold one
    for long -- every store write is a self-contained pooled transaction, and
    the slow embedding and abstraction work runs between transactions -- but a
    long single-statement utility against a live vault, such as a content-store
    optimize or a vault teardown, can. Running one concurrently with the suite
    makes these two tests wait it out; past the ceiling they fail naming the
    backend and its query. The ceiling is generous for that reason rather than
    for the sibling workers, which clear in a tick.

    Only ``VACUUM FULL`` is pinned this way. A plain ``VACUUM`` reclaims under
    the same held cross-database id, which is why the fragment test below needs
    no such wait.

    Bounded; on timeout the remaining holders are named.

    ``pg_stat_activity`` shows other roles' ``backend_xmin`` and ``backend_xid``
    only to a superuser or a member of ``pg_read_all_stats``, and autovacuum
    workers run as the bootstrap superuser; without that visibility the wait
    would be a silent no-op, so the precondition is checked first and fails
    loudly."""
    async with pg_pool.connection() as conn:
        privilege = await (
            await conn.execute(
                "SELECT rolsuper OR pg_has_role(current_user, 'pg_read_all_stats', 'member') "
                "FROM pg_roles WHERE rolname = current_user"
            )
        ).fetchone()
    assert privilege and privilege[0], (
        "cannot see other backends' snapshots: run the suite as a superuser "
        "or GRANT pg_read_all_stats TO the test role"
    )

    # Writers already open at this point are exactly the ones whose ids can
    # predate the deletion; identify each by (pid, xid) so a later transaction
    # on the same backend is not mistaken for the one being waited out. Every
    # member carries a truthy xid, so a holder with none cannot match.
    incumbent_writers = {(h.pid, h.xid) for h in await _horizon_holders(pg_pool) if h.xid}

    deadline = time.monotonic() + timeout
    while True:
        blocking = [
            h
            for h in await _horizon_holders(pg_pool)
            if (h.pid, h.xid) in incumbent_writers or h.pins_snapshot
        ]
        if not blocking:
            return
        if time.monotonic() > deadline:
            raise AssertionError(f"reclaim horizon still pinned after {timeout}s by: {blocking}")
        await asyncio.sleep(0.2)


async def _disable_autovacuum(pg_pool) -> None:
    """Pin autovacuum off on every content-store surface for a bloat test.

    Bloat is observed by deliberately *not* reclaiming dead tuples; with
    autovacuum live, the launcher could clear them mid-test and make the signal
    vanish. Test-only determinism -- production leaves autovacuum on (it is the
    binding's self-healing path, with optimize() as the forcing function).

    Reads the surface set from the binding rather than naming the tables here. A
    surface the accounting measures but this helper does not pin would let the
    launcher clear the very dead tuples the test asserts on, which reds
    intermittently rather than outright -- the worst way for a fixture to be
    wrong."""
    async with pg_pool.connection() as conn:
        for surface in _CONTENT_STORE_SURFACES:
            await conn.execute(f"ALTER TABLE {surface} SET (autovacuum_enabled = false)")


# Per-surface bloat readings, spelled as direct SQL rather than reused from the
# binding. The totals are asserted against these, and a control taken through the
# code under test would agree with it by construction whatever it measured.
_SURFACE_STATS_SQL = """
    SELECT pg_total_relation_size(%s::regclass),
           dead_tuple_count,
           table_len / current_setting('block_size')::bigint,
           free_space / current_setting('block_size')::bigint
    FROM pgstattuple(%s)
"""


async def _surface_stats(pg_pool: AsyncConnectionPool, surface: str) -> dict[str, int]:
    """``bytes`` / ``versions`` / ``fragments`` / ``small_fragments`` for one surface."""
    async with pg_pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(_SURFACE_STATS_SQL, (surface, surface))
        row = await cur.fetchone()
    assert row is not None, f"pgstattuple returned no row for {surface!r}"
    return {
        "bytes": int(row[0]),
        "versions": int(row[1]),
        "fragments": int(row[2]),
        "small_fragments": int(row[3]),
    }


async def _surface_row_count(pg_pool: AsyncConnectionPool, surface: str) -> int:
    """Live row count for one surface, read outside the binding."""
    async with pg_pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(f"SELECT count(*) FROM {surface}")  # noqa: S608 -- fixed constant
        row = await cur.fetchone()
    return int(row[0]) if row else 0


@pytest.fixture
async def store(pg_pool):
    return PostgresContentStore(pg_pool)


# ---------------------------------------------------------------------------
# pgstattuple EXECUTE grant (CAS-ADR-042)
#
# The cloud bootstrap grants each workload role EXECUTE on the untrusted
# pgstattuple functions the content store uses for bloat measurement; CONNECT +
# CREATE alone does not carry it. This proves the emitted grant *confers the
# privilege* against a real server -- a role without it hits InsufficientPrivilege
# and gains access once the grant runs -- not merely that a string was produced.
# The cloud bootstrap module itself is Entra-only and never connects to local PG;
# this exercises its pure statement builder.
# ---------------------------------------------------------------------------


async def test_pgstattuple_grant_confers_execute_to_unprivileged_role(pg_dsn):
    """A role lacking the grant cannot execute pgstattuple; the bootstrap's grant fixes it.

    Mirrors the content store's own call form (an unknown-typed name literal, so whichever
    overload production resolves to is the one exercised). SELECT on the probe table is
    granted first so pgstattuple's relation-level check passes and the *function* EXECUTE
    privilege is what the assertion isolates.
    """
    import psycopg

    from sage.storage.postgres.cloud_bootstrap import pgstattuple_grant_statement

    role = "pgst-probe-unpriv"

    async def _drop_role_if_present(conn) -> None:
        # Existence check is parameterized; the drops interpolate the (hardcoded,
        # validator-legal) role as a quoted identifier -- identifiers cannot be bound.
        present = await (
            await conn.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (role,))
        ).fetchone()
        if present:
            await conn.execute(f'DROP OWNED BY "{role}"')
            await conn.execute(f'DROP ROLE "{role}"')

    async with await psycopg.AsyncConnection.connect(pg_dsn, autocommit=True) as conn:
        row = await (
            await conn.execute(
                "SELECT rolsuper OR rolcreaterole FROM pg_roles WHERE rolname = current_user"
            )
        ).fetchone()
        if not (row and row[0]):
            pytest.skip("SAGE_TEST_PG_DSN role cannot create roles; privilege boundary untestable")

        await conn.execute('CREATE EXTENSION IF NOT EXISTS "pgstattuple"')
        await conn.execute("DROP TABLE IF EXISTS public.pgst_probe")
        await conn.execute("CREATE TABLE public.pgst_probe (id int)")
        await conn.execute("INSERT INTO public.pgst_probe VALUES (1)")
        await _drop_role_if_present(conn)
        await conn.execute(f'CREATE ROLE "{role}" NOLOGIN')
        await conn.execute(f'GRANT USAGE ON SCHEMA public TO "{role}"')
        await conn.execute(f'GRANT SELECT ON public.pgst_probe TO "{role}"')
        try:
            # Before the grant: EXECUTE on pgstattuple is denied.
            await conn.execute(f'SET ROLE "{role}"')
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                await conn.execute("SELECT dead_tuple_count FROM pgstattuple('public.pgst_probe')")
            await conn.execute("RESET ROLE")

            # Apply exactly the statement the bootstrap emits.
            await conn.execute(pgstattuple_grant_statement(role))

            # After the grant: the role can execute pgstattuple and read the stat.
            await conn.execute(f'SET ROLE "{role}"')
            granted = await (
                await conn.execute("SELECT dead_tuple_count FROM pgstattuple('public.pgst_probe')")
            ).fetchone()
            await conn.execute("RESET ROLE")
            assert granted is not None and granted[0] is not None
        finally:
            await conn.execute("RESET ROLE")
            await _drop_role_if_present(conn)
            await conn.execute("DROP TABLE IF EXISTS public.pgst_probe")


# ---------------------------------------------------------------------------
# Group A -- CRUD / read methods (port conformance)
# ---------------------------------------------------------------------------


async def test_index_chunks_replaces_not_appends(store):
    """Re-indexing a document replaces its chunks (AD-025), it does not append."""
    await store.index_chunks(
        "d1",
        [
            _chunk("d1", content="first", chunk_index=0),
            _chunk("d1", content="second", chunk_index=1),
        ],
    )
    await store.index_chunks("d1", [_chunk("d1", content="only", chunk_index=0)])
    assert await store.count_rows() == 1
    assert [c.content for c in await store.get_all_chunks("d1")] == ["only"]


async def test_get_all_chunks_document_order(store):
    """Chunks come back sorted by chunk_index regardless of insert order."""
    await store.index_chunks(
        "d1",
        [
            _chunk("d1", content="c2", chunk_index=2),
            _chunk("d1", content="c0", chunk_index=0),
            _chunk("d1", content="c1", chunk_index=1),
        ],
    )
    got = await store.get_all_chunks("d1")
    assert [c.chunk_index for c in got] == [0, 1, 2]
    assert [c.content for c in got] == ["c0", "c1", "c2"]


async def test_get_chunks_by_heading_prefix_exact_child_not_partial(store):
    """Prefix matches the exact heading and its '>'-separated children only --
    a sibling that merely shares a string prefix ("Methodology") is excluded."""
    await store.index_chunks(
        "d1",
        [
            _chunk("d1", content="a", heading_path="Method", chunk_index=0),
            _chunk("d1", content="b", heading_path="Method > Data", chunk_index=1),
            _chunk("d1", content="c", heading_path="Methodology", chunk_index=2),
        ],
    )
    got = await store.get_chunks_by_heading_prefix("d1", "Method")
    assert {c.heading_path for c in got} == {"Method", "Method > Data"}


async def test_get_heading_paths_needs_no_exclusion(store):
    """Every path on the passage surface is a real heading.

    The passage surface holds authored passages only (CAS-ADR-049), so the
    enumeration carries no exclusion and a document-level row cannot reach it.
    """
    await store.index_chunks(
        "d1",
        [
            _chunk("d1", content="intro", heading_path="Intro", chunk_index=0),
            _chunk("d1", content="body", heading_path="Body", chunk_index=1),
        ],
    )
    await store.upsert_document_surface(
        DocumentSurface(
            document_id="d1",
            matchable="Some Title",
            orienting="Abstract: prose",
            embedding=[0.0] * EMBEDDING_DIM,
        )
    )
    assert await store.get_heading_paths("d1") == ["Intro", "Body"]


async def test_has_chunks(store):
    assert await store.has_chunks("nope") is False
    await store.index_chunks("d1", [_chunk("d1", content="x")])
    assert await store.has_chunks("d1") is True
    assert await store.has_chunks("nope") is False


async def test_remove_document_idempotent(store):
    await store.index_chunks("d1", [_chunk("d1", content="x")])
    await store.remove_document("d1")
    assert await store.has_chunks("d1") is False
    # Removing an absent document is a no-op, not an error.
    await store.remove_document("d1")
    await store.remove_document("never")


async def test_upsert_document_surface_preserves_passages(store):
    """Rewriting document-level text leaves the document's passages alone."""
    await store.index_chunks(
        "d1", [_chunk("d1", content="body one", heading_path="Body", chunk_index=0)]
    )
    for orienting in ("old abstract", "new abstract"):
        await store.upsert_document_surface(
            DocumentSurface(
                document_id="d1",
                matchable="Title",
                orienting=orienting,
                embedding=[0.0] * EMBEDDING_DIM,
            )
        )

    assert [c.content for c in await store.get_all_chunks("d1")] == ["body one"], (
        "passages are untouched by a document-surface rewrite"
    )
    assert [r.document_id for r in await store.search_bm25("title", limit=10)] == ["d1"], (
        "the rewrite replaced the prior row rather than duplicating it"
    )


async def test_remove_document_clears_the_document_surface(store):
    """A removed document leaves no document-level row behind."""
    await store.index_chunks("d1", [_chunk("d1", content="body")])
    await store.upsert_document_surface(
        DocumentSurface(
            document_id="d1",
            matchable="Etaword Catalog",
            orienting="",
            embedding=[0.0] * EMBEDDING_DIM,
        )
    )
    assert [r.document_id for r in await store.search_bm25("etaword", limit=10)] == ["d1"]

    await store.remove_document("d1")
    assert await store.search_bm25("etaword", limit=10) == [], (
        "a stale document-level row would keep answering for a removed document"
    )


async def test_update_chunk_metadata_keeps_content_searchable(store):
    """Metadata update touches only metadata columns; content/tsv are intact."""
    await store.index_chunks("d1", [_chunk("d1", content="alpha unique", doc_type="ticket")])
    await store.update_chunk_metadata("d1", {"doc_type": "adr"})
    assert all(c.doc_type == "adr" for c in await store.get_all_chunks("d1"))
    assert any(r.document_id == "d1" for r in await store.search_bm25("alpha", limit=10))


# ---------------------------------------------------------------------------
# Group B -- search (semantic, keyword, filters)
# ---------------------------------------------------------------------------


async def test_search_semantic_ranks_nearest_and_scores_similarity(store):
    """Nearest chunk ranks first and a self-query scores ~1.0 (1 - distance)."""
    await store.index_chunks("d0", [_chunk("d0", content="zero", embedding=_emb(0))])
    await store.index_chunks("d1", [_chunk("d1", content="one", embedding=_emb(1))])
    await store.index_chunks("d2", [_chunk("d2", content="two", embedding=_emb(2))])
    res = await store.search_semantic(_emb(0), limit=10)
    assert res[0].document_id == "d0"
    assert res[0].score == pytest.approx(1.0, abs=1e-6)
    for r in res:
        if r.document_id != "d0":
            assert r.score == pytest.approx(0.0, abs=1e-6)  # orthogonal -> distance 1


async def test_search_bm25_finds_content_term(store):
    await store.index_chunks("d1", [_chunk("d1", content="alphaword shared body")])
    await store.index_chunks("d2", [_chunk("d2", content="betaword shared body")])
    assert [r.document_id for r in await store.search_bm25("alphaword", limit=10)] == ["d1"]


async def test_search_bm25_finds_heading_only_term(store):
    """A term present only in heading_path is findable -- weighted tsv covers it."""
    await store.index_chunks(
        "d1",
        [
            _chunk(
                "d1",
                content="ordinary body with nothing special",
                heading_path="Topic > gammaword overview",
            )
        ],
    )
    res = await store.search_bm25("gammaword", limit=10)
    assert any(r.document_id == "d1" for r in res), (
        "heading-only term must surface via the weighted tsv (heading_path coverage)"
    )


async def test_search_bm25_empty_query_returns_empty(store):
    await store.index_chunks("d1", [_chunk("d1", content="something")])
    assert await store.search_bm25("", limit=10) == []
    assert await store.search_bm25("   ", limit=10) == []


async def test_search_bm25_requires_every_term_across_the_document(store):
    """Multi-term keyword queries stay conjunctive: one absent term matches nothing.

    CAS-ADR-048 moves the matching unit from the chunk to the document but keeps
    full-term strictness, so widening the scope must not weaken the predicate.
    Single-term queries behave identically under AND and OR and so cannot tell
    the two apart; this is the case that can.
    """
    await store.index_chunks("d1", [_chunk("d1", content="alphaword betaword gammaword")])

    present = await store.search_bm25("alphaword betaword gammaword", limit=10)
    assert [r.document_id for r in present] == ["d1"], "every term present must match"

    absent = await store.search_bm25("alphaword betaword gammaword deltaword", limit=10)
    assert absent == [], (
        "one absent term must cull the whole match under AND semantics; an OR "
        "backend would still return d1 on the three terms it does carry"
    )


async def test_search_bm25_terms_may_span_chunks_of_one_document(store):
    """The conjunction is per document, not per chunk (CAS-ADR-048).

    The union of a document's chunks carries both terms, so the document
    matches even though no single chunk does. Under the chunk-scoped predicate
    this returned nothing, which made retrieval a function of where the
    projection happened to place a boundary.
    """
    await store.index_chunks(
        "d1",
        [
            _chunk("d1", content="alphaword only here", chunk_index=0),
            _chunk("d1", content="betaword only here", chunk_index=1),
        ],
    )

    assert [r.document_id for r in await store.search_bm25("alphaword betaword", limit=10)] == [
        "d1"
    ], "terms split across two chunks of one document must match the document"


async def test_search_bm25_does_not_match_across_documents(store):
    """The union is per document, not corpus-wide.

    The sharper control on the scope move: a disjunctive regression returns
    both documents here, where the split-across-chunks case alone would not
    distinguish one.
    """
    await store.index_chunks("d1", [_chunk("d1", content="alphaword only here")])
    await store.index_chunks("d2", [_chunk("d2", content="betaword only here")])

    assert await store.search_bm25("alphaword betaword", limit=10) == [], (
        "one term in each of two documents must not match either of them"
    )


async def test_search_bm25_returns_one_row_per_matching_document(store):
    """A matching document is represented once, by its best-matching chunk.

    Three chunks all carry the term, so a row-per-chunk binding returns three
    rows and this fails; ``limit`` is a document budget, not a row budget.
    """
    await store.index_chunks(
        "d1",
        [
            _chunk("d1", content="alphaword first", chunk_index=0),
            _chunk("d1", content="alphaword second", chunk_index=1),
            _chunk("d1", content="alphaword third", chunk_index=2),
        ],
    )

    res = await store.search_bm25("alphaword", limit=10)
    assert [r.document_id for r in res] == ["d1"], (
        "one row per matching document; a row-per-chunk binding returns three"
    )


async def test_search_bm25_limit_is_a_document_budget(store):
    """``limit`` bounds documents, not rows.

    The row-per-document shape is pinned above, but only at a limit no result
    reaches; that leaves what ``limit`` counts unasserted. Each document here
    carries three matching chunks, so a row budget spends the whole of
    ``limit=2`` inside one document. Both budgets return two rows, so the
    assertion is on the count of *distinct* document ids.
    """
    for doc_id in ("d1", "d2", "d3"):
        await store.index_chunks(
            doc_id,
            [
                _chunk(doc_id, content="alphaword first", chunk_index=0),
                _chunk(doc_id, content="alphaword second", chunk_index=1),
                _chunk(doc_id, content="alphaword third", chunk_index=2),
            ],
        )

    res = await store.search_bm25("alphaword", limit=2)
    assert len({r.document_id for r in res}) == 2, (
        "two documents, not two chunks of one; a row budget answers with one id"
    )


async def test_search_bm25_excerpt_is_the_best_matching_chunk(store):
    """Co-occurrence within a chunk is a ranking signal, not a matching one.

    The co-occurring chunk sits in the *middle*, so neither end of document
    order is the answer: a binding that returns the first chunk fails, and so
    does one that returns the last. Two chunks cannot separate those two
    rivals, because with two the best chunk is always at one end or the other.
    """
    await store.index_chunks(
        "d1",
        [
            _chunk("d1", content="alphaword alone", heading_path="S1", chunk_index=0),
            _chunk("d1", content="alphaword betaword together", heading_path="S2", chunk_index=1),
            _chunk("d1", content="alphaword again alone", heading_path="S3", chunk_index=2),
        ],
    )

    res = await store.search_bm25("alphaword betaword", limit=10)
    assert [r.document_id for r in res] == ["d1"]
    assert res[0].heading_path == "S2", (
        "the excerpt is the chunk carrying both terms, not the first chunk in document order"
    )


async def test_search_bm25_ranks_a_co_occurring_document_above_a_split_one(store):
    """Both documents match; the one whose chunk carries both terms ranks first.

    The co-occurring document is ``d2`` deliberately. Document id is the
    tiebreak within an equal score (``ORDER BY doc_score DESC, document_id``),
    so seeding it as ``d1`` would put the expected winner first under
    alphabetical order as well, and a binding that ranked on nothing at all
    would pass -- which is the whole of what this test is for.
    """
    await store.index_chunks("d2", [_chunk("d2", content="alphaword betaword together")])
    await store.index_chunks(
        "d1",
        [
            _chunk("d1", content="alphaword only here", chunk_index=0),
            _chunk("d1", content="betaword only here", chunk_index=1),
        ],
    )

    res = await store.search_bm25("alphaword betaword", limit=10)
    assert {r.document_id for r in res} == {"d1", "d2"}, "both documents match under document scope"
    assert res[0].document_id == "d2", (
        "co-occurrence within one chunk outranks the same terms split apart, "
        "against the alphabetical order of the ids"
    )


async def test_search_bm25_reports_the_count_of_chunks_carrying_query_terms(store):
    """``matched_chunk_count`` counts the document's chunks carrying a query term.

    Two of three chunks carry the term, so the expected value distinguishes a
    hard-coded 1 from a count of every chunk in the document.
    """
    await store.index_chunks(
        "d1",
        [
            _chunk("d1", content="alphaword first", chunk_index=0),
            _chunk("d1", content="alphaword second", chunk_index=1),
            _chunk("d1", content="nothing relevant", chunk_index=2),
        ],
    )

    res = await store.search_bm25("alphaword", limit=10)
    assert [r.matched_chunk_count for r in res] == [2], (
        "two of three chunks carry the term; neither 1 nor 3 is the answer"
    )


async def test_search_bm25_counts_chunks_carrying_any_term_not_the_whole_query(store):
    """The count is of chunks carrying *a* query term, not chunks that match.

    A single-term query cannot tell those apart -- there, a chunk carrying a
    term and a chunk satisfying the query are the same chunk. Under the
    document-scoped match they part company: here no chunk carries both terms,
    so a count of chunks satisfying the whole query is zero while the count of
    chunks carrying one is two. The signal is meant to say how much of the
    document bears on the query, so zero would be the wrong answer on exactly
    the queries the scope move exists to serve.
    """
    await store.index_chunks(
        "d1",
        [
            _chunk("d1", content="alphaword only here", chunk_index=0),
            _chunk("d1", content="betaword only here", chunk_index=1),
            _chunk("d1", content="nothing relevant", chunk_index=2),
        ],
    )

    res = await store.search_bm25("alphaword betaword", limit=10)
    assert [r.matched_chunk_count for r in res] == [2], (
        "both term-bearing chunks count, though neither satisfies the query alone"
    )


async def test_parse_keyword_query_reports_lexemes_not_raw_words(store):
    """The parse reports what Postgres actually requires, after stopwords and stemming.

    A whitespace split would report ``why``/``did``/``the`` as required terms.
    They are stopwords: the tsquery drops them, so naming them would misdirect a
    caller trying to understand an empty result.
    """
    parse = await store.parse_keyword_query("Why did the running alphaword?")

    assert "alphaword" in parse.terms
    assert "run" in parse.terms, "'running' must appear stemmed, as the tsquery holds it"
    for stopword in ("why", "did", "the"):
        assert stopword not in parse.terms, f"{stopword!r} is a stopword and is not required"
    assert parse.all_required


async def test_parse_keyword_query_omits_negated_terms(store):
    """A ``-excluded`` term is not a required term and must not be reported as one."""
    parse = await store.parse_keyword_query("alphaword -betaword")

    assert "alphaword" in parse.terms
    assert "betaword" not in parse.terms, "an excluded term is not something the caller must supply"


async def test_parse_keyword_query_omits_a_negated_phrase(store):
    """A negated *phrase* excludes every lexeme in it, not just the first.

    ``-"a b"`` renders ``!( 'a' <-> 'b' )`` -- the negation sits before a
    parenthesised group rather than before a quote. Matching a ``!`` only where
    it abuts a quote drops ``'a'`` and keeps ``'b'``, reporting a term the
    caller explicitly excluded as one they must supply. That rival passes
    ``test_parse_keyword_query_omits_negated_terms``, whose negation is a bare
    word; only a phrase separates them.
    """
    parse = await store.parse_keyword_query('-"alphaword betaword" gammaword')

    assert parse.terms == ("gammaword",), (
        "both lexemes of the negated phrase must be absent, and gammaword present"
    )


async def test_parse_keyword_query_reports_an_or_query_as_not_all_required(store):
    """``or`` renders an alternation, so the query is not conjunctive.

    ``websearch_to_tsquery`` reads ``or`` as ``|``. A chunk satisfies such a
    query while carrying only one term, so describing it as requiring every
    term states the opposite of what the caller wrote.
    """
    alternation = await store.parse_keyword_query("alphaword or betaword")
    assert set(alternation.terms) == {"alphaword", "betaword"}
    assert not alternation.all_required, "an alternation must not report as all-required"

    conjunction = await store.parse_keyword_query("alphaword betaword")
    assert conjunction.all_required, "a bare-term query is conjunctive"


async def test_parse_keyword_query_carries_no_terms_when_every_word_is_a_stopword(store):
    """A non-blank query can still search for nothing at all."""
    parse = await store.parse_keyword_query("the a of")

    assert parse.terms == (), "every word is a stopword, so the tsquery is empty"
    assert parse.excluded == (), "nothing was excluded either -- nothing rendered at all"


async def test_parse_keyword_query_separates_exclusion_only_from_discarded_input(store):
    """An exclusion-only query renders a search; an all-stopword query renders nothing.

    Both carry no required terms, so the term list alone cannot tell them
    apart -- and a caller told "every word was discarded" when the backend
    searched for chunks lacking a term has been misinformed. ``excluded`` is
    what separates them.
    """
    exclusion_only = await store.parse_keyword_query("-alphaword")
    assert exclusion_only.terms == ()
    assert exclusion_only.excluded == ("alphaword",), (
        "a rendered negation means a search ran, for chunks lacking the term"
    )

    discarded = await store.parse_keyword_query("the a of")
    assert discarded.terms == () and discarded.excluded == (), (
        "nothing rendered at all -- the two cases must be distinguishable"
    )


async def test_parse_keyword_query_reports_a_quoted_phrase_as_adjacent(store):
    """A phrase requires adjacency, which is stronger than carrying every term.

    ``"a b"`` renders ``'a' <-> 'b'``. A chunk carrying both terms apart
    satisfies a conjunction and not this, so a caller told only that every term
    is required has been told something weaker than the truth.
    """
    phrase = await store.parse_keyword_query('"alphaword betaword"')
    assert phrase.terms == ("alphaword", "betaword")
    assert phrase.adjacent, "a quoted phrase must report adjacency"


async def test_parse_keyword_query_reports_a_phrase_holding_a_stopword_as_adjacent(store):
    """A phrase's stopwords are dropped but keep their positions.

    ``"alphaword the betaword"`` renders ``'alphaword' <2> 'betaword'`` rather
    than ``<->``, because the discarded stopword still consumed a position.
    Reading adjacency off the ``<->`` spelling alone misses every phrase that
    contains one, which is most prose phrases.
    """
    phrase = await store.parse_keyword_query('"alphaword the betaword"')
    assert phrase.terms == ("alphaword", "betaword")
    assert phrase.adjacent, "a distance of two is still an adjacency requirement"


async def test_parse_keyword_query_does_not_report_a_compound_token_as_adjacent(store):
    """A compound the tokenizer split is not a phrase the caller wrote.

    ``CAS-ADR-048`` renders ``'cas-adr' <-> 'cas' <-> 'adr' <-> '048'`` -- the
    tokenizer's own model of a hyphenated identifier, not a quotation. Reading
    adjacency off the rendered operator alone reports every identifier as a
    phrase, and the empty-result advisory then tells a caller who quoted
    nothing to try unquoting it. Identifiers are the query shape this vault
    sees most.
    """
    identifier = await store.parse_keyword_query("CAS-ADR-048 governance")
    assert "cas-adr" in identifier.terms, "precondition: the compound splits into lexemes"
    assert not identifier.adjacent, "a tokenizer-produced adjacency is not a caller-written phrase"


async def test_parse_keyword_query_does_not_report_an_excluded_phrase_as_adjacent(store):
    """An excluded phrase imposes no adjacency the caller must satisfy.

    The compound in the second query is the discriminating half. Reading the
    caller's quotes and the rendered operator as two independent conditions
    lets each be satisfied by a different part of the query -- the excluded
    span supplies the quotes, the split identifier supplies the adjacency --
    and neither is a phrase the caller required.
    """
    excluded = await store.parse_keyword_query('alphaword -"beta gamma"')
    assert not excluded.adjacent, "the quoted span is a thing to avoid, not a requirement to meet"

    with_compound = await store.parse_keyword_query('CAS-ADR-048 -"beta gamma"')
    assert not with_compound.adjacent, (
        "the identifier's own adjacency must not stand in for the excluded phrase's"
    )

    bare = await store.parse_keyword_query("alphaword betaword")
    assert bare.terms == ("alphaword", "betaword")
    assert not bare.adjacent, "the same terms unquoted carry no adjacency requirement"


async def test_parse_keyword_query_does_not_report_a_quoted_single_word_as_adjacent(store):
    """One quoted word is not a phrase: there is nothing for it to be adjacent to.

    Paired with a hyphenated identifier, which is the query shape this vault
    sees most. The quotes and the rendered adjacency arrive from different
    halves of the query, so treating their co-occurrence as a phrase advises
    the caller to unquote something that imposes no adjacency at all.
    """
    single = await store.parse_keyword_query('"alphaword" CAS-ADR-048')
    assert not single.adjacent, "a single quoted word carries no adjacency requirement"

    two_singles = await store.parse_keyword_query('"alphaword" "betaword" CAS-ADR-048')
    assert not two_singles.adjacent, (
        "two singly-quoted words are two spans, not one phrase spanning the gap "
        "between them; a pattern read across that gap sees a phrase in the space"
    )

    real_phrase = await store.parse_keyword_query('"alphaword betaword" CAS-ADR-048')
    assert real_phrase.adjacent, (
        "positive control: a genuine phrase alongside the same compound still reports"
    )


async def test_parse_keyword_query_reports_an_unclosed_quote_as_adjacent(store):
    """An unclosed quote opens a phrase, and the search enforces one.

    ``alpha "beta gamma`` renders ``'alpha' & 'beta' <-> 'gamma'``: the
    tokenizer runs the span to the end of the query rather than discarding it.
    Reporting no adjacency here would leave the caller of an empty result
    reading the bare-conjunction advisory while the thing that actually failed
    was an adjacency they did not realize they had asked for.
    """
    unclosed = await store.parse_keyword_query('alphaword "betaword gammaword')
    assert unclosed.adjacent, "the trailing span is a phrase the search will enforce"


async def test_search_bm25_phrase_requires_adjacency_not_just_presence(store):
    """The adjacency the parse reports is the one the search enforces."""
    await store.index_chunks("d1", [_chunk("d1", content="alphaword zzz betaword")])

    assert [r.document_id for r in await store.search_bm25("alphaword betaword", limit=10)] == [
        "d1"
    ], "both terms are present, so the bare conjunction matches"
    assert await store.search_bm25('"alphaword betaword"', limit=10) == [], (
        "the terms are not adjacent, so the phrase does not match"
    )


async def test_search_bm25_phrase_matches_within_one_chunk(store):
    """The positive control for the phrase exception.

    Without it, a binding that stopped matching phrases *altogether* would pass
    the chunk-scoping test below; neither case is evidence on its own.
    """
    await store.index_chunks("d1", [_chunk("d1", content="alphaword betaword together")])

    assert [r.document_id for r in await store.search_bm25('"alphaword betaword"', limit=10)] == [
        "d1"
    ], "adjacent terms within one chunk must match the phrase"


async def test_search_bm25_phrase_stays_chunk_scoped(store):
    """A quoted phrase is the one deliberate exception to document scope.

    CAS-ADR-048 point 3: adjacency across a chunk boundary is not meaningful.
    The terms are adjacent only if the two chunks are read as one run of text,
    which they are not.
    """
    await store.index_chunks(
        "d1",
        [
            _chunk("d1", content="something ending in alphaword", chunk_index=0),
            _chunk("d1", content="betaword opening the next section", chunk_index=1),
        ],
    )

    assert await store.search_bm25('"alphaword betaword"', limit=10) == [], (
        "adjacency across a chunk boundary is not a phrase match"
    )
    assert [r.document_id for r in await store.search_bm25("alphaword betaword", limit=10)] == [
        "d1"
    ], "the same terms unquoted match at document scope"


async def test_search_bm25_mixed_phrase_and_bare_term(store):
    """One query can carry both scopes: the phrase chunk-scoped, the term not.

    The bare term sits in a different chunk from the phrase, so a binding that
    collapses the whole query back to chunk scope returns nothing.
    """
    await store.index_chunks(
        "d1",
        [
            _chunk("d1", content="alphaword betaword together", chunk_index=0),
            _chunk("d1", content="gammaword elsewhere entirely", chunk_index=1),
        ],
    )

    assert [
        r.document_id for r in await store.search_bm25('"alphaword betaword" gammaword', limit=10)
    ] == ["d1"], "phrase satisfied within a chunk, bare term satisfied across the document"

    assert await store.search_bm25('"alphaword gammaword" betaword', limit=10) == [], (
        "a phrase whose terms never sit adjacent in any one chunk still fails"
    )


# ---------------------------------------------------------------------------
# Provenance: derived text ranks and orients, but never satisfies a match
# (CAS-ADR-049 point 4). Authored text -- a document's passages, their
# headings, its title and tags -- may satisfy a match wherever it lives. The
# generated abstract, the source filename stem, and that stem's expansion are
# derived: they reach ranking and never the match union.
# ---------------------------------------------------------------------------


async def _surface(store, document_id, *, matchable="", orienting="", **kw):
    """Write a document-level row, defaulting both halves to empty."""
    await store.upsert_document_surface(
        DocumentSurface(
            document_id=document_id,
            matchable=matchable,
            orienting=orienting,
            embedding=[0.0] * EMBEDDING_DIM,
            **kw,
        )
    )


async def test_derived_text_alone_does_not_satisfy_a_match(store):
    """A term present only in derived text cannot make a document match."""
    await store.index_chunks("d1", [_chunk("d1", content="ordinary body prose")])
    await _surface(
        store,
        "d1",
        matchable="Ordinary Document",
        orienting="Abstract: zzabstractterm governs every boundary",
    )

    assert await store.search_bm25("zzabstractterm", limit=10) == [], (
        "a generated abstract is evidence about a document, not content of it"
    )
    assert [r.document_id for r in await store.search_bm25("ordinary", limit=10)] == ["d1"], (
        "positive control: the same store answers an authored term, so the "
        "empty result above is the provenance rule and not an unindexed row"
    )


async def test_filename_stem_does_not_satisfy_a_match(store):
    """Text incidental to how a document arrived does not match either."""
    await store.index_chunks("d1", [_chunk("d1", content="ordinary body prose")])
    await _surface(
        store, "d1", matchable="Ordinary Document", orienting="zzstemword-v2 zzstemword v2"
    )

    assert await store.search_bm25("zzstemword", limit=10) == [], (
        "a filename is an artifact of how the document arrived, not content"
    )


async def test_derived_text_cannot_complete_a_conjunction(store):
    """Derived text cannot supply the term the authored text is missing.

    The sharper form of the rule: document scope lets terms combine freely
    across a document, so without this the document surface's derived half
    would silently become a universal donor for any conjunction it completes.
    """
    await store.index_chunks("d1", [_chunk("d1", content="alphaword appears in the body")])
    await _surface(
        store,
        "d1",
        matchable="Some Title",
        orienting="Abstract: betaword appears only in the generated summary",
    )

    assert await store.search_bm25("alphaword betaword", limit=10) == [], (
        "derived text may not complete a conjunction the authored text leaves open"
    )
    assert [r.document_id for r in await store.search_bm25("alphaword", limit=10)] == ["d1"], (
        "positive control: the authored term alone still matches, so the empty "
        "result above is the missing term and not a broken conjunction"
    )


async def test_derived_text_still_ranks_a_matched_document(store):
    """Derived text keeps its ranking value on a document that does match.

    The control against excluding derived text outright: it is barred from
    satisfying a match, not removed from the store or from ranking. The two
    documents carry identical authored text, so the only thing separating
    their scores is the derived half of the document surface.
    """
    for doc_id in ("d1", "d2"):
        await store.index_chunks(doc_id, [_chunk(doc_id, content="alphaword body prose")])
    await _surface(store, "d1", matchable="Title One", orienting="alphaword " * 20)
    await _surface(store, "d2", matchable="Title Two", orienting="unrelated summary")

    res = await store.search_bm25("alphaword", limit=10)
    assert [r.document_id for r in res] == ["d1", "d2"], (
        "both match on authored text; the richer derived text outranks the "
        "barer one, so derived text is still in the ranking pool"
    )
    assert res[0].score > res[1].score


async def test_title_satisfies_a_match(store):
    """A document's title is authored text and may satisfy a match.

    Inverts the behaviour that stood while document-level text shared the
    passage surface, where a title matched only where an authored heading
    path happened to restate it.
    """
    await store.index_chunks(
        "titled",
        [_chunk("titled", content="body prose sharing no term with the title")],
    )
    await _surface(store, "titled", matchable="Deltaword Catalog", orienting="unrelated")

    assert [r.document_id for r in await store.search_bm25("deltaword", limit=10)] == ["titled"], (
        "the title is authored text and matches from the document surface, "
        "whether or not a heading path restates it"
    )


async def test_tags_satisfy_a_match(store):
    """A document's tags are authored text and may satisfy a match."""
    await store.index_chunks("tagged", [_chunk("tagged", content="unrelated body prose")])
    await _surface(store, "tagged", matchable="Some Title epsilonword", orienting="")

    assert [r.document_id for r in await store.search_bm25("epsilonword", limit=10)] == ["tagged"]


async def test_a_conjunction_spans_the_title_and_the_body(store):
    """Authored text is one union across both surfaces.

    CAS-ADR-049 makes a document match when *its authored text* carries every
    required term -- its passages, their headings, its title, its tags. A
    query splitting its terms between a title and a body must therefore
    match, which independently-ranked surfaces could not express.
    """
    await store.index_chunks("split", [_chunk("split", content="bodyword in the prose")])
    await _surface(store, "split", matchable="Titleword Catalog", orienting="")

    assert [r.document_id for r in await store.search_bm25("titleword bodyword", limit=10)] == [
        "split"
    ], "one term from the title and one from a passage still make the document match"


async def test_document_surface_hit_reports_no_matched_passages(store):
    """The matched-passage count names passages and nothing else.

    A document matched only through its document surface has no passage
    carrying the term, so the count is zero rather than one -- the count is a
    statement about the document's own text.
    """
    await store.index_chunks("titled", [_chunk("titled", content="unrelated body prose")])
    await _surface(store, "titled", matchable="Zetaword Catalog", orienting="")

    [hit] = await store.search_bm25("zetaword", limit=10)
    assert hit.matched_chunk_count is not None
    assert hit.matched_chunk_count == 0, (
        "a document-level hit is not a passage and must not be counted as one"
    )
    assert hit.heading_path == "", "a document-level hit carries no passage excerpt"
    assert hit.is_document_surface is True, "the row does not name the surface it came from"


async def test_document_surface_leg_returns_no_excerpt_on_the_semantic_arm(store):
    """The semantic arm's document-level row carries no excerpt either.

    The stored authored half is widened to a superset of the title's
    renderings so a caller reaching for one form finds the other. Serving that
    widening back as the row's content hands a caller duplicated tokens in
    place of anything the document says. A document-level row is not a passage
    and has no excerpt to give, on either arm (CAS-ADR-049 Decision 5).
    """
    # Orthogonal one-hot vectors, so the query reaches the document-level row
    # rather than the passage. A zero vector has no defined cosine and the arm
    # drops such a row, which would empty the result and prove nothing.
    await store.index_chunks(
        "surfaced",
        [_chunk("surfaced", content="unrelated body prose", embedding=_emb(1))],
    )
    await store.upsert_document_surface(
        DocumentSurface(
            document_id="surfaced",
            matchable="ZetawordCatalog Zetaword Catalog",
            orienting="",
            embedding=_emb(0),
        )
    )

    hits = await store.search_semantic(_emb(0), limit=10)
    surface_hits = [h for h in hits if h.document_id == "surfaced" and h.is_document_surface]

    assert surface_hits, "the document-level row did not compete on the semantic arm"
    [hit] = surface_hits
    assert hit.content == "", "the semantic arm's document-level row carries an excerpt"
    assert "zetaword" not in hit.content.lower(), (
        "the row exposes its index-side expansion as content"
    )
    assert hit.matched_chunk_count == 0, "the semantic arm counts a document-level row as a passage"


async def test_matched_passage_count_counts_only_passages(store):
    """Passages carrying a term are counted; the document surface is not."""
    await store.index_chunks(
        "d1",
        [
            _chunk("d1", content="alphaword here", chunk_index=0),
            _chunk("d1", content="alphaword again", chunk_index=1),
            _chunk("d1", content="nothing relevant", chunk_index=2),
        ],
    )
    await _surface(store, "d1", matchable="alphaword Catalog", orienting="alphaword summary")

    [hit] = await store.search_bm25("alphaword", limit=10)
    assert hit.matched_chunk_count == 2, (
        "two passages carry the term; neither half of the document surface counts"
    )


async def test_filtered_keyword_search_admits_no_document_the_unfiltered_one_excludes(store):
    """A filtered keyword search is the unfiltered one restricted, nothing else."""
    await store.index_chunks(
        "adr1",
        [
            _chunk("adr1", content="alphaword here", chunk_index=0, doc_type="adr"),
            _chunk("adr1", content="betaword there", chunk_index=1, doc_type="adr"),
        ],
    )
    await store.index_chunks(
        "tic1",
        [
            _chunk("tic1", content="alphaword here", chunk_index=0, doc_type="ticket"),
            _chunk("tic1", content="betaword there", chunk_index=1, doc_type="ticket"),
        ],
    )
    await store.index_chunks(
        "adr2", [_chunk("adr2", content="alphaword only, no partner", doc_type="adr")]
    )

    unfiltered = {r.document_id for r in await store.search_bm25("alphaword betaword", limit=10)}
    filtered = {
        r.document_id
        for r in await store.search_bm25(
            "alphaword betaword", limit=10, filters={"doc_type": "adr"}
        )
    }

    assert unfiltered == {"adr1", "tic1"}
    assert filtered <= unfiltered, "a filter may only narrow"
    assert filtered == {"adr1"}, "and it narrows to exactly the slice it names"


async def test_filter_applies_to_the_match_union_not_just_the_ranking_pool(store):
    """Filter predicates apply at the matching unit (CAS-ADR-048 consequences).

    The chunk metadata is deliberately skewed -- a state ordinary ingest never
    produces, since the columns are document properties denormalized onto every
    chunk. It is the only way to tell a union computed inside the filtered
    slice from one computed over all chunks and filtered afterwards.
    """
    await store.index_chunks(
        "d1",
        [
            _chunk("d1", content="alphaword here", chunk_index=0, doc_type="adr"),
            _chunk("d1", content="betaword there", chunk_index=1, doc_type="ticket"),
        ],
    )

    assert (
        await store.search_bm25("alphaword betaword", limit=10, filters={"doc_type": "adr"}) == []
    ), "only one term survives the filter, so the conjunction is unsatisfied within it"
    assert [r.document_id for r in await store.search_bm25("alphaword betaword", limit=10)] == [
        "d1"
    ], "unfiltered, the union carries both terms"


async def test_parse_keyword_query_empty_for_blank_query(store):
    """A blank query carries no required terms.

    Pins the contract, not the guard that implements it: Postgres renders the
    empty tsquery for ``''`` and ``'   '`` too, so this passes with or without
    the early return. It is not evidence that branch is reached.
    """
    assert (await store.parse_keyword_query("")).terms == ()
    assert (await store.parse_keyword_query("   ")).terms == ()


async def test_search_filter_pushdown_excludes_nonmatching(store):
    """Filter predicates exclude rows that match the query but fail the filter."""
    await store.index_chunks(
        "adr1",
        [_chunk("adr1", content="shared topic", doc_type="adr", lifecycle_status="active")],
    )
    await store.index_chunks(
        "tic1",
        [_chunk("tic1", content="shared topic", doc_type="ticket", lifecycle_status="archived")],
    )
    # Scalar equality predicate.
    assert [
        r.document_id for r in await store.search_bm25("shared", filters={"doc_type": "adr"})
    ] == ["adr1"]
    # List (IN-clause) predicate.
    assert [
        r.document_id
        for r in await store.search_bm25("shared", filters={"lifecycle_status": ["active"]})
    ] == ["adr1"]
    # Filters apply to the semantic arm too.
    res = await store.search_semantic(_emb(0), limit=10, filters={"doc_type": "ticket"})
    assert {r.document_id for r in res} == {"tic1"}


async def test_search_filter_document_id_in_clause(store):
    """The graph-resolved document_id list restricts results to those ids."""
    for d in ("d1", "d2", "d3"):
        await store.index_chunks(d, [_chunk(d, content="common term")])
    res = await store.search_bm25("common", filters={"document_id": ["d1", "d3"]})
    assert {r.document_id for r in res} == {"d1", "d3"}


# ---------------------------------------------------------------------------
# Group C -- stats & bloat signals (real Postgres signals, not zero-stubs)
# ---------------------------------------------------------------------------


async def test_count_rows(store):
    assert await store.count_rows() == 0
    await store.index_chunks(
        "d1", [_chunk("d1", content="a"), _chunk("d1", content="b", chunk_index=1)]
    )
    await store.index_chunks("d2", [_chunk("d2", content="c")])
    assert await store.count_rows() == 3


async def test_count_rows_counts_every_content_store_surface(store):
    """The row count spans the store, not the passage table alone.

    The count is the live denominator the dashboard divides the dead-tuple
    count by, so it has to describe the same scope the dead-tuple count does.

    Anti-coincidental-pass. A passage-only count returns 3 and reds. The second
    document carries a document-level row and no passages, which is what closes
    the near-rival: counting passages plus *distinct documents in the passage
    table* also reaches the right answer whenever every document has both, and
    the surface holds one row per document, so that is the shape a plausible
    implementation takes. It reads 4 against the 5 asserted here, and it is
    wrong for exactly the vault this fixture builds -- one whose document-level
    rows and passage-bearing documents are not the same set, which any removal
    or any document indexed before its surface is written produces."""
    await store.index_chunks(
        "d1",
        [
            _chunk("d1", content="a", chunk_index=0),
            _chunk("d1", content="b", chunk_index=1),
            _chunk("d1", content="c", chunk_index=2),
        ],
    )
    for doc_id in ("d1", "d2"):
        await store.upsert_document_surface(
            DocumentSurface(document_id=doc_id, matchable="Title", orienting="abstract")
        )
    assert await store.count_rows() == 5


async def test_count_retained_versions_rises_with_churn(store, pg_pool):
    """Dead MVCC tuples (retained old versions) rise with un-optimized churn."""
    await _disable_autovacuum(pg_pool)
    assert await store.count_retained_versions() == 0
    await _churn(store)
    assert await store.count_retained_versions() > 0


async def test_count_small_fragments_rises_after_vacuum(store, pg_pool):
    """After churn is plain-vacuumed, reclaimable free-space pages appear."""
    await _disable_autovacuum(pg_pool)
    await _churn(store)
    async with pg_pool.connection() as conn:
        await conn.set_autocommit(True)
        await conn.execute("VACUUM chunks")  # dead tuples -> in-page free space
    assert await store.count_small_fragments() > 0


async def test_count_methods_zero_when_table_absent(pg_dsn):
    """Stat reads return 0 (not raise) against a schema with no chunks table."""
    import psycopg

    from sage.storage.postgres.pool import pool_from_conninfo

    schema = "sage_test_empty_" + os.urandom(4).hex()
    async with await psycopg.AsyncConnection.connect(pg_dsn, autocommit=True) as conn:
        await conn.execute(f'CREATE SCHEMA "{schema}"')
    try:
        # search_path = the empty schema, then public (for the vector/pgstattuple
        # extensions); 'chunks' exists in neither, so the lookups hit the
        # table-absent path. Mirrors a freshly created, not-yet-bootstrapped vault.
        pool = pool_from_conninfo(pg_dsn, search_path=f"{schema},public")
        await pool.open()
        try:
            s = PostgresContentStore(pool)
            assert await s.count_rows() == 0
            assert await s.count_retained_versions() == 0
            assert await s.count_small_fragments() == 0
            assert await s.measured_byte_size() == 0
        finally:
            await pool.close()
    finally:
        async with await psycopg.AsyncConnection.connect(pg_dsn, autocommit=True) as conn:
            await conn.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')


@pytest.mark.parametrize("present", ["chunks", "document_surface"])
async def test_stat_reads_report_the_surface_that_is_present(pg_dsn, present):
    """An absent surface does not zero the readings of the one that exists.

    Guards the tolerance's granularity, not merely its existence. Wrapping the
    whole reading in one table-absent handler -- the shape a single-surface
    accounting could afford -- makes a schema missing any surface report zero
    for all of them, which reads as a pristine store rather than a partly
    provisioned one. A vault mid-provision is exactly when that lie is told.

    Run in both directions, because one direction is not a gate. With only the
    passage surface provisioned, an accounting that reads the passages and
    nothing else reports every figure correctly -- the arm is silent about the
    rival it most needs to exclude. The document-surface arm is the one that
    reds against it, and it is the reason this is parametrized rather than
    written once.

    Each arm churns its surface so the dead-tuple reading carries signal:
    against a freshly written surface it is legitimately zero, which is also
    what a reading that measured nothing returns, and an assertion that cannot
    tell those apart is not one."""
    import psycopg

    from sage.storage.postgres.pool import pool_from_conninfo
    from sage.storage.postgres.schema import CHUNKS_TABLE, DOCUMENT_SURFACE_TABLE

    ddl = {"chunks": CHUNKS_TABLE, "document_surface": DOCUMENT_SURFACE_TABLE}[present]
    schema = "sage_test_partial_" + os.urandom(4).hex()
    async with await psycopg.AsyncConnection.connect(pg_dsn, autocommit=True) as conn:
        await conn.execute(f'CREATE SCHEMA "{schema}"')
        await conn.execute(f'SET search_path TO "{schema}", public')
        await conn.execute(ddl)
        await conn.execute(f"ALTER TABLE {present} SET (autovacuum_enabled = false)")
    try:
        pool = pool_from_conninfo(pg_dsn, search_path=f"{schema},public")
        await pool.open()
        try:
            s = PostgresContentStore(pool)
            if present == "chunks":
                await _churn(s)
            else:
                await _churn_document_surface(s, docs=10, rewrites=3)
            assert await s.count_rows() > 0
            assert await s.measured_byte_size() > 0
            assert await s.count_retained_versions() > 0
            # count_small_fragments is deliberately not asserted here: a churned
            # but unvacuumed surface has no free space yet, and ">= 0" is true of
            # every implementation including one that measured nothing. It shares
            # its statement builder with the dead-tuple read above, so the arm
            # that discriminates it is the sum assertion in the companion test.

            # The reclamation has to tolerate the absent surface the same way,
            # and one statement naming both does not: VACUUM resolves its whole
            # relation list before processing any of it, so the absent name
            # aborts the command and the present surface keeps every dead tuple
            # it had. That is a reclaim of nothing reported as a reclaim.
            await _await_reclaimable_horizon(pool)
            snap = await s.optimize(timedelta(0))
            assert snap["pre_versions"] > 0
            if snap["post_versions"] != 0:
                pytest.fail(
                    f"VACUUM FULL kept {snap['post_versions']} dead tuples on a partly "
                    f"provisioned schema:\n{await _horizon_diagnostics(pool)}"
                )
            assert await s.count_retained_versions() == 0
        finally:
            await pool.close()
    finally:
        async with await psycopg.AsyncConnection.connect(pg_dsn, autocommit=True) as conn:
            await conn.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')


# ---------------------------------------------------------------------------
# Group D -- optimize()
# ---------------------------------------------------------------------------


async def test_optimize_reclaims_versions_and_bytes(store, pg_pool):
    """VACUUM FULL clears dead tuples and shrinks the relation; snapshot proves it."""
    await _disable_autovacuum(pg_pool)
    await _churn(store)
    pre_bytes = await store.measured_byte_size()
    await _await_reclaimable_horizon(pg_pool)
    snap = await store.optimize(timedelta(0))
    assert snap["pre_versions"] > 0
    if snap["post_versions"] != 0:
        pytest.fail(
            f"VACUUM FULL kept {snap['post_versions']} dead tuples, so something pinned "
            f"the reclaim horizon that the wait does not model:\n"
            f"{await _horizon_diagnostics(pg_pool)}"
        )
    assert snap["post_bytes"] < snap["pre_bytes"]
    assert snap["post_small_fragments"] <= snap["pre_small_fragments"]
    assert await store.count_retained_versions() == 0
    assert await store.measured_byte_size() < pre_bytes


async def test_optimize_accepts_nonzero_threshold(store, pg_pool):
    """cleanup_older_than is accepted but irrelevant on Postgres: full reclaim."""
    await _disable_autovacuum(pg_pool)
    await _churn(store)
    await _await_reclaimable_horizon(pg_pool)
    snap = await store.optimize(timedelta(days=7))
    if snap["post_versions"] != 0:
        pytest.fail(
            f"VACUUM FULL kept {snap['post_versions']} dead tuples, so something pinned "
            f"the reclaim horizon that the wait does not model:\n"
            f"{await _horizon_diagnostics(pg_pool)}"
        )


async def test_optimize_reclaims_the_document_surface_leaving_passages_alone(store, pg_pool):
    """The document surface is observed and reclaimed; the passages are untouched.

    The surface takes a delete-then-insert on every metadata change and every
    abstract refresh, so it accumulates dead tuples by construction. Nothing
    reported them and nothing reclaimed them.

    The passages here are written exactly once and never rewritten, which is
    what makes the reading discriminating: the passage surface holds no dead
    tuple of its own, so a dead-tuple total above zero can only have come from
    the other surface. Churning both would let a passage-only accounting satisfy
    every assertion below unchanged.

    Two rivals this test does *not* exclude, named so its silence is not read
    as coverage. Substituting one surface for the other rather than summing
    them satisfies everything here, because only one surface carries churn; the
    companion test below is where that is caught. And an ``optimize`` narrowed
    to the document surface alone also passes, because the passages this test
    keeps have no dead tuples for a reclamation to miss; the pre-existing
    passage-side reclaim test is what holds that end."""
    await _disable_autovacuum(pg_pool)
    await store.index_chunks("kept", _fat_chunks("kept", k=30))
    passages_before = [c.content for c in await store.get_all_chunks("kept")]
    passages_only = await store.measured_byte_size()

    await _churn_document_surface(store)

    passage_stats = await _surface_stats(pg_pool, "chunks")
    assert passage_stats["versions"] == 0, (
        "the passage surface was rewritten, so a nonzero total below would no "
        "longer isolate the document surface"
    )
    assert await store.count_retained_versions() > 0, (
        "the document surface's dead tuples are invisible to the dead-tuple total"
    )
    assert await store.measured_byte_size() > passages_only, (
        "the document surface's footprint is absent from the size total"
    )

    await _await_reclaimable_horizon(pg_pool)
    snap = await store.optimize(timedelta(0))
    assert snap["pre_versions"] > 0
    if snap["post_versions"] != 0:
        pytest.fail(
            f"VACUUM FULL kept {snap['post_versions']} dead tuples, so something pinned "
            f"the reclaim horizon that the wait does not model:\n"
            f"{await _horizon_diagnostics(pg_pool)}"
        )
    assert snap["post_bytes"] < snap["pre_bytes"]
    assert await store.count_retained_versions() == 0

    # The passage surface came through the widened reclamation intact. Its byte
    # size is deliberately not asserted: VACUUM FULL rewrites a relation even
    # when it has nothing to reclaim, so a byte equality here would be a flake
    # rather than a control.
    assert (await _surface_stats(pg_pool, "chunks"))["versions"] == 0
    assert await _surface_row_count(pg_pool, "chunks") == 30
    assert [c.content for c in await store.get_all_chunks("kept")] == passages_before


async def test_the_bloat_totals_sum_the_surfaces_rather_than_substituting(store, pg_pool):
    """Each total equals the sum of its per-surface parts, with both parts nonzero.

    The companion to the test above, against a different wrong implementation.
    Reading one surface in place of the other -- a substitution rather than a
    sum -- satisfies "the document surface is now observed" completely, and is
    invisible whenever either surface is empty. Churning both to different
    magnitudes and asserting the arithmetic closes that, and closes
    double-counting one surface at the same time.

    The surfaces are named here rather than read from the binding, and the
    binding's own set is asserted to be exactly them. A control taken from
    ``_CONTENT_STORE_SURFACES`` would narrow along with it: dropping a surface
    from the constant drops it from both sides of the equality, and the test
    passes having measured the narrowing it exists to catch -- as this one did,
    until the probe that broke the constant found it still green. Adding a third
    surface is meant to red this test, so the control is extended deliberately
    rather than by inheriting whatever the constant says.

    Reads the totals only; it never calls ``optimize``, so an implementation
    that reports both surfaces and reclaims one passes here. The test above is
    what excludes that."""
    assert _CONTENT_STORE_SURFACES == ("chunks", "document_surface"), (
        "the content store's surface set changed; extend this test's own control "
        "to name every surface, rather than reading the set under test"
    )
    surfaces = ("chunks", "document_surface")

    await _disable_autovacuum(pg_pool)
    await _churn(store)
    await _churn_document_surface(store)

    parts = {s: await _surface_stats(pg_pool, s) for s in surfaces}
    for surface, stats in parts.items():
        assert stats["versions"] > 0, f"{surface} carries no dead tuples; the sum would not bind"
        assert stats["bytes"] > 0

    assert await store.count_retained_versions() == sum(s["versions"] for s in parts.values())
    assert await store.measured_byte_size() == sum(s["bytes"] for s in parts.values())

    async with pg_pool.connection() as conn:
        await conn.set_autocommit(True)
        for surface in surfaces:
            await conn.execute(f"VACUUM {surface}")  # dead tuples -> in-page free space
    free = {s: await _surface_stats(pg_pool, s) for s in surfaces}
    for surface, stats in free.items():
        assert stats["small_fragments"] > 0, f"{surface} freed no pages; the sum would not bind"
    assert await store.count_small_fragments() == sum(s["small_fragments"] for s in free.values())


# ---------------------------------------------------------------------------
# Group E (Postgres half) -- measured_byte_size from real relation size
# ---------------------------------------------------------------------------


async def test_measured_byte_size_grows_with_ingest(store):
    base = await store.measured_byte_size()
    await store.index_chunks("big", _fat_chunks("big", k=40))
    assert await store.measured_byte_size() > base


# ---------------------------------------------------------------------------
# Group F -- RRF fusion + RetrievalService runs unchanged (criterion 1)
# ---------------------------------------------------------------------------


def _rrf_fusion_corpus() -> dict[str, list[Chunk]]:
    """Strictly graded semantic similarity to ``_emb(0)`` (d0>d1>d2>d3), with the
    keyword term living only in d3's *heading*. With heading coverage the keyword
    arm lifts d3 above the semantic order; without it, d3 stays last -- so the
    fused order differs between a heading-covering and a content-only keyword arm.
    """
    return {
        "d0": [
            _chunk(
                "d0",
                content="primary topic body",
                heading_path="Overview",
                embedding=_graded_emb(1.0, 1),
            )
        ],
        "d1": [
            _chunk(
                "d1", content="secondary notes", heading_path="Notes", embedding=_graded_emb(0.9, 2)
            )
        ],
        "d2": [
            _chunk(
                "d2",
                content="tertiary remarks",
                heading_path="Remarks",
                embedding=_graded_emb(0.8, 3),
            )
        ],
        "d3": [
            _chunk(
                "d3",
                content="ordinary closing text",
                heading_path="zephyrword summary",
                embedding=_graded_emb(0.7, 4),
            )
        ],
    }


async def _fused(store, query_emb, query_text, limit=5):
    sem = await store.search_semantic(query_emb, limit * 3)
    kw = await store.search_bm25(query_text, limit * 3)
    return [(r.document_id, r.heading_path) for r in rrf_fuse(sem, kw, limit)]


async def test_rrf_fusion_lifts_heading_only_keyword_match(store):
    """The keyword arm matches d3's heading only; fusion must still lift it
    to the top -- this fails if the Postgres keyword arm lost heading
    coverage."""
    for doc_id, chunks in _rrf_fusion_corpus().items():
        await store.index_chunks(doc_id, chunks)

    pg_fused = await _fused(store, _emb(0), "zephyrword")

    assert pg_fused[0] == ("d3", "zephyrword summary")


async def test_retrieval_service_runs_unchanged_on_postgres(
    store, graph_store, stub_embedding_provider, minimal_config
):
    """The production RetrievalService hybrid path runs on the Postgres binding
    with no change -- proving the port swap is transparent above the port."""
    from sage.services.retrieval import RetrievalService

    await store.index_chunks(
        "d0", [_chunk("d0", content="alpha primary", heading_path="A", embedding=_emb(0))]
    )
    await store.index_chunks(
        "d1", [_chunk("d1", content="beta other", heading_path="B", embedding=_emb(1))]
    )

    service = RetrievalService(
        graph_store=graph_store,
        content_store=store,
        embedding_provider=stub_embedding_provider,
        config=minimal_config,
    )
    results = await service._hybrid_rrf(_emb(0), "alpha", 10)
    assert any(r.document_id == "d0" for r in results)


# ---------------------------------------------------------------------------
# Both surfaces carry the same filter columns, so both must stay current
# ---------------------------------------------------------------------------


async def test_metadata_update_reaches_the_document_surface(store):
    """A lifecycle change stops the title matching under the old predicate.

    Both arms push a caller's predicates down to both surfaces, so a document
    whose passages were re-stamped but whose document-level row was not would
    keep answering an ``active``-filtered query through its title after being
    archived. The existing metadata test covers passages only, which is
    exactly the gap: the surface is the second site and shares no writer.
    """
    await store.index_chunks(
        "d1", [_chunk("d1", content="ordinary body prose", lifecycle_status="active")]
    )
    await store.upsert_document_surface(
        DocumentSurface(
            document_id="d1",
            matchable="Zetaword Catalog",
            orienting="",
            embedding=[0.0] * EMBEDDING_DIM,
            lifecycle_status="active",
        )
    )
    active = {"lifecycle_status": "active"}
    assert [
        r.document_id for r in await store.search_bm25("zetaword", limit=10, filters=active)
    ] == ["d1"], "precondition: the title matches under the filter before the change"

    await store.update_chunk_metadata("d1", {"lifecycle_status": "archived"})

    assert await store.search_bm25("zetaword", limit=10, filters=active) == [], (
        "an archived document still answered an active-filtered query by its title"
    )
    assert [
        r.document_id
        for r in await store.search_bm25(
            "zetaword", limit=10, filters={"lifecycle_status": "archived"}
        )
    ] == ["d1"], "positive control: it matches under its new lifecycle_status"


async def test_semantic_arm_applies_the_updated_filter_to_both_surfaces(store):
    """The same gap on the vector arm, which pushes the predicate down too."""
    await store.index_chunks(
        "d1",
        [
            _chunk(
                "d1",
                content="body",
                lifecycle_status="active",
                embedding=[1.0] + [0.0] * (EMBEDDING_DIM - 1),
            )
        ],
    )
    await store.upsert_document_surface(
        DocumentSurface(
            document_id="d1",
            matchable="Title",
            orienting="",
            embedding=[1.0] + [0.0] * (EMBEDDING_DIM - 1),
            lifecycle_status="active",
        )
    )
    await store.update_chunk_metadata("d1", {"lifecycle_status": "archived"})

    query = [1.0] + [0.0] * (EMBEDDING_DIM - 1)
    assert (
        await store.search_semantic(query, limit=10, filters={"lifecycle_status": "active"})
    ) == [], "an archived document's surface row survived an active filter"

    # The control: an implementation that simply stopped returning surface rows
    # under any filter would satisfy the assertion above. This separates
    # "filtered correctly" from "filtered out entirely".
    archived = await store.search_semantic(
        query, limit=10, filters={"lifecycle_status": "archived"}
    )
    assert any(h.heading_path == "" for h in archived), (
        "the surface row is filtered out under every predicate, not re-stamped"
    )


# ---------------------------------------------------------------------------
# Passage reads see passages only, including on a vault awaiting migration
# ---------------------------------------------------------------------------


def _legacy_header(document_id: str, *, content: str) -> Chunk:
    """The synthetic document-header row, as the ingestion pipeline wrote it.

    Seeded from the port's two constants rather than their values, so a sweep
    that greps for either marker by name reaches this module -- which holds the
    only assertions that the migration window still exists.
    """
    return _chunk(
        document_id,
        content=content,
        heading_path=LEGACY_DOCUMENT_HEADER_HEADING_PATH,
        chunk_index=LEGACY_DOCUMENT_HEADER_CHUNK_INDEX,
    )


async def test_passage_reads_exclude_a_legacy_document_level_row(store):
    """A vault that has not run its migration cannot leak its legacy rows.

    The per-consumer exclusions are gone, so between deploying this code and
    running the migration the only thing keeping a legacy row out of heading
    enumeration, reconstructed projection text and the abstraction input is
    the passage surface's own definition.
    """
    await store.index_chunks(
        "d1",
        [
            _legacy_header("d1", content="Title: T\nAbstract: a previously generated abstract"),
            _chunk("d1", content="authored body", heading_path="Body", chunk_index=0),
        ],
    )

    assert await store.get_heading_paths("d1") == ["Body"]
    assert [c.content for c in await store.get_all_chunks("d1")] == ["authored body"]
    hits = await store.search_semantic([0.0] * EMBEDDING_DIM, limit=10)
    assert all(h.heading_path != LEGACY_DOCUMENT_HEADER_HEADING_PATH for h in hits), (
        "a legacy row reached a caller through the semantic arm"
    )


async def test_a_legacy_row_cannot_satisfy_a_keyword_match_across_the_document(store):
    """A term only the legacy row carries does not make its document match.

    The document-scoped keyword arm resolves each operand to a set of document
    ids before anything is ranked, so the scoping has to hold there or a legacy
    row admits its whole document on text no author wrote.
    """
    await store.index_chunks(
        "d1",
        [
            _legacy_header("d1", content="Abstract: zzheaderterm governs the summary"),
            _chunk("d1", content="alphaword in the body", heading_path="Body", chunk_index=0),
        ],
    )

    assert await store.search_bm25("zzheaderterm", limit=10) == [], (
        "a legacy row satisfied a match through the document-scoped arm"
    )
    assert [r.document_id for r in await store.search_bm25("alphaword", limit=10)] == ["d1"], (
        "positive control: the same arm matches on the authored passage, so the "
        "empty result above is the scoping and not an unreachable query"
    )


async def test_a_legacy_row_does_not_rank_or_count_toward_a_matched_document(store):
    """A matched document's excerpt and passage count see passages only.

    The ranking stage is scoped separately from the matching stage, so a
    document that matched on its authored text can still have its legacy row
    reach the excerpt or inflate the count if only the match arm is scoped.
    """
    await store.index_chunks(
        "d1",
        [
            _legacy_header("d1", content="Abstract: alphaword restated in the summary"),
            _chunk("d1", content="alphaword in the body", heading_path="Body", chunk_index=0),
        ],
    )

    results = await store.search_bm25("alphaword", limit=10)

    assert [r.document_id for r in results] == ["d1"]
    assert results[0].heading_path != LEGACY_DOCUMENT_HEADER_HEADING_PATH, (
        "a legacy row became the excerpt"
    )
    assert results[0].matched_chunk_count == 1, (
        "the count names passages, so a legacy row must not be one of them"
    )


async def test_a_legacy_row_cannot_satisfy_a_within_chunk_keyword_query(store):
    """The single-chunk fallback is scoped too, not only the decomposed arms.

    A query carrying an exclusion cannot be decomposed into document-scoped
    operands and falls back to evaluating against one chunk, which is a third
    statement and needs its own scoping.
    """
    await store.index_chunks(
        "d1",
        [
            _legacy_header("d1", content="Abstract: zzheaderterm governs the summary"),
            _chunk("d1", content="alphaword in the body", heading_path="Body", chunk_index=0),
        ],
    )

    assert await store.search_bm25("zzheaderterm -nothingword", limit=10) == [], (
        "a legacy row satisfied a match through the within-chunk fallback"
    )
    assert [r.document_id for r in await store.search_bm25("alphaword -nothingword", limit=10)] == [
        "d1"
    ], (
        "positive control: the exclusion still reaches the fallback and can "
        "return a hit, so the empty result above is the scoping"
    )


def test_the_passage_surface_scoping_is_expressed_once():
    """The scoping stays at one anchor rather than decaying into literals.

    Every passage read on this binding goes through ``_passage_rows_only``, so
    the condition is stated once and a read added without it is visible. A site
    that spells the predicate inline would be a second, unexplained statement of
    it -- which is the shape this anchor replaced.

    The call-count assertion excludes the module that defines the helper and
    calls it nowhere, which "no inline predicate" would otherwise satisfy
    trivially. It does not reach two further rivals, and this test is not where
    they are caught: a module keeping one call and dropping the scoping from the
    other reads satisfies both assertions, and so does one scoping by the
    heading path instead. The behavioural tests above exclude the first --
    between them they exercise every scoped read, so a dropped guard reds one of
    them -- and the second is not a defect, since neither marker is canonical.

    This test's own job is the narrower one: keeping the condition stated once
    rather than re-spelled, unexplained, at each site.

    Matched by operator rather than by one spelling. A substring test for a
    single rendering (``"chunk_index >"``) is evaded by the same predicate
    written without the space, and ruff does not normalise inside a string
    literal, so the evading spelling survives the formatter. Ordering comparisons
    and ``BETWEEN`` are the operators a passage-surface predicate can be built
    from, in either operand order; bare ``=`` is deliberately not among them,
    because it is how the column is bound in Python (``chunk_index=row[4]``) and
    is not a scoping predicate in any spelling.
    """
    source = Path(inspect.getfile(PostgresContentStore)).read_text(encoding="utf-8")

    # After this change the only legitimate comparison against the column lives
    # inside the helper, which builds it from a variable and so matches neither.
    inline = re.search(
        r"chunk_index\s*(?:<|>|BETWEEN\b)|(?:<|>)=?\s*chunk_index", source, re.IGNORECASE
    )
    assert inline is None, (
        "the passage-surface predicate is spelled inline somewhere "
        f"({inline.group(0)!r} if matched); route it through _passage_rows_only "
        "so the condition stays named once"
    )
    # One definition plus a call at every passage read.
    assert source.count("_passage_rows_only(") >= 2, (
        "_passage_rows_only is defined but never called -- the passage reads are no longer scoped"
    )


def test_the_content_store_surface_set_is_expressed_once():
    """The accounting names its surfaces from one place rather than by literal.

    This is the decay the accounting already suffered once: the reads and the
    reclaim statement each spelled the passage table by name, so a second
    surface arrived and every one of them kept measuring the first. Stating the
    set once means a third surface is one edit, and a reading added without it
    is visible as one.

    Scoped to the accounting members rather than to the module. Both surface
    names appear as literals throughout the CRUD statements, legitimately and
    unavoidably -- a module-wide scan would be satisfied by nothing and would
    catch nothing.

    Matched on the quoted token, because that is the only form a table name can
    take inside these statements: each is interpolated into SQL, so a literal
    reintroduced here would be a quoted one. The constant's own definition is
    excluded by construction -- it is not a member of the class.
    """
    # Every member that builds a statement over the surfaces: none may spell one
    # inline. ``_present_surfaces`` is here for that assertion and not for the
    # one below, because it receives the set as a parameter rather than reading
    # it -- the member that names the set it reclaims is ``optimize``.
    members = (
        PostgresContentStore.count_rows,
        PostgresContentStore.count_retained_versions,
        PostgresContentStore.count_small_fragments,
        PostgresContentStore.measured_byte_size,
        PostgresContentStore.optimize,
        PostgresContentStore._bloat_snapshot,
        PostgresContentStore._present_surfaces,
    )
    reads_the_set = tuple(m for m in members if m is not PostgresContentStore._present_surfaces)
    for member in members:
        source = inspect.getsource(member)
        for surface in _CONTENT_STORE_SURFACES:
            spelled = re.search(rf"""['"]{re.escape(surface)}\b""", source)
            assert spelled is None, (
                f"{member.__name__} spells the surface {surface!r} inline; render it "
                "from _CONTENT_STORE_SURFACES so the set stays named once"
            )
        if member not in reads_the_set:
            continue
        assert "_CONTENT_STORE_SURFACES" in source, (
            f"{member.__name__} reads neither the surface set nor a surface name -- "
            "it no longer describes the content store's surfaces"
        )
        # Indexing into the set satisfies the two assertions above while still
        # measuring a single surface, and reads at a glance exactly like the
        # correct code. It is not the only such form -- unpacking the tuple or
        # taking the first of an iterator over it evade this too -- so this
        # assertion is the cheap guard against the shape most likely to be
        # written, not a closed proof. What excludes the others is behavioural:
        # every member listed here is also covered by the tests above.
        assert "_CONTENT_STORE_SURFACES[" not in source, (
            f"{member.__name__} subscripts the surface set; it must read the whole "
            "set, not one member of it"
        )


async def test_service_reports_zero_passages_for_a_keyword_document_level_hit(
    store, graph_store, stub_embedding_provider, minimal_config
):
    """The binding's zero survives the service, on the keyword arm.

    The count the caller sees is the service's, and the service reconciles two
    ways of reporting one: an arm that returns a row per passage, whose rows it
    tallies, and an arm whose row stands for a whole document and carries its
    own count. Taking the larger of the two read both -- until a document
    matched through its document surface, where the honest answer is zero and
    the tally of rows is one. The larger was one, so the arm whose storage layer
    had this right all along still served a caller the wrong number.

    Against the real binding rather than the in-memory double, which stores a
    document surface but never consults it on the keyword arm.
    """
    from datetime import datetime, timezone

    from sage.models.enums import PipelineStatus, SourceType
    from sage.models.schemas import DiscoverRequest, Document, RetrievalMode
    from sage.services.retrieval import RetrievalService

    now = datetime.now(timezone.utc)
    doc = Document(
        id="0000ab01_surface_only",
        title="Zetaword Catalog",
        source_type=SourceType.MARKDOWN,
        source_path="imports/unrelated_stem.md",
        lifecycle_status="active",
        source_content_hash="sha256:" + "ab" * 32,
        adapter_version="1",
        created_by="t",
        created_at=now,
        last_modified_by="t",
        updated_at=now,
        pipeline_status=PipelineStatus.ABSTRACTION_COMPLETE,
        tags=["design"],
    )
    await graph_store.insert_document(doc)
    # The body shares no term with the title, so a hit naming the title's terms
    # cannot have come through a passage.
    await store.index_chunks(
        doc.id,
        [_chunk(doc.id, content="unrelated body prose", embedding=[0.0] * EMBEDDING_DIM)],
    )
    await _surface(store, doc.id, matchable="Zetaword Catalog", orienting="")

    service = RetrievalService(
        graph_store=graph_store,
        content_store=store,
        embedding_provider=stub_embedding_provider,
        config=minimal_config,
    )
    response = await service.discover(DiscoverRequest(mode=RetrievalMode.KEYWORD, query="zetaword"))

    hits = [h for h in response.results if h.document.id == doc.id]
    assert hits, "the document surface did not answer the keyword query"
    assert hits[0].matched_chunk_count == 0, (
        "the binding's zero was floored to one on the way to the caller"
    )
    assert not (hits[0].chunk_content or ""), "a document-level hit carries no excerpt"


# ---------------------------------------------------------------------------
# The whole-query fallback reaches both surfaces (CAS-ADR-049 Decisions 7-8)
# ---------------------------------------------------------------------------


async def test_a_negated_query_still_reaches_a_title(store):
    """A query carrying a negation can still match a document by its title.

    A negation makes the query's shape undecomposable, so it is evaluated whole
    against a single text unit rather than across the document. That scope is
    deliberate and unchanged here -- but reading only passages left the title
    unreachable by any such query, which is a hole in the guarantee that a
    document is findable by its own name.
    """
    await store.index_chunks("negated", [_chunk("negated", content="unrelated body prose")])
    await _surface(store, "negated", matchable="Zetaword Catalog", orienting="")

    hits = await store.search_bm25("zetaword -absentword", limit=10)

    assert [h.document_id for h in hits] == ["negated"], (
        "a negated query could not reach the document's title"
    )


async def test_the_fallback_does_not_make_derived_text_matchable(store):
    """Widening the fallback to the surface must not widen it past the line.

    The surface carries authored text and derived text in separate columns, and
    only the authored one is a match arm. Reaching the surface from this path
    must use that column, or a negated query would match a filename stem or a
    generated abstract that no other query form can reach.
    """
    await store.index_chunks("derived", [_chunk("derived", content="unrelated body prose")])
    await _surface(
        store, "derived", matchable="Ordinary Title", orienting="iotaword-quarterly-review"
    )

    assert await store.search_bm25("iotaword -absentword", limit=10) == [], (
        "derived text satisfied a match through the whole-query fallback"
    )
    control = await store.search_bm25("ordinary -absentword", limit=10)
    assert [h.document_id for h in control] == ["derived"], (
        "positive control: authored text on the same row is reachable this way"
    )


async def test_a_headingless_passage_is_not_mistaken_for_a_document_surface(store):
    """An empty heading path does not make a row document-level.

    The reason the surface is named by a field rather than inferred: a
    document whose source carries no headings has a genuine passage whose
    heading path is empty, and it is indistinguishable from a document-level
    row by that property alone. Nothing else in the suite holds a headingless
    passage, so a tally "simplified" back to the heading-path heuristic would
    go green everywhere else and wrong here.
    """
    await store.index_chunks(
        "headless",
        [
            _chunk(
                "headless",
                content="zetaword catalog in a document that has no headings",
                heading_path="",
                embedding=_emb(0),
            )
        ],
    )

    [keyword_hit] = await store.search_bm25("zetaword", limit=10)
    assert keyword_hit.heading_path == "", "the fixture is not a headingless passage"
    assert keyword_hit.is_document_surface is False, (
        "a headingless passage was classified as a document-level row"
    )
    assert keyword_hit.matched_chunk_count == 1, "a genuine passage counts as one"

    semantic = [h for h in await store.search_semantic(_emb(0), limit=10)]
    assert [h.is_document_surface for h in semantic] == [False]
    assert semantic[0].content.startswith("zetaword catalog"), (
        "the passage's excerpt was suppressed as if it were document-level"
    )


# ---------------------------------------------------------------------------
# Which path answers, and that the set of paths is closed
#
# The tests above cover what each path matches. These cover the dispatch: which
# of the two paths answers a given query shape, where the folded arm sits
# relative to them, and that nothing else answers at all. They read the path
# from what a caller sees rather than from the method that produced it, so they
# survive a rename of the private methods and would fail a consolidation that
# changed the answer.
#
# The observable is the match unit, not the row shape. Both paths now return
# one row per matching document and budget limit by documents, so the row shape
# no longer distinguishes them; what still does is which text has to satisfy
# the query and therefore what matched_chunk_count counts. The document scope
# counts a document's passages carrying a required lexeme, the within-unit path
# counts those satisfying the whole query, and a passage carrying an excluded
# term separates the two.
# ---------------------------------------------------------------------------


async def _two_chunk_document(store, document_id="routed", term="deltaword"):
    """A document whose term is carried by two passages, not one.

    Two is what makes the count legible: either path collapses the document
    into one row, and only the count says how many passages stood behind it.
    With a single passage the count is one on any scope and the assertions
    below would hold against a binding that never counted at all.
    """
    await store.index_chunks(
        document_id,
        [
            _chunk(document_id, content=f"{term} in the first passage", chunk_index=0),
            _chunk(document_id, content=f"{term} in the second passage", chunk_index=1),
        ],
    )


async def _one_passage_excludes(store, document_id="routed"):
    """Both passages carry the term; only one also carries the excluded word.

    This is what separates the two match units now that both answer with one
    row per document. Against ``deltaword -absentword`` the document scope
    would count both passages -- it ranks and counts on the query's *required*
    lexemes, of which ``absentword`` is not one -- while the within-unit path
    counts only the passage that satisfies the query entire. A fixture whose
    passages both satisfy the exclusion counts two either way and would leave
    the routing unasserted.
    """
    await store.index_chunks(
        document_id,
        [
            _chunk(document_id, content="deltaword in the first passage", chunk_index=0),
            _chunk(document_id, content="deltaword absentword together", chunk_index=1),
        ],
    )


async def test_a_decomposable_query_is_answered_at_document_scope(store):
    """A plain conjunction collapses a document's passages into one row."""
    await _two_chunk_document(store)

    rows = [r for r in await store.search_bm25("deltaword", limit=10) if r.document_id == "routed"]

    assert len(rows) == 1, "a document-scoped answer returns each document once"
    assert rows[0].matched_chunk_count == 2, (
        "the row must count both passages carrying the term, not just its excerpt's"
    )


async def test_a_negated_query_is_answered_within_one_unit(store):
    """A negation is evaluated per unit, so a passage answers for itself."""
    await _one_passage_excludes(store)

    rows = [
        r
        for r in await store.search_bm25("deltaword -absentword", limit=10)
        if r.document_id == "routed"
    ]

    assert len(rows) == 1, "a document is represented once whichever path answered"
    assert rows[0].matched_chunk_count == 1, (
        "the passage carrying the excluded term was counted, so the query was "
        "evaluated across the document rather than within one passage"
    )


async def test_an_alternation_query_is_answered_at_document_scope(store):
    """An alternation decomposes too, into one intersection per branch."""
    await _two_chunk_document(store)

    rows = [
        r
        for r in await store.search_bm25("deltaword or absentword", limit=10)
        if r.document_id == "routed"
    ]

    assert len(rows) == 1, "a document-scoped answer returns each document once"
    assert rows[0].matched_chunk_count == 2, (
        "the row must count both passages carrying the term, not just its excerpt's"
    )


async def test_an_alternation_branch_is_satisfied_across_a_document(store):
    """A branch's conjuncts may be split between passages, as a bare one may.

    The scope discontinuity this closes: ``deltaword epsilonword`` matched a
    document carrying one term in each passage, while the weaker ``gammaword or
    deltaword epsilonword`` did not, because the alternation sent the whole
    query to the within-unit path and no passage carries both.

    Both branches are given a document to match, and both must come back. A
    bare conjunction would satisfy every claim below about ``routed`` on its
    own, so the branch reaching ``disjunct`` is what makes this a test of the
    alternation rather than a second copy of the conjunction test above -- and
    it pins the branches as unioned rather than intersected, which would return
    neither document.
    """
    await store.index_chunks(
        "routed",
        [
            _chunk("routed", content="deltaword in the first passage", chunk_index=0),
            _chunk("routed", content="epsilonword in the second passage", chunk_index=1),
        ],
    )
    await store.index_chunks("disjunct", [_chunk("disjunct", content="gammaword only here")])

    rows = {
        r.document_id: r
        for r in await store.search_bm25("gammaword or deltaword epsilonword", limit=10)
    }

    assert set(rows) == {"routed", "disjunct"}, (
        "each branch must contribute the documents it matches, and only a union does"
    )
    assert rows["routed"].matched_chunk_count == 2, (
        "a branch's conjuncts held apart in two passages did not match across them"
    )


async def test_a_negation_beside_an_alternation_still_routes_within_one_unit(store):
    """Negation refuses the decomposition whatever else the query carries.

    The two refusals are ordered, and this is what pins the order: a query
    carrying both a ``|`` and a ``!`` must still reach the within-unit path. An
    implementation that split the branches first and looked for the negation
    afterwards would document-scope a negated query, closing a question
    deliberately left open rather than answering it.
    """
    await _one_passage_excludes(store)

    rows = [
        r
        for r in await store.search_bm25("deltaword -absentword or gammaword", limit=10)
        if r.document_id == "routed"
    ]

    assert len(rows) == 1, "a document is represented once whichever path answered"
    assert rows[0].matched_chunk_count == 1, (
        "the passage carrying the excluded term was counted, so the branches "
        "were split before the negation was looked for"
    )


async def test_an_exclusion_narrows_the_scope_of_the_whole_query(store):
    """Appending an excluded term can drop a document the query without it matched.

    The disclosed consequence of the negation keeping the scope it had. The
    refusal is whole-query, so a conjunction that was satisfied across two
    passages is re-evaluated within one the moment any term is excluded --
    which makes a strictly weaker query match strictly less, the shape
    CAS-ADR-048 names as the defect it closes for the alternation and leaves
    open for the negation (Decision 8).

    Pinned because it is now stated on the caller-facing surfaces, and a
    contract sentence with no test is a claim rather than a guarantee. If the
    negation is ever scoped per branch, this test is the one that should fail
    and be rewritten rather than quietly deleted.
    """
    await store.index_chunks(
        "split",
        [
            _chunk("split", content="deltaword in the first passage", chunk_index=0),
            _chunk("split", content="epsilonword in the second passage", chunk_index=1),
        ],
    )

    assert [r.document_id for r in await store.search_bm25("deltaword epsilonword", limit=10)] == [
        "split"
    ], "positive control: the conjunction is satisfied across the document"

    assert await store.search_bm25("deltaword epsilonword -absentword", limit=10) == [], (
        "excluding a term the document does not carry still narrowed it out, "
        "because the exclusion re-scopes the whole query to one passage"
    )


async def test_an_exclusion_only_query_is_answered_within_one_unit(store):
    """A query requiring no lexeme is answered per unit, like the shapes above.

    It reaches the fallback by the first dispatch decision rather than the
    second, but the two are ordered rather than independent: every no-lexeme
    query also fails the decomposition, so deleting the first decision outright
    leaves this test green. What is pinned here is the behaviour -- a query
    asking only for an absence is answered within one unit -- and not the
    decision that delivers it, which no test can isolate because nothing routes
    on it alone.
    """
    await _two_chunk_document(store)

    rows = [
        r for r in await store.search_bm25("-absentword", limit=50) if r.document_id == "routed"
    ]

    assert len(rows) == 1, "a query asking only for an absence still reaches the document"
    assert rows[0].matched_chunk_count == 2, (
        "both passages satisfy an absence, and the row must say so; the "
        "document scope cannot answer this shape at all, having no lexeme to rank on"
    )


# ---------------------------------------------------------------------------
# The within-unit path budgets limit by documents
#
# The scope stays within one unit of text; what the rows below pin is that the
# budget does not. A path returning one row per matching passage under a single
# outer limit lets one document spend the whole of it, so another document --
# including the active head of the crowder's own supersedes chain -- never
# reaches the caller at all, and no downstream re-rank can recover a row that
# was never fetched.
#
# Every query here carries a negation, because a plain one routes to the
# document scope and would pass against the defect.
# ---------------------------------------------------------------------------


async def _crowder_and_victim(store, passages=4, crowder="a_crowder", victim="b_victim"):
    """One document with many matching passages beside one with a single passage.

    The relative order of the ids is load-bearing and not decorative. For a
    query of this shape ``ts_rank`` returns its floor identically for every
    row, so the score orders nothing and the document id supplies the order
    outright. The crowder must sort *first* or a row budget spends itself on
    the victim, which is already the document the assertions look for, and the
    test passes against the defect it exists to catch. Callers reaching the
    graph store pass well-formed ids that keep that order.
    """
    await store.index_chunks(
        crowder,
        [_chunk(crowder, content=f"alphaword passage {i}", chunk_index=i) for i in range(passages)],
    )
    await store.index_chunks(victim, [_chunk(victim, content="alphaword once only")])


async def test_a_within_unit_limit_is_a_document_budget(store):
    """``limit`` bounds documents on this path too, not the rows it fetched.

    The crowder carries four matching passages against a limit of two, so a row
    budget is spent inside it before the victim is reached and the victim is
    the document that never comes back.

    A third matching document is what makes this a test of the budget rather
    than of the row shape. Without it the corpus holds exactly as many
    documents as the limit admits, and a binding applying no limit at all
    returns the same two ids and passes.
    """
    await _crowder_and_victim(store)
    await store.index_chunks("c_surplus", [_chunk("c_surplus", content="alphaword here too")])

    rows = await store.search_bm25("alphaword -absentword", limit=2)

    assert {r.document_id for r in rows} == {"a_crowder", "b_victim"}, (
        "two documents, not four passages of one; a row budget answers with one id, "
        "and a binding that never applies the limit answers with three"
    )


async def test_the_two_keyword_paths_budget_limit_alike(store):
    """The same corpus and terms answer with the same documents on either path.

    The negation is the only difference between the two calls, and it changes
    which path answers. Nothing about the corpus or the query changed, so a
    caller who appends an excluded term must not thereby lose a document the
    query without it returned.
    """
    await _crowder_and_victim(store)

    document_scoped = await store.search_bm25("alphaword", limit=2)
    within_unit = await store.search_bm25("alphaword -absentword", limit=2)

    assert {r.document_id for r in within_unit} == {r.document_id for r in document_scoped}, (
        "appending an exclusion dropped a document by changing what limit counts"
    )


async def test_a_within_unit_row_prefers_a_passage_excerpt_over_the_surface(store):
    """A document matching on both surfaces is represented by its best passage.

    The surface row sorts at the index the port reserves for document-level
    text, which is ahead of every passage, so a representative chosen on that
    order alone is the surface row -- and the document comes back excerptless
    and marked document-level despite its passages having matched.
    """
    await store.index_chunks("both", [_chunk("both", content="zetaword in the body prose")])
    await _surface(store, "both", matchable="Zetaword Catalog", orienting="")

    [row] = await store.search_bm25("zetaword -absentword", limit=10)

    assert row.is_document_surface is False, (
        "a document whose passage matched was reported as document-level"
    )
    assert "zetaword" in row.content, "the surface row displaced the passage's excerpt"
    assert row.heading_path, "the surface row displaced the passage's heading"
    assert row.matched_chunk_count == 1, "the matching passage was not counted"


async def test_a_within_unit_document_matched_only_by_its_surface_is_document_level(store):
    """No passage matched, so the row is document-level and counts none.

    The complement of the test above, and what keeps its fix from flooring
    every document at one passage: a document reached through nothing but its
    title must still answer zero, which is what the field's published
    description promises (CAS-ADR-049 Decision 5).
    """
    await store.index_chunks("titled", [_chunk("titled", content="unrelated body prose")])
    await _surface(store, "titled", matchable="Zetaword Catalog", orienting="")

    [row] = await store.search_bm25("zetaword -absentword", limit=10)

    assert row.is_document_surface is True, "only the title matched, so the row is document-level"
    assert row.matched_chunk_count == 0, "a document-level row stands for no passage"
    assert not row.content, "a document-level row carries no excerpt"


async def _keyword_service(store, graph_store, stub_embedding_provider, minimal_config):
    """A retrieval service over the real binding rather than the double.

    The in-memory double implements no within-unit path, so a service test of
    that path has nowhere else to run.
    """
    from sage.services.retrieval import RetrievalService

    return RetrievalService(
        graph_store=graph_store,
        content_store=store,
        embedding_provider=stub_embedding_provider,
        config=minimal_config,
    )


async def _insert_document(graph_store, document_id, title, lifecycle_status):
    from datetime import datetime, timezone

    from sage.models.enums import PipelineStatus, SourceType
    from sage.models.schemas import Document

    now = datetime.now(timezone.utc)
    await graph_store.insert_document(
        Document(
            id=document_id,
            title=title,
            source_type=SourceType.MARKDOWN,
            source_path=f"imports/{document_id}.md",
            lifecycle_status=lifecycle_status,
            source_content_hash="sha256:" + "ab" * 32,
            adapter_version="1",
            created_by="t",
            created_at=now,
            last_modified_by="t",
            updated_at=now,
            pipeline_status=PipelineStatus.ABSTRACTION_COMPLETE,
        )
    )


async def test_a_negated_query_does_not_drop_the_active_head_for_its_predecessor(
    store, graph_store, stub_embedding_provider, minimal_config
):
    """The active head of a chain outranks its own predecessor at any limit.

    The lifecycle guarantee sorts the hits it was given, so it is silent about
    which hits those were: a predecessor carrying more matching passages than
    the whole fetch budget leaves the head unfetched, and an agent asking a
    negation-shaped question is handed a retired version with no sign that a
    newer one exists and matched better.

    The predecessor's passage count is derived from the service's own
    over-fetch rather than written as a literal, so only a change to what the
    budget *counts* satisfies this. A literal count large enough for today's
    multiplier would leave a rival the docstring could not honestly exclude:
    raising the multiplier would clear that threshold and turn the test green
    while the shape it describes stood untouched.
    """
    from sage.models.schemas import DiscoverRequest, RetrievalMode
    from sage.services.retrieval import _FETCH_MULTIPLIER_NONE

    limit = 2
    crowding_passages = limit * _FETCH_MULTIPLIER_NONE + 2

    await _insert_document(graph_store, "0000aa01_adr_v1", "Retired Catalog", "archived")
    await _insert_document(graph_store, "0000bb02_adr_v2", "Current Catalog", "active")
    await store.index_chunks(
        "0000aa01_adr_v1",
        [
            _chunk("0000aa01_adr_v1", content=f"alphaword passage {i}", chunk_index=i)
            for i in range(crowding_passages)
        ],
    )
    await store.index_chunks(
        "0000bb02_adr_v2", [_chunk("0000bb02_adr_v2", content="alphaword stated once")]
    )

    service = await _keyword_service(store, graph_store, stub_embedding_provider, minimal_config)
    response = await service.discover(
        DiscoverRequest(mode=RetrievalMode.KEYWORD, query="alphaword -absentword", limit=limit)
    )

    ids = [h.document.id for h in response.results]
    assert "0000bb02_adr_v2" in ids, "the active head was crowded out by its own predecessor"
    assert ids[0] == "0000bb02_adr_v2", "the active head was fetched but did not rank first"


async def test_total_available_agrees_across_the_two_keyword_paths(
    store, graph_store, stub_embedding_provider, minimal_config
):
    """A caller reading ``total_available`` to decide whether to page sees one number.

    It counts the documents surviving dedup over the rows that were fetched, so
    a path spending its budget on one document's passages reports a total the
    corpus does not support -- and reports a different one at each limit, for a
    query and a corpus that did not change.
    """
    from sage.models.schemas import DiscoverRequest, RetrievalMode
    from sage.services.retrieval import _FETCH_MULTIPLIER_NONE

    await _crowder_and_victim(
        store,
        passages=2 * _FETCH_MULTIPLIER_NONE + 2,
        crowder="0000aa01_crowder",
        victim="0000bb02_victim",
    )
    await _insert_document(graph_store, "0000aa01_crowder", "Crowder", "active")
    await _insert_document(graph_store, "0000bb02_victim", "Victim", "active")

    service = await _keyword_service(store, graph_store, stub_embedding_provider, minimal_config)

    async def _total(query, limit):
        response = await service.discover(
            DiscoverRequest(mode=RetrievalMode.KEYWORD, query=query, limit=limit)
        )
        return response.total_available

    assert await _total("alphaword -absentword", 2) == await _total("alphaword", 2), (
        "the two paths disagree on how many documents match the same terms"
    )
    assert await _total("alphaword -absentword", 2) == await _total("alphaword -absentword", 3), (
        "the total moved with the page size, for a fixed query and corpus"
    )


# ---------------------------------------------------------------------------
# Ordering is total, so two identical calls agree
#
# Both verbs rank on a score two rows can share, and a clause that stops there
# leaves the tied block in whatever order the scan produced. The document
# scope's own ordering already broke its ties on the id; these cover the two
# clauses that did not, and each is written the way the tie is actually
# reachable: rows inserted in descending id order, so insertion order and id
# order disagree, and the scan order perturbed between two calls, because
# Postgres will answer a small unperturbed table the same way twice and a test
# without the perturbation passes against the defect.
# ---------------------------------------------------------------------------


_TIED_CONTENT = "tiedword carried identically"
_TIED_HEADINGS = {"tie_zulu": "", "tie_alfa": "Shared"}
_TIED_BRAVO_CONTENT = " ".join(["tiedword"] * 8)


def _tied_passages(document_id):
    """The two passages a tied document carries, built the one way.

    Written in descending index order, so physical order and index order
    disagree. Without that the two land in the order the assertions expect
    anyway, and a sort key that ties across them -- the heading, which is not
    unique within a document -- passes on a table small enough for Postgres to
    answer the same way twice however it is perturbed.
    """
    return [
        _chunk(
            document_id,
            content=f"{_TIED_CONTENT} {index}",
            heading_path=_TIED_HEADINGS[document_id],
            chunk_index=index,
            embedding=_emb(0),
        )
        for index in (1, 0)
    ]


async def _perturb_scan_order(store):
    """Move both tied documents' passages to new physical positions, unchanged.

    ``index_chunks`` is delete-then-insert, so what comes back out is what was
    there before, at a new scan position -- the content-store counterpart of
    the graph store's helper of the same name. Rewriting a *different* shape
    would reshape the fixture rather than perturb it, which is why this rebuilds
    from ``_tied_passages`` rather than composing a chunk of its own.

    Both, not one. Each tied document carries a different tie -- ``tie_alfa``'s
    two passages share a heading, ``tie_zulu``'s share the empty one with its
    surface row -- so perturbing one leaves the other's block in whatever order
    it was written, which a small table hands back the same way twice. A sort
    key that ties there then passes.
    """
    for document_id in _TIED_HEADINGS:
        await store.index_chunks(document_id, _tied_passages(document_id))


async def _tied_documents(store, content=_TIED_CONTENT):
    """A tied pair, and one document that outranks it on both verbs.

    The pair is written in descending id order, with identical text and one
    shared one-hot embedding, so an identical ``ts_rank`` and an identical
    cosine distance leave nothing but the id to separate them -- a genuine tie
    rather than a near-miss a float comparison would break.

    Each of the pair carries *two* passages under one heading and a surface,
    which is what makes the fixture reach the ties inside a document as well as
    the one between them. A heading is not unique within a document, so a sort
    ending there leaves those two passages tied; and ``tie_zulu``'s passages
    carry no heading at all, so on that document the passages tie with the
    surface row too, which also reports an empty heading. Only the passage
    index separates all three.

    ``tie_bravo`` is the third document and is not decoration either. It
    carries the term repeatedly and a different one-hot vector, so it outranks
    the pair on both verbs, and its id sorts *between* theirs. Without it every
    row in the fixture is tied, and an ordering that dropped the score entirely
    and sorted on the id alone would satisfy the assertions below while
    destroying the ranking -- the rival the tie itself cannot exclude. It
    carries no document surface, so its higher rank shows up once rather than
    twice.
    """
    for document_id in ("tie_zulu", "tie_alfa"):
        await store.index_chunks(document_id, _tied_passages(document_id))
        await store.upsert_document_surface(
            DocumentSurface(
                document_id=document_id, matchable=content, orienting="", embedding=_emb(0)
            )
        )
    await store.index_chunks(
        "tie_bravo",
        [_chunk("tie_bravo", content=_TIED_BRAVO_CONTENT, embedding=_emb(1))],
    )
    return content


async def test_a_tied_within_unit_result_orders_on_the_document_id(store):
    """The fallback's rows are ordered totally, so a rerun cannot disagree.

    Reached by a negation, which is the shape that still routes here -- and for
    a query of this shape the id is not breaking a tie between scores, it is
    the whole order. ``ts_rank`` returns its floor wherever a negation is what
    the match turns on, identically for every row whatever its text, so
    ``doc_score DESC`` here sorts a column that is constant by construction and
    before this clause the rows came back in whatever order the scan produced
    -- which the perturbation below then changes underneath a caller who
    changed nothing. The floor is not a property of every query reaching this
    path: an alternation one of whose branches carries no exclusion scores
    above it, which is why the score clause is not merely decorative.

    ``tie_bravo`` carries the term eight times and still does not lead, which
    is that floor stated as behaviour: it outranks the pair on any query
    without a negation, and ranks level with it on this one. The claim that the
    score is not merely being ignored belongs to the semantic sibling below,
    whose path has real scores to order.

    The rows are compared with their excerpts, so the order *within* a document
    is asserted and not only the order between them. A document answers once,
    so that order is no longer visible as rows but as which passage represents
    it: the same ``chunk_index`` tiebreak, moved from the outer clause into the
    window that picks the representative, and still the only thing separating
    two passages under one heading.
    """
    await _tied_documents(store)

    first = [(r.document_id, r.content) for r in await store.search_bm25("tiedword -x", limit=10)]
    await _perturb_scan_order(store)
    second = [(r.document_id, r.content) for r in await store.search_bm25("tiedword -x", limit=10)]

    assert first == second, "two identical calls disagreed after a no-op rewrite"
    assert {r.score for r in await store.search_bm25("tiedword -x", limit=10)} == {1e-20}, (
        "the floor is the premise of the assertion below; if scores vary here, "
        "this path orders on something and the id is no longer the whole order"
    )
    assert first == [
        ("tie_alfa", f"{_TIED_CONTENT} 0"),
        ("tie_bravo", _TIED_BRAVO_CONTENT),
        ("tie_zulu", f"{_TIED_CONTENT} 0"),
    ], "documents must order on the id, and each be represented by its first passage"


async def test_a_tied_semantic_result_orders_on_the_document_id(store):
    """The same for the semantic verb's outer sort over its two arms.

    What this buys is bounded, and the bound is worth stating: each arm still
    takes its own ``LIMIT`` under an order the vector index supplies, and that
    clause is deliberately left alone, since a tiebreak on it is a clause no
    index can serve and would cost a full scan of both tables. So a tie *at an
    arm's cutoff* can still vary the set this sorts. Fixed here is the part
    that costs nothing: given the same rows, the answer no longer depends on
    the order they arrived in. The fixture stays well inside both limits, so
    the set is fixed and the sort is what is under test.

    Queried on ``tie_bravo``'s own vector, so it scores 1 against the pair's 0
    and must lead. As on the sibling above, the leading claim is what keeps the
    id a tiebreak rather than the sort, and the excerpts are compared so the
    order within a document is asserted along with the order between them.
    """
    await _tied_documents(store)

    first = [(r.document_id, r.content) for r in await store.search_semantic(_emb(1), limit=10)]
    await _perturb_scan_order(store)
    second = [(r.document_id, r.content) for r in await store.search_semantic(_emb(1), limit=10)]

    assert first == second, "two identical calls disagreed after a no-op rewrite"
    assert first == [
        ("tie_bravo", _TIED_BRAVO_CONTENT),
        ("tie_alfa", ""),
        ("tie_alfa", f"{_TIED_CONTENT} 0"),
        ("tie_alfa", f"{_TIED_CONTENT} 1"),
        ("tie_zulu", ""),
        ("tie_zulu", f"{_TIED_CONTENT} 0"),
        ("tie_zulu", f"{_TIED_CONTENT} 1"),
    ], (
        "the nearer document must lead and the tied set must then order on the id "
        "and the passage index, not on the order the arms produced it"
    )


async def _folded_surface_document(store, document_id="folded", title="Epsilon Level Text"):
    """A spaced title on a document surface, over passages carrying none of its words.

    The passage surface indexes literally, and a query naming a compound or a
    hyphenated identifier renders lexemes no spaced passage carries --
    ``first-passage`` becomes a phrase over ``first-passag``. Such a query
    therefore has nothing to reach on that surface by construction, which is
    why the folded arm is confined to the document surface and why a document
    with one is the only kind it can match.

    Not every query against this fixture needs folding to land: an underscore
    renders as its parts alone and matches the expanded title directly. Which
    renderings need the arm and which do not is the subject of
    ``test_a_hyphenated_query_reaches_a_spaced_title_only_by_folding``.
    """
    await store.index_chunks(
        document_id, [_chunk(document_id, content="body prose carrying none of the terms")]
    )
    await _surface(store, document_id, matchable=title, orienting="")


async def test_the_folded_arm_is_reachable_only_from_the_document_scoped_path(store):
    """Folding widens the document-scoped path and nothing else.

    The arm is spliced into that path's query, so a shape routed to the
    within-unit path never gets it. A title reachable only by folding is
    therefore reachable by a bare query and not by the same query carrying a
    negation -- which is the arm's placement, stated as behaviour rather than
    as a claim in a docstring.
    """
    await _folded_surface_document(store)

    reached = await store.search_bm25("epsilonLevelText", limit=10)
    assert [r.document_id for r in reached] == ["folded"], (
        "the folded arm did not reach a title the raw tokenization cannot match"
    )

    assert await store.search_bm25("epsilonLevelText -absentword", limit=10) == [], (
        "the folded arm reached a query routed to the within-unit path"
    )
    control = await store.search_bm25("epsilon -absentword", limit=10)
    assert [r.document_id for r in control] == ["folded"], (
        "positive control: the within-unit path does reach this surface, just not "
        "through a folded compound"
    )


async def test_an_alternation_reaches_the_folded_arm(store):
    """Routing an alternation to the document-scoped path hands it that path's arm.

    The widening is not confined to what the branches themselves match. The
    folded arm is spliced into the document-scoped query, so a title reachable
    only by folding used to become unreachable the moment a disjunct was
    appended to the query that reached it -- ``epsilonLevelText`` landed and
    ``epsilonLevelText or absentword`` did not, though the second asks for
    strictly less.
    """
    await _folded_surface_document(store)

    hits = await store.search_bm25("epsilonLevelText or absentword", limit=10)

    assert [r.document_id for r in hits] == ["folded"], (
        "an alternation did not reach the arm its path carries"
    )


async def test_an_alternation_query_still_reaches_a_title(store):
    """A branch may be satisfied by the document surface rather than a passage.

    Each arm spans both authored surfaces, so distributing the alternation must
    distribute that union with it rather than resolving branches against
    passages alone -- which would leave a document findable by its own name
    only until a disjunct was added to the query.
    """
    await store.index_chunks("alternated", [_chunk("alternated", content="unrelated body prose")])
    await _surface(store, "alternated", matchable="Zetaword Catalog", orienting="")

    hits = await store.search_bm25("zetaword or absentword", limit=10)

    assert [h.document_id for h in hits] == ["alternated"], (
        "an alternation query could not reach the document's title"
    )


async def test_a_hyphenated_query_reaches_a_spaced_title_only_by_folding(store):
    """The hyphen is the separator the arm is load-bearing for, and the underscore is not.

    Folding treats the two as one character class; the text-search
    configuration does not. ``epsilon_level`` renders as its parts alone, which
    the index-side expansion already writes into adjacent positions, so it
    lands without the arm. ``epsilon-level`` renders as the hyphenated whole
    *followed by* those parts, and the expansion never produces that whole --
    so the query demands a lexeme no widening of the index supplies, and only
    the folded arm can answer it.

    Pinned because the distinction is invisible in the transform and easy to
    state backwards, and because the hyphenated form is the one nearly every
    identifier-shaped title carries.

    The two "without the arm" claims are made against the *within-unit* path,
    by appending a negation. That path never gets the folded arm, so what lands
    there landed without it. Asserting the bare renderings instead would say
    nothing: with the arm in place both land, so a binding where the underscore
    also needed the arm would pass identically -- the claim would hold only
    under a mutation the suite does not run.
    """
    await _folded_surface_document(store, title="Epsilon Level")

    underscore = await store.search_bm25("epsilon_level -absentword", limit=10)
    assert [r.document_id for r in underscore] == ["folded"], (
        "the underscore rendering needs no arm, so it must land on the path that has none"
    )

    assert await store.search_bm25("epsilon-level -absentword", limit=10) == [], (
        "the hyphenated rendering must be unreachable on that same path, or the arm "
        "is not what answers it"
    )

    assert [r.document_id for r in await store.search_bm25("epsilon-level", limit=10)] == [
        "folded"
    ], "and on the path that does have the arm, the same hyphenated rendering lands"


async def test_every_keyword_query_is_answered_by_exactly_one_path(store, monkeypatch):
    """No query is answered by both paths, or by neither.

    The row shape above says which of the two answered; it cannot say that a
    third did. Counting entries into each path is the one claim behaviour
    cannot make, so it is the only claim this test makes: a path added later
    that emitted a familiar row shape shows up here as a query entering
    neither.

    What it does not reach is a path that *delegates* to one of these two after
    doing something of its own first -- that still enters exactly one, and the
    row-shape tests above catch it only if it changes the shape. The claim here
    is that the dispatch has no third exit, not that nothing else runs.
    """
    await _two_chunk_document(store)
    entered: list[str] = []

    for name in ("_search_bm25_across_document", "_search_bm25_within_chunk"):
        original = getattr(type(store), name)

        def spy(self, *args, _name=name, _original=original, **kwargs):
            entered.append(_name)
            return _original(self, *args, **kwargs)

        monkeypatch.setattr(type(store), name, spy)

    queries = [
        "deltaword",
        "deltaword -absentword",
        "deltaword or absentword",
        "deltaword or absentword -otherword",
        "-absentword",
        "epsilonLevelText",
    ]
    for query in queries:
        entered.clear()
        await store.search_bm25(query, limit=10)
        assert len(entered) == 1, f"{query!r} entered {entered}, not exactly one path"

    entered.clear()
    for query in queries:
        await store.search_bm25(query, limit=10)
    assert set(entered) == {"_search_bm25_across_document", "_search_bm25_within_chunk"}, (
        "the matrix must reach both paths, or a path could be removed without failing"
    )


async def _render_statements(store, monkeypatch, query, limit=10):
    """The rendering statements one search issues, and what that search found.

    A rendering is the statement that asks the configuration how it reads a
    string; every other statement in a search is a match or a rank. Selecting
    on the shape rather than counting all statements keeps this measuring the
    round-trips the dispatch spends, not the search's own.

    The hits come back alongside so a caller can assert the search it measured
    was a real one. A statement count says nothing about whether anything
    matched, and a query reaching no document renders exactly as often as one
    reaching every document -- so without an assertion consuming the hits, the
    seeded corpus is scaffolding nothing reads, and would describe itself as a
    control while being inert.
    """
    seen: list[str] = []
    original = type(store)._fetchall

    async def spy(self, sql, *args, **kwargs):
        if sql.lstrip().startswith("SELECT websearch_to_tsquery"):
            seen.append(sql)
        return await original(self, sql, *args, **kwargs)

    monkeypatch.setattr(type(store), "_fetchall", spy)
    hits = await store.search_bm25(query, limit=limit)
    return seen, hits


async def test_a_keyword_search_renders_its_query_once(store, monkeypatch):
    """One round-trip answers both decisions and the folded arm.

    Dispatch needs the backend for exactly one thing: how the configuration
    reads the query. Both decisions read the same rendering, and the folded arm
    reads a second one of the same kind, so a search that asks more than once
    is asking twice for what it already had.
    """
    await _folded_surface_document(store)

    rendered, hits = await _render_statements(store, monkeypatch, "epsilonLevelText")

    assert len(rendered) == 1, f"the search issued {len(rendered)} rendering statements, not one"
    assert rendered[0].count("websearch_to_tsquery") == 2, (
        "a query with something to fold renders both forms, in the one statement"
    )
    assert [h.document_id for h in hits] == ["folded"], (
        "positive control: the measured search must be one that reaches a document, "
        "or the count is of a query that bailed before the arms it is supposed to cost"
    )


async def test_a_query_with_nothing_to_fold_renders_only_the_raw_form(store, monkeypatch):
    """Folding is decided before the statement is built, not after.

    The folded arm is dropped when folding changes nothing, and that is a pure
    text test -- so a query with no separators and no compound must not pay for
    a second rendering it would discard.
    """
    await _two_chunk_document(store)

    rendered, hits = await _render_statements(store, monkeypatch, "deltaword")

    assert len(rendered) == 1
    assert rendered[0].count("websearch_to_tsquery") == 1, (
        "a query with nothing to fold rendered a folded form anyway"
    )
    assert [h.document_id for h in hits] == ["routed"], (
        "positive control: the measured search must be one that reaches a document"
    )
