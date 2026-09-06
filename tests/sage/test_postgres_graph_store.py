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
from datetime import datetime, timedelta, timezone

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


def _doc(
    i: int,
    *,
    tier3: dict | None = None,
    doc_type: str = "ticket",
    title: str | None = None,
    source_path: str | None = None,
    tags: list[str] | None = None,
    lifecycle_status: str = "active",
    document_date: str | None = None,
    semantic_abstract: str | None = None,
    source_modified_at: datetime | None = None,
    authority_scope: str | None = None,
) -> Document:
    now = datetime.now(timezone.utc)
    return Document(
        id=f"{i:08x}_doc_{i}",
        title=f"Doc {i}" if title is None else title,
        source_type=SourceType.MARKDOWN,
        source_path=f"/x/{i}.md" if source_path is None else source_path,
        lifecycle_status=lifecycle_status,
        source_content_hash=f"sha256:{i:064x}",
        adapter_version="1",
        created_by="t",
        created_at=now,
        last_modified_by="t",
        updated_at=now,
        pipeline_status=PipelineStatus.PROJECTION_COMPLETE,
        tags=["a", "b"] if tags is None else tags,
        doc_type=doc_type,
        tier3_metadata=tier3,
        document_date=document_date,
        semantic_abstract=semantic_abstract,
        source_modified_at=source_modified_at,
        authority_scope=authority_scope,
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
        "stored_content_hash": "sha256:" + "cd" * 32,
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
    assert doc.source_content_hash != doc.stored_content_hash, (
        "the two hashes are distinct columns; the fixture must not let one stand in for the other"
    )


def test_pg_row_to_document_tolerates_a_row_without_the_stored_hash_column():
    """A row selected before the additive column reached a vault's schema maps to
    a null ``stored_content_hash`` rather than raising.

    Anti-coincidental-pass: the key is absent from the row entirely (not present
    and null), which is what a ``SELECT *`` against a not-yet-migrated schema
    returns. A ``row["stored_content_hash"]`` lookup raises KeyError here, so
    this fails loudly if the read path stops tolerating the pre-migration shape.
    """
    now_iso = datetime(2026, 5, 21, 10, 30, tzinfo=timezone.utc).isoformat()
    row = {
        "id": "00000001_doc_1",
        "title": "T",
        "source_type": SourceType.MARKDOWN.value,
        "source_path": "/x/1.md",
        "lifecycle_status": "active",
        "version_label": None,
        "project": None,
        "tags": None,
        "authority_scope": None,
        "doc_type": None,
        "source_content_hash": "sha256:" + "ab" * 32,
        "adapter_version": "1",
        "created_by": "t",
        "created_at": now_iso,
        "last_modified_by": "t",
        "updated_at": now_iso,
        "projected_at": None,
        "indexed_at": None,
        "source_modified_at": None,
        "document_date": None,
        "semantic_abstract": None,
        "pipeline_status": PipelineStatus.PROJECTION_COMPLETE.value,
        "pipeline_error": None,
        "tier3_metadata": None,
        "metadata_confirmed": False,
    }
    assert "stored_content_hash" not in row

    doc = PostgresGraphStore._row_to_document(row)

    assert doc.stored_content_hash is None
    assert doc.source_content_hash == "sha256:" + "ab" * 32


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


# ---------------------------------------------------------------------------
# C7: search_metadata admits by authored metadata only (server)
#
# CAS-ADR-049 Decision 4: derived text ranks and orients, and never satisfies a
# match -- and the rule "binds every path into a caller's result set, not the
# content store's matching alone". search_metadata feeds the service-layer
# boost, which is such a path, so its admission set is subject to the same
# provenance line the retrieval surfaces are held to: the title and the tags
# are authored and may admit; the source path is incidental to how a document
# arrived and may only rank.
# ---------------------------------------------------------------------------


async def test_title_admits_a_document(postgres_graph_store):
    """A term carried only by the title admits the document."""
    doc = _doc(701, title="Zetaword Catalog", source_path="/x/unrelated.md", tags=["alpha"])
    await postgres_graph_store.insert_document(doc)

    found = await postgres_graph_store.search_metadata("zetaword")

    assert [d.id for d in found] == [doc.id]


async def test_tags_admit_a_document(postgres_graph_store):
    """A term carried only by a tag admits the document."""
    doc = _doc(702, title="Unrelated Title", source_path="/x/unrelated.md", tags=["zetaword"])
    await postgres_graph_store.insert_document(doc)

    found = await postgres_graph_store.search_metadata("zetaword")

    assert [d.id for d in found] == [doc.id]


async def test_source_path_alone_does_not_admit_a_document(postgres_graph_store):
    """A filename is derived text and cannot admit a document on its own.

    The defect this pins: a query naming nothing but a filename stem returned
    the document, so a filename satisfied a match. The positive control in the
    same call is what makes the refusal meaningful -- without it the assertion
    would pass just as well against a ``search_metadata`` that had stopped
    returning anything at all.
    """
    derived_only = _doc(
        703,
        title="Unrelated Title",
        source_path="/imports/zetaword-quarterly-review.md",
        tags=["alpha"],
    )
    authored = _doc(704, title="Zetaword Digest", source_path="/x/plain.md", tags=["alpha"])
    await postgres_graph_store.insert_document(derived_only)
    await postgres_graph_store.insert_document(authored)

    found = await postgres_graph_store.search_metadata("zetaword")
    found_ids = [d.id for d in found]

    assert derived_only.id not in found_ids, (
        "a term carried only by the source path admitted the document"
    )
    assert found_ids == [authored.id], (
        "positive control: the same term in an authored field still admits"
    )


async def test_source_path_still_orders_among_admitted_documents(postgres_graph_store):
    """Derived text ranks what it may no longer admit.

    Both documents are admitted by an authored field; only one also carries the
    term in its source path. That document ranks first. This is the half of the
    old behaviour the decision keeps -- the source path contributes to ranking
    -- expressed over a set it can no longer widen.
    """
    # Inserted in the reverse of the asserted order, deliberately. Both share a
    # title, so the primary key of the ordering cannot separate them and only
    # the source-path key can. Were they inserted in the asserted order, a
    # query that had lost that key would return them in insertion order and
    # pass anyway.
    tag_only = _doc(705, title="Unrelated Title", source_path="/x/plain.md", tags=["zetaword"])
    tag_and_path = _doc(
        706,
        title="Unrelated Title",
        source_path="/imports/zetaword-review.md",
        tags=["zetaword"],
    )
    await postgres_graph_store.insert_document(tag_only)
    await postgres_graph_store.insert_document(tag_and_path)

    found = await postgres_graph_store.search_metadata("zetaword")
    found_ids = [d.id for d in found]

    assert set(found_ids) == {tag_only.id, tag_and_path.id}, (
        "both documents are admitted by their tag"
    )
    assert found_ids[0] == tag_and_path.id, (
        "the source-path match orders first among documents already admitted"
    )


async def test_a_wildcard_in_the_query_matches_literally(postgres_graph_store):
    """A query is text to find, not a pattern to apply.

    The admission test is a substring containment expressed as ``ILIKE``, so a
    caller's ``%`` or ``_`` would otherwise be read as the pattern language's
    own wildcards -- ``%`` matching any run of characters and ``_`` any single
    one. A caller searching for a title that contains a percent sign gets every
    document instead.
    """
    literal = _doc(707, title="Coverage 80% Report", source_path="/x/a.md", tags=["alpha"])
    # The decoy is what an unescaped '%' would additionally match: read as a
    # wildcard the query becomes "80", anything, " Report". A decoy sharing no
    # words would be excluded either way and prove nothing, since a wildcard
    # only ever widens the pattern.
    decoy = _doc(708, title="Coverage 80 Quarterly Report", source_path="/x/b.md", tags=["alpha"])
    await postgres_graph_store.insert_document(literal)
    await postgres_graph_store.insert_document(decoy)

    found = await postgres_graph_store.search_metadata("80% Report")

    assert [d.id for d in found] == [literal.id], (
        "the query's '%' acted as a wildcard instead of matching literally"
    )


async def test_an_underscore_in_the_query_matches_literally(postgres_graph_store):
    """The single-character wildcard is escaped too.

    ``_`` is the easier one to miss, because it is a legitimate character in
    the identifiers and filename stems callers actually search for, so an
    unescaped one returns a superset rather than an obvious error.
    """
    literal = _doc(709, title="Report_v2", source_path="/x/c.md", tags=["alpha"])
    decoy = _doc(710, title="ReportXv2", source_path="/x/d.md", tags=["alpha"])
    await postgres_graph_store.insert_document(literal)
    await postgres_graph_store.insert_document(decoy)

    found = await postgres_graph_store.search_metadata("Report_v2")

    assert [d.id for d in found] == [literal.id], (
        "the query's '_' matched any single character instead of an underscore"
    )


async def test_a_wildcard_in_an_abstract_query_matches_literally(postgres_graph_store):
    """The abstract lookup escapes its pattern for the same reason.

    It feeds the abstract boost, which is another path into a caller's result
    set, and it splices the query into the same operator. A sibling of a fixed
    site with the identical shape is where a sweep stops one method short.
    """
    literal = _doc(711, title="Alpha", source_path="/x/e.md", tags=["alpha"])
    literal.semantic_abstract = "Coverage reached 80% Report thresholds."
    decoy = _doc(712, title="Beta", source_path="/x/f.md", tags=["alpha"])
    decoy.semantic_abstract = "Coverage reached 80 Quarterly Report thresholds."
    await postgres_graph_store.insert_document(literal)
    await postgres_graph_store.insert_document(decoy)

    found = await postgres_graph_store.search_abstracts("80% Report")

    assert [d.id for d in found] == [literal.id], (
        "the query's '%' acted as a wildcard against the abstract"
    )


# ---------------------------------------------------------------------------
# C8: the boost helpers' truncation is ranked, not arbitrary (server)
#
# Both helpers feed a service-layer relevance boost and both cut at ``limit``.
# The predicate either side of that cut is a bare ILIKE containment, which
# carries no notion of a better match, so the survivors are ordered by the
# salience the retrieval layer already applies a few steps later: active
# first (BH-069), then document_date descending with nulls last (BH-070),
# then the primary key to make the order total. search_metadata keeps its own
# match-quality keys -- title match, then source-path match -- ahead of that
# block, so a better match still outranks a more salient one.
#
# The shared tests are parametrized across both helpers deliberately. The rule
# has to hold for both, and a fix applied to one alone reds the other arm here
# rather than passing review on a promise.
# ---------------------------------------------------------------------------

_BOOST_TERM = "omicronword"


def _metadata_doc(i: int, **kwargs) -> Document:
    """A document ``search_metadata`` admits by its tag alone.

    The title is shared and the source path carries nothing of the term, so
    every document built here lands in the same match-quality bucket and only
    the salience order can separate them.
    """
    kwargs.setdefault("title", "Shared Boost Title")
    kwargs.setdefault("tags", [_BOOST_TERM])
    return _doc(i, **kwargs)


def _abstract_doc(i: int, **kwargs) -> Document:
    """A document ``search_abstracts`` admits by its abstract alone."""
    kwargs.setdefault("title", "Shared Boost Title")
    kwargs.setdefault("tags", ["a"])
    kwargs.setdefault("semantic_abstract", f"Concerning {_BOOST_TERM} matters.")
    return _doc(i, **kwargs)


_BOOST_HELPERS = [
    pytest.param("search_metadata", _metadata_doc, id="metadata"),
    pytest.param("search_abstracts", _abstract_doc, id="abstracts"),
]

# The tags each helper's factory relies on to be admitted. A test that stamps
# its own tags has to carry these forward or the document stops matching, which
# would make the test pass by matching nothing.
_ADMITTING_TAGS = {"search_metadata": [_BOOST_TERM], "search_abstracts": ["a"]}


@pytest.mark.parametrize("helper_name, make_doc", _BOOST_HELPERS)
async def test_truncation_keeps_the_active_matches(postgres_graph_store, helper_name, make_doc):
    """The cut takes the documents that would have ranked highest anyway.

    150 documents match through the helper's own admitting field and are
    otherwise indistinguishable, so nothing but the salience order separates
    them. Every third is superseded, which interleaves the two lifecycles
    across the id space: the 100 active documents are deliberately *not* the
    100 lowest ids. That is what makes this test pin the rule adopted rather
    than reproducibility alone -- appending only the primary key returns a
    stable slice that still cuts active documents in favour of superseded
    ones, and reds here.
    """
    expected_active: list[str] = []
    for i in range(800, 950):
        superseded = (i - 800) % 3 == 0
        doc = make_doc(i, lifecycle_status="superseded" if superseded else "active")
        await postgres_graph_store.insert_document(doc)
        if not superseded:
            expected_active.append(doc.id)
    assert len(expected_active) == 100, "fixture: exactly 100 of the 150 documents are active"

    found = await getattr(postgres_graph_store, helper_name)(_BOOST_TERM, limit=100)

    assert [d.id for d in found] == expected_active, (
        "the truncation admitted superseded documents while active matches were cut"
    )


@pytest.mark.parametrize("helper_name, make_doc", _BOOST_HELPERS)
async def test_truncation_is_total_among_equals(postgres_graph_store, helper_name, make_doc):
    """Equal matches are cut on the primary key, so the slice is the same one twice.

    Comparing the two calls to each other would pass against the unordered
    truncation as readily as against the fix: one plan, one heap order, no
    concurrent writer. The assertion carrying this test is the second one --
    the survivors are the 100 lowest ids. The documents are inserted in
    *descending* id order so heap order and id order disagree, and the
    unordered truncation returns the 100 highest instead.
    """
    all_ids = sorted(make_doc(i).id for i in range(800, 950))
    for i in reversed(range(800, 950)):
        await postgres_graph_store.insert_document(make_doc(i))

    first = await getattr(postgres_graph_store, helper_name)(_BOOST_TERM, limit=100)
    second = await getattr(postgres_graph_store, helper_name)(_BOOST_TERM, limit=100)

    assert [d.id for d in first] == [d.id for d in second], (
        "two identical calls over an unchanged corpus returned different slices"
    )
    assert [d.id for d in first] == all_ids[:100], (
        "the surviving 100 were heap order rather than the 100 lowest ids"
    )


@pytest.mark.parametrize("helper_name, make_doc", _BOOST_HELPERS)
async def test_recency_orders_the_truncation(postgres_graph_store, helper_name, make_doc):
    """Among equally-matching active documents the recent survive; the undated sort last.

    Inserted oldest-id first with the newest document last, so neither
    insertion order nor ``id ASC`` yields the expected pair. A fix that
    stopped at the primary key keeps the undated document and cuts the 2026
    one, and reds here.
    """
    undated = make_doc(960, document_date=None)
    older = make_doc(961, document_date="2025-01-15")
    newer = make_doc(962, document_date="2026-01-15")
    for doc in (undated, older, newer):
        await postgres_graph_store.insert_document(doc)

    found = await getattr(postgres_graph_store, helper_name)(_BOOST_TERM, limit=2)

    assert [d.id for d in found] == [newer.id, older.id], (
        "the cut ignored document_date: an undated document survived a dated one"
    )


@pytest.mark.parametrize("helper_name, make_doc", _BOOST_HELPERS)
async def test_an_undated_document_is_cut_on_when_it_was_modified(
    postgres_graph_store, helper_name, make_doc
):
    """A document with no authored date is ranked on the date it was modified.

    The cut exists to mirror the reranking that runs after it, and that
    reranking resolves a document's date as ``document_date`` falling back to
    ``source_modified_at`` -- so a recently ingested document carrying no
    authored date is one it boosts. Ranking such a document last here would cut
    it before it ever reached that boost, which is the shape of the very defect
    the ordering was added to fix.

    The sibling above cannot see this: every document it builds leaves
    ``source_modified_at`` unset, so the fallback has nothing to reach for and
    an undated document sorts last under either rule. Here the undated document
    is the *more* recently modified one and the dated one is six years stale,
    so ranking undated-last returns exactly the wrong document.
    """
    stale_but_dated = make_doc(990, document_date="2020-01-01")
    undated_but_fresh = make_doc(
        991,
        document_date=None,
        source_modified_at=datetime(2026, 9, 1, tzinfo=timezone.utc),
    )
    await postgres_graph_store.insert_document(stale_but_dated)
    await postgres_graph_store.insert_document(undated_but_fresh)

    found = await getattr(postgres_graph_store, helper_name)(_BOOST_TERM, limit=1)

    assert [d.id for d in found] == [undated_but_fresh.id], (
        "an undated document was cut on its missing authored date rather than "
        "on when it was modified, so the cut dropped a document the rerank boosts"
    )


async def test_metadata_match_quality_outranks_salience(postgres_graph_store):
    """The salience block sits behind the match-quality keys, never in front of them.

    A superseded *title* match against an active *tag-only* match. The title
    match is the better match and ranks first, though the other document is
    the more salient one. This is the inverted-fix trap: prepending the
    lifecycle key rather than appending it satisfies every other test in this
    section while silently demoting every title match below every tag match.
    """
    title_match = _doc(
        970,
        title=f"{_BOOST_TERM} Digest",
        tags=["a"],
        lifecycle_status="superseded",
    )
    tag_match = _doc(971, title="Unrelated Title", tags=[_BOOST_TERM])
    await postgres_graph_store.insert_document(title_match)
    await postgres_graph_store.insert_document(tag_match)

    found = await postgres_graph_store.search_metadata(_BOOST_TERM)

    assert [d.id for d in found] == [title_match.id, tag_match.id], (
        "the salience keys were placed ahead of the match-quality keys"
    )


async def test_source_path_quality_outranks_salience(postgres_graph_store):
    """The salience block sits behind *both* match-quality keys, not between them.

    The sibling above pins only the boundary above the title key. A salience
    block inserted between the title key and the source-path key passes it --
    neither document there carries a matching source path, so the misplaced
    keys are never exercised -- and passes
    ``test_source_path_still_orders_among_admitted_documents`` too, whose two
    documents are both active and both undated, leaving the salience keys tied
    and the source-path key still deciding. Two of the ordering's three
    boundaries were pinned; this is the third.

    A superseded document matching by tag *and* source path against an active
    one matching by tag alone. The source-path match is the better match and
    ranks first.
    """
    path_match = _doc(
        980,
        title="Unrelated Title",
        source_path=f"/imports/{_BOOST_TERM}-review.md",
        tags=[_BOOST_TERM],
        lifecycle_status="superseded",
    )
    salient = _doc(981, title="Unrelated Title", source_path="/x/plain.md", tags=[_BOOST_TERM])
    await postgres_graph_store.insert_document(path_match)
    await postgres_graph_store.insert_document(salient)

    found = await postgres_graph_store.search_metadata(_BOOST_TERM)

    assert [d.id for d in found] == [path_match.id, salient.id], (
        "the salience keys were placed between the two match-quality keys"
    )


# ---------------------------------------------------------------------------
# C9: the boost helpers rank their cut over the documents the filter admits
#
# C8 above fixed *which* of the matching documents survive the cut. This
# section fixes *what the cut ranks over*. Both helpers cap their match set,
# and their caller applies its own constraints to what comes back -- so a
# caller asking for a lifecycle the ranking sorts last received a capful of
# documents its filter then discarded, and no boost at all. The filters reach
# the WHERE clause instead, and the cut is drawn from the eligible documents.
#
# Parametrized across both helpers for the reason C8 is: the shape is shared
# and a repair to one alone reds the other arm here.
# ---------------------------------------------------------------------------


def _dated(n: int) -> str:
    """The nth day of an ascending run of distinct authored dates."""
    return (datetime(2020, 1, 1, tzinfo=timezone.utc) + timedelta(days=n)).date().isoformat()


@pytest.mark.parametrize("helper_name, make_doc", _BOOST_HELPERS)
async def test_the_cut_is_drawn_from_the_documents_the_filter_admits(
    postgres_graph_store, helper_name, make_doc
):
    """A caller filtering to a non-active lifecycle receives the documents it asked for.

    150 documents match through the helper's admitting field; every fifth is
    superseded, which interleaves the two lifecycles across the id space so
    the 30 superseded documents are not a contiguous block the cut could
    reach by accident. The unfiltered control below asserts what makes this
    test discriminate: the cut ranks active documents first, so an unfiltered
    hundred holds no superseded document at all, and a filter applied to that
    hundred can only return nothing.
    """
    helper = getattr(postgres_graph_store, helper_name)
    superseded_ids: list[str] = []
    for i in range(1000, 1150):
        is_superseded = (i - 1000) % 5 == 0
        doc = make_doc(i, lifecycle_status="superseded" if is_superseded else "active")
        await postgres_graph_store.insert_document(doc)
        if is_superseded:
            superseded_ids.append(doc.id)
    assert len(superseded_ids) == 30, "fixture: 30 of the 150 matching documents are superseded"

    # The trap, asserted rather than assumed: the cap starves this caller only
    # because the unfiltered cut is entirely active. On a corpus small enough
    # to fit inside the cap, or one the ranking did not sort this way, the
    # assertion below would pass against filter-after-cut too.
    unfiltered = await helper(_BOOST_TERM, limit=100)
    assert len(unfiltered) == 100, "fixture: the match set must exceed the cap"
    assert not set(d.id for d in unfiltered) & set(superseded_ids), (
        "fixture: the unfiltered cut must hold no superseded document, or a "
        "filter applied after the cut would have had something to return"
    )

    found = await helper(_BOOST_TERM, filters={"lifecycle_status": "superseded"}, limit=100)

    assert [d.id for d in found] == superseded_ids, (
        "the cut was ranked over the whole match set, so the filter had only "
        "active documents to discard"
    )


@pytest.mark.parametrize("helper_name, make_doc", _BOOST_HELPERS)
async def test_a_minority_doc_type_survives_the_cut(postgres_graph_store, helper_name, make_doc):
    """The same holds on ``doc_type``: a minority type is not crowded out of the cap.

    All 150 documents are active, so the lifecycle key cannot separate them
    and the starvation here is purely one of capacity -- 130 documents of one
    type fill a cap of 100 before the 20 of the type asked for are reached.
    """
    helper = getattr(postgres_graph_store, helper_name)
    wanted_ids: list[str] = []
    for i in range(1200, 1350):
        is_wanted = (i - 1200) >= 130
        doc = make_doc(i, doc_type="adr" if is_wanted else "note")
        await postgres_graph_store.insert_document(doc)
        if is_wanted:
            wanted_ids.append(doc.id)
    assert len(wanted_ids) == 20, "fixture: 20 of the 150 matching documents are adrs"

    unfiltered = await helper(_BOOST_TERM, limit=100)
    assert not set(d.id for d in unfiltered) & set(wanted_ids), (
        "fixture: the 130 notes must fill the cap ahead of every adr"
    )

    found = await helper(_BOOST_TERM, filters={"doc_type": "adr"}, limit=100)

    assert [d.id for d in found] == wanted_ids, (
        "the cut was ranked over every matching type rather than the one asked for"
    )


@pytest.mark.parametrize("helper_name, make_doc", _BOOST_HELPERS)
async def test_the_filtered_cut_keeps_the_salience_order(
    postgres_graph_store, helper_name, make_doc
):
    """Pushing the filters down must not displace the ranking C8 established.

    The obvious wrong fix routes the whole statement through the shared
    document query, which orders by the *browsing* order rather than the boost
    cut's. Here 150 admitted documents carry ascending dates against ascending
    ids, so date order and id order disagree: the expected survivors are the
    100 most recent, which are the 100 *highest* ids, and any fix that lost
    the date keys returns the 100 lowest instead.
    """
    helper = getattr(postgres_graph_store, helper_name)
    ids_by_date: list[str] = []
    for n, i in enumerate(range(1400, 1550)):
        doc = make_doc(i, lifecycle_status="superseded", document_date=_dated(n))
        await postgres_graph_store.insert_document(doc)
        ids_by_date.append(doc.id)
    expected = list(reversed(ids_by_date))[:100]

    found = await helper(_BOOST_TERM, filters={"lifecycle_status": "superseded"}, limit=100)

    assert [d.id for d in found] == expected, (
        "the filtered cut lost the boost ordering: the survivors were not the "
        "100 most recently dated documents"
    )


@pytest.mark.parametrize("helper_name, make_doc", _BOOST_HELPERS)
async def test_the_default_failed_exclusion_survives_the_filter_pushdown(
    postgres_graph_store, helper_name, make_doc
):
    """Failed documents are excluded by default and admitted on an explicit filter.

    Routing the filters through the shared WHERE builder brings its
    failed-pipeline default with them, and both arms are asserted because
    either one alone is satisfied by a constant. A pushdown hardcoding the
    exclusion off passes the second arm; one hardcoding it on passes the first.
    """
    helper = getattr(postgres_graph_store, helper_name)
    healthy = make_doc(1600)
    failed = make_doc(1601)
    failed.pipeline_status = PipelineStatus.FAILED
    await postgres_graph_store.insert_document(healthy)
    await postgres_graph_store.insert_document(failed)

    by_default = await helper(_BOOST_TERM)
    assert [d.id for d in by_default] == [healthy.id], (
        "a failed document reached the boost with no filter asking for it"
    )

    on_request = await helper(_BOOST_TERM, filters={"pipeline_status": "failed"})
    assert [d.id for d in on_request] == [failed.id], (
        "an explicit pipeline_status filter did not override the default exclusion"
    )


@pytest.mark.parametrize("helper_name, make_doc", _BOOST_HELPERS)
async def test_a_tag_filter_admits_only_documents_carrying_every_tag(
    postgres_graph_store, helper_name, make_doc
):
    """The tag branch of the shared builder is reached through the new parameter.

    Tags resolve through a correlated EXISTS per tag rather than a column
    predicate, so a pushdown that handled only the scalar columns passes every
    test above and drops this one. The filter names two tags and the fixture
    gives one of them to every document, so a builder that treated the list as
    a disjunction returns all 150.
    """
    helper = getattr(postgres_graph_store, helper_name)
    shared_tag = "sharedboosttag"
    wanted_ids: list[str] = []
    for i in range(1700, 1850):
        is_wanted = (i - 1700) >= 135
        tags = [shared_tag, "narrowing"] if is_wanted else [shared_tag]
        doc = make_doc(i, tags=[*_ADMITTING_TAGS[helper_name], *tags])
        await postgres_graph_store.insert_document(doc)
        if is_wanted:
            wanted_ids.append(doc.id)
    assert len(wanted_ids) == 15, "fixture: 15 of the 150 documents carry both tags"

    found = await helper(_BOOST_TERM, filters={"tags": [shared_tag, "narrowing"]}, limit=100)

    assert [d.id for d in found] == wanted_ids, (
        "the tag filter did not narrow to documents carrying every named tag"
    )


@pytest.mark.parametrize("helper_name, make_doc", _BOOST_HELPERS)
async def test_a_tier3_filter_reaches_the_cut(postgres_graph_store, helper_name, make_doc):
    """A tier3 filter narrows the cut like any other.

    This is the one filter the caller's own post-cut gate never applied, so
    before the pushdown a boosted document could reach a caller whose tier3
    filter excluded it. The fixture is over-cap so the test fails on the
    filter rather than on the cap.
    """
    helper = getattr(postgres_graph_store, helper_name)
    wanted_ids: list[str] = []
    for i in range(1900, 2050):
        is_wanted = (i - 1900) >= 140
        doc = make_doc(
            i,
            tier3={"ticket_id": "T-WANTED"} if is_wanted else {"ticket_id": "T-OTHER"},
        )
        await postgres_graph_store.insert_document(doc)
        if is_wanted:
            wanted_ids.append(doc.id)
    assert len(wanted_ids) == 10, "fixture: 10 of the 150 documents carry the wanted value"

    found = await helper(
        _BOOST_TERM, filters={"tier3_metadata": {"ticket_id": "T-WANTED"}}, limit=100
    )

    assert [d.id for d in found] == wanted_ids, "the tier3 filter did not reach the WHERE clause"


@pytest.mark.parametrize("helper_name, make_doc", _BOOST_HELPERS)
async def test_an_unfiltered_cut_is_unchanged(postgres_graph_store, helper_name, make_doc):
    """No filters means the behaviour C8 pinned, unchanged.

    The control for this section rather than a discriminator: it passes
    against a ``filters`` parameter that is accepted and ignored, and it is
    here so that a regression in the unfiltered path -- the one every ordinary
    caller takes -- is attributed to this change rather than hunted for.
    """
    helper = getattr(postgres_graph_store, helper_name)
    expected_active: list[str] = []
    for i in range(2100, 2250):
        superseded = (i - 2100) % 3 == 0
        doc = make_doc(i, lifecycle_status="superseded" if superseded else "active")
        await postgres_graph_store.insert_document(doc)
        if not superseded:
            expected_active.append(doc.id)
    assert len(expected_active) == 100, "fixture: exactly 100 of the 150 documents are active"

    found = await helper(_BOOST_TERM, filters=None, limit=100)

    assert [d.id for d in found] == expected_active, (
        "an unfiltered call no longer returns the 100 documents C8 pinned"
    )


@pytest.mark.parametrize("helper_name, make_doc", _BOOST_HELPERS)
async def test_an_authority_scope_predicate_reaches_the_cut(
    postgres_graph_store, helper_name, make_doc
):
    """``has_authority_scope`` narrows the ranked set like a column filter does.

    The retrieval layer's authoritative scope is a rule about a column rather
    than a value to match, so it has no equality filter to travel on and needs
    a predicate of its own. Without it a caller asking for authoritative
    documents receives a cut ranked over every document and keeps only what
    survives its own gate -- the same starvation the filters above close.
    """
    helper = getattr(postgres_graph_store, helper_name)
    wanted_ids: list[str] = []
    for i in range(2300, 2450):
        is_wanted = (i - 2300) >= 140
        doc = make_doc(i, authority_scope="canonical" if is_wanted else None)
        await postgres_graph_store.insert_document(doc)
        if is_wanted:
            wanted_ids.append(doc.id)
    assert len(wanted_ids) == 10, "fixture: 10 of the 150 documents carry an authority scope"

    unfiltered = await helper(_BOOST_TERM, limit=100)
    assert not set(d.id for d in unfiltered) & set(wanted_ids), (
        "fixture: the 140 unscoped documents must fill the cap ahead of every scoped one"
    )

    found = await helper(_BOOST_TERM, filters={"has_authority_scope": True}, limit=100)

    assert [d.id for d in found] == wanted_ids, (
        "the authority-scope predicate did not reach the WHERE clause"
    )


@pytest.mark.parametrize("helper_name, make_doc", _BOOST_HELPERS)
async def test_an_empty_authority_scope_is_not_an_authority_scope(
    postgres_graph_store, helper_name, make_doc
):
    """The predicate tests the column's truthiness, not merely its presence.

    The service-layer gate this mirrors rejects a document on ``not
    doc.authority_scope``, which an empty string satisfies. A predicate written
    as ``IS NOT NULL`` alone passes the sibling above -- every document there is
    either unscoped or properly scoped -- and admits the empty one here, which
    is a document the caller's own gate would then discard: the cut would be
    spent on a row that cannot survive, which is the defect rather than a
    smaller version of it.
    """
    helper = getattr(postgres_graph_store, helper_name)
    scoped = make_doc(2500, authority_scope="canonical")
    blank = make_doc(2501, authority_scope="")
    await postgres_graph_store.insert_document(scoped)
    await postgres_graph_store.insert_document(blank)

    found = await helper(_BOOST_TERM, filters={"has_authority_scope": True})

    assert [d.id for d in found] == [scoped.id], (
        "a document whose authority_scope is the empty string was admitted"
    )


async def test_a_filter_binds_over_the_whole_metadata_disjunction(postgres_graph_store):
    """A title match is subject to the filter rather than exempted by it.

    ``search_metadata`` admits through a disjunction, so a filter has to be
    conjoined to the *whole* of it. Written without the parentheses --
    ``title OR tags AND filters`` -- SQL binds it as ``title OR (tags AND
    filters)`` and every title match comes back whatever the caller asked for.

    Nothing else in this section can see that. Every other fixture here admits
    its documents through the helper's own field, which for the metadata arm
    is the tag, so the left arm of the disjunction is never satisfied and the
    misparenthesised rival returns the right answer throughout. Both documents
    below match by *title* and by nothing else, which puts the whole weight of
    the result on where the filter binds.
    """
    excluded = _doc(2600, title=f"{_BOOST_TERM} Digest", tags=["a"])
    admitted = _doc(2601, title=f"{_BOOST_TERM} Review", tags=["a"], lifecycle_status="superseded")
    await postgres_graph_store.insert_document(excluded)
    await postgres_graph_store.insert_document(admitted)

    unfiltered = await postgres_graph_store.search_metadata(_BOOST_TERM)
    assert {d.id for d in unfiltered} == {excluded.id, admitted.id}, (
        "fixture: both documents must be admitted by title, or the filter is "
        "not the only thing separating them below"
    )

    found = await postgres_graph_store.search_metadata(
        _BOOST_TERM, filters={"lifecycle_status": "superseded"}
    )

    assert [d.id for d in found] == [admitted.id], (
        "a title match was returned past a filter that excludes it: the filter "
        "binds to one arm of the admitting disjunction rather than to all of it"
    )
