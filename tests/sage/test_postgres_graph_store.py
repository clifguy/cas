"""PostgresGraphStore-specific tests (CAS-ADR-042).

Two tiers. The concreteness check (C0) and the row-converter exhaustive-field
closure tests (C1) are pure unit tests that need no server. The rest exercise
behavior only Postgres provides -- the chain-head trigger, the partial tier3
unique index shape, the close barrier, and concurrent multi-writer access that
the embedded SQLite store's single-writer model cannot support -- against a real
Postgres via the ``postgres_graph_store`` fixture, which skips without
``SAGE_TEST_PG_DSN``. Cross-backend parity (CRUD, edges, traversal, lifecycle,
uniqueness behaving identically to SQLite) is covered by the parametrized
``graph_store`` fixture in the shared behavioral test modules.
"""

import asyncio
import inspect
import uuid
from datetime import datetime, timezone

import pytest
from pydantic_core import PydanticUndefined

from sage.adapters.interfaces import GraphStore, NaturalKeyConflict
from sage.models.enums import (
    EdgeType,
    PipelineStatus,
    RationaleKind,
    ResolutionPolicy,
    SourceType,
    UserType,
)
from sage.models.schemas import Document, Edge, StagingEdge, User
from sage.storage.tier3_uniqueness import Tier3UniqueViolation

# Skips the whole module if psycopg is absent (the import below pulls it in).
# The C0/C1 unit tests then run anywhere psycopg is installed; C2+ use the
# postgres_graph_store fixture, which additionally skips without SAGE_TEST_PG_DSN.
PostgresGraphStore = pytest.importorskip("sage.storage.postgres.graph_store").PostgresGraphStore


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _doc(i: int, *, tier3: dict | None = None, doc_type: str = "ticket") -> Document:
    now = datetime.now(timezone.utc)
    return Document(
        id=f"{i:08x}_doc_{i}",
        title=f"Doc {i}",
        source_type=SourceType.MARKDOWN,
        source_path=f"/x/{i}.md",
        lifecycle_status="active",
        source_content_hash=f"sha256:{i:064x}",
        adapter_version="1",
        created_by="t",
        created_at=now,
        last_modified_by="t",
        updated_at=now,
        pipeline_status=PipelineStatus.PROJECTION_COMPLETE,
        tags=["a", "b"],
        doc_type=doc_type,
        tier3_metadata=tier3,
    )


def _edge(src: str, tgt: str | None, n: int, edge_type: EdgeType = EdgeType.REFERENCES) -> Edge:
    return Edge(
        id=str(uuid.UUID(int=n)),
        source_id=src,
        target_id=tgt,
        edge_type=edge_type,
        resolution_policy=ResolutionPolicy.TRANSITIVE_BOTH,
        created_at=datetime.now(timezone.utc),
        rationale="r",
    )


# ---------------------------------------------------------------------------
# C0: concreteness (no server)
# ---------------------------------------------------------------------------


def test_postgres_graph_store_is_concrete_graphstore():
    """PostgresGraphStore implements the full GraphStore port with no abstract
    methods left over. Fails at import/instantiation time if a method named in
    the ABC is missing -- catching an unported method without a server."""
    assert issubclass(PostgresGraphStore, GraphStore)
    assert not inspect.isabstract(PostgresGraphStore)


# ---------------------------------------------------------------------------
# C1: row-converter exhaustive-field closure (no server)
#
# Per the *CAS Projection-Point Audit Conventions* steering document, every
# projection point owes a closure pair: a single owning factory and an
# exhaustive-fields test that fails closed when a field is added to the
# destination model but not wired through the factory. psycopg dict rows are
# plain dicts with jsonb columns already parsed and booleans native, so the
# sentinel rows here are dicts. The loops use the three-branch closure idiom:
# the list/dict branch is forward defense for a future default_factory field
# (whose empty-container default would satisfy a naive ``is not None``); the
# non-None-default branch catches a dropped field whose Pydantic default
# would satisfy ``is not None`` coincidentally.
# ---------------------------------------------------------------------------


def _pg_edge_row() -> dict:
    return {
        "id": str(uuid.UUID(int=0xED9E0001)),
        "source_id": "00000001_doc_source",
        "target_id": "00000002_doc_target",
        "edge_type": EdgeType.REFERENCES.value,
        "resolution_policy": ResolutionPolicy.TRANSITIVE_BOTH.value,
        "source_valid_from_version": "00000001_doc_source",
        "target_valid_from_version": "00000002_doc_target",
        "valid_until_version": "00000003_doc_tombstone",
        "retracted_edge_id": str(uuid.UUID(int=0xED9E0002)),
        "created_at": datetime(2026, 5, 21, 10, 30, tzinfo=timezone.utc).isoformat(),
        "notes": "sentinel notes",
        "rationale": "sentinel rationale",
        "rationale_kind": RationaleKind.VERSION_CHAIN.value,
        "synced_from_version": "00000004_doc_synced",
        "synced_from_content_hash": "sha256:" + "ab" * 32,
    }


