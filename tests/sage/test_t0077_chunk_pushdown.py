"""Expand LanceDB pre-filter coverage to include lifecycle_status and project.

Block 1 tests — Schema, Chunk dataclass, ingest row population, and
schema-migration null-fill for the two new columns.

Block 2 tests — _content_filters() pushdown: pushdownable filter keys
(lifecycle_status, project, doc_type) flow straight to the chunk store
as column predicates; only non-pushdownable keys (tags, pipeline_status,
document_ids, tier3) force a graph-store document_id IN-clause
resolution.

Block 3 tests — Tiered over-fetch multiplier: pure-pushdown filters
warrant a smaller fetch headroom than mixed-filter cases.

Implements the parent audit ticket finding F-7. Reduces the
hybrid RRF over-fetch multiplier dependency by letting LanceDB
pre-filter on these document-level scalars at chunk-search time
instead of resolving them via a graph-store document_id IN clause.
"""

from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone
from pathlib import Path

import pyarrow as pa
import pytest

try:
    import lancedb

    _HAS_LANCEDB = True
except ImportError:
    _HAS_LANCEDB = False

if _HAS_LANCEDB:
    from sage.adapters.content_store_lancedb import (
        _FILTERABLE_COLUMNS,
        CHUNKS_SCHEMA,
        CHUNKS_TABLE,
        VECTOR_DIMENSIONS,
        LanceDBContentStore,
    )

from sage.adapters.interfaces import Chunk

requires_lancedb = pytest.mark.skipif(not _HAS_LANCEDB, reason="lancedb not available")


# ── Block 1 / T1: CHUNKS_SCHEMA carries lifecycle_status and project ───


@requires_lancedb
def test_t0077_chunks_schema_includes_lifecycle_status_and_project():
    """CHUNKS_SCHEMA must declare lifecycle_status and project as
    nullable utf8 columns so LanceDB can pre-filter on them at top-K
    time (without a graph-store round trip).
    """
    names = set(CHUNKS_SCHEMA.names)
    assert "lifecycle_status" in names, (
        "lifecycle_status must be a chunk-table column so it can be "
        "pre-filtered at LanceDB query time."
    )
    assert "project" in names, (
        "project must be a chunk-table column so it can be pre-filtered at LanceDB query time."
    )

    # Schema field types must match the document-level shape.
    for col in ("lifecycle_status", "project"):
        field = CHUNKS_SCHEMA.field(col)
        assert field.type == pa.utf8(), f"{col} must be utf8"
        assert field.nullable, f"{col} must be nullable to permit backfill"


@requires_lancedb
def test_t0077_filterable_columns_includes_lifecycle_status_and_project():
    """_FILTERABLE_COLUMNS must accept the new keys so _build_where()
    will pass them through to LanceDB instead of dropping them on the
    floor (the existing whitelist behavior).
    """
    assert "lifecycle_status" in _FILTERABLE_COLUMNS
    assert "project" in _FILTERABLE_COLUMNS


# ── Block 1 / T2: Chunk dataclass carries the fields ──────────────────


def test_t0077_chunk_dataclass_carries_lifecycle_status_and_project():
    """The Chunk dataclass must hold lifecycle_status and project so
    the ingest path can populate them from the parent document at
    chunk-write time (mirroring the existing doc_type pattern).
    """
    chunk = Chunk(
        document_id="d_a",
        heading_path="H",
        content="body",
        chunk_index=0,
        lifecycle_status="active",
        project="CAS",
    )
    assert chunk.lifecycle_status == "active"
    assert chunk.project == "CAS"

    # Default value is None so callers that omit the fields still work.
    chunk_default = Chunk(
        document_id="d_b",
        heading_path="H",
        content="body",
        chunk_index=0,
    )
    assert chunk_default.lifecycle_status is None
    assert chunk_default.project is None


# ── Block 1 / T3: index_chunks writes the new columns ─────────────────


