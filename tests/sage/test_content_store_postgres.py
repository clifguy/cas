"""PostgresContentStore: behaviour, stats/bloat, and RRF fusion (CAS-ADR-042).

Runs against a real Postgres named by ``SAGE_TEST_PG_DSN`` via the ``pg_pool``
harness (a disposable per-session schema, truncated per test); skips cleanly
when no server is configured.
"""

from __future__ import annotations

import asyncio
import math
import os
import time
from datetime import timedelta
from typing import TYPE_CHECKING, Any

import pytest

from sage.adapters.content_store_postgres import PostgresContentStore
from sage.adapters.interfaces import Chunk, DocumentSurface
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
    so the 200 prior rows become dead MVCC tuples awaiting reclamation."""
    await store.index_chunks(doc_id, _fat_chunks(doc_id, k=200))
    await store.index_chunks(doc_id, _fat_chunks(doc_id, k=5))


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
    """Pin autovacuum off on the chunks table for the duration of a bloat test.

    Bloat is observed by deliberately *not* reclaiming dead tuples; with
    autovacuum live, the launcher could clear them mid-test and make the signal
    vanish. Test-only determinism -- production leaves autovacuum on (it is the
    binding's self-healing path, with optimize() as the forcing function)."""
    async with pg_pool.connection() as conn:
        await conn.execute("ALTER TABLE chunks SET (autovacuum_enabled = false)")


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
    assert await store.count_chunks() == 1
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
    """Both documents match; the one whose chunk carries both terms ranks first."""
    await store.index_chunks("d1", [_chunk("d1", content="alphaword betaword together")])
    await store.index_chunks(
        "d2",
        [
            _chunk("d2", content="alphaword only here", chunk_index=0),
            _chunk("d2", content="betaword only here", chunk_index=1),
        ],
    )

    res = await store.search_bm25("alphaword betaword", limit=10)
    assert {r.document_id for r in res} == {"d1", "d2"}, "both documents match under document scope"
    assert res[0].document_id == "d1", (
        "co-occurrence within one chunk outranks the same terms split apart"
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


async def test_count_chunks(store):
    assert await store.count_chunks() == 0
    await store.index_chunks(
        "d1", [_chunk("d1", content="a"), _chunk("d1", content="b", chunk_index=1)]
    )
    await store.index_chunks("d2", [_chunk("d2", content="c")])
    assert await store.count_chunks() == 3


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
            assert await s.count_chunks() == 0
            assert await s.count_retained_versions() == 0
            assert await s.count_small_fragments() == 0
            assert await s.measured_byte_size() == 0
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
            _chunk(
                "d1",
                content="Title: T\nAbstract: a previously generated abstract",
                heading_path="__document_header__",
                chunk_index=-1,
            ),
            _chunk("d1", content="authored body", heading_path="Body", chunk_index=0),
        ],
    )

    assert await store.get_heading_paths("d1") == ["Body"]
    assert [c.content for c in await store.get_all_chunks("d1")] == ["authored body"]
    hits = await store.search_semantic([0.0] * EMBEDDING_DIM, limit=10)
    assert all(h.heading_path != "__document_header__" for h in hits), (
        "a legacy row reached a caller through the semantic arm"
    )