def test_pg_row_to_edge_populates_every_edge_field():
    edge = PostgresGraphStore._row_to_edge(_pg_edge_row())
    for field_name, field_info in Edge.model_fields.items():
        value = getattr(edge, field_name)
        annotation = field_info.annotation
        default = field_info.default
        if annotation == list[str] or annotation == (dict | None):
            assert value, (
                f"Edge.{field_name} not populated by _row_to_edge "
                "(empty/falsy default would pass a naive 'is not None' check)"
            )
        elif default is not PydanticUndefined and default is not None:
            assert value != default, (
                f"Edge.{field_name} matches its default ({default!r}); "
                "_row_to_edge may have dropped this field"
            )
        else:
            assert value is not None, f"Edge.{field_name} not populated by _row_to_edge"


def _pg_staging_edge_row() -> dict:
    return {
        "id": str(uuid.UUID(int=0x57A6_0001)),
        "source_id": "00000001_doc_source",
        "target_id": "00000002_doc_target",
        "edge_type": EdgeType.REFERENCES.value,
        "inference_evidence": "sentinel evidence",
        "confidence_tier": 3,
        "created_at": datetime(2026, 5, 21, 10, 30, tzinfo=timezone.utc).isoformat(),
    }


def test_pg_row_to_staging_edge_populates_every_staging_edge_field():
    staging = PostgresGraphStore._row_to_staging_edge(_pg_staging_edge_row())
    for field_name, field_info in StagingEdge.model_fields.items():
        value = getattr(staging, field_name)
        annotation = field_info.annotation
        default = field_info.default
        if annotation == list[str] or annotation == (dict | None):
            assert value, (
                f"StagingEdge.{field_name} not populated by _row_to_staging_edge "
                "(empty/falsy default would pass a naive 'is not None' check)"
            )
        elif default is not PydanticUndefined and default is not None:
            assert value != default, (
                f"StagingEdge.{field_name} matches its default ({default!r}); "
                "_row_to_staging_edge may have dropped this field"
            )
        else:
            assert value is not None, (
                f"StagingEdge.{field_name} not populated by _row_to_staging_edge"
            )


def test_pg_row_to_document_populates_every_field():
    now_iso = datetime(2026, 5, 21, 10, 30, tzinfo=timezone.utc).isoformat()
    row = {
        "id": "00000001_doc_1",
        "title": "T",
        "source_type": SourceType.MARKDOWN.value,
        "source_path": "/x/1.md",
        "lifecycle_status": "active",
        "version_label": "v1",
        "project": "P",
        "tags": ["a", "b"],
        "authority_scope": "scope",
        "doc_type": "ticket",
        "source_content_hash": "sha256:" + "ab" * 32,
        "adapter_version": "1",
        "created_by": "t",
        "created_at": now_iso,
        "last_modified_by": "t",
        "updated_at": now_iso,
        "projected_at": now_iso,
        "indexed_at": now_iso,
        "source_modified_at": now_iso,
        "document_date": "2026-05-21",
        "semantic_abstract": "abs",
        "pipeline_status": PipelineStatus.PROJECTION_COMPLETE.value,
        "pipeline_error": "err",
        "tier3_metadata": {"ticket_id": "T-1"},
        "metadata_confirmed": True,
    }
    doc = PostgresGraphStore._row_to_document(row)
    assert doc.tags == ["a", "b"]
    assert doc.tier3_metadata == {"ticket_id": "T-1"}
    assert doc.metadata_confirmed is True
    for field_name in Document.model_fields:
        # version_label/pipeline_error etc. all carry non-None sentinels above.
        assert getattr(doc, field_name) is not None, (
            f"Document.{field_name} not populated by _row_to_document"
        )


def test_pg_row_to_user_populates_every_field():
    row = {
        "id": str(uuid.UUID(int=0xA11CE)),
        "display_name": "Alice",
        "user_type": UserType.HUMAN.value,
        "created_at": datetime(2026, 5, 21, tzinfo=timezone.utc).isoformat(),
    }
    user = PostgresGraphStore._row_to_user(row)
    for field_name in User.model_fields:
        assert getattr(user, field_name) is not None, (
            f"User.{field_name} not populated by _row_to_user"
        )