@requires_lancedb
async def test_t0077_index_chunks_writes_lifecycle_status_and_project(tmp_path):
    """When the ingest path builds a Chunk with lifecycle_status and
    project populated, index_chunks must persist those values to the
    underlying LanceDB row.
    """
    brain_root = tmp_path / "brain"
    brain_root.mkdir()
    store = LanceDBContentStore(brain_root)

    chunk = Chunk(
        document_id="doc_t0077_a",
        heading_path="Body",
        content="document content",
        embedding=[0.1] * VECTOR_DIMENSIONS,
        chunk_index=0,
        doc_type="note",
        lifecycle_status="active",
        project="CAS",
    )
    await store.index_chunks("doc_t0077_a", [chunk])

    db = lancedb.connect(str(brain_root / "lancedb"))
    table = db.open_table(CHUNKS_TABLE)
    rows = table.to_arrow().to_pylist()
    assert len(rows) == 1
    row = rows[0]
    assert row["lifecycle_status"] == "active", (
        "index_chunks must persist Chunk.lifecycle_status to the LanceDB "
        "row so the column can be used for pre-filter pushdown."
    )
    assert row["project"] == "CAS"


@requires_lancedb
async def test_t0077_replace_synthetic_header_writes_lifecycle_status_and_project(
    tmp_path,
):
    """The synthetic header chunk path must also persist the new
    columns. writes this chunk separately at Stage-3 abstraction
    completion, and it must carry the same lifecycle_status/project as
    the body chunks so the pre-filter is consistent across header and
    body rows.
    """
    brain_root = tmp_path / "brain"
    brain_root.mkdir()
    store = LanceDBContentStore(brain_root)

    from sage.adapters.interfaces import SYNTHETIC_HEADER_HEADING_PATH

    # Seed a body chunk first so the table exists.
    await store.index_chunks(
        "doc_t0077_b",
        [
            Chunk(
                document_id="doc_t0077_b",
                heading_path="Body",
                content="body content",
                embedding=[0.0] * VECTOR_DIMENSIONS,
                chunk_index=1,
                lifecycle_status="active",
                project="CAS",
            )
        ],
    )

    header = Chunk(
        document_id="doc_t0077_b",
        heading_path=SYNTHETIC_HEADER_HEADING_PATH,
        content="title and abstract",
        embedding=[0.0] * VECTOR_DIMENSIONS,
        chunk_index=0,
        doc_type="note",
        lifecycle_status="active",
        project="CAS",
    )
    await store.replace_synthetic_header_chunk("doc_t0077_b", header)

    db = lancedb.connect(str(brain_root / "lancedb"))
    table = db.open_table(CHUNKS_TABLE)
    rows = table.to_arrow().to_pylist()
    header_rows = [r for r in rows if r["heading_path"] == SYNTHETIC_HEADER_HEADING_PATH]
    assert len(header_rows) == 1
    assert header_rows[0]["lifecycle_status"] == "active"
    assert header_rows[0]["project"] == "CAS"


# ── Block 1 / T4: Schema migration adds columns as NULL ───────────────


if _HAS_LANCEDB:
    _PRE_T0077_CHUNKS_SCHEMA = pa.schema(
        [
            pa.field("document_id", pa.utf8()),
            pa.field("heading_path", pa.utf8()),
            pa.field("content", pa.utf8()),
            pa.field("chunk_index", pa.int32()),
            pa.field("vector", pa.list_(pa.float32(), VECTOR_DIMENSIONS)),
            pa.field("doc_type", pa.utf8(), nullable=True),
        ]
    )

    def _build_pre_t0077_lancedb(brain_root: Path, *, n_rows: int = 1) -> None:
        """Create a LanceDB chunks table at the pre-schema
        (doc_type only; no lifecycle_status or project columns).
        Mirrors the MIG-007 helper in test_migrate_flag.py for the
        earlier doc_type migration.
        """
        brain_root.mkdir(parents=True, exist_ok=True)
        db = lancedb.connect(str(brain_root / "lancedb"))
        rows = [
            {
                "document_id": f"doc_{i}",
                "heading_path": f"H{i}",
                "content": f"content {i}",
                "chunk_index": i,
                "vector": [0.0] * VECTOR_DIMENSIONS,
                "doc_type": "note",
            }
            for i in range(n_rows)
        ]
        db.create_table(CHUNKS_TABLE, data=rows, schema=_PRE_T0077_CHUNKS_SCHEMA)


