"""PostgresContentStore: behaviour, stats/bloat, and RRF fusion (CAS-ADR-042).

Runs against a real Postgres named by ``SAGE_TEST_PG_DSN`` via the ``pg_pool``
harness (a disposable per-session schema, truncated per test); skips cleanly
when no server is configured.
"""

from __future__ import annotations

import math
import os
from datetime import timedelta

import pytest

from sage.adapters.content_store_postgres import PostgresContentStore
from sage.adapters.interfaces import SYNTHETIC_HEADER_HEADING_PATH, Chunk
from sage.storage.postgres.schema import EMBEDDING_DIM
from sage.utils.rrf import rrf_fuse

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


async def test_get_heading_paths_excludes_synthetic_header(store):
    """The synthetic header marker is not a real heading and is omitted."""
    await store.index_chunks(
        "d1",
        [
            _chunk("d1", content="hdr", heading_path=SYNTHETIC_HEADER_HEADING_PATH, chunk_index=0),
            _chunk("d1", content="intro", heading_path="Intro", chunk_index=1),
            _chunk("d1", content="body", heading_path="Body", chunk_index=2),
        ],
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


async def test_replace_synthetic_header_preserves_body(store):
    """Header replacement swaps only the marker chunk; body chunks survive."""
    await store.index_chunks(
        "d1",
        [
            _chunk("d1", content="old header", heading_path=SYNTHETIC_HEADER_HEADING_PATH),
            _chunk("d1", content="body one", heading_path="Body", chunk_index=1),
        ],
    )
    await store.replace_synthetic_header_chunk(
        "d1",
        _chunk("d1", content="new header", heading_path=SYNTHETIC_HEADER_HEADING_PATH),
    )
    all_chunks = await store.get_all_chunks("d1")
    bodies = [c.content for c in all_chunks if c.heading_path != SYNTHETIC_HEADER_HEADING_PATH]
    headers = [c.content for c in all_chunks if c.heading_path == SYNTHETIC_HEADER_HEADING_PATH]
    assert bodies == ["body one"]
    assert headers == ["new header"]


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
    snap = await store.optimize(timedelta(0))
    assert snap["pre_versions"] > 0
    assert snap["post_versions"] == 0
    assert snap["post_bytes"] < snap["pre_bytes"]
    assert snap["post_small_fragments"] <= snap["pre_small_fragments"]
    assert await store.count_retained_versions() == 0
    assert await store.measured_byte_size() < pre_bytes


async def test_optimize_accepts_nonzero_threshold(store, pg_pool):
    """cleanup_older_than is accepted but irrelevant on Postgres: full reclaim."""
    await _disable_autovacuum(pg_pool)
    await _churn(store)
    snap = await store.optimize(timedelta(days=7))
    assert snap["post_versions"] == 0


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