# ---------------------------------------------------------------------------
# C2: initialize idempotency + chain-head trigger (server)
# ---------------------------------------------------------------------------


async def _is_chain_head(store, doc_id: str) -> bool:
    async with store._pool.connection() as conn:
        cur = await conn.execute("SELECT is_chain_head FROM documents WHERE id = %s", (doc_id,))
        row = await cur.fetchone()
        return row[0]


async def test_initialize_idempotent_and_trigger_flips_chain_head(postgres_graph_store):
    """initialize() is idempotent, and the chain-head trigger flips
    is_chain_head on ANY supersedes-edge insert -- here via bare insert_edge,
    NOT the atomic method (which also flips explicitly). Anti-coincidental:
    dropping the trigger from initialize leaves is_chain_head true and this
    fails, distinguishing the trigger from the atomic-method explicit flip."""
    store = postgres_graph_store
    await store.initialize(migrate=True)  # second call: no error

    newer = _doc(1)
    older = _doc(2)
    await store.insert_document(newer)
    await store.insert_document(older)
    assert await _is_chain_head(store, older.id) is True

    # Bare supersedes edge (not supersede_atomic): only the trigger can flip.
    await store.insert_edge(_edge(newer.id, older.id, 100, EdgeType.SUPERSEDES))
    assert await _is_chain_head(store, older.id) is False
    assert await _is_chain_head(store, newer.id) is True


# ---------------------------------------------------------------------------
# C3: tier3 partial-unique index shape (server)
# ---------------------------------------------------------------------------


async def test_tier3_pg_partial_unique_index_shape(postgres_graph_store):
    store = postgres_graph_store
    assert await store.tier3_unique_index_exists("ticket", "ticket_id") is False

    await store.ensure_tier3_unique_index("ticket", "ticket_id")
    assert await store.tier3_unique_index_exists("ticket", "ticket_id") is True

    async with store._pool.connection() as conn:
        cur = await conn.execute(
            "SELECT indexdef FROM pg_indexes "
            "WHERE schemaname = current_schema() AND indexname = %s",
            ("idx_tier3_unique_ticket_ticket_id",),
        )
        indexdef = (await cur.fetchone())[0]
    assert "UNIQUE" in indexdef
    assert "WHERE" in indexdef  # partial
    assert "is_chain_head" in indexdef
    assert "->>" in indexdef and "ticket_id" in indexdef  # jsonb expression index

    await store.drop_tier3_unique_index("ticket", "ticket_id")
    assert await store.tier3_unique_index_exists("ticket", "ticket_id") is False


async def test_tier3_identifier_injection_rejected(postgres_graph_store):
    store = postgres_graph_store
    with pytest.raises(ValueError):
        await store.ensure_tier3_unique_index("ticket", "id; DROP TABLE documents;--")
    with pytest.raises(ValueError):
        await store.ensure_tier3_unique_index("ticket'; --", "ticket_id")


# ---------------------------------------------------------------------------
# C4: close barrier (CAS-ADR-036); injected pool not closed by the store
# ---------------------------------------------------------------------------


async def test_close_barrier_and_injected_pool_not_closed(postgres_graph_store):
    store = postgres_graph_store
    pool = store._pool

    await store.close()
    assert store._closed is True
    with pytest.raises(RuntimeError, match="closed"):
        await store.list_all_documents()
    await store.close()  # idempotent

    # The store does not own the injected pool; it stays usable.
    assert not pool.closed
    async with pool.connection() as conn:
        cur = await conn.execute("SELECT 1")
        assert (await cur.fetchone())[0] == 1


# ---------------------------------------------------------------------------
# C5: concurrent multi-writer access SQLite could not support
# ---------------------------------------------------------------------------

_N = 8


async def test_c5a_concurrent_distinct_document_inserts(postgres_graph_store):
    """N coroutines insert distinct documents concurrently; all land. The
    embedded SQLite store serializes writers behind one thread."""
    store = postgres_graph_store
    await asyncio.gather(*[store.insert_document(_doc(i)) for i in range(_N)])
    assert await store.get_total_document_count() == _N


async def test_c5b_concurrent_distinct_edge_inserts(postgres_graph_store):
    store = postgres_graph_store
    docs = [_doc(i) for i in range(_N + 1)]
    for d in docs:
        await store.insert_document(d)
    # Each edge has a distinct natural key (distinct target).
    results = await asyncio.gather(
        *[store.insert_edge(_edge(docs[0].id, docs[i].id, i)) for i in range(1, _N + 1)]
    )
    assert all(created for _edge_obj, created in results)
    assert await store.get_total_edge_count() == _N