@requires_lancedb
def test_t0077_schema_migration_adds_missing_columns_as_null(tmp_path):
    """When a vault was built before (no lifecycle_status /
    project columns on the chunks table) and the operator runs with
    --migrate, the destructive rebuild must add the new columns and
    leave existing rows with NULL for those columns (backfill is a
    separate, follow-on step).

    Mirrors test_mig_007_lancedb_applies_with_flag's contract for the
    earlier doc_type migration.
    """
    brain = tmp_path / "brain"
    _build_pre_t0077_lancedb(brain, n_rows=3)

    store = LanceDBContentStore(brain, migrate=True)
    assert store.pending_schema_columns() == set()

    db = lancedb.connect(str(brain / "lancedb"))
    table = db.open_table(CHUNKS_TABLE)
    names = set(table.schema.names)
    assert "lifecycle_status" in names, (
        "Migration must add the lifecycle_status column to existing tables."
    )
    assert "project" in names, "Migration must add the project column to existing tables."
    assert table.count_rows() == 3, "Migration must preserve row count."

    rows = table.to_arrow().to_pylist()
    docs = sorted(r["document_id"] for r in rows)
    assert docs == ["doc_0", "doc_1", "doc_2"], "Migration must preserve document IDs."
    assert all(r["lifecycle_status"] is None for r in rows), (
        "Migration must add lifecycle_status as NULL on legacy rows; the "
        "backfill script populates real values in a separate step."
    )
    assert all(r["project"] is None for r in rows), (
        "Migration must add project as NULL on legacy rows; the backfill "
        "script populates real values in a separate step."
    )

    # The existing doc_type column must be preserved verbatim.
    assert all(r["doc_type"] == "note" for r in rows)

    backup = brain / "chunks_migration_backup.parquet"
    assert not backup.exists(), "backup must be deleted on success"


# ── Block 2 / Block 3 fixtures ────────────────────────────────────────


_DOC_ID_RE = re.compile(r"^[0-9a-f]{8}_[a-z0-9_]+$")
_SHA256_RE = re.compile(r"^sha256:[0-9a-f]{64}$")


def _doc_id(short: str) -> str:
    if _DOC_ID_RE.fullmatch(short):
        return short
    return f"{hashlib.sha256(short.encode()).hexdigest()[:8]}_{short}"


def _sha(short: str) -> str:
    if _SHA256_RE.fullmatch(short):
        return short
    return "sha256:" + hashlib.sha256(f"sage-test-hash:{short}".encode()).hexdigest()


def _make_doc(
    short: str,
    *,
    lifecycle_status: str = "active",
    project: str | None = None,
    doc_type: str | None = "note",
    tags: list[str] | None = None,
):
    from sage.models.enums import PipelineStatus, SourceType
    from sage.models.schemas import Document

    now = datetime.now(timezone.utc)
    return Document(
        id=_doc_id(short),
        title=f"Test {short}",
        source_type=SourceType.MARKDOWN,
        source_path=f"test/{short}.md",
        lifecycle_status=lifecycle_status,
        source_content_hash=_sha(short),
        adapter_version="0.1.0",
        created_by="testuser",
        created_at=now,
        last_modified_by="testuser",
        updated_at=now,
        projected_at=now,
        pipeline_status=PipelineStatus.ABSTRACTION_COMPLETE,
        project=project,
        doc_type=doc_type,
        tags=tags or [],
    )


class _RecordingContentStore:
    """Wraps StubContentStore and records every search_semantic /
    search_bm25 filter dict so tests can inspect what reached the
    LanceDB layer.
    """

    def __init__(self, inner):
        self._inner = inner
        self.semantic_filter_calls: list[dict | None] = []
        self.bm25_filter_calls: list[dict | None] = []

    async def search_semantic(self, query_embedding, limit=10, filters=None):
        self.semantic_filter_calls.append(dict(filters) if isinstance(filters, dict) else filters)
        return await self._inner.search_semantic(query_embedding, limit, filters)

    async def search_bm25(self, query, limit=10, filters=None):
        self.bm25_filter_calls.append(dict(filters) if isinstance(filters, dict) else filters)
        return await self._inner.search_bm25(query, limit, filters)

    def __getattr__(self, name):
        # Delegate everything else (index_chunks, get_all_chunks, etc.) to inner.
        return getattr(self._inner, name)


# ── Block 2 / T1: _content_filters pushdown for lifecycle_status only ─


async def test_t0077_content_filter_pushdown_lifecycle_only(
    graph_store, stub_content_store, stub_embedding_provider, minimal_config, monkeypatch
):
    """When the only active filter is lifecycle_status, _content_filters
    must return {"lifecycle_status":...} with has_doc_constraints=False
    and must NOT invoke graph_store.query_documents (no IN-clause
    resolution needed).
    """
    from sage.models.enums import RetrievalMode
    from sage.models.schemas import DiscoverRequest, RetrievalFilters
    from sage.services.retrieval import RetrievalService

    service = RetrievalService(
        graph_store=graph_store,
        content_store=stub_content_store,
        embedding_provider=stub_embedding_provider,
        config=minimal_config,
    )

    calls = []
    original_query = graph_store.query_documents

    async def spy_query(*args, **kwargs):
        calls.append((args, kwargs))
        return await original_query(*args, **kwargs)

    monkeypatch.setattr(graph_store, "query_documents", spy_query)

    request = DiscoverRequest(
        mode=RetrievalMode.SEMANTIC,
        query="anything",
        filters=RetrievalFilters(lifecycle_status="active"),
        limit=10,
    )
    content_filters, has_doc_constraints = await service._content_filters(request)

    assert content_filters == {"lifecycle_status": "active"}, (
        "Only lifecycle_status should be present; it must be pushed "
        "straight to the chunk store as a column predicate."
    )
    assert has_doc_constraints is False, (
        "Pushdownable-only filter sets must not set has_doc_constraints "
        "(no graph-store resolution is needed, so the short-circuit on "
        "empty match set does not apply)."
    )
    assert calls == [], (
        "Pure-pushdown filter sets must skip the graph_store SQL round trip entirely."
    )


async def test_t0077_content_filter_pushdown_project_only(
    graph_store, stub_content_store, stub_embedding_provider, minimal_config, monkeypatch
):
    """Same contract as lifecycle_status, but for project."""
    from sage.models.enums import RetrievalMode
    from sage.models.schemas import DiscoverRequest, RetrievalFilters
    from sage.services.retrieval import RetrievalService

    service = RetrievalService(
        graph_store=graph_store,
        content_store=stub_content_store,
        embedding_provider=stub_embedding_provider,
        config=minimal_config,
    )

    calls = []
    original_query = graph_store.query_documents

    async def spy_query(*args, **kwargs):
        calls.append((args, kwargs))
        return await original_query(*args, **kwargs)

    monkeypatch.setattr(graph_store, "query_documents", spy_query)

    request = DiscoverRequest(
        mode=RetrievalMode.SEMANTIC,
        query="x",
        filters=RetrievalFilters(project="CAS"),
        limit=10,
    )
    content_filters, has_doc_constraints = await service._content_filters(request)

    assert content_filters == {"project": "CAS"}
    assert has_doc_constraints is False
    assert calls == [], "Pure-pushdown filter sets must skip graph SQL."


async def test_t0077_content_filter_pushdown_mixed_doc_type_lifecycle(
    graph_store, stub_content_store, stub_embedding_provider, minimal_config, monkeypatch
):
    """doc_type + lifecycle_status are both pushdownable; the combined
    set must reach the chunk store as two AND'd column predicates with
    no graph-store call.
    """
    from sage.models.enums import RetrievalMode
    from sage.models.schemas import DiscoverRequest, RetrievalFilters
    from sage.services.retrieval import RetrievalService

    service = RetrievalService(
        graph_store=graph_store,
        content_store=stub_content_store,
        embedding_provider=stub_embedding_provider,
        config=minimal_config,
    )

    calls = []
    original_query = graph_store.query_documents

    async def spy_query(*args, **kwargs):
        calls.append((args, kwargs))
        return await original_query(*args, **kwargs)

    monkeypatch.setattr(graph_store, "query_documents", spy_query)

    request = DiscoverRequest(
        mode=RetrievalMode.SEMANTIC,
        query="x",
        filters=RetrievalFilters(doc_type="note", lifecycle_status="active"),
        limit=10,
    )
    content_filters, has_doc_constraints = await service._content_filters(request)

    assert content_filters == {"doc_type": "note", "lifecycle_status": "active"}
    assert has_doc_constraints is False
    assert calls == [], "Two pushdownable filters must skip graph SQL."