async def test_c5c_concurrent_same_edge_one_winner(postgres_graph_store):
    """N coroutines race the SAME natural-key edge: exactly one is created,
    the rest raise NaturalKeyConflict. The unique index -- not Python-side
    bookkeeping -- enforces it under concurrency."""
    store = postgres_graph_store
    a, b = _doc(1), _doc(2)
    await store.insert_document(a)
    await store.insert_document(b)
    results = await asyncio.gather(
        *[store.insert_edge(_edge(a.id, b.id, 1000 + i)) for i in range(_N)],
        return_exceptions=True,
    )
    created = [r for r in results if not isinstance(r, Exception) and r[1] is True]
    conflicts = [r for r in results if isinstance(r, NaturalKeyConflict)]
    assert len(created) == 1, results
    assert len(conflicts) == _N - 1, results
    assert await store.get_total_edge_count() == 1


async def test_c5d_concurrent_same_tier3_value_one_winner(postgres_graph_store):
    """After the tier3 unique index is active, N coroutines race documents
    carrying the same ticket_id: exactly one commits, the rest raise
    Tier3UniqueViolation. Atomic-at-ingest under concurrency -- the headline
    guarantee. Anti-coincidental: drop the index and all N commit."""
    store = postgres_graph_store
    await store.ensure_tier3_unique_index("ticket", "ticket_id")
    docs = [_doc(i, tier3={"ticket_id": "T-DUP"}) for i in range(_N)]
    results = await asyncio.gather(
        *[store.insert_document(d) for d in docs], return_exceptions=True
    )
    committed = [r for r in results if r is None]
    violations = [r for r in results if isinstance(r, Tier3UniqueViolation)]
    assert len(committed) == 1, results
    assert len(violations) == _N - 1, results
    assert await store.get_total_document_count() == 1


# ---------------------------------------------------------------------------
# C6: measured_byte_size -- live graph-store size (server)
# ---------------------------------------------------------------------------


async def test_clear_pipeline_error_for_statuses_only_touches_recovered_documents(
    postgres_graph_store,
):
    """The backfill nulls stale errors and leaves live failures alone.

    Four fixture rows separate the ways the predicate can be wrong. The
    ``failed`` row catches a WHERE clause too broad to distinguish a recovered
    document from one still in failure. The row already carrying a null
    catches a rowcount inflated by no-op updates -- it would push the return
    to 3. And the second call catches a dropped ``pipeline_error IS NOT NULL``
    guard, which would keep re-reporting rows it did not change.
    """
    store = postgres_graph_store
    recovered_complete = _doc(9001).model_copy(
        update={
            "pipeline_status": PipelineStatus.ABSTRACTION_COMPLETE,
            "pipeline_error": "abstraction failed after 3 attempts; last error: stale",
        }
    )
    recovered_skipped = _doc(9002).model_copy(
        update={
            "pipeline_status": PipelineStatus.ABSTRACTION_SKIPPED,
            "pipeline_error": "abstraction failed after 3 attempts; last error: stale",
        }
    )
    still_failed = _doc(9003).model_copy(
        update={
            "pipeline_status": PipelineStatus.FAILED,
            "pipeline_error": "a failure that is still true",
        }
    )
    already_clean = _doc(9004).model_copy(
        update={"pipeline_status": PipelineStatus.ABSTRACTION_COMPLETE, "pipeline_error": None}
    )
    for doc in (recovered_complete, recovered_skipped, still_failed, already_clean):
        await store.insert_document(doc)

    statuses = [
        PipelineStatus.ABSTRACTION_COMPLETE.value,
        PipelineStatus.ABSTRACTION_SKIPPED.value,
    ]
    cleared = await store.clear_pipeline_error_for_statuses(statuses)

    assert cleared == 2
    assert (await store.get_document(recovered_complete.id)).pipeline_error is None
    assert (await store.get_document(recovered_skipped.id)).pipeline_error is None
    assert (
        await store.get_document(still_failed.id)
    ).pipeline_error == "a failure that is still true"
    assert (await store.get_document(already_clean.id)).pipeline_error is None

    assert await store.clear_pipeline_error_for_statuses(statuses) == 0