async def test_t0077_content_filter_mixed_with_tags_falls_back_to_graph(
    graph_store, stub_content_store, stub_embedding_provider, minimal_config
):
    """When a non-pushdownable filter (tags) is present, the graph-store
    resolution must run; the resulting chunk filter must include
    lifecycle_status (pushed down as a column) AND document_id (the
    IN-clause from the graph resolution).
    """
    from sage.models.enums import RetrievalMode
    from sage.models.schemas import DiscoverRequest, RetrievalFilters
    from sage.services.retrieval import RetrievalService

    # Seed the vault with a doc that matches both filters so the IN
    # clause is non-empty.
    doc = _make_doc("d_tagged", lifecycle_status="active", tags=["sage"])
    await graph_store.insert_document(doc)

    service = RetrievalService(
        graph_store=graph_store,
        content_store=stub_content_store,
        embedding_provider=stub_embedding_provider,
        config=minimal_config,
    )

    request = DiscoverRequest(
        mode=RetrievalMode.SEMANTIC,
        query="x",
        filters=RetrievalFilters(lifecycle_status="active", tags=["sage"]),
        limit=10,
    )
    content_filters, has_doc_constraints = await service._content_filters(request)

    assert content_filters is not None
    assert content_filters.get("lifecycle_status") == "active", (
        "Pushdownable keys must remain as column predicates even when a "
        "non-pushdownable key forces a graph-store resolution."
    )
    assert "document_id" in content_filters, (
        "Non-pushdownable filter must trigger the document_id IN-clause resolution path."
    )
    assert doc.id in content_filters["document_id"]
    assert has_doc_constraints is True


async def test_t0077_pushdown_skips_graph_when_query_documents_raises(
    graph_store, stub_content_store, stub_embedding_provider, minimal_config, monkeypatch
):
    """Anti-coincidental-pass gate: if a future change silently reverts
    to graph-store resolution for pushdownable-only filters, this test
    catches it. query_documents is patched to raise; the request must
    still succeed because no SQL round trip should happen.
    """
    from sage.models.enums import RetrievalMode
    from sage.models.schemas import DiscoverRequest, RetrievalFilters
    from sage.services.retrieval import RetrievalService

    service = RetrievalService(
        graph_store=graph_store,
        content_store=stub_content_store,
        embedding_provider=stub_embedding_provider,
        config=minimal_config,
    )

    async def boom(*args, **kwargs):
        raise AssertionError("query_documents must NOT be called for pure-pushdown filter sets.")

    monkeypatch.setattr(graph_store, "query_documents", boom)

    request = DiscoverRequest(
        mode=RetrievalMode.SEMANTIC,
        query="x",
        filters=RetrievalFilters(lifecycle_status="active", project="CAS"),
        limit=10,
    )
    # Should not raise; assertion above guards the pushdown invariant.
    content_filters, has_doc_constraints = await service._content_filters(request)
    assert content_filters == {"lifecycle_status": "active", "project": "CAS"}
    assert has_doc_constraints is False


# ── Block 2 / T2: end-to-end round-trip via stub content store ────────


async def test_t0077_lifecycle_pushdown_round_trip_semantic(
    graph_store, stub_content_store, stub_embedding_provider, minimal_config
):
    """End-to-end: a semantic-mode discover with a lifecycle_status-only
    filter must result in `lifecycle_status="active"` reaching the
    content store's search_semantic call as a column predicate (not
    encoded into a document_id IN list).
    """
    from sage.models.enums import RetrievalMode
    from sage.models.schemas import DiscoverRequest, RetrievalFilters
    from sage.services.retrieval import RetrievalService

    recording = _RecordingContentStore(stub_content_store)
    service = RetrievalService(
        graph_store=graph_store,
        content_store=recording,
        embedding_provider=stub_embedding_provider,
        config=minimal_config,
    )

    request = DiscoverRequest(
        mode=RetrievalMode.SEMANTIC,
        query="anything",
        filters=RetrievalFilters(lifecycle_status="active"),
        limit=10,
        use_hybrid=False,
    )
    await service.discover(request)

    assert recording.semantic_filter_calls, (
        "search_semantic must be invoked under pure-pushdown filters."
    )
    last = recording.semantic_filter_calls[-1]
    assert last is not None, "filters dict must be non-None"
    assert last.get("lifecycle_status") == "active", (
        "lifecycle_status must reach the content store as a column "
        "predicate, not encoded as a document_id IN list."
    )
    assert "document_id" not in last, (
        "Pure-pushdown filter sets must not synthesize a document_id "
        "IN clause from the graph store."
    )


async def test_t0077_lifecycle_pushdown_round_trip_hybrid(
    graph_store, stub_content_store, stub_embedding_provider, minimal_config
):
    """Same as above, but exercising the hybrid RRF path. Hybrid invokes
    both search_semantic and search_bm25; both must receive
    lifecycle_status as a column predicate.
    """
    from sage.models.enums import RetrievalMode
    from sage.models.schemas import DiscoverRequest, RetrievalFilters
    from sage.services.retrieval import RetrievalService

    recording = _RecordingContentStore(stub_content_store)
    service = RetrievalService(
        graph_store=graph_store,
        content_store=recording,
        embedding_provider=stub_embedding_provider,
        config=minimal_config,
    )

    request = DiscoverRequest(
        mode=RetrievalMode.SEMANTIC,
        query="anything",
        filters=RetrievalFilters(lifecycle_status="active"),
        limit=10,
        use_hybrid=True,
    )
    await service.discover(request)

    assert recording.semantic_filter_calls, "hybrid path must call search_semantic"
    assert recording.bm25_filter_calls, "hybrid path must call search_bm25"

    for arm, calls in (
        ("semantic", recording.semantic_filter_calls),
        ("bm25", recording.bm25_filter_calls),
    ):
        last = calls[-1]
        assert last is not None, f"{arm} filters dict must be non-None"
        assert last.get("lifecycle_status") == "active", (
            f"hybrid {arm} arm must receive lifecycle_status as a column predicate."
        )
        assert "document_id" not in last, (
            f"hybrid {arm} arm under pure-pushdown filters must not see a document_id IN clause."
        )


async def test_t0077_lifecycle_pushdown_round_trip_keyword(
    graph_store, stub_content_store, stub_embedding_provider, minimal_config
):
    """Keyword mode also routes through _content_filters; pushdownable
    filters must reach search_bm25 as column predicates.
    """
    from sage.models.enums import RetrievalMode
    from sage.models.schemas import DiscoverRequest, RetrievalFilters
    from sage.services.retrieval import RetrievalService

    recording = _RecordingContentStore(stub_content_store)
    service = RetrievalService(
        graph_store=graph_store,
        content_store=recording,
        embedding_provider=stub_embedding_provider,
        config=minimal_config,
    )

    request = DiscoverRequest(
        mode=RetrievalMode.KEYWORD,
        query="alpha-marker",
        filters=RetrievalFilters(project="CAS"),
        limit=10,
    )
    await service.discover(request)

    assert recording.bm25_filter_calls, "keyword path must call search_bm25"
    last = recording.bm25_filter_calls[-1]
    assert last is not None
    assert last.get("project") == "CAS"
    assert "document_id" not in last


# ── Block 3: tiered over-fetch multiplier ─────────────────────────────


def test_t0077_fetch_limit_no_filters():
    """With no filters present, the multiplier is 5x (dedup-only
    over-fetch headroom).
    """
    from sage.models.enums import RetrievalMode
    from sage.models.schemas import DiscoverRequest
    from sage.services.retrieval import RetrievalService

    request = DiscoverRequest(
        mode=RetrievalMode.SEMANTIC,
        query="x",
        limit=10,
    )
    assert RetrievalService._fetch_limit(request) == 50


def test_t0077_fetch_limit_pushdown_only():
    """With only pushdownable filters, the multiplier drops to 3x
    because LanceDB can pre-filter the candidate set exactly (no
    document_id IN-clause bloat).
    """
    from sage.models.enums import RetrievalMode
    from sage.models.schemas import DiscoverRequest, RetrievalFilters
    from sage.services.retrieval import RetrievalService

    request = DiscoverRequest(
        mode=RetrievalMode.SEMANTIC,
        query="x",
        filters=RetrievalFilters(lifecycle_status="active", project="CAS"),
        limit=10,
    )
    assert RetrievalService._fetch_limit(request) == 30, (
        "Pure-pushdown filter sets must use the smaller 3x multiplier; "
        "no IN-clause means the LanceDB top-K is already tight."
    )


def test_t0077_fetch_limit_mixed():
    """When a non-pushdownable filter is present (e.g. tags), keep the
    10x multiplier because the graph-resolved document_id IN list can
    bloat the candidate set.
    """
    from sage.models.enums import RetrievalMode
    from sage.models.schemas import DiscoverRequest, RetrievalFilters
    from sage.services.retrieval import RetrievalService

    request = DiscoverRequest(
        mode=RetrievalMode.SEMANTIC,
        query="x",
        filters=RetrievalFilters(lifecycle_status="active", tags=["sage"]),
        limit=10,
    )
    assert RetrievalService._fetch_limit(request) == 100, (
        "Mixed filter sets (any non-pushdownable key present) keep the "
        "conservative 10x backstop because the document_id IN clause "
        "cardinality is graph-resolved."
    )