async def test_clear_pipeline_error_for_statuses_no_statuses_is_a_noop(postgres_graph_store):
    """An empty status list clears nothing rather than every row.

    Trap: ``pipeline_status = ANY('{}')`` matches nothing in Postgres, but an
    implementation that built the predicate differently could degrade to an
    unconditional UPDATE. The seeded row proves it did not.
    """
    store = postgres_graph_store
    doc = _doc(9005).model_copy(
        update={
            "pipeline_status": PipelineStatus.ABSTRACTION_COMPLETE,
            "pipeline_error": "stale",
        }
    )
    await store.insert_document(doc)

    assert await store.clear_pipeline_error_for_statuses([]) == 0
    assert (await store.get_document(doc.id)).pipeline_error == "stale"


async def test_measured_byte_size_grows_with_ingest(postgres_graph_store):
    """The live relation-size stat grows as documents/edges are inserted."""
    store = postgres_graph_store
    base = await store.measured_byte_size()

    docs = [_doc(i) for i in range(50)]
    for doc in docs:
        await store.insert_document(doc)
    for n, (a, b) in enumerate(zip(docs, docs[1:])):
        await store.insert_edge(_edge(a.id, b.id, n))

    assert await store.measured_byte_size() > base


async def test_measured_byte_size_zero_when_tables_absent(pg_dsn):
    """measured_byte_size returns 0 (not raise) against a schema with no graph tables.

    to_regclass resolves each table name against the connection's search_path;
    none exist in the bare schema, so every relation-size lookup is NULL and
    COALESCE reports 0. Mirrors a freshly created, not-yet-bootstrapped vault --
    the same trap covered for the content store's chunks table in
    ``test_content_store_postgres.py::test_count_methods_zero_when_table_absent``.
    """
    import os

    import psycopg

    from sage.storage.postgres.pool import pool_from_conninfo

    schema = "sage_test_empty_" + os.urandom(4).hex()
    async with await psycopg.AsyncConnection.connect(pg_dsn, autocommit=True) as conn:
        await conn.execute(f'CREATE SCHEMA "{schema}"')
    try:
        pool = pool_from_conninfo(pg_dsn, search_path=f"{schema},public")
        await pool.open()
        try:
            store = PostgresGraphStore(pool)
            assert await store.measured_byte_size() == 0
        finally:
            await pool.close()
    finally:
        async with await psycopg.AsyncConnection.connect(pg_dsn, autocommit=True) as conn:
            await conn.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')


async def test_storage_present_sees_through_search_path_fallback(pg_dsn):
    """storage_present probes the schema catalog, not search_path resolution.

    Two disposable schemas share one pool's search_path. After the primary is
    dropped out of band, the store's unqualified queries still *succeed* -- they
    fall through to the second schema's same-named tables (the positive control:
    this masking is exactly why an error-triggered registry reconcile never
    fires) -- yet storage_present reports the primary absent. A probe built on
    name resolution (``to_regclass('documents')``) would be masked the same way
    the queries are; the ``information_schema`` probe cannot be.
    """
    import os

    import psycopg

    from sage.storage.postgres.pool import pool_from_conninfo
    from sage.storage.postgres.schema import (
        assert_disposable_target,
        bootstrap_schema,
        drop_schema,
    )

    primary = "sage_test_primary_" + os.urandom(4).hex()
    decoy = "sage_test_decoy_" + os.urandom(4).hex()
    async with await psycopg.AsyncConnection.connect(pg_dsn, autocommit=True) as conn:
        await bootstrap_schema(conn, schema=primary, extensions=["vector", "pgstattuple"])
        await bootstrap_schema(conn, schema=decoy, extensions=["vector", "pgstattuple"])
    try:
        pool = pool_from_conninfo(pg_dsn, search_path=f"{primary},{decoy},public")
        await pool.open()
        try:
            store = PostgresGraphStore(pool)
            assert await store.storage_present(primary) is True
            assert await store.storage_present(decoy) is True

            async with await psycopg.AsyncConnection.connect(pg_dsn, autocommit=True) as conn:
                await drop_schema(conn, assert_disposable_target(primary))

            # Positive control for the masking mechanism: the unqualified count
            # query still succeeds against the fallback schema's table, so no
            # error would ever surface the drop.
            assert await store.get_total_document_count() == 0
            assert await store.storage_present(primary) is False
            assert await store.storage_present(decoy) is True
        finally:
            await pool.close()
    finally:
        async with await psycopg.AsyncConnection.connect(pg_dsn, autocommit=True) as conn:
            for schema in (primary, decoy):
                await conn.execute(
                    f'DROP SCHEMA IF EXISTS "{assert_disposable_target(schema)}" CASCADE'
                )