# ── Block 4: metadata-sync propagation ────────────────────────────────


class _RecordingChunkMetaStore:
    """Wraps StubContentStore and captures every update_chunk_metadata
    call so tests can assert that lifecycle_status / project edits
    propagate from the document store down to the chunk store.
    """

    def __init__(self, inner):
        self._inner = inner
        self.calls: list[tuple[str, dict]] = []

    async def update_chunk_metadata(self, document_id, metadata):
        self.calls.append((document_id, dict(metadata)))
        return await self._inner.update_chunk_metadata(document_id, metadata)

    def __getattr__(self, name):
        return getattr(self._inner, name)


async def test_t0077_update_metadata_project_syncs_to_chunks(
    graph_store, lock_manager, minimal_config, stub_content_store
):
    """When MetadataService.update_metadata changes a document's project,
    the new value must be pushed to all of the document's chunk rows so
    LanceDB pre-filter by project keeps matching after the edit.
    """
    from sage.models.schemas import UpdateMetadataRequest
    from sage.services.metadata import MetadataService

    recorder = _RecordingChunkMetaStore(stub_content_store)
    service = MetadataService(graph_store, lock_manager, minimal_config, recorder)

    doc = _make_doc("doc_proj_sync", project="alpha")
    await graph_store.insert_document(doc)

    await service.update_metadata(
        doc.id, UpdateMetadataRequest(project="beta"), modified_by="testuser"
    )

    project_calls = [(did, md) for did, md in recorder.calls if "project" in md and did == doc.id]
    assert project_calls, (
        "MetadataService.update_metadata must push project changes to "
        "the chunk store so LanceDB pre-filter pushdown remains correct."
    )
    assert project_calls[-1][1]["project"] == "beta"


async def test_t0077_update_metadata_doc_type_still_syncs(
    graph_store, lock_manager, minimal_config, stub_content_store
):
    """Regression guard: the existing doc_type sync behavior must not
    be broken by the new project-sync wiring.
    """
    from sage.models.schemas import UpdateMetadataRequest
    from sage.services.metadata import MetadataService

    recorder = _RecordingChunkMetaStore(stub_content_store)
    service = MetadataService(graph_store, lock_manager, minimal_config, recorder)

    doc = _make_doc("doc_dt_sync", doc_type="note")
    await graph_store.insert_document(doc)

    await service.update_metadata(
        doc.id, UpdateMetadataRequest(doc_type="memo"), modified_by="testuser"
    )

    dt_calls = [(did, md) for did, md in recorder.calls if "doc_type" in md and did == doc.id]
    assert dt_calls
    assert dt_calls[-1][1]["doc_type"] == "memo"


async def test_t0077_set_lifecycle_syncs_to_chunks(
    graph_store, lock_manager, minimal_config, stub_content_store
):
    """When LifecycleService.set_lifecycle transitions a document, the
    new lifecycle_status must be pushed to the document's chunk rows so
    LanceDB pre-filter by lifecycle_status remains correct.
    """
    from sage.models.schemas import SetLifecycleRequest
    from sage.services.lifecycle import LifecycleService

    recorder = _RecordingChunkMetaStore(stub_content_store)
    service = LifecycleService(graph_store, lock_manager, minimal_config, content_store=recorder)

    doc = _make_doc("doc_lc_sync", lifecycle_status="active")
    await graph_store.insert_document(doc)

    await service.set_lifecycle(doc.id, SetLifecycleRequest(action="complete"))

    ls_calls = [
        (did, md) for did, md in recorder.calls if "lifecycle_status" in md and did == doc.id
    ]
    assert ls_calls, (
        "LifecycleService.set_lifecycle must push the new lifecycle_status "
        "to the chunk store; otherwise stale chunk metadata makes the "
        "T-0077 pre-filter return wrong results after a transition."
    )
    assert ls_calls[-1][1]["lifecycle_status"] == "completed"


async def test_t0077_supersede_syncs_predecessor_lifecycle_to_chunks(
    graph_store, lock_manager, minimal_config, stub_content_store
):
    """The supersede flow flips the predecessor's lifecycle_status (to
    `archived` in the minimal config). That flip must propagate to the
    predecessor's chunk rows just like other transitions.
    """
    from sage.models.schemas import SetLifecycleRequest
    from sage.services.lifecycle import LifecycleService

    recorder = _RecordingChunkMetaStore(stub_content_store)
    service = LifecycleService(graph_store, lock_manager, minimal_config, content_store=recorder)

    predecessor = _make_doc("doc_pred", lifecycle_status="active")
    new_version = _make_doc("doc_new", lifecycle_status="active")
    await graph_store.insert_document(predecessor)
    await graph_store.insert_document(new_version)

    await service.set_lifecycle(
        predecessor.id,
        SetLifecycleRequest(action="supersede", successor_id=new_version.id),
    )

    ls_calls = [
        (did, md)
        for did, md in recorder.calls
        if "lifecycle_status" in md and did == predecessor.id
    ]
    assert ls_calls, (
        "supersede must push the predecessor's new lifecycle_status to "
        "the predecessor's chunk rows."
    )
    assert ls_calls[-1][1]["lifecycle_status"] == "archived"


async def test_t0077_lifecycle_service_without_content_store_no_op(
    graph_store, lock_manager, minimal_config
):
    """Backwards-compat: callers that omit content_store from the
    LifecycleService constructor (the legacy signature) must still be
    able to drive transitions without error. The chunk-sync step is a
    no-op in this configuration.
    """
    from sage.models.schemas import SetLifecycleRequest
    from sage.services.lifecycle import LifecycleService

    # Omit content_store; this is the pre-calling convention.
    service = LifecycleService(graph_store, lock_manager, minimal_config)

    doc = _make_doc("doc_lc_no_cs", lifecycle_status="active")
    await graph_store.insert_document(doc)

    # Should not raise: missing content_store is tolerated.
    response = await service.set_lifecycle(doc.id, SetLifecycleRequest(action="complete"))
    assert response.document.lifecycle_status == "completed"


async def test_t0077_ingest_supersede_syncs_predecessor_chunks(
    tmp_vault_dir, graph_store, ingestion_service, stub_content_store
):
    """cas-code-review F4 finding: the ingest-side supersede path
    (`insert_with_supersede_atomic`) commits the predecessor's
    `lifecycle_status` flip directly in SQL, bypassing
    LifecycleService.set_lifecycle. Without an explicit chunk-sync
    after that atomic commit, the predecessor's chunks keep their
    stale `lifecycle_status="active"` even though the document is
    now archived, breaking the pre-filter for archived
    versions.
    """
    from sage.models.schemas import IngestRequest

    # Seed v1 file and ingest it to land active chunks for the predecessor.
    v1_path = tmp_vault_dir / "sources" / "t0077_supersede_v1.md"
    v1_path.parent.mkdir(parents=True, exist_ok=True)
    v1_path.write_text("# V1\n\nOriginal body.")
    v1 = await ingestion_service.ingest(
        IngestRequest(source="t0077_supersede_v1.md", source_type="markdown")
    )
    pred_id = v1.document.id

    # Confirm predecessor chunks land as active.
    pred_chunks_before = await stub_content_store.get_all_chunks(pred_id)
    assert pred_chunks_before, "ingest must produce chunks for v1"
    assert all(c.lifecycle_status == "active" for c in pred_chunks_before)

    # Seed v2 file and ingest with supersede.
    v2_path = tmp_vault_dir / "sources" / "t0077_supersede_v2.md"
    v2_path.write_text("# V2\n\nRevised body.")
    await ingestion_service.ingest(
        IngestRequest(
            source="t0077_supersede_v2.md",
            source_type="markdown",
            predecessor_id=pred_id,
        )
    )

    # The predecessor's chunks must now carry lifecycle_status="archived"
    # so the LanceDB pre-filter for active/archived chunks stays correct.
    pred_chunks_after = await stub_content_store.get_all_chunks(pred_id)
    assert pred_chunks_after, (
        "predecessor chunks must still exist after supersede (only the "
        "lifecycle_status is supposed to change)."
    )
    assert all(c.lifecycle_status == "archived" for c in pred_chunks_after), (
        "ingest-side supersede must push the predecessor's new "
        "lifecycle_status to the chunk store; otherwise stale chunk "
        "metadata makes T-0077 pre-filter return wrong results."
    )
