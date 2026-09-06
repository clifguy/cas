"""Retrieval tests: BH-020, BH-021, BH-027, BH-028, BH-029, BH-030,
BH-058, BH-059, BH-060, BH-061, BH-069, BH-070, BH-072 through BH-088,
BH-101 through BH-115.

Covers semantic retrieval (pure vector and hybrid RRF), deterministic
retrieval (heading path prefix match), keyword-only retrieval,
catalog mode (filter-only enumeration with sort), pipeline/scope gating,
title indexing in chunks, semantic abstract surfacing on DocumentSummary,
and two-pass abstract-boosted retrieval.
"""

import hashlib
import inspect
import json
import re
import uuid as _uuid
from datetime import datetime, timedelta, timezone
from typing import NamedTuple

import pydantic_core
import pytest
from pydantic import ValidationError
from pydantic_core import PydanticUndefined

from sage.adapters.interfaces import Chunk, DocumentSurface, KeywordQueryParse
from sage.adapters.stubs import SeededEmbeddingProvider, StubContentStore
from sage.api.errors import (
    DocumentNotFoundError,
    HeadingNotFoundError,
    MissingFieldError,
    ModeParameterMismatchError,
    PipelineIncompleteError,
    translate_validation_error,
)
from sage.models.enums import (
    EdgeType,
    FacetField,
    PipelineStatus,
    ResponseMode,
    RetrievalMode,
    RetrievalScope,
    RetrievalTarget,
    SourceType,
)
from sage.models.schemas import (
    _EDGE_FORBIDDEN_PARAMS,
    _FACET_FORBIDDEN_PARAMS,
    DiscoverHit,
    DiscoverRequest,
    DiscoverResponse,
    Document,
    DocumentSummary,
    Edge,
    EdgeHit,
    FacetHit,
    RetrievalFilters,
    UpdateMetadataRequest,
)
from sage.services.document_surface import compose_document_surface, embedding_text
from sage.services.retrieval import (
    DEFAULT_FACET_VALUE_LIMIT,
    DEFAULT_MCP_INLINE_BUDGET_BYTES,
    RetrievalService,
    _apply_facets_budget_hint,
)

_DOC_ID_RE = re.compile(r"^[0-9a-f]{8}_[a-z0-9_]+$")


def _id(name: str) -> str:
    """Translate a short test name to a shape-conformant document ID.

    The ID validator in sage/models/schemas.py requires the pattern
    ^[0-9a-f]{8}_[a-z0-9_]+$. Test fixtures use short readable names
    like "doc_a"; this helper wraps them so the values still construct
    valid Document instances. Idempotent: an already-canonical id
    passes through unchanged so wrapping is safe to apply at every
    call site.
    """
    if _DOC_ID_RE.fullmatch(name):
        return name
    return f"{hashlib.sha256(name.encode()).hexdigest()[:8]}_{name}"


_SHA256_RE = re.compile(r"^sha256:[0-9a-f]{64}$")


def _sha(name: str) -> str:
    """Deterministic canonical Sha256 from a short test name.

    The Sha256Str validator requires `^sha256:[0-9a-f]{64}$`. Test
    fixtures historically used short readable strings like
    f"hash_{doc_id}" or "sha256:abc"; this helper maps any such
    name to a stable canonical Sha256. Idempotent.
    """
    if _SHA256_RE.fullmatch(name):
        return name
    return "sha256:" + hashlib.sha256(f"sage-test-hash:{name}".encode()).hexdigest()


def _make_doc(
    doc_id: str,
    lifecycle_status: str = "active",
    pipeline_status: PipelineStatus = PipelineStatus.ABSTRACTION_COMPLETE,
    project: str | None = None,
    source_type: SourceType = SourceType.MARKDOWN,
    doc_type: str | None = None,
    authority_scope: str | None = None,
    tags: list[str] | None = None,
    document_date: str | None = None,
    source_modified_at: datetime | None = None,
    semantic_abstract: str | None = None,
    tier3_metadata: dict | None = None,
    version_label: str | None = None,
) -> Document:
    now = datetime.now(timezone.utc)
    return Document(
        id=doc_id,
        title=f"Test {doc_id}",
        source_type=source_type,
        source_path=f"test/{doc_id}.md",
        lifecycle_status=lifecycle_status,
        source_content_hash=_sha(doc_id),
        adapter_version="0.1.0",
        created_by="testuser",
        created_at=now,
        last_modified_by="testuser",
        updated_at=now,
        projected_at=now,
        pipeline_status=pipeline_status,
        project=project,
        doc_type=doc_type,
        authority_scope=authority_scope,
        tags=tags or [],
        document_date=document_date,
        source_modified_at=source_modified_at,
        semantic_abstract=semantic_abstract,
        tier3_metadata=tier3_metadata,
        version_label=version_label,
    )


@pytest.fixture
def seeded_embedding_provider():
    return SeededEmbeddingProvider()


@pytest.fixture
def retrieval_service(graph_store, stub_content_store, seeded_embedding_provider, minimal_config):
    return RetrievalService(
        graph_store=graph_store,
        content_store=stub_content_store,
        embedding_provider=seeded_embedding_provider,
        config=minimal_config,
    )


def _document_level_hit(response, document_id):
    """The response's document-level hit for a document, or ``None``.

    Naming the hit this way is what makes the assertions below discriminating
    -- a document with a body chunk is returned whether or not its surface was
    ever written, so asserting only that the document came back says nothing
    about the surface.

    Two conditions, because the empty heading path alone is not sufficient: a
    hit the metadata boost injected carries one too, and it never reached a
    retrieval surface at all. What separates them is that a boosted hit reports
    no passage count -- it was admitted without consulting a passage -- while a
    hit that came through a surface always reports one, even when the answer is
    zero.

    The count is not required to *be* zero by the predicate, though in practice
    it now is. A document whose surface and whose passages both answer is one
    hit, and that hit carries the passage's heading: the fusion lets a passage
    row displace a surface row, and the service hands the excerpt to a passage
    that arrives after a surface-sourced hit. So such a document is not
    classified document-level here at all, and a test asserting the zero seeds
    a document with no passages -- not to avoid an ambiguity, but because
    nothing else can produce the shape.

    One case this cannot see: a passage of a document that has no headings at
    all carries an empty path too. No fixture here has one.
    """
    for hit in response.results:
        if (
            hit.document.id == document_id
            and hit.heading_path is None
            and hit.matched_chunk_count is not None
        ):
            return hit
    return None


async def _index_document_surface(
    content_store: StubContentStore,
    embedding_provider,
    document_id: str,
    doc,
) -> None:
    """Helper: compose and index a document's document-level row.

    Uses the production composition so a test seeds the same text ingest
    would, rather than a hand-written approximation of it.
    """
    surface = compose_document_surface(document_id, doc)
    [surface.embedding] = await embedding_provider.embed([embedding_text(surface)])
    await content_store.upsert_document_surface(surface)


async def _index_doc_chunks(
    content_store: StubContentStore,
    embedding_provider,
    document_id: str,
    chunks_data: list[tuple[str, str]],
    doc_type: str | None = None,
    lifecycle_status: str | None = None,
    project: str | None = None,
) -> None:
    """Helper: index chunks for a document in the content store.

    chunks_data: list of (heading_path, content) tuples.
    doc_type, lifecycle_status, project: optional document-level scalars
    to stamp on the chunks for pre-filter testing. Production ingest
    stamps all three from the parent ``Document``; tests opt in per-call.
    """
    chunks = []
    for i, (heading_path, content) in enumerate(chunks_data):
        chunks.append(
            Chunk(
                document_id=document_id,
                heading_path=heading_path,
                content=content,
                chunk_index=i,
                doc_type=doc_type,
                lifecycle_status=lifecycle_status,
                project=project,
            )
        )

    # Embed chunks
    texts = [c.content for c in chunks]
    embeddings = await embedding_provider.embed(texts)
    for chunk, emb in zip(chunks, embeddings):
        chunk.embedding = emb

    await content_store.index_chunks(document_id, chunks)


# ---------------------------------------------------------------------------
# BH-020: Failed pipeline quarantines document from semantic retrieval
# ---------------------------------------------------------------------------


async def test_bh_020_failed_pipeline_excluded_from_semantic(
    graph_store, stub_content_store, seeded_embedding_provider, retrieval_service
):
    """A document with pipeline_status=failed must not appear in semantic results."""
    # Create two documents: one healthy, one failed
    doc_ok = _make_doc(_id("doc_ok"))
    doc_failed = _make_doc(_id("doc_failed"), pipeline_status=PipelineStatus.FAILED)
    doc_failed.pipeline_error = "LLM unavailable"
    await graph_store.insert_document(doc_ok)
    await graph_store.insert_document(doc_failed)

    # Index chunks for both
    await _index_doc_chunks(
        stub_content_store,
        seeded_embedding_provider,
        _id("doc_ok"),
        [("Section 1", "This document discusses report claims and prior art.")],
    )
    await _index_doc_chunks(
        stub_content_store,
        seeded_embedding_provider,
        _id("doc_failed"),
        [("Section 1", "This document discusses report claims and prior art.")],
    )

    request = DiscoverRequest(mode=RetrievalMode.SEMANTIC, query="report claims")
    response = await retrieval_service.discover(request)

    doc_ids = [h.document.id for h in response.results]
    assert _id("doc_ok") in doc_ids
    assert _id("doc_failed") not in doc_ids


# ---------------------------------------------------------------------------
# BH-021: Failed document excluded from deterministic retrieval
# ---------------------------------------------------------------------------


async def test_bh_021_failed_doc_excluded_from_deterministic(
    graph_store, stub_content_store, seeded_embedding_provider, retrieval_service
):
    """Deterministic retrieval on a failed-pipeline document raises 422."""
    doc = _make_doc(_id("doc_failed"), pipeline_status=PipelineStatus.FAILED)
    doc.pipeline_error = "LLM unavailable"
    await graph_store.insert_document(doc)

    request = DiscoverRequest(
        mode=RetrievalMode.DETERMINISTIC,
        document_id=_id("doc_failed"),
        heading_path="Section 1",
    )
    with pytest.raises(PipelineIncompleteError) as exc_info:
        await retrieval_service.discover(request)
    assert exc_info.value.status_code == 422
    assert exc_info.value.code == "pipeline_incomplete"


# ---------------------------------------------------------------------------
# BH-027: Hybrid retrieval uses Reciprocal Rank Fusion
# ---------------------------------------------------------------------------


async def test_bh_027_hybrid_rrf(
    graph_store, stub_content_store, seeded_embedding_provider, retrieval_service
):
    """Document matching both vector and BM25 ranks highest under RRF."""
    # doc_vector: matches well on vector (similar embedding), less on keywords
    doc_vector = _make_doc(_id("doc_vector"))
    await graph_store.insert_document(doc_vector)

    # doc_bm25: matches well on keywords, less on vector
    doc_bm25 = _make_doc(_id("doc_bm25"))
    await graph_store.insert_document(doc_bm25)

    # doc_both: matches well on both
    doc_both = _make_doc(_id("doc_both"))
    await graph_store.insert_document(doc_both)

    # Index chunks with content designed to produce different ranking per mode.
    # doc_vector gets content with same semantic embedding direction as query
    await _index_doc_chunks(
        stub_content_store,
        seeded_embedding_provider,
        _id("doc_vector"),
        [("Section 1", "neural network architecture deep learning models")],
    )
    # doc_bm25 gets content with exact keyword matches
    await _index_doc_chunks(
        stub_content_store,
        seeded_embedding_provider,
        _id("doc_bm25"),
        [("Section 1", "report claims prior art novelty claims")],
    )
    # doc_both gets content matching both
    await _index_doc_chunks(
        stub_content_store,
        seeded_embedding_provider,
        _id("doc_both"),
        [("Section 1", "report claims prior art neural network deep learning")],
    )

    request = DiscoverRequest(
        mode=RetrievalMode.SEMANTIC,
        query="report claims prior art",
        use_hybrid=True,
    )
    response = await retrieval_service.discover(request)

    assert response.mode == RetrievalMode.SEMANTIC
    assert len(response.results) > 0

    # All results should have RRF relevance_score
    for hit in response.results:
        assert hit.relevance_score is not None
        assert hit.relevance_score > 0.0

    # Results are ordered by RRF score descending
    scores = [h.relevance_score for h in response.results]
    assert scores == sorted(scores, reverse=True)


# ---------------------------------------------------------------------------
# BH-028: Non-hybrid retrieval uses pure vector scores
# ---------------------------------------------------------------------------


async def test_bh_028_pure_vector_semantic(
    graph_store, stub_content_store, seeded_embedding_provider, retrieval_service
):
    """Non-hybrid semantic search returns pure vector similarity scores."""
    doc_a = _make_doc(_id("doc_a"))
    doc_b = _make_doc(_id("doc_b"))
    await graph_store.insert_document(doc_a)
    await graph_store.insert_document(doc_b)

    # Index with distinct content
    await _index_doc_chunks(
        stub_content_store,
        seeded_embedding_provider,
        _id("doc_a"),
        [("Section 1", "machine learning classification algorithms")],
    )
    await _index_doc_chunks(
        stub_content_store,
        seeded_embedding_provider,
        _id("doc_b"),
        [("Section 1", "gardening tips for spring planting season")],
    )

    request = DiscoverRequest(
        mode=RetrievalMode.SEMANTIC,
        query="machine learning classification",
        use_hybrid=False,
    )
    response = await retrieval_service.discover(request)

    assert response.mode == RetrievalMode.SEMANTIC
    assert len(response.results) > 0

    # All results have relevance_score (vector similarity)
    for hit in response.results:
        assert hit.relevance_score is not None

    # No BM25 influence: results ordered by vector score
    scores = [h.relevance_score for h in response.results]
    assert scores == sorted(scores, reverse=True)


# ---------------------------------------------------------------------------
# BH-029: Deterministic retrieval uses prefix match on heading_path
# ---------------------------------------------------------------------------


async def test_bh_029_deterministic_prefix_match(
    graph_store, stub_content_store, seeded_embedding_provider, retrieval_service
):
    """heading_path matches the specified heading and all children."""
    # Set document_date and source_modified_at so the assertions
    # below can lock the deterministic-site field-drop regression at the
    # consumer surface (both were silently omitted before the from_document
    # consolidation).
    deterministic_smt = datetime(2026, 5, 20, 12, 0, tzinfo=timezone.utc)
    doc = _make_doc(
        _id("doc_structured"),
        document_date="2026-05-15",
        source_modified_at=deterministic_smt,
    )
    await graph_store.insert_document(doc)

    # Index chunks with heading hierarchy
    await _index_doc_chunks(
        stub_content_store,
        seeded_embedding_provider,
        _id("doc_structured"),
        [
            ("Section 3 > Definitions", "Top-level definitions content."),
            ("Section 3 > Definitions > Normalization", "Normalization overview."),
            ("Section 3 > Definitions > Normalization > Overview", "Detailed overview."),
            ("Section 3 > Definitions > Normalization > Rules", "Normalization rules."),
            ("Section 4 > Implementation", "Implementation details."),
        ],
    )

    request = DiscoverRequest(
        mode=RetrievalMode.DETERMINISTIC,
        document_id=_id("doc_structured"),
        heading_path="Section 3 > Definitions > Normalization",
    )
    response = await retrieval_service.discover(request)

    assert response.mode == RetrievalMode.DETERMINISTIC
    assert len(response.results) == 3

    heading_paths = [h.heading_path for h in response.results]
    assert "Section 3 > Definitions > Normalization" in heading_paths
    assert "Section 3 > Definitions > Normalization > Overview" in heading_paths
    assert "Section 3 > Definitions > Normalization > Rules" in heading_paths

    # Should NOT include parent or sibling
    assert "Section 3 > Definitions" not in heading_paths
    assert "Section 4 > Implementation" not in heading_paths

    # Chunks in document order (by chunk_index)
    indices = [
        heading_paths.index("Section 3 > Definitions > Normalization"),
        heading_paths.index("Section 3 > Definitions > Normalization > Overview"),
        heading_paths.index("Section 3 > Definitions > Normalization > Rules"),
    ]
    assert indices == sorted(indices)

    # relevance_score is null for deterministic mode
    for hit in response.results:
        assert hit.relevance_score is None

    # Deterministic site previously dropped source_modified_at. The
    # from_document consolidation fixes that; assert the consumer-surface
    # round-trip. document_date is a bare YYYY-MM-DD calendar-date string —
    # the projection deliberately does not promote it to a UTC-anchored
    # datetime, because doing so would shift the wire-side calendar for
    # non-UTC consumers.
    summary = response.results[0].document
    assert summary.source_modified_at == deterministic_smt
    assert type(summary.document_date) is str
    assert summary.document_date == "2026-05-15"


# ---------------------------------------------------------------------------
# BH-030: Deterministic retrieval with non-existent heading returns 404
# ---------------------------------------------------------------------------


async def test_bh_030_deterministic_heading_not_found(
    graph_store, stub_content_store, seeded_embedding_provider, retrieval_service
):
    """Missing heading path returns 404 with heading_not_found code."""
    doc = _make_doc(_id("doc_headings"))
    await graph_store.insert_document(doc)

    await _index_doc_chunks(
        stub_content_store,
        seeded_embedding_provider,
        _id("doc_headings"),
        [("Section 1", "Some content.")],
    )

    request = DiscoverRequest(
        mode=RetrievalMode.DETERMINISTIC,
        document_id=_id("doc_headings"),
        heading_path="Nonexistent > Section",
    )
    with pytest.raises(HeadingNotFoundError) as exc_info:
        await retrieval_service.discover(request)
    assert exc_info.value.status_code == 404
    assert exc_info.value.code == "heading_not_found"


async def test_deterministic_heading_not_found_lists_available(
    graph_store, stub_content_store, seeded_embedding_provider, retrieval_service
):
    """HeadingNotFoundError detail includes available_headings for the document."""
    doc = _make_doc(_id("doc_avail"))
    await graph_store.insert_document(doc)

    await _index_doc_chunks(
        stub_content_store,
        seeded_embedding_provider,
        _id("doc_avail"),
        [
            ("ABSTRACT", "Abstract text."),
            ("CLAIMS -- Remove Before Filing", "Claim 1 content."),
            ("DETAILED DESCRIPTION", "Description text."),
            ("DETAILED DESCRIPTION > Subsection A", "Sub A content."),
        ],
    )

    request = DiscoverRequest(
        mode=RetrievalMode.DETERMINISTIC,
        document_id=_id("doc_avail"),
        heading_path="CLAIMS",
    )
    with pytest.raises(HeadingNotFoundError) as exc_info:
        await retrieval_service.discover(request)

    detail = exc_info.value.detail
    assert "available_headings" in detail
    headings = detail["available_headings"]
    assert "ABSTRACT" in headings
    assert "CLAIMS -- Remove Before Filing" in headings
    assert "DETAILED DESCRIPTION" in headings
    # Child heading should also appear in the list
    assert "DETAILED DESCRIPTION > Subsection A" in headings


# ---------------------------------------------------------------------------
# Content store: get_heading_paths
# ---------------------------------------------------------------------------


async def test_get_heading_paths_returns_distinct_ordered(
    stub_content_store, seeded_embedding_provider
):
    """get_heading_paths returns distinct heading paths in document order."""
    await _index_doc_chunks(
        stub_content_store,
        seeded_embedding_provider,
        _id("doc_hp"),
        [
            ("Overview", "Intro."),
            ("Overview", "More intro."),  # duplicate heading, two chunks
            ("Technical Description", "Tech stuff."),
            ("Technical Description > Details", "Details."),
            ("Conclusion", "End."),
        ],
    )

    paths = await stub_content_store.get_heading_paths(_id("doc_hp"))
    assert paths == [
        "Overview",
        "Technical Description",
        "Technical Description > Details",
        "Conclusion",
    ]


async def test_get_heading_paths_returns_only_authored_headings(
    stub_content_store, seeded_embedding_provider
):
    """Document-level text cannot reach heading enumeration.

    It lives on its own surface (CAS-ADR-049), so the enumeration carries no
    exclusion and there is nothing for one to catch.
    """
    await _index_doc_chunks(
        stub_content_store,
        seeded_embedding_provider,
        _id("doc_hp_synth"),
        [("Overview", "Intro."), ("Conclusion", "End.")],
    )
    await stub_content_store.upsert_document_surface(
        DocumentSurface(
            document_id=_id("doc_hp_synth"),
            matchable="Some Title",
            orienting="Abstract: prose",
        )
    )

    paths = await stub_content_store.get_heading_paths(_id("doc_hp_synth"))
    assert paths == ["Overview", "Conclusion"]


async def test_get_heading_paths_empty_document(stub_content_store):
    """get_heading_paths returns empty list for unknown document."""
    paths = await stub_content_store.get_heading_paths("nonexistent")
    assert paths == []


# ---------------------------------------------------------------------------
# Additional: semantic mode requires query
# ---------------------------------------------------------------------------


async def test_semantic_requires_query(retrieval_service):
    """Semantic mode without query raises 400."""
    request = DiscoverRequest(mode=RetrievalMode.SEMANTIC)
    with pytest.raises(MissingFieldError):
        await retrieval_service.discover(request)


# ---------------------------------------------------------------------------
# Additional: deterministic mode requires document_id and heading_path
# ---------------------------------------------------------------------------


async def test_deterministic_requires_document_id(retrieval_service):
    request = DiscoverRequest(
        mode=RetrievalMode.DETERMINISTIC,
        heading_path="Section 1",
    )
    with pytest.raises(MissingFieldError):
        await retrieval_service.discover(request)


async def test_deterministic_requires_heading_path(retrieval_service):
    request = DiscoverRequest(
        mode=RetrievalMode.DETERMINISTIC,
        document_id=_id("some_doc"),
    )
    with pytest.raises(MissingFieldError):
        await retrieval_service.discover(request)


async def test_deterministic_nonexistent_document(graph_store, retrieval_service):
    request = DiscoverRequest(
        mode=RetrievalMode.DETERMINISTIC,
        document_id=_id("nonexistent"),
        heading_path="Section 1",
    )
    with pytest.raises(DocumentNotFoundError):
        await retrieval_service.discover(request)


# ---------------------------------------------------------------------------
# Mode_parameter_mismatch fires at DiscoverRequest construction
# ---------------------------------------------------------------------------


def test_discover_request_construction_rejects_catalog_with_heading_path():
    """Catalog mode + heading_path raises ValidationError carrying a
    mode_parameter_mismatch typed error at the Pydantic model_validator,
    before any service-layer code runs. Guards the validator from being
    silently bypassed by future refactors. The validator raises
    PydanticCustomError (not the public ModeParameterMismatchError) because
    sage.models is a leaf layer per the import-linter contract; the
    sage.api.errors translator reconstructs the typed SAGEError at the
    transport boundary."""
    with pytest.raises(ValidationError) as excinfo:
        DiscoverRequest(mode=RetrievalMode.CATALOG, heading_path="X")
    errors = excinfo.value.errors()
    assert any(e["type"] == "mode_parameter_mismatch" for e in errors)
    custom_err = next(e for e in errors if e["type"] == "mode_parameter_mismatch")
    assert custom_err["ctx"]["mode"] == "catalog"
    assert custom_err["ctx"]["forbidden_param"] == "heading_path"


# ---------------------------------------------------------------------------
# mode_parameter_mismatch: the validator's message and the constrained axis
# ---------------------------------------------------------------------------
#
# The rejection is raised as a PydanticCustomError in the models layer,
# which cannot import the api layer, and is rebuilt into the public
# SAGEError by the translator in sage.api.errors. Two properties of that
# seam are pinned here: the validator's own message survives it, and the
# structured detail names the axis the constraint is actually on.
#
# Every branch of the validator gets a row. Rows carry the axis and the
# allowed set the branch is expected to report, so a branch whose ctx
# says one thing and whose message says another fails on the row rather
# than passing on a shared prefix.

_MODE = "mode"
_TARGET = "target"


class _MismatchCase(NamedTuple):
    """One rejection: what to send, and what the envelope must say."""

    branch: int
    label: str
    kwargs: dict
    axis: str
    allowed: list[str]
    forbidden_param: str
    expected_mode: str
    expected_target: str


def _case(
    branch: int,
    label: str,
    axis: str,
    allowed: list[str],
    forbidden_param: str,
    expected_mode: str = "catalog",
    expected_target: str = "documents",
    **kwargs: object,
) -> _MismatchCase:
    return _MismatchCase(
        branch=branch,
        label=label,
        kwargs=kwargs,
        axis=axis,
        allowed=allowed,
        forbidden_param=forbidden_param,
        expected_mode=expected_mode,
        expected_target=expected_target,
    )


_DOCS = RetrievalTarget.DOCUMENTS.value
_EDGES = RetrievalTarget.EDGES.value
_FACETS = RetrievalTarget.FACETS.value
_CATALOG = RetrievalMode.CATALOG.value

_MISMATCH_CASES: list[_MismatchCase] = [
    # 1. A parameter that only deterministic mode gives meaning to.
    _case(
        1,
        "heading_path outside deterministic",
        _MODE,
        [RetrievalMode.DETERMINISTIC.value],
        "heading_path",
        mode=RetrievalMode.CATALOG,
        heading_path="Section 1",
    ),
    # 2. A parameter deterministic mode has no use for.
    _case(
        2,
        "query in deterministic",
        _MODE,
        [RetrievalMode.SEMANTIC.value, RetrievalMode.KEYWORD.value],
        "query",
        expected_mode=RetrievalMode.DETERMINISTIC.value,
        mode=RetrievalMode.DETERMINISTIC,
        document_id=_id("d1"),
        heading_path="Section 1",
        query="anything",
    ),
    _case(
        2,
        "query in catalog",
        _MODE,
        [RetrievalMode.SEMANTIC.value, RetrievalMode.KEYWORD.value],
        "query",
        mode=RetrievalMode.CATALOG,
        query="anything",
    ),
    # 3. The edges target needs catalog mode.
    _case(
        3,
        "target=edges outside catalog",
        _MODE,
        [_CATALOG],
        "target",
        expected_mode=RetrievalMode.SEMANTIC.value,
        expected_target=_EDGES,
        mode=RetrievalMode.SEMANTIC,
        target=RetrievalTarget.EDGES,
        query="q",
    ),
    # 4. Document-only filter keys against the edges target. Allowed on
    # documents and on facets, which aggregates document metadata.
    _case(
        4,
        "doc-only filter key on edges",
        _TARGET,
        [_DOCS, _FACETS],
        "filters.doc_type",
        expected_target=_EDGES,
        mode=RetrievalMode.CATALOG,
        target=RetrievalTarget.EDGES,
        filters=RetrievalFilters(doc_type="ticket"),
    ),
    _case(
        4,
        "doc-only filter key on edges (tier3_metadata)",
        _TARGET,
        [_DOCS, _FACETS],
        "filters.tier3_metadata",
        expected_target=_EDGES,
        mode=RetrievalMode.CATALOG,
        target=RetrievalTarget.EDGES,
        filters=RetrievalFilters(tier3_metadata={"k": "v"}),
    ),
    # 5. Edge-only filter keys against the default documents target.
    _case(
        5,
        "edge-only filter key on documents",
        _TARGET,
        [_EDGES],
        "filters.source_id",
        mode=RetrievalMode.CATALOG,
        filters=RetrievalFilters(source_id=_id("d1")),
    ),
    # 6. Facet-only parameters against a non-facets target.
    _case(
        6,
        "facet_fields off the facets target",
        _TARGET,
        [_FACETS],
        "facet_fields",
        mode=RetrievalMode.CATALOG,
        facet_fields=[FacetField.TAGS],
    ),
    _case(
        6,
        "facet_value_limit off the facets target",
        _TARGET,
        [_FACETS],
        "facet_value_limit",
        mode=RetrievalMode.CATALOG,
        facet_value_limit=5,
    ),
    # 7. Document-only request parameters against the edges target.
    _case(
        7,
        "min_relevance on edges",
        _TARGET,
        [_DOCS],
        "min_relevance",
        expected_target=_EDGES,
        mode=RetrievalMode.CATALOG,
        target=RetrievalTarget.EDGES,
        min_relevance=0.5,
    ),
    _case(
        7,
        "include_abstracts on edges",
        _TARGET,
        [_DOCS],
        "include_abstracts",
        expected_target=_EDGES,
        mode=RetrievalMode.CATALOG,
        target=RetrievalTarget.EDGES,
        include_abstracts=True,
    ),
    # 8. The facets target needs catalog mode.
    _case(
        8,
        "target=facets outside catalog",
        _MODE,
        [_CATALOG],
        "target",
        expected_mode=RetrievalMode.KEYWORD.value,
        expected_target=_FACETS,
        mode=RetrievalMode.KEYWORD,
        target=RetrievalTarget.FACETS,
        query="q",
    ),
    # 9. Edge-only filter keys against the facets target.
    _case(
        9,
        "edge-only filter key on facets",
        _TARGET,
        [_EDGES],
        "filters.edge_type",
        expected_target=_FACETS,
        mode=RetrievalMode.CATALOG,
        target=RetrievalTarget.FACETS,
        filters=RetrievalFilters(edge_type=EdgeType.REFERENCES),
    ),
    # 10. Parameters the facets target has nothing to apply them to. The
    # seven document-only ones are rejected on edges as well, so only
    # documents remains; the three pagination and payload-shape knobs are
    # not, so edges remains open to them.
    _case(
        10,
        "sort_by on facets",
        _TARGET,
        [_DOCS],
        "sort_by",
        expected_target=_FACETS,
        mode=RetrievalMode.CATALOG,
        target=RetrievalTarget.FACETS,
        sort_by="title",
    ),
    _case(
        10,
        "document_id on facets",
        _TARGET,
        [_DOCS],
        "document_id",
        expected_target=_FACETS,
        mode=RetrievalMode.CATALOG,
        target=RetrievalTarget.FACETS,
        document_id=_id("d1"),
    ),
    _case(
        10,
        "limit on facets",
        _TARGET,
        [_DOCS, _EDGES],
        "limit",
        expected_target=_FACETS,
        mode=RetrievalMode.CATALOG,
        target=RetrievalTarget.FACETS,
        limit=5,
    ),
    _case(
        10,
        "response_mode on facets",
        _TARGET,
        [_DOCS, _EDGES],
        "response_mode",
        expected_target=_FACETS,
        mode=RetrievalMode.CATALOG,
        target=RetrievalTarget.FACETS,
        response_mode=ResponseMode.LIGHT,
    ),
]

_MISMATCH_IDS = [f"{c.branch}-{c.label}" for c in _MISMATCH_CASES]


def _reject(case: _MismatchCase) -> tuple[dict, ModeParameterMismatchError]:
    """Trip one branch and return the raw Pydantic error beside the
    public error the boundary translator builds from it."""
    with pytest.raises(ValidationError) as info:
        DiscoverRequest(**case.kwargs)
    raw = next(e for e in info.value.errors() if e["type"] == "mode_parameter_mismatch")
    translated = translate_validation_error(info.value)
    assert isinstance(translated, ModeParameterMismatchError), (
        f"{case.label}: the translator returned {translated!r}, so the "
        "rejection would reach the caller through the untyped fallback path."
    )
    return raw, translated


@pytest.mark.parametrize("case", _MISMATCH_CASES, ids=_MISMATCH_IDS)
def test_mode_parameter_mismatch_delivers_the_validators_message(case):
    """The message a caller receives is the one the validator wrote.

    The expected value is read back from the validator on this run
    rather than restated here. A second copy of the string would go
    stale silently, and -- the point of the assertion -- would still
    pass for a translator that re-synthesized a message that happened
    to match the one branch the copy was taken from. Comparing against
    the validator's own rendering holds for every branch and for
    branches not yet written.
    """
    raw, translated = _reject(case)
    assert translated.message == raw["msg"]


@pytest.mark.parametrize("case", _MISMATCH_CASES, ids=_MISMATCH_IDS)
def test_mode_parameter_mismatch_detail_names_the_constrained_axis(case):
    """The structured detail reports the axis the constraint is on.

    A caller reading `detail` rather than `message` learns the same
    thing: both axes' current values, and the allowed set for whichever
    one the branch constrains. Reporting `allowed_modes` on a branch
    constrained by `target` would name a set the caller cannot act on.
    """
    _, translated = _reject(case)
    detail = translated.detail

    assert detail["mode"] == case.expected_mode
    assert detail["target"] == case.expected_target
    assert detail["forbidden_param"] == case.forbidden_param

    axis_keys = {"allowed_modes", "allowed_targets"}
    present = axis_keys & set(detail)
    expected_key = f"allowed_{case.axis}s"
    assert present == {expected_key}, (
        f"{case.label}: constraint is on the {case.axis} axis, so detail "
        f"must carry {expected_key} and nothing from the other axis; "
        f"got {sorted(present)}."
    )
    assert detail[expected_key] == sorted(case.allowed)

    # `key` is an input to a message template, not part of the published
    # contract. It rides the validator's ctx on the filter-key branches
    # and must not leak into the envelope.
    assert "key" not in detail


@pytest.mark.parametrize("case", _MISMATCH_CASES, ids=_MISMATCH_IDS)
def test_mode_parameter_mismatch_message_is_not_the_re_synthesized_form(case):
    """The delivered message is not one composed from the detail fields.

    Without this, the assertion that the message equals the validator's
    could pass on a coincidence: a re-synthesized sentence that happened
    to match what the validator wrote. No branch's message coincides
    with the composed form today, so the check is a real trap rather
    than a restatement -- and it holds for the mode-constrained branches
    too, whose detail is otherwise unchanged.
    """
    _, translated = _reject(case)
    detail = translated.detail
    param = detail["forbidden_param"]
    # Composed from what was actually delivered, not from what this row
    # expects. A trap built from the row's own expectations would not
    # fire on a translator that composed from a detail of a different
    # shape -- which is exactly the shape the unfixed translator sends.
    allowed = detail.get("allowed_modes") or detail.get("allowed_targets") or []
    composed = (
        f"Parameter {param!r} is not valid for mode {detail['mode']!r}. "
        f"Allowed modes for {param!r}: {sorted(allowed)!r}"
    )
    assert translated.message != composed


def test_mode_parameter_mismatch_cases_cover_every_branch():
    """Every rejection site in the validator has at least one case.

    The three tests above are only as good as the table that drives
    them, and a branch added later would otherwise be uncovered in
    silence. Counted from the validator's own source so the pin cannot
    drift from what is there.
    """
    source = inspect.getsource(DiscoverRequest._reject_mode_parameter_mismatch)
    # Each rejection site opens with the error type as a quoted first
    # argument. The docstring names the same code in prose, which this
    # form does not match.
    sites = source.count('"mode_parameter_mismatch",')
    covered = {case.branch for case in _MISMATCH_CASES}
    assert sites > 0, "the rejection-site count has gone stale; it matched nothing"
    assert covered == set(range(1, sites + 1)), (
        f"the validator raises at {sites} sites; the case table covers branches {sorted(covered)}."
    )


# ---------------------------------------------------------------------------
# Additional: scope gating
# ---------------------------------------------------------------------------


async def test_authoritative_scope_filters(
    graph_store, stub_content_store, seeded_embedding_provider, retrieval_service
):
    """Authoritative scope excludes documents without authority_scope."""
    doc_auth = _make_doc(_id("doc_auth"), authority_scope="example_vault")
    doc_plain = _make_doc(_id("doc_plain"))
    await graph_store.insert_document(doc_auth)
    await graph_store.insert_document(doc_plain)

    await _index_doc_chunks(
        stub_content_store,
        seeded_embedding_provider,
        _id("doc_auth"),
        [("Section 1", "Authoritative report content.")],
    )
    await _index_doc_chunks(
        stub_content_store,
        seeded_embedding_provider,
        _id("doc_plain"),
        [("Section 1", "Non-authoritative report content.")],
    )

    request = DiscoverRequest(
        mode=RetrievalMode.SEMANTIC,
        query="report content",
        scope=RetrievalScope.AUTHORITATIVE,
    )
    response = await retrieval_service.discover(request)

    doc_ids = [h.document.id for h in response.results]
    assert _id("doc_auth") in doc_ids
    assert _id("doc_plain") not in doc_ids


async def test_filter_by_project(
    graph_store, stub_content_store, seeded_embedding_provider, retrieval_service
):
    """Filters narrow results to matching project."""
    doc_pim = _make_doc(_id("doc_pim"), project="example_vault")
    doc_other = _make_doc(_id("doc_other"), project="basketball")
    await graph_store.insert_document(doc_pim)
    await graph_store.insert_document(doc_other)

    await _index_doc_chunks(
        stub_content_store,
        seeded_embedding_provider,
        _id("doc_pim"),
        [("Section 1", "Report filing process documentation.")],
        project="example_vault",
    )
    await _index_doc_chunks(
        stub_content_store,
        seeded_embedding_provider,
        _id("doc_other"),
        [("Section 1", "Report filing process documentation.")],
        project="basketball",
    )

    request = DiscoverRequest(
        mode=RetrievalMode.SEMANTIC,
        query="report filing",
        filters=RetrievalFilters(project="example_vault"),
    )
    response = await retrieval_service.discover(request)

    doc_ids = [h.document.id for h in response.results]
    assert _id("doc_pim") in doc_ids
    assert _id("doc_other") not in doc_ids


# ---------------------------------------------------------------------------
# BH-058: a document's identifying terms are reachable without being in a body
# ---------------------------------------------------------------------------


async def test_bh_058_document_identity_is_reachable_off_the_passage_surface(
    graph_store, stub_content_store, seeded_embedding_provider, retrieval_service
):
    """A document is reachable by an identifying term its body never carries.

    The behaviour this pins is older than the mechanism that serves it. It was
    once served by a "search preamble" -- the title, source filename and tags
    prepended to the document's first chunk at indexing time -- and this test
    hand-wrote that preamble into a chunk body, so it went on passing after
    CAS-ADR-049 abolished the arrangement, on text nothing in production
    writes. Passages now hold authored passages only (Decision 1) and
    document-level text has a surface of its own (Decision 2).

    Seeded through the production composer, so the fixture cannot drift from
    what ingest actually stores.

    The code is carried by an authored tag. It is also in the filename, as the
    original report had it, and that route is now closed: a filename is derived
    text and admits nothing (Decision 4), which is the subject of
    ``test_filename_stem_does_not_admit_a_document_through_the_boost`` below.
    The document has no passages at all, so the surface is the only thing that
    can answer -- and the assertion is on the document-level hit rather than on
    the document, because on a corpus this small a semantic query returns
    nearly everything and the weaker assertion would pass against a store that
    never wrote a surface.
    """
    doc = _make_doc(_id("doc_pv07"))
    doc.title = "ClinicalNormalization"
    doc.source_path = "imports/EXAMPLE_PV07_ClinicalNormalization_v1_0.md"
    doc.tags = ["PV07"]
    await graph_store.insert_document(doc)
    await _index_document_surface(
        stub_content_store, seeded_embedding_provider, _id("doc_pv07"), doc
    )

    response = await retrieval_service.discover(
        DiscoverRequest(mode=RetrievalMode.SEMANTIC, query="PV07", use_hybrid=True)
    )

    assert _document_level_hit(response, _id("doc_pv07")) is not None, (
        "the document surface did not carry the document's identity"
    )


# ---------------------------------------------------------------------------
# Documents whose identifying terms live in title/semantic_abstract
# or in CamelCase compounds are discoverable via the synthetic header chunk
# ---------------------------------------------------------------------------


async def test_camelcase_title_searchable_via_split_tokens(
    graph_store, stub_content_store, seeded_embedding_provider, retrieval_service
):
    """A document whose title is a CamelCase compound and whose body is
    sparse placeholder content is retrievable by natural-language queries
    against the constituent words. The document surface's authored half
    carries the compound's case-split expansion, which is what lets
    "dashboard" reach "PortfolioDashboard" (CAS-ADR-049)."""
    doc = _make_doc(_id("doc_portfolio"))
    doc.title = "PortfolioDashboard_Template"
    doc.source_path = "imports/2026-05-11_EXAMPLE_REF_PortfolioDashboard_Template_v3.xlsx"
    doc.tags = ["REF", "template"]
    doc.semantic_abstract = (
        "Authoritative template for the Engineering Portfolio Dashboard "
        "with rows per report and columns per filing pipeline stage."
    )
    await graph_store.insert_document(doc)

    # No passage at all. The document surface is then the only thing that can
    # answer, so the document-level hit below is evidence about the surface
    # rather than about a body chunk that would have been returned anyway.
    # Its authored half carries the case-split expansion of the compound
    # title, which is what makes "dashboard" reach "PortfolioDashboard".
    await _index_document_surface(
        stub_content_store, seeded_embedding_provider, _id("doc_portfolio"), doc
    )

    # Add a noise document so the assertion is non-trivial.
    doc_noise = _make_doc(_id("doc_noise"))
    doc_noise.title = "ClinicalNormalization"
    await graph_store.insert_document(doc_noise)
    await _index_doc_chunks(
        stub_content_store,
        seeded_embedding_provider,
        _id("doc_noise"),
        [("Section 1", "Discusses cryptographic accumulator seals.")],
    )

    # The diagnostic query: should land doc_portfolio in semantic and hybrid
    # modes. No keyword arm here: the case-split identifier line is derived
    # text, which ranks and orients but never satisfies a keyword match
    # (CAS-ADR-049), and "dashboard" also sits in the title, so the metadata
    # boost would surface the document whatever the keyword arm did. The
    # refusal is asserted where nothing else can supply the term, in
    # test_semantic_abstract_drives_a_semantic_match below.
    for mode_kwargs in (
        {
            "mode": RetrievalMode.SEMANTIC,
            "query": "dashboard template",
            "use_hybrid": False,
        },
        {
            "mode": RetrievalMode.SEMANTIC,
            "query": "dashboard template",
            "use_hybrid": True,
        },
    ):
        request = DiscoverRequest(**mode_kwargs)
        response = await retrieval_service.discover(request)
        doc_ids = [h.document.id for h in response.results[:5]]
        assert _id("doc_portfolio") in doc_ids, (
            f"doc_portfolio missing from top 5 for {mode_kwargs!r}; got {doc_ids}"
        )
        # The hit itself is the evidence: it carries no excerpt to inspect,
        # because a document-level hit is not a passage (CAS-ADR-049), and the
        # document has no passage that could have supplied one instead.
        hit = _document_level_hit(response, _id("doc_portfolio"))
        assert hit is not None, (
            f"no document-level hit for {mode_kwargs!r}; the document surface "
            "contributed nothing to this result"
        )


async def test_semantic_abstract_drives_a_semantic_match(
    graph_store, stub_content_store, seeded_embedding_provider, retrieval_service
):
    """A document with sparse body but a descriptive semantic_abstract is
    retrievable against the abstract's terms -- because the abstract is
    carried by the document surface, which the semantic arm covers.

    Semantic, not keyword. A generated abstract is derived text: it ranks and
    orients a document but never satisfies a keyword match (CAS-ADR-049). The
    keyword arm below asserts that refusal end to end. The terms live in the
    abstract alone -- the title is a bare year-suffixed word and the tags and
    source are empty -- so nothing but the document surface's derived half
    could supply them, and the metadata boost has nothing to find either."""
    doc = _make_doc(_id("doc_abstract_only"))
    doc.title = "Catalog_2026"
    doc.semantic_abstract = (
        "Cryptographic accumulator seals govern every commit boundary in this catalog."
    )
    await graph_store.insert_document(doc)

    # No passage, so nothing but the document surface can answer for this
    # document and the document-level hit below is evidence about it.
    await _index_document_surface(
        stub_content_store, seeded_embedding_provider, _id("doc_abstract_only"), doc
    )

    request = DiscoverRequest(
        mode=RetrievalMode.SEMANTIC,
        query="cryptographic accumulator",
    )
    response = await retrieval_service.discover(request)
    doc_ids = [h.document.id for h in response.results]
    assert _id("doc_abstract_only") in doc_ids

    # The abstract is carried by the document surface and nowhere else, and
    # the document has no passage, so the hit can only have come from the
    # surface. It carries no excerpt to read the abstract back from -- a
    # document-level hit is not a passage (CAS-ADR-049) -- so its presence,
    # and the absence of any other row that could have produced it, is the
    # evidence.
    hit = _document_level_hit(response, _id("doc_abstract_only"))
    assert hit is not None, "the abstract's document-level hit is absent"

    # A sibling whose authored body carries the same terms, so the refusal
    # below is a statement about provenance rather than about a keyword arm
    # that answers nothing for this fixture.
    doc_authored = _make_doc(_id("doc_authored_seals"))
    await graph_store.insert_document(doc_authored)
    await _index_doc_chunks(
        stub_content_store,
        seeded_embedding_provider,
        _id("doc_authored_seals"),
        [("Section 1", "Cryptographic accumulator seals in the body text.")],
    )

    keyword = await retrieval_service.discover(
        DiscoverRequest(mode=RetrievalMode.KEYWORD, query="cryptographic accumulator")
    )
    keyword_ids = [h.document.id for h in keyword.results]
    assert _id("doc_authored_seals") in keyword_ids, (
        "positive control: the same terms in authored text do match"
    )
    assert _id("doc_abstract_only") not in keyword_ids, (
        "the abstract orients the document but does not make it match; these "
        "terms are carried only by the document surface's derived half"
    )


async def test_document_level_hit_carries_no_heading_path(
    graph_store, stub_content_store, seeded_embedding_provider, retrieval_service
):
    """A hit won by document-level text reports no heading path.

    There is no internal sentinel to mask any more: document-level text is not
    a passage and carries no heading, so the hit's heading_path is simply
    absent rather than a marker string a caller must never see
    (CAS-ADR-049). Exercised in semantic mode, the arm where a document-level
    row can outrank a passage.
    """
    doc = _make_doc(_id("doc_mask"))
    doc.title = "MaskProbe_Template"
    doc.semantic_abstract = "probe content"
    await graph_store.insert_document(doc)

    # No passages: the document's only indexed text is document-level, so the
    # hit under test can only be the document-level one. With a body chunk the
    # service's per-document dedup may return the passage instead, and the
    # assertion would then say nothing about how a document-level hit reports
    # its heading path.
    await _index_document_surface(
        stub_content_store, seeded_embedding_provider, _id("doc_mask"), doc
    )

    request = DiscoverRequest(mode=RetrievalMode.SEMANTIC, query="probe template")
    response = await retrieval_service.discover(request)

    hits = [hit for hit in response.results if hit.document.id == _id("doc_mask")]
    assert hits, "doc_mask was not in results"
    assert _document_level_hit(response, _id("doc_mask")) is not None, (
        "no document-level hit was returned, so this asserts nothing about how "
        "one reports its heading path"
    )
    # Asserted as a property rather than as a set of permitted values: which
    # surface wins this query is a ranking outcome, and enumerating both
    # outcomes would pass against an implementation that leaked a marker on
    # either one. What must hold on every outcome is that nothing internal
    # reaches the caller.
    assert not (hits[0].heading_path or "").startswith("__"), (
        f"internal marker {hits[0].heading_path!r} reached a caller"
    )


# ---------------------------------------------------------------------------
# BH-059: Keyword-only retrieval mode uses BM25 without embedding
# ---------------------------------------------------------------------------


async def test_bh_059_keyword_mode_bm25_only(
    graph_store, stub_content_store, seeded_embedding_provider, retrieval_service
):
    """Keyword mode returns BM25 matches without requiring query embedding."""
    doc_match = _make_doc(_id("doc_match"))
    doc_nomatch = _make_doc(_id("doc_nomatch"))
    await graph_store.insert_document(doc_match)
    await graph_store.insert_document(doc_nomatch)

    await _index_doc_chunks(
        stub_content_store,
        seeded_embedding_provider,
        _id("doc_match"),
        [("Section 1", "Title: PV07_Report\n\nDetailed analysis of PV07 claims.")],
    )
    await _index_doc_chunks(
        stub_content_store,
        seeded_embedding_provider,
        _id("doc_nomatch"),
        [("Section 1", "Title: Gardening_Guide\n\nTips for spring planting.")],
    )

    request = DiscoverRequest(
        mode=RetrievalMode.KEYWORD,
        query="PV07",
    )
    response = await retrieval_service.discover(request)

    assert response.mode == RetrievalMode.KEYWORD
    doc_ids = [h.document.id for h in response.results]
    assert _id("doc_match") in doc_ids
    assert _id("doc_nomatch") not in doc_ids


# ---------------------------------------------------------------------------
# BH-060: Keyword mode requires query field
# ---------------------------------------------------------------------------


async def test_bh_060_keyword_requires_query(retrieval_service):
    """Keyword mode without query raises MissingFieldError."""
    request = DiscoverRequest(mode=RetrievalMode.KEYWORD)
    with pytest.raises(MissingFieldError):
        await retrieval_service.discover(request)


# ---------------------------------------------------------------------------
# BH-061: Keyword mode excludes failed-pipeline documents
# ---------------------------------------------------------------------------


async def test_bh_061_keyword_excludes_failed_pipeline(
    graph_store, stub_content_store, seeded_embedding_provider, retrieval_service
):
    """Failed-pipeline documents are excluded from keyword mode results."""
    doc_ok = _make_doc(_id("doc_ok_kw"))
    doc_failed = _make_doc(_id("doc_failed_kw"), pipeline_status=PipelineStatus.FAILED)
    doc_failed.pipeline_error = "LLM unavailable"
    await graph_store.insert_document(doc_ok)
    await graph_store.insert_document(doc_failed)

    await _index_doc_chunks(
        stub_content_store,
        seeded_embedding_provider,
        _id("doc_ok_kw"),
        [("Section 1", "Report claims analysis for PV07.")],
    )
    await _index_doc_chunks(
        stub_content_store,
        seeded_embedding_provider,
        _id("doc_failed_kw"),
        [("Section 1", "Report claims analysis for PV07.")],
    )

    request = DiscoverRequest(
        mode=RetrievalMode.KEYWORD,
        query="PV07",
    )
    response = await retrieval_service.discover(request)

    doc_ids = [h.document.id for h in response.results]
    assert _id("doc_ok_kw") in doc_ids
    assert _id("doc_failed_kw") not in doc_ids


# ---------------------------------------------------------------------------
# Explicit pipeline_status="failed" filter is honored end-to-end
# ---------------------------------------------------------------------------


async def test_explicit_failed_filter_returns_failed_doc_semantic(
    graph_store, stub_content_store, seeded_embedding_provider, retrieval_service
):
    """Explicit pipeline_status=failed filter must surface failed docs in semantic mode.

    The storage layer's default_exclude_failed steps aside when the caller passes an
    explicit pipeline_status filter; the service-layer post-filter at _results_to_hits
    must mirror that behavior, otherwise the documented override path is silently
    re-gated on the way out. Both docs index chunks (anti-coincidental-pass: without
    chunks for the failed doc, LanceDB returns nothing and the test would pass for
    the wrong reason).
    """
    doc_ok = _make_doc(_id("doc_ok_sem_exp"))
    doc_failed = _make_doc(_id("doc_failed_sem_exp"), pipeline_status=PipelineStatus.FAILED)
    doc_failed.pipeline_error = "LLM unavailable"
    await graph_store.insert_document(doc_ok)
    await graph_store.insert_document(doc_failed)

    await _index_doc_chunks(
        stub_content_store,
        seeded_embedding_provider,
        _id("doc_ok_sem_exp"),
        [("Section 1", "This document discusses report claims and prior art.")],
    )
    await _index_doc_chunks(
        stub_content_store,
        seeded_embedding_provider,
        _id("doc_failed_sem_exp"),
        [("Section 1", "This document discusses report claims and prior art.")],
    )

    request = DiscoverRequest(
        mode=RetrievalMode.SEMANTIC,
        query="report claims",
        filters=RetrievalFilters(pipeline_status="failed"),
    )
    response = await retrieval_service.discover(request)

    doc_ids = [h.document.id for h in response.results]
    assert _id("doc_failed_sem_exp") in doc_ids
    assert _id("doc_ok_sem_exp") not in doc_ids


async def test_explicit_failed_filter_returns_failed_doc_keyword(
    graph_store, stub_content_store, seeded_embedding_provider, retrieval_service
):
    """Explicit pipeline_status=failed filter is honored in non-wildcard keyword mode.

    Mirrors BH-061 setup but flips the assertion: the caller asked for failed docs and
    must get them. Exercises the _results_to_hits site on the keyword scoring path.
    """
    doc_ok = _make_doc(_id("doc_ok_kw_exp"))
    doc_failed = _make_doc(_id("doc_failed_kw_exp"), pipeline_status=PipelineStatus.FAILED)
    doc_failed.pipeline_error = "LLM unavailable"
    await graph_store.insert_document(doc_ok)
    await graph_store.insert_document(doc_failed)

    await _index_doc_chunks(
        stub_content_store,
        seeded_embedding_provider,
        _id("doc_ok_kw_exp"),
        [("Section 1", "Report claims analysis for PV07.")],
    )
    await _index_doc_chunks(
        stub_content_store,
        seeded_embedding_provider,
        _id("doc_failed_kw_exp"),
        [("Section 1", "Report claims analysis for PV07.")],
    )

    request = DiscoverRequest(
        mode=RetrievalMode.KEYWORD,
        query="PV07",
        filters=RetrievalFilters(pipeline_status="failed"),
    )
    response = await retrieval_service.discover(request)

    doc_ids = [h.document.id for h in response.results]
    assert _id("doc_failed_kw_exp") in doc_ids
    assert _id("doc_ok_kw_exp") not in doc_ids


async def test_explicit_failed_filter_returns_failed_doc_keyword_wildcard(
    graph_store, stub_content_store, seeded_embedding_provider, retrieval_service
):
    """Explicit pipeline_status=failed filter is honored in keyword wildcard mode.

    query="*" routes through _list_filtered (the dashboard drill-down path), which has
    its own redundant Python post-filter. The explicit filter must override that gate
    just as it overrides the SQL default at the storage layer.
    """
    doc_ok = _make_doc(_id("doc_ok_wc_exp"))
    doc_failed = _make_doc(_id("doc_failed_wc_exp"), pipeline_status=PipelineStatus.FAILED)
    doc_failed.pipeline_error = "LLM unavailable"
    await graph_store.insert_document(doc_ok)
    await graph_store.insert_document(doc_failed)

    await _index_doc_chunks(
        stub_content_store,
        seeded_embedding_provider,
        _id("doc_ok_wc_exp"),
        [("Section 1", "Report claims analysis.")],
    )
    await _index_doc_chunks(
        stub_content_store,
        seeded_embedding_provider,
        _id("doc_failed_wc_exp"),
        [("Section 1", "Report claims analysis.")],
    )

    request = DiscoverRequest(
        mode=RetrievalMode.KEYWORD,
        query="*",
        filters=RetrievalFilters(pipeline_status="failed"),
    )
    response = await retrieval_service.discover(request)

    doc_ids = [h.document.id for h in response.results]
    assert _id("doc_failed_wc_exp") in doc_ids
    assert _id("doc_ok_wc_exp") not in doc_ids


async def test_explicit_failed_filter_returns_failed_doc_metadata_boost(
    graph_store, stub_content_store, seeded_embedding_provider, retrieval_service
):
    """Explicit pipeline_status=failed filter is honored at the metadata-boost site.

    Targets the third post-filter site (_boost_metadata_matches). To exercise it
    specifically, the test uses a query that matches only the failed doc's metadata
    (title/source_path) but does NOT match any chunk content. The chunk-search path
    returns nothing, so the failed doc can only enter the results via the
    metadata-boost path -- which is the site under test. Without the gate at that
    site, the boost path silently drops the failed doc and the assertion fails.
    """
    doc_ok = _make_doc(_id("doc_ok_boost"))
    doc_failed = _make_doc(_id("doc_failed_boost"), pipeline_status=PipelineStatus.FAILED)
    doc_failed.pipeline_error = "LLM unavailable"
    await graph_store.insert_document(doc_ok)
    await graph_store.insert_document(doc_failed)

    # Chunk content deliberately does NOT contain the query string, so BM25 has
    # nothing to return. The failed doc must reach results via _boost_metadata_matches
    # (which searches title/source_path/tags) or not at all.
    await _index_doc_chunks(
        stub_content_store,
        seeded_embedding_provider,
        _id("doc_ok_boost"),
        [("Section 1", "Report claims analysis.")],
    )
    await _index_doc_chunks(
        stub_content_store,
        seeded_embedding_provider,
        _id("doc_failed_boost"),
        [("Section 1", "Report claims analysis.")],
    )

    # Query matches the failed doc's title ("Test <sha>_doc_failed_boost") and
    # source_path ("test/<sha>_doc_failed_boost.md") via SQL LIKE in
    # search_metadata; does not appear in any chunk content.
    request = DiscoverRequest(
        mode=RetrievalMode.KEYWORD,
        query="failed_boost",
        filters=RetrievalFilters(pipeline_status="failed"),
    )
    response = await retrieval_service.discover(request)

    doc_ids = [h.document.id for h in response.results]
    assert _id("doc_failed_boost") in doc_ids


# ---------------------------------------------------------------------------
# BH-069: Active lifecycle tier sort -- active always ranks above non-active
# ---------------------------------------------------------------------------


async def test_bh_069_active_lifecycle_ranks_higher(
    graph_store, stub_content_store, seeded_embedding_provider, retrieval_service
):
    """Active documents always rank above non-active via lifecycle tier sort,
    even when the non-active document has a higher content relevance score.
    """
    doc_active = _make_doc(_id("doc_active"), lifecycle_status="active")
    doc_completed = _make_doc(_id("doc_completed"), lifecycle_status="completed")
    doc_archived = _make_doc(_id("doc_archived"), lifecycle_status="archived")
    await graph_store.insert_document(doc_active)
    await graph_store.insert_document(doc_completed)
    await graph_store.insert_document(doc_archived)

    # Give the archived doc MORE matching terms so it scores higher in BM25.
    # The tier sort must still place the active doc first.
    active_content = "Report filing process."
    archived_content = "Report filing process for clinical normalization review."
    completed_content = "Report filing process for clinical normalization."
    await _index_doc_chunks(
        stub_content_store,
        seeded_embedding_provider,
        _id("doc_active"),
        [("Section 1", active_content)],
    )
    await _index_doc_chunks(
        stub_content_store,
        seeded_embedding_provider,
        _id("doc_archived"),
        [("Section 1", archived_content)],
    )
    await _index_doc_chunks(
        stub_content_store,
        seeded_embedding_provider,
        _id("doc_completed"),
        [("Section 1", completed_content)],
    )

    request = DiscoverRequest(
        mode=RetrievalMode.SEMANTIC,
        query="report filing clinical normalization",
    )
    response = await retrieval_service.discover(request)

    doc_ids = [h.document.id for h in response.results]
    assert _id("doc_active") in doc_ids
    assert _id("doc_completed") in doc_ids
    assert _id("doc_archived") in doc_ids

    # Active document must appear before both non-active documents
    active_idx = doc_ids.index(_id("doc_active"))
    completed_idx = doc_ids.index(_id("doc_completed"))
    archived_idx = doc_ids.index(_id("doc_archived"))
    assert active_idx < completed_idx, (
        f"Active ({active_idx}) should rank above completed ({completed_idx})"
    )
    assert active_idx < archived_idx, (
        f"Active ({active_idx}) should rank above archived ({archived_idx})"
    )


async def test_bh_069_active_boost_applies_to_keyword_mode(
    graph_store, stub_content_store, seeded_embedding_provider, retrieval_service
):
    """Lifecycle boost also applies in keyword mode."""
    doc_active = _make_doc(_id("doc_kw_active"), lifecycle_status="active")
    doc_archived = _make_doc(_id("doc_kw_archived"), lifecycle_status="archived")
    await graph_store.insert_document(doc_active)
    await graph_store.insert_document(doc_archived)

    identical_content = "Detailed analysis of PV07 claims and prior art."
    for doc_id in [_id("doc_kw_active"), _id("doc_kw_archived")]:
        await _index_doc_chunks(
            stub_content_store,
            seeded_embedding_provider,
            doc_id,
            [("Section 1", identical_content)],
        )

    request = DiscoverRequest(
        mode=RetrievalMode.KEYWORD,
        query="PV07 claims",
    )
    response = await retrieval_service.discover(request)

    doc_ids = [h.document.id for h in response.results]
    assert _id("doc_kw_active") in doc_ids
    assert _id("doc_kw_archived") in doc_ids
    assert doc_ids.index(_id("doc_kw_active")) < doc_ids.index(_id("doc_kw_archived"))


async def test_bh_069_deterministic_mode_unaffected(
    graph_store, stub_content_store, seeded_embedding_provider, retrieval_service
):
    """Deterministic mode does not apply salience reranking (no relevance scores)."""
    doc = _make_doc(_id("doc_det"), lifecycle_status="archived")
    await graph_store.insert_document(doc)

    await _index_doc_chunks(
        stub_content_store,
        seeded_embedding_provider,
        _id("doc_det"),
        [("Section 1", "Some content."), ("Section 2", "More content.")],
    )

    request = DiscoverRequest(
        mode=RetrievalMode.DETERMINISTIC,
        document_id=_id("doc_det"),
        heading_path="Section 1",
    )
    response = await retrieval_service.discover(request)

    # Deterministic results have no relevance scores -- salience rerank is skipped
    for hit in response.results:
        assert hit.relevance_score is None


# ---------------------------------------------------------------------------
# BH-070: Recent documents rank above older documents in semantic search
# ---------------------------------------------------------------------------


async def test_bh_070_recent_documents_rank_higher(
    graph_store, stub_content_store, seeded_embedding_provider, retrieval_service
):
    """Documents with more recent document_date rank above older ones
    when content relevance is otherwise equal.
    """
    now = datetime.now(timezone.utc)
    doc_recent = _make_doc(
        _id("doc_recent"),
        document_date="2026-04-01",
        source_modified_at=now - timedelta(days=5),
    )
    doc_old = _make_doc(
        _id("doc_old"),
        document_date="2024-01-15",
        source_modified_at=now - timedelta(days=800),
    )
    await graph_store.insert_document(doc_recent)
    await graph_store.insert_document(doc_old)

    identical_content = "Report filing process for clinical normalization."
    for doc_id in [_id("doc_recent"), _id("doc_old")]:
        await _index_doc_chunks(
            stub_content_store,
            seeded_embedding_provider,
            doc_id,
            [("Section 1", identical_content)],
        )

    request = DiscoverRequest(
        mode=RetrievalMode.SEMANTIC,
        query="report filing clinical normalization",
    )
    response = await retrieval_service.discover(request)

    doc_ids = [h.document.id for h in response.results]
    assert _id("doc_recent") in doc_ids
    assert _id("doc_old") in doc_ids
    assert doc_ids.index(_id("doc_recent")) < doc_ids.index(_id("doc_old")), (
        "Recent document should rank above older document"
    )


async def test_bh_070_recency_uses_source_modified_at_fallback(
    graph_store, stub_content_store, seeded_embedding_provider, retrieval_service
):
    """When document_date is null, recency boost falls back to source_modified_at."""
    now = datetime.now(timezone.utc)
    doc_recent = _make_doc(
        _id("doc_recent_fb"),
        document_date=None,
        source_modified_at=now - timedelta(days=3),
    )
    doc_old = _make_doc(
        _id("doc_old_fb"),
        document_date=None,
        source_modified_at=now - timedelta(days=900),
    )
    await graph_store.insert_document(doc_recent)
    await graph_store.insert_document(doc_old)

    identical_content = "Report filing process for clinical normalization."
    for doc_id in [_id("doc_recent_fb"), _id("doc_old_fb")]:
        await _index_doc_chunks(
            stub_content_store,
            seeded_embedding_provider,
            doc_id,
            [("Section 1", identical_content)],
        )

    request = DiscoverRequest(
        mode=RetrievalMode.SEMANTIC,
        query="report filing clinical normalization",
    )
    response = await retrieval_service.discover(request)

    doc_ids = [h.document.id for h in response.results]
    assert doc_ids.index(_id("doc_recent_fb")) < doc_ids.index(_id("doc_old_fb")), (
        "Recent source_modified_at should rank above older when document_date is null"
    )


async def test_bh_070_no_date_documents_not_penalized(
    graph_store, stub_content_store, seeded_embedding_provider, retrieval_service
):
    """Documents with no date information still appear in results (no crash,
    no artificial penalty that would push them below relevance threshold).
    """
    doc_dated = _make_doc(_id("doc_dated"), document_date="2026-03-01")
    doc_undated = _make_doc(_id("doc_undated"))  # no document_date, no source_modified_at
    await graph_store.insert_document(doc_dated)
    await graph_store.insert_document(doc_undated)

    identical_content = "Report filing process for clinical normalization."
    for doc_id in [_id("doc_dated"), _id("doc_undated")]:
        await _index_doc_chunks(
            stub_content_store,
            seeded_embedding_provider,
            doc_id,
            [("Section 1", identical_content)],
        )

    request = DiscoverRequest(
        mode=RetrievalMode.SEMANTIC,
        query="report filing clinical normalization",
    )
    response = await retrieval_service.discover(request)

    doc_ids = [h.document.id for h in response.results]
    # Both documents must appear -- undated is not excluded
    assert _id("doc_dated") in doc_ids
    assert _id("doc_undated") in doc_ids


async def test_bh_069_070_combined_active_recent_ranks_highest(
    graph_store, stub_content_store, seeded_embedding_provider, retrieval_service
):
    """Active + recent beats active + old, which beats archived + recent.
    Both boosts work together additively.
    """
    doc_active_recent = _make_doc(
        _id("doc_ar"),
        lifecycle_status="active",
        document_date="2026-04-01",
    )
    doc_active_old = _make_doc(
        _id("doc_ao"),
        lifecycle_status="active",
        document_date="2023-01-01",
    )
    doc_archived_recent = _make_doc(
        _id("doc_sr"),
        lifecycle_status="archived",
        document_date="2026-04-01",
    )
    await graph_store.insert_document(doc_active_recent)
    await graph_store.insert_document(doc_active_old)
    await graph_store.insert_document(doc_archived_recent)

    identical_content = "Report filing process for clinical normalization."
    for doc_id in [_id("doc_ar"), _id("doc_ao"), _id("doc_sr")]:
        await _index_doc_chunks(
            stub_content_store,
            seeded_embedding_provider,
            doc_id,
            [("Section 1", identical_content)],
        )

    request = DiscoverRequest(
        mode=RetrievalMode.SEMANTIC,
        query="report filing clinical normalization",
    )
    response = await retrieval_service.discover(request)

    doc_ids = [h.document.id for h in response.results]
    ar_idx = doc_ids.index(_id("doc_ar"))
    ao_idx = doc_ids.index(_id("doc_ao"))
    sr_idx = doc_ids.index(_id("doc_sr"))

    # Active + recent should be first
    assert ar_idx < ao_idx, "Active+recent should rank above active+old"
    assert ar_idx < sr_idx, "Active+recent should rank above archived+recent"


# ---------------------------------------------------------------------------
# Pre-filter: doc_type filtering at content store level
# ---------------------------------------------------------------------------


async def test_prefilter_doc_type_semantic(
    graph_store, stub_content_store, seeded_embedding_provider, retrieval_service
):
    """Semantic search with doc_type filter only returns matching documents,
    even when non-matching documents have higher relevance scores.

    This tests the pre-filter path: the content store itself excludes
    chunks from non-matching doc_types before scoring, rather than
    relying on post-filter depletion.
    """
    doc_patent = _make_doc(_id("doc_patent"), doc_type="design_spec")
    doc_report = _make_doc(_id("doc_report"), doc_type="report")
    doc_ref = _make_doc(_id("doc_ref"), doc_type="reference_document")
    await graph_store.insert_document(doc_patent)
    await graph_store.insert_document(doc_report)
    await graph_store.insert_document(doc_ref)

    # All documents get identical content so only the filter differentiates
    identical_content = "Clinical pathway integration and normalization process."
    for doc_id, doc_type in [
        (_id("doc_patent"), "design_spec"),
        (_id("doc_report"), "report"),
        (_id("doc_ref"), "reference_document"),
    ]:
        await _index_doc_chunks(
            stub_content_store,
            seeded_embedding_provider,
            doc_id,
            [("Section 1", identical_content)],
            doc_type=doc_type,
        )

    request = DiscoverRequest(
        mode=RetrievalMode.SEMANTIC,
        query="clinical pathway",
        scope=RetrievalScope.FILTERED,
        filters=RetrievalFilters(doc_type="design_spec"),
    )
    response = await retrieval_service.discover(request)

    doc_ids = [h.document.id for h in response.results]
    assert _id("doc_patent") in doc_ids
    assert _id("doc_report") not in doc_ids
    assert _id("doc_ref") not in doc_ids


async def test_prefilter_doc_type_keyword(
    graph_store, stub_content_store, seeded_embedding_provider, retrieval_service
):
    """Keyword search with doc_type filter only returns matching documents."""
    doc_patent = _make_doc(_id("doc_patent_kw"), doc_type="design_spec")
    doc_report = _make_doc(_id("doc_report_kw"), doc_type="report")
    await graph_store.insert_document(doc_patent)
    await graph_store.insert_document(doc_report)

    identical_content = "Detailed analysis of PV07 claims and prior art."
    for doc_id, doc_type in [
        (_id("doc_patent_kw"), "design_spec"),
        (_id("doc_report_kw"), "report"),
    ]:
        await _index_doc_chunks(
            stub_content_store,
            seeded_embedding_provider,
            doc_id,
            [("Section 1", identical_content)],
            doc_type=doc_type,
        )

    request = DiscoverRequest(
        mode=RetrievalMode.KEYWORD,
        query="PV07 claims",
        filters=RetrievalFilters(doc_type="design_spec"),
    )
    response = await retrieval_service.discover(request)

    doc_ids = [h.document.id for h in response.results]
    assert _id("doc_patent_kw") in doc_ids
    assert _id("doc_report_kw") not in doc_ids


async def test_prefilter_doc_type_hybrid(
    graph_store, stub_content_store, seeded_embedding_provider, retrieval_service
):
    """Hybrid RRF search with doc_type filter only returns matching documents."""
    doc_patent = _make_doc(_id("doc_patent_hyb"), doc_type="design_spec")
    doc_report = _make_doc(_id("doc_report_hyb"), doc_type="report")
    await graph_store.insert_document(doc_patent)
    await graph_store.insert_document(doc_report)

    identical_content = "Report filing process for clinical normalization."
    for doc_id, doc_type in [
        (_id("doc_patent_hyb"), "design_spec"),
        (_id("doc_report_hyb"), "report"),
    ]:
        await _index_doc_chunks(
            stub_content_store,
            seeded_embedding_provider,
            doc_id,
            [("Section 1", identical_content)],
            doc_type=doc_type,
        )

    request = DiscoverRequest(
        mode=RetrievalMode.SEMANTIC,
        query="report filing",
        use_hybrid=True,
        filters=RetrievalFilters(doc_type="design_spec"),
    )
    response = await retrieval_service.discover(request)

    doc_ids = [h.document.id for h in response.results]
    assert _id("doc_patent_hyb") in doc_ids
    assert _id("doc_report_hyb") not in doc_ids


async def test_prefilter_no_filter_returns_all_doc_types(
    graph_store, stub_content_store, seeded_embedding_provider, retrieval_service
):
    """Without a doc_type filter, all document types appear in results."""
    doc_patent = _make_doc(_id("doc_patent_all"), doc_type="design_spec")
    doc_report = _make_doc(_id("doc_report_all"), doc_type="report")
    await graph_store.insert_document(doc_patent)
    await graph_store.insert_document(doc_report)

    identical_content = "Report filing process for clinical normalization."
    for doc_id, doc_type in [
        (_id("doc_patent_all"), "design_spec"),
        (_id("doc_report_all"), "report"),
    ]:
        await _index_doc_chunks(
            stub_content_store,
            seeded_embedding_provider,
            doc_id,
            [("Section 1", identical_content)],
            doc_type=doc_type,
        )

    request = DiscoverRequest(
        mode=RetrievalMode.SEMANTIC,
        query="report filing",
    )
    response = await retrieval_service.discover(request)

    doc_ids = [h.document.id for h in response.results]
    assert _id("doc_patent_all") in doc_ids
    assert _id("doc_report_all") in doc_ids


async def test_postfilter_project_still_applies_with_prefilter(
    graph_store, stub_content_store, seeded_embedding_provider, retrieval_service
):
    """Post-filter fields (project) still work alongside doc_type pre-filter."""
    doc_match = _make_doc(_id("doc_match_both"), doc_type="design_spec", project="example_vault")
    doc_wrong_project = _make_doc(_id("doc_wrong_proj"), doc_type="design_spec", project="other")
    doc_wrong_type = _make_doc(_id("doc_wrong_type"), doc_type="report", project="example_vault")
    await graph_store.insert_document(doc_match)
    await graph_store.insert_document(doc_wrong_project)
    await graph_store.insert_document(doc_wrong_type)

    identical_content = "Report filing process for clinical normalization."
    for doc_id, doc_type, project in [
        (_id("doc_match_both"), "design_spec", "example_vault"),
        (_id("doc_wrong_proj"), "design_spec", "other"),
        (_id("doc_wrong_type"), "report", "example_vault"),
    ]:
        await _index_doc_chunks(
            stub_content_store,
            seeded_embedding_provider,
            doc_id,
            [("Section 1", identical_content)],
            doc_type=doc_type,
            project=project,
        )

    request = DiscoverRequest(
        mode=RetrievalMode.SEMANTIC,
        query="report filing",
        filters=RetrievalFilters(doc_type="design_spec", project="example_vault"),
    )
    response = await retrieval_service.discover(request)

    doc_ids = [h.document.id for h in response.results]
    assert _id("doc_match_both") in doc_ids
    assert _id("doc_wrong_proj") not in doc_ids
    assert _id("doc_wrong_type") not in doc_ids


async def test_prefilter_doc_type_null_chunks_excluded(
    graph_store, stub_content_store, seeded_embedding_provider, retrieval_service
):
    """Chunks without doc_type metadata (pre-migration data) are excluded
    when a doc_type filter is active.
    """
    doc_typed = _make_doc(_id("doc_typed"), doc_type="design_spec")
    doc_untyped = _make_doc(_id("doc_untyped"), doc_type="design_spec")
    await graph_store.insert_document(doc_typed)
    await graph_store.insert_document(doc_untyped)

    identical_content = "Report filing process for clinical normalization."
    # doc_typed has doc_type on its chunks
    await _index_doc_chunks(
        stub_content_store,
        seeded_embedding_provider,
        _id("doc_typed"),
        [("Section 1", identical_content)],
        doc_type="design_spec",
    )
    # doc_untyped has no doc_type on chunks (simulates pre-migration data)
    await _index_doc_chunks(
        stub_content_store,
        seeded_embedding_provider,
        _id("doc_untyped"),
        [("Section 1", identical_content)],
        doc_type=None,
    )

    request = DiscoverRequest(
        mode=RetrievalMode.SEMANTIC,
        query="report filing",
        filters=RetrievalFilters(doc_type="design_spec"),
    )
    response = await retrieval_service.discover(request)

    doc_ids = [h.document.id for h in response.results]
    assert _id("doc_typed") in doc_ids
    # doc_untyped passes the post-filter (graph store has correct doc_type)
    # but content store pre-filter excluded its chunks because chunk.doc_type is None.
    # The post-filter cannot recover it because it never appeared in search results.
    # This is acceptable: pre-migration data requires re-indexing.
    assert _id("doc_untyped") not in doc_ids


async def test_metadata_doc_type_change_syncs_to_content_store(
    graph_store,
    stub_content_store,
    seeded_embedding_provider,
    retrieval_service,
    metadata_service,
):
    """Changing doc_type via MetadataService updates content store chunks,
    so subsequent filtered searches reflect the new type.
    """
    doc = _make_doc(_id("doc_retyped"), doc_type="note")
    await graph_store.insert_document(doc)
    await _index_doc_chunks(
        stub_content_store,
        seeded_embedding_provider,
        _id("doc_retyped"),
        [("Section 1", "Clinical pathway integration process.")],
        doc_type="note",
    )

    # Verify it appears under "note" filter
    request_note = DiscoverRequest(
        mode=RetrievalMode.SEMANTIC,
        query="clinical pathway",
        filters=RetrievalFilters(doc_type="note"),
    )
    response = await retrieval_service.discover(request_note)
    assert _id("doc_retyped") in [h.document.id for h in response.results]

    # Change doc_type to memo via MetadataService
    await metadata_service._update_metadata(
        _id("doc_retyped"),
        UpdateMetadataRequest(doc_type="memo"),
        modified_by="test_user",
    )

    # Now it should appear under memo, not note
    request_memo = DiscoverRequest(
        mode=RetrievalMode.SEMANTIC,
        query="clinical pathway",
        filters=RetrievalFilters(doc_type="memo"),
    )
    response = await retrieval_service.discover(request_memo)
    assert _id("doc_retyped") in [h.document.id for h in response.results]

    response = await retrieval_service.discover(request_note)
    assert _id("doc_retyped") not in [h.document.id for h in response.results]


async def test_bh_070_document_date_in_summary(
    graph_store, stub_content_store, seeded_embedding_provider, retrieval_service
):
    """DiscoverHit.document summary includes document_date as datetime."""
    doc = _make_doc(_id("doc_date_summary"), document_date="2026-03-15")
    await graph_store.insert_document(doc)

    await _index_doc_chunks(
        stub_content_store,
        seeded_embedding_provider,
        _id("doc_date_summary"),
        [("Section 1", "Report filing process documentation.")],
    )

    request = DiscoverRequest(
        mode=RetrievalMode.SEMANTIC,
        query="report filing",
    )
    response = await retrieval_service.discover(request)

    hit = next(h for h in response.results if h.document.id == _id("doc_date_summary"))
    # document_date is a bare YYYY-MM-DD calendar-date string on the
    # DocumentSummary projection (no UTC-datetime promotion at the
    # projection boundary). The on-disk Document carries the same shape.
    assert type(hit.document.document_date) is str
    assert hit.document.document_date == "2026-03-15"


async def test_source_modified_at_in_summary(
    graph_store, stub_content_store, seeded_embedding_provider, retrieval_service
):
    """DiscoverHit.document summary includes source_modified_at field."""
    now = datetime.now(timezone.utc)
    mod_time = now - timedelta(days=10)
    doc = _make_doc(_id("doc_sma_summary"), source_modified_at=mod_time)
    await graph_store.insert_document(doc)

    await _index_doc_chunks(
        stub_content_store,
        seeded_embedding_provider,
        _id("doc_sma_summary"),
        [("Section 1", "Report filing process documentation.")],
    )

    request = DiscoverRequest(
        mode=RetrievalMode.SEMANTIC,
        query="report filing",
    )
    response = await retrieval_service.discover(request)

    hit = next(h for h in response.results if h.document.id == _id("doc_sma_summary"))
    assert hit.document.source_modified_at is not None
    # Stored as ISO string in SQLite, so compare to second precision
    assert abs((hit.document.source_modified_at - mod_time).total_seconds()) < 1.0


async def test_rerank_salience_no_extra_queries(
    graph_store, stub_content_store, seeded_embedding_provider, retrieval_service
):
    """Salience reranking uses summary fields, not extra get_document calls.

    Verifies that source_modified_at fallback works correctly via the
    DocumentSummary (not via a separate Document fetch).
    """
    now = datetime.now(timezone.utc)
    # Two docs: same content, same lifecycle, but different source_modified_at.
    # No document_date -- forces the fallback to source_modified_at on the summary.
    doc_recent = _make_doc(
        _id("doc_nq_recent"),
        document_date=None,
        source_modified_at=now - timedelta(days=5),
    )
    doc_old = _make_doc(
        _id("doc_nq_old"),
        document_date=None,
        source_modified_at=now - timedelta(days=900),
    )
    await graph_store.insert_document(doc_recent)
    await graph_store.insert_document(doc_old)

    identical_content = "Report filing process for clinical normalization."
    for doc_id in [_id("doc_nq_recent"), _id("doc_nq_old")]:
        await _index_doc_chunks(
            stub_content_store,
            seeded_embedding_provider,
            doc_id,
            [("Section 1", identical_content)],
        )

    # Patch get_document to track calls -- it should NOT be called by reranking
    call_count = 0
    original_get_document = graph_store.get_document

    async def counting_get_document(doc_id):
        nonlocal call_count
        call_count += 1
        return await original_get_document(doc_id)

    graph_store.get_document = counting_get_document

    request = DiscoverRequest(
        mode=RetrievalMode.SEMANTIC,
        query="report filing clinical normalization",
    )
    response = await retrieval_service.discover(request)

    # Restore original method
    graph_store.get_document = original_get_document

    doc_ids = [h.document.id for h in response.results]
    # Recency still works via summary field
    assert doc_ids.index(_id("doc_nq_recent")) < doc_ids.index(_id("doc_nq_old")), (
        "Recent doc should rank higher via source_modified_at on summary"
    )

    # get_document was called during _results_to_hits (doc cache), but NOT
    # during _rerank_salience. With 2 docs, cache calls = 2, rerank calls = 0.
    # If reranking still called get_document, we'd see 4 calls.
    assert call_count == 2, (
        f"Expected 2 get_document calls (cache only), got {call_count}. "
        "Reranking should use summary fields, not fetch full Documents."
    )


# ---------------------------------------------------------------------------
# BH-072 through BH-079: Catalog mode
# ---------------------------------------------------------------------------


async def _seed_catalog_docs(graph_store):
    """Seed 5 documents for catalog mode tests.

    Returns dict mapping doc_id to Document for verification.
    """
    docs = {
        _id("doc_a"): _make_doc(_id("doc_a"), doc_type="design_spec", tags=["PV07"]),
        _id("doc_b"): _make_doc(_id("doc_b"), doc_type="design_spec", tags=["PV08"]),
        _id("doc_c"): _make_doc(_id("doc_c"), doc_type="glossary", tags=["PV07"]),
        _id("doc_d"): _make_doc(
            _id("doc_d"),
            doc_type="design_spec",
            tags=["PV07"],
            lifecycle_status="archived",
        ),
        _id("doc_e"): _make_doc(_id("doc_e"), doc_type="checklist", tags=["PV07"]),
    }
    for doc in docs.values():
        await graph_store.insert_document(doc)
    return docs


async def test_bh_072_catalog_returns_filtered_documents(
    graph_store,
    retrieval_service,
):
    """Catalog mode returns all documents matching doc_type filter."""
    await _seed_catalog_docs(graph_store)

    request = DiscoverRequest(
        mode=RetrievalMode.CATALOG,
        scope=RetrievalScope.FILTERED,
        filters=RetrievalFilters(doc_type="design_spec"),
    )
    response = await retrieval_service.discover(request)

    assert response.mode == RetrievalMode.CATALOG
    result_ids = {h.document.id for h in response.results}
    assert result_ids == {_id("doc_a"), _id("doc_b"), _id("doc_d")}
    assert response.total_available == 3


async def test_bh_073_catalog_pagination(
    graph_store,
    retrieval_service,
):
    """Catalog mode supports limit + offset pagination."""
    await _seed_catalog_docs(graph_store)

    # Page 1
    req1 = DiscoverRequest(mode=RetrievalMode.CATALOG, limit=2, offset=0)
    resp1 = await retrieval_service.discover(req1)
    assert len(resp1.results) == 2
    assert resp1.total_available == 5

    # Page 2
    req2 = DiscoverRequest(mode=RetrievalMode.CATALOG, limit=2, offset=2)
    resp2 = await retrieval_service.discover(req2)
    assert len(resp2.results) == 2
    assert resp2.total_available == 5

    # Page 3 (partial)
    req3 = DiscoverRequest(mode=RetrievalMode.CATALOG, limit=2, offset=4)
    resp3 = await retrieval_service.discover(req3)
    assert len(resp3.results) == 1
    assert resp3.total_available == 5

    # No overlap
    all_ids = (
        {h.document.id for h in resp1.results}
        | {h.document.id for h in resp2.results}
        | {h.document.id for h in resp3.results}
    )
    assert len(all_ids) == 5


async def test_bh_074_catalog_tag_filtering_deterministic(
    graph_store,
    retrieval_service,
):
    """Catalog mode tag filtering returns exactly matching documents."""
    await _seed_catalog_docs(graph_store)

    request = DiscoverRequest(
        mode=RetrievalMode.CATALOG,
        scope=RetrievalScope.FILTERED,
        filters=RetrievalFilters(tags=["PV07"]),
    )
    response = await retrieval_service.discover(request)

    result_ids = {h.document.id for h in response.results}
    assert result_ids == {_id("doc_a"), _id("doc_c"), _id("doc_d"), _id("doc_e")}
    assert response.total_available == 4


async def test_bh_075_catalog_no_chunk_content_or_scores(
    graph_store,
    retrieval_service,
):
    """Catalog mode returns no chunk content or relevance scores."""
    await _seed_catalog_docs(graph_store)

    request = DiscoverRequest(mode=RetrievalMode.CATALOG, limit=5)
    response = await retrieval_service.discover(request)

    assert len(response.results) > 0
    for hit in response.results:
        assert hit.chunk_content is None
        assert hit.heading_path is None
        assert hit.relevance_score is None
        # Document metadata is populated
        assert hit.document.id is not None
        assert hit.document.title is not None
        assert hit.document.lifecycle_status is not None


async def test_bh_076_catalog_includes_failed_pipeline(
    graph_store,
    retrieval_service,
):
    """Catalog mode includes failed-pipeline documents.

    Catalog is filter-only document enumeration; visibility is a property of
    the document, not of the query. Pre-this test asserted exclusion;
    the BH-020 default-exclude has been narrowed to scoring modes only.
    """
    doc_ok = _make_doc(_id("doc_ok"))
    doc_fail = _make_doc(
        _id("doc_fail"),
        pipeline_status=PipelineStatus.FAILED,
    )
    await graph_store.insert_document(doc_ok)
    await graph_store.insert_document(doc_fail)

    request = DiscoverRequest(mode=RetrievalMode.CATALOG)
    response = await retrieval_service.discover(request)

    result_ids = {h.document.id for h in response.results}
    assert _id("doc_ok") in result_ids
    assert _id("doc_fail") in result_ids
    assert response.total_available == 2


async def test_bh_077_catalog_no_filters_returns_all(
    graph_store,
    retrieval_service,
):
    """Catalog mode with no filters returns all documents, including failed."""
    await _seed_catalog_docs(graph_store)
    # Add one failed doc
    doc_fail = _make_doc(_id("doc_fail"), pipeline_status=PipelineStatus.FAILED)
    await graph_store.insert_document(doc_fail)

    request = DiscoverRequest(mode=RetrievalMode.CATALOG)
    response = await retrieval_service.discover(request)

    assert response.total_available == 6  # 5 healthy + 1 failed all returned by catalog
    assert len(response.results) == 6


# ---------------------------------------------------------------------------
# Mode-scoped BH-020 -- catalog enumerates failed; scoring excludes
# ---------------------------------------------------------------------------


async def test_catalog_returns_failed_pipeline_doc_by_default(
    graph_store,
    retrieval_service,
):
    """Catalog mode returns failed-pipeline docs without an explicit filter.

    The headline behaviour change: an active document whose pipeline status
    is FAILED is enumerable by catalog mode. Pre-it was silently
    excluded at the storage layer.
    """
    doc_ok = _make_doc(_id("t0148_ok"))
    doc_fail = _make_doc(_id("t0148_fail"), pipeline_status=PipelineStatus.FAILED)
    await graph_store.insert_document(doc_ok)
    await graph_store.insert_document(doc_fail)

    request = DiscoverRequest(mode=RetrievalMode.CATALOG)
    response = await retrieval_service.discover(request)

    result_ids = {h.document.id for h in response.results}
    assert _id("t0148_ok") in result_ids
    assert _id("t0148_fail") in result_ids
    assert response.total_available == 2


async def test_catalog_explicit_pipeline_status_filter_still_narrows(
    graph_store,
    retrieval_service,
):
    """Catalog mode still honours an explicit pipeline_status filter.

    The default-exclude moved; positive selection via an explicit filter
    must continue to work the same way it always did.
    """
    doc_ok = _make_doc(_id("t0148_ok2"))
    doc_fail = _make_doc(_id("t0148_fail2"), pipeline_status=PipelineStatus.FAILED)
    await graph_store.insert_document(doc_ok)
    await graph_store.insert_document(doc_fail)

    request = DiscoverRequest(
        mode=RetrievalMode.CATALOG,
        scope=RetrievalScope.FILTERED,
        filters=RetrievalFilters(pipeline_status="abstraction_complete"),
    )
    response = await retrieval_service.discover(request)

    result_ids = {h.document.id for h in response.results}
    assert result_ids == {_id("t0148_ok2")}
    assert response.total_available == 1


async def test_semantic_still_excludes_failed_by_default(
    graph_store,
    stub_content_store,
    seeded_embedding_provider,
    retrieval_service,
):
    """Semantic mode still excludes failed-pipeline docs by default.

    Guards the asymmetry: relaxing catalog must not regress scoring-mode
    behaviour. Chunks are indexed for the failed doc so its exclusion is
    *because* of the pipeline filter, not because LanceDB has nothing to
    score (anti-coincidental-pass setup, mirroring test_bh_020).
    """
    doc_ok = _make_doc(_id("t0148_sem_ok"))
    doc_fail = _make_doc(_id("t0148_sem_fail"), pipeline_status=PipelineStatus.FAILED)
    await graph_store.insert_document(doc_ok)
    await graph_store.insert_document(doc_fail)
    await _index_doc_chunks(
        stub_content_store,
        seeded_embedding_provider,
        _id("t0148_sem_ok"),
        [("Section 1", "This document discusses report claims and prior art.")],
    )
    await _index_doc_chunks(
        stub_content_store,
        seeded_embedding_provider,
        _id("t0148_sem_fail"),
        [("Section 1", "This document discusses report claims and prior art.")],
    )

    request = DiscoverRequest(mode=RetrievalMode.SEMANTIC, query="report claims")
    response = await retrieval_service.discover(request)

    doc_ids = [h.document.id for h in response.results]
    assert _id("t0148_sem_ok") in doc_ids
    assert _id("t0148_sem_fail") not in doc_ids


async def test_storage_query_documents_default_exclude_failed_flag(
    graph_store,
):
    """Storage layer honours the default_exclude_failed flag at both settings.

    Pins the storage contract directly, independent of the retrieval
    service. Both settings exercised in one test so the assertion shape
    is anti-coincidental: a refactor that drops the gate entirely would
    fail one half; a refactor that hard-codes True would fail the other.
    """
    doc_ok = _make_doc(_id("t0148_store_ok"))
    doc_fail = _make_doc(_id("t0148_store_fail"), pipeline_status=PipelineStatus.FAILED)
    await graph_store.insert_document(doc_ok)
    await graph_store.insert_document(doc_fail)

    # Default behaviour preserved: failed doc excluded.
    docs_default, total_default = await graph_store.query_documents()
    default_ids = {d.id for d in docs_default}
    assert _id("t0148_store_ok") in default_ids
    assert _id("t0148_store_fail") not in default_ids
    assert total_default == 1

    # Flag flipped off: failed doc surfaces.
    docs_all, total_all = await graph_store.query_documents(default_exclude_failed=False)
    all_ids = {d.id for d in docs_all}
    assert _id("t0148_store_ok") in all_ids
    assert _id("t0148_store_fail") in all_ids
    assert total_all == 2


async def test_bh_078_catalog_total_available_independent_of_page(
    graph_store,
    retrieval_service,
):
    """Catalog mode total_available is full count, not page size."""
    # Insert 10 documents
    for i in range(10):
        doc = _make_doc(_id(f"doc_{i:02d}"))
        await graph_store.insert_document(doc)

    request = DiscoverRequest(mode=RetrievalMode.CATALOG, limit=3, offset=0)
    response = await retrieval_service.discover(request)

    assert len(response.results) == 3
    assert response.total_available == 10


async def test_bh_079_catalog_combined_filters(
    graph_store,
    retrieval_service,
):
    """Catalog mode with multiple filters uses AND semantics."""
    await _seed_catalog_docs(graph_store)

    request = DiscoverRequest(
        mode=RetrievalMode.CATALOG,
        scope=RetrievalScope.FILTERED,
        filters=RetrievalFilters(
            doc_type="design_spec",
            tags=["PV07"],
            lifecycle_status="active",
        ),
    )
    response = await retrieval_service.discover(request)

    result_ids = {h.document.id for h in response.results}
    assert result_ids == {_id("doc_a")}
    assert response.total_available == 1


# ---------------------------------------------------------------------------
# BH-080 through BH-083: Catalog sort
# ---------------------------------------------------------------------------


async def _seed_catalog_sort_docs(graph_store):
    """Seed documents with varying lifecycle and dates for sort tests."""
    docs = {
        _id("sort_a"): _make_doc(
            _id("sort_a"),
            lifecycle_status="active",
            document_date="2026-03-15",
            doc_type="design_spec",
        ),
        _id("sort_b"): _make_doc(
            _id("sort_b"),
            lifecycle_status="archived",
            document_date="2026-04-01",
            doc_type="design_spec",
        ),
        _id("sort_c"): _make_doc(
            _id("sort_c"),
            lifecycle_status="active",
            document_date="2026-02-10",
            doc_type="design_spec",
        ),
        _id("sort_d"): _make_doc(
            _id("sort_d"),
            lifecycle_status="archived",
            document_date="2026-04-10",
            doc_type="design_spec",
        ),
        _id("sort_e"): _make_doc(
            _id("sort_e"),
            lifecycle_status="active",
            document_date=None,
            doc_type="design_spec",
        ),
    }
    for doc in docs.values():
        await graph_store.insert_document(doc)
    return docs


async def test_bh_080_catalog_default_sort_lifecycle_then_date(
    graph_store,
    retrieval_service,
):
    """Default catalog sort: active lifecycle first, then document_date desc."""
    await _seed_catalog_sort_docs(graph_store)

    request = DiscoverRequest(mode=RetrievalMode.CATALOG, limit=10)
    response = await retrieval_service.discover(request)

    ids = [h.document.id for h in response.results]
    # Active docs first (sort_a, sort_c, sort_e) sorted by date desc,
    # then non-active (sort_b, sort_d) sorted by date desc.
    # sort_e has no date, sorts last among active.
    assert ids[0:3] == [_id("sort_a"), _id("sort_c"), _id("sort_e")]
    assert ids[3:5] == [_id("sort_d"), _id("sort_b")]


async def test_bh_081_catalog_sort_by_title(
    graph_store,
    retrieval_service,
):
    """Catalog sort by title ascending."""
    await _seed_catalog_sort_docs(graph_store)

    request = DiscoverRequest(
        mode=RetrievalMode.CATALOG,
        sort_by="title",
        sort_order="asc",
        limit=10,
    )
    response = await retrieval_service.discover(request)

    titles = [h.document.title for h in response.results]
    assert titles == sorted(titles)


async def test_bh_082_catalog_sort_by_document_date_desc(
    graph_store,
    retrieval_service,
):
    """Catalog sort by document_date descending. Nulls sort last."""
    await _seed_catalog_sort_docs(graph_store)

    request = DiscoverRequest(
        mode=RetrievalMode.CATALOG,
        sort_by="document_date",
        sort_order="desc",
        limit=10,
    )
    response = await retrieval_service.discover(request)

    ids = [h.document.id for h in response.results]
    # 2026-04-10, 2026-04-01, 2026-03-15, 2026-02-10, None
    assert ids == [_id("sort_d"), _id("sort_b"), _id("sort_a"), _id("sort_c"), _id("sort_e")]


async def test_bh_083_catalog_sort_ignored_by_other_modes(
    graph_store,
    stub_content_store,
    seeded_embedding_provider,
    retrieval_service,
):
    """sort_by and sort_order are silently ignored for non-catalog modes."""
    await _seed_catalog_sort_docs(graph_store)

    # Index at least one doc so keyword mode has content to search
    await _index_doc_chunks(
        stub_content_store,
        seeded_embedding_provider,
        _id("sort_a"),
        [("Intro", "Test text about reports")],
    )

    request = DiscoverRequest(
        mode=RetrievalMode.KEYWORD,
        query="reports",
        sort_by="document_date",
        sort_order="desc",
        limit=10,
    )
    # Should not raise
    response = await retrieval_service.discover(request)
    assert response.mode == RetrievalMode.KEYWORD


# ---------------------------------------------------------------------------
# BH-084 through BH-088: Document-level response mode
# ---------------------------------------------------------------------------


async def _seed_response_level_docs(graph_store, content_store, embedding_provider):
    """Seed 3 documents with indexed chunks for response_level tests."""
    for doc_id in (_id("rl_a"), _id("rl_b"), _id("rl_c")):
        doc = _make_doc(doc_id, doc_type="design_spec")
        await graph_store.insert_document(doc)
        await _index_doc_chunks(
            content_store,
            embedding_provider,
            doc_id,
            [("Section 1", f"Integration testing for {doc_id} document.")],
            doc_type="design_spec",
        )


async def test_bh_086_response_mode_full_vs_light_preserves_scores_and_order(
    graph_store,
    stub_content_store,
    seeded_embedding_provider,
    retrieval_service,
):
    """response_mode=light produces the same scores, order, heading paths,
    and matched_chunk_counts as response_mode=full; only chunk_content
    differs (retirement of response_level)."""
    await _seed_response_level_docs(
        graph_store,
        stub_content_store,
        seeded_embedding_provider,
    )

    full_request = DiscoverRequest(
        mode=RetrievalMode.SEMANTIC,
        query="integration",
        response_mode=ResponseMode.FULL,
        limit=10,
    )
    light_request = DiscoverRequest(
        mode=RetrievalMode.SEMANTIC,
        query="integration",
        response_mode=ResponseMode.LIGHT,
        limit=10,
    )

    full_response = await retrieval_service.discover(full_request)
    light_response = await retrieval_service.discover(light_request)

    # Same documents in same order
    full_ids = [h.document.id for h in full_response.results]
    light_ids = [h.document.id for h in light_response.results]
    assert full_ids == light_ids

    # Same scores
    full_scores = [h.relevance_score for h in full_response.results]
    light_scores = [h.relevance_score for h in light_response.results]
    assert full_scores == light_scores

    # Same heading paths
    full_headings = [h.heading_path for h in full_response.results]
    light_headings = [h.heading_path for h in light_response.results]
    assert full_headings == light_headings

    # Same matched_chunk_counts
    full_counts = [h.matched_chunk_count for h in full_response.results]
    light_counts = [h.matched_chunk_count for h in light_response.results]
    assert full_counts == light_counts

    # Full response has content; light response does not
    assert any(h.chunk_content is not None for h in full_response.results)
    assert all(h.chunk_content is None for h in light_response.results)


async def test_bh_084_multi_chunk_matched_chunk_count(
    graph_store,
    stub_content_store,
    seeded_embedding_provider,
    retrieval_service,
):
    """matched_chunk_count reflects multiple matching chunks per document."""
    doc = _make_doc(_id("multi_chunk"), doc_type="design_spec")
    await graph_store.insert_document(doc)
    # Index 3 chunks for the same document
    await _index_doc_chunks(
        stub_content_store,
        seeded_embedding_provider,
        _id("multi_chunk"),
        [
            ("Section 1", "Integration testing for claims."),
            ("Section 2", "Integration of prior art references."),
            ("Section 3", "Integration with existing report family."),
        ],
        doc_type="design_spec",
    )

    request = DiscoverRequest(
        mode=RetrievalMode.SEMANTIC,
        query="integration",
        response_mode=ResponseMode.LIGHT,
        limit=10,
    )
    response = await retrieval_service.discover(request)

    # Should have exactly one hit for the document
    hits_for_doc = [h for h in response.results if h.document.id == _id("multi_chunk")]
    assert len(hits_for_doc) == 1
    # All 3 chunks should be counted
    assert hits_for_doc[0].matched_chunk_count == 3


async def test_matched_chunk_count_content_only_not_metadata(
    graph_store,
    stub_content_store,
    seeded_embedding_provider,
    retrieval_service,
):
    """matched_chunk_count reflects content matches only, not metadata matches.

    When a document is found by both content search and metadata search
    (title contains the query), matched_chunk_count should count only the
    content-store chunks, not be bumped by the metadata match.
    """
    # Title contains "integration" so metadata search will find it too
    doc = _make_doc(_id("dual_match"), doc_type="design_spec")
    doc.title = "Integration Testing Guide"
    await graph_store.insert_document(doc)
    # Index exactly 2 chunks
    await _index_doc_chunks(
        stub_content_store,
        seeded_embedding_provider,
        _id("dual_match"),
        [
            ("Section 1", "Integration testing for claims."),
            ("Section 2", "Integration of prior art references."),
        ],
        doc_type="design_spec",
    )

    request = DiscoverRequest(
        mode=RetrievalMode.SEMANTIC,
        query="integration",
        response_mode=ResponseMode.LIGHT,
        limit=10,
    )
    response = await retrieval_service.discover(request)

    hits_for_doc = [h for h in response.results if h.document.id == _id("dual_match")]
    assert len(hits_for_doc) == 1
    # Count should be 2 (content chunks only), not 3
    assert hits_for_doc[0].matched_chunk_count == 2


async def test_metadata_boost_promotes_existing_low_score_hit(
    graph_store,
    stub_content_store,
    seeded_embedding_provider,
    retrieval_service,
):
    """Metadata match promotes a document already in content results.

    When a document code (e.g., "PV13") appears in an authored metadata field
    and the document is also found by content search at a low relevance score,
    the metadata boost should promote its score above the highest content
    score. Previously, metadata boost skipped documents already in content
    results, leaving code-based lookups at near-zero relevance.

    The code is carried by the title. It used to be carried by the source path,
    which no longer admits a document (CAS-ADR-049 Decision 4; the refusal is
    pinned by ``test_filename_stem_does_not_admit_a_document_through_the_boost``
    below). Note that the default ``_make_doc`` title embeds the document id, so
    a code resembling any id fragment would be matched by the title whatever the
    source path said -- the title here is set explicitly so the promotion under
    test is driven by a field that is unambiguously authored.
    """
    # Target doc: code "PV13" is in the title, content barely matches
    target = _make_doc(_id("target_pv13"), doc_type="design_spec")
    target.title = "PV13 Authoritative Accumulator"
    target.source_path = "reports/unrelated_stem.docx"
    await graph_store.insert_document(target)
    await _index_doc_chunks(
        stub_content_store,
        seeded_embedding_provider,
        _id("target_pv13"),
        [("Section 1", "Clinical normalization of respiratory signals.")],
        doc_type="design_spec",
    )

    # Distractor doc: strong content match for "PV13" but no metadata match
    distractor = _make_doc(_id("distractor"), doc_type="design_spec")
    distractor.title = "Prior Art Survey"
    distractor.source_path = "reports/PV99_Unrelated.docx"
    await graph_store.insert_document(distractor)
    await _index_doc_chunks(
        stub_content_store,
        seeded_embedding_provider,
        _id("distractor"),
        [("Section 1", "PV13 is referenced in the prior art analysis.")],
        doc_type="design_spec",
    )

    request = DiscoverRequest(
        mode=RetrievalMode.KEYWORD,
        query="PV13",
        limit=10,
    )
    response = await retrieval_service.discover(request)

    ids = [h.document.id for h in response.results]
    assert _id("target_pv13") in ids, "Metadata-matched doc must appear in results"
    assert _id("distractor") in ids, "Content-matched doc must appear in results"

    # Target must rank first: metadata identity match outranks content match
    assert ids.index(_id("target_pv13")) < ids.index(_id("distractor")), (
        "Document with code in its title should rank above "
        "document that merely mentions the code in body text"
    )

    # Target's score should be above the distractor's content score
    target_hit = next(h for h in response.results if h.document.id == _id("target_pv13"))
    distractor_hit = next(h for h in response.results if h.document.id == _id("distractor"))
    assert target_hit.relevance_score > distractor_hit.relevance_score


async def test_filename_stem_does_not_admit_a_document_through_the_boost(
    graph_store,
    stub_content_store,
    seeded_embedding_provider,
    retrieval_service,
):
    """A filename cannot put a document in a caller's results.

    CAS-ADR-049 Decision 4 binds every path into a result set, not the content
    store's matching alone: a service-layer boost that admits a document on
    derived text defeats the provenance rule as surely as an unfiltered index
    would. The source path is derived -- incidental to how the document arrived
    -- so a query naming nothing but a filename stem must not reach it.

    Both documents carry an explicitly authored title and tag set, because
    ``_make_doc`` derives its default title *and* its default source path from
    the same document id: left at the default, every document's title contains
    its own filename stem and the refusal below could not be observed.
    """
    derived_only = _make_doc(_id("filename_only"))
    derived_only.title = "Authoritative Accumulator"
    derived_only.tags = ["design"]
    derived_only.source_path = "reports/QV77_AuthoritativeAccumulator.docx"
    await graph_store.insert_document(derived_only)
    await _index_doc_chunks(
        stub_content_store,
        seeded_embedding_provider,
        _id("filename_only"),
        [("Section 1", "Clinical normalization of respiratory signals.")],
    )

    # Positive control: the same term in an authored field, so the assertion
    # below is a statement about provenance rather than about a boost that
    # stopped working. Without it, deleting the boost outright would pass.
    authored = _make_doc(_id("authored_code"))
    authored.title = "QV77 Prior Art Survey"
    authored.tags = ["design"]
    authored.source_path = "reports/unrelated_stem.docx"
    await graph_store.insert_document(authored)
    await _index_doc_chunks(
        stub_content_store,
        seeded_embedding_provider,
        _id("authored_code"),
        [("Section 1", "Clinical normalization of respiratory signals.")],
    )

    response = await retrieval_service.discover(
        DiscoverRequest(mode=RetrievalMode.KEYWORD, query="QV77", limit=10)
    )
    ids = [h.document.id for h in response.results]

    assert _id("authored_code") in ids, (
        "positive control: a code in the title still admits the document"
    )
    assert _id("filename_only") not in ids, (
        "a code carried only by the filename admitted the document; the boost "
        "is admitting on derived text"
    )


# ---------------------------------------------------------------------------
# A document-level hit is not a passage: it is not counted as one, and it
# carries no excerpt (CAS-ADR-049 Decisions 4 and 5).
# ---------------------------------------------------------------------------


async def _seed_surface_only_match(
    graph_store, stub_content_store, seeded_embedding_provider, doc_id, *, title
):
    """Seed a document that has a document surface and no passages at all.

    No passages, deliberately. The arms fuse on the document, so a document
    that also has a body chunk comes back as one hit carrying whatever that
    chunk contributes -- which is the right answer, and the wrong fixture for
    asserting what a document reached through document-level text alone
    reports. With no passage in the store there is nothing else the hit could
    have come from.
    """
    doc = _make_doc(_id(doc_id))
    doc.title = title
    doc.tags = ["design"]
    doc.source_path = f"reports/{doc_id}.md"
    await graph_store.insert_document(doc)
    await _index_document_surface(stub_content_store, seeded_embedding_provider, _id(doc_id), doc)
    return doc


async def test_document_level_hit_reports_zero_matched_passages_semantic(
    graph_store, stub_content_store, seeded_embedding_provider, retrieval_service
):
    """A document-level hit counts no passages, on the semantic arm.

    The count names passages (CAS-ADR-049 Decision 5) and a document-level row
    is not one, so the answer is zero rather than one. ``DiscoverHit``'s own
    published description already says so.
    """
    await _seed_surface_only_match(
        graph_store,
        stub_content_store,
        seeded_embedding_provider,
        "surface_count_sem",
        title="Zetaword Accumulator Catalog",
    )

    response = await retrieval_service.discover(
        DiscoverRequest(mode=RetrievalMode.SEMANTIC, query="zetaword accumulator catalog")
    )

    hit = _document_level_hit(response, _id("surface_count_sem"))
    assert hit is not None, "the document surface contributed nothing to this result"
    assert hit.matched_chunk_count == 0, (
        "a document-level hit is not a passage and must not be counted as one"
    )


# The keyword arm's end-to-end counterpart lives in
# ``tests/sage/test_content_store_postgres.py``. This double stores a document
# surface but never consults it on the keyword arm, by design, so a keyword hit
# won through document-level text can only be observed against a real backend.


async def test_a_surface_row_does_not_inflate_a_passage_count(
    graph_store, stub_content_store, seeded_embedding_provider, retrieval_service
):
    """A document matching on both surfaces counts its passages only.

    Two passages carry the term and the document surface carries it too. The
    count is two, not three: the surface row rides alongside the passages in
    the same result set and must not be tallied with them. It arrives after the
    row that wins the excerpt, which is the case a tally that tested provenance
    only on a document's first row would miss.

    Removing the surface from the setup leaves this passing, which is the point
    rather than a gap: a correct tally makes the extra row a no-op, so what the
    seeding buys is a red when the tally regresses -- it reports three.
    """
    doc = _make_doc(_id("both_surfaces"))
    doc.title = "Zetaword Accumulator Catalog"
    doc.tags = ["design"]
    await graph_store.insert_document(doc)
    await _index_doc_chunks(
        stub_content_store,
        seeded_embedding_provider,
        _id("both_surfaces"),
        [
            ("Section 1", "zetaword accumulator catalog in the first passage"),
            ("Section 2", "zetaword accumulator catalog in the second passage"),
        ],
    )
    await _index_document_surface(
        stub_content_store, seeded_embedding_provider, _id("both_surfaces"), doc
    )

    # Pure vector rather than the fused default, so the surface row and the
    # passage rows all reach the service as separate rows. Fusion would collapse
    # them first and the tally under test would never see the surface row.
    response = await retrieval_service.discover(
        DiscoverRequest(
            mode=RetrievalMode.SEMANTIC,
            query="zetaword accumulator catalog",
            use_hybrid=False,
        )
    )

    hits = [h for h in response.results if h.document.id == _id("both_surfaces")]
    assert len(hits) == 1, "the document is deduplicated to one hit"
    assert hits[0].matched_chunk_count == 2, (
        "the surface row was tallied as if it were a third passage"
    )


async def test_a_passage_supplies_the_excerpt_when_the_surface_outranks_it(
    graph_store, stub_content_store, seeded_embedding_provider, retrieval_service
):
    """A document that matched passages is served one of them.

    The service dedupes to the document's best-ranking row, and for a
    title-shaped query that row is routinely the document surface -- which is
    what the surface is for. Left there, the hit reports the passages it
    matched and carries neither an excerpt nor a heading for any of them. The
    score stays the document's best across both surfaces; only the excerpt
    moves.

    Pure vector rather than the fused default, so the rows reach the service
    separately and the handoff under test is the service's own.
    """
    # The title is a compound, so the metadata boost -- which matches by
    # substring -- cannot reach it with this query and does not overwrite the
    # score on the way out. The document surface can: its authored half is
    # widened to the split rendering. Without that the assertion below would be
    # about the boost's synthetic score rather than about the handoff.
    doc = _make_doc(_id("surface_outranks"))
    doc.title = "ZetawordAccumulator_Catalog"
    doc.tags = ["design"]
    await graph_store.insert_document(doc)
    await _index_doc_chunks(
        stub_content_store,
        seeded_embedding_provider,
        _id("surface_outranks"),
        [("Section 1", "Clinical normalization of respiratory signals.")],
    )
    await _index_document_surface(
        stub_content_store, seeded_embedding_provider, _id("surface_outranks"), doc
    )

    # Same passage, no surface. Its score is the passage's alone.
    control = _make_doc(_id("passage_only"))
    control.title = "Unrelated Control"
    await graph_store.insert_document(control)
    await _index_doc_chunks(
        stub_content_store,
        seeded_embedding_provider,
        _id("passage_only"),
        [("Section 1", "Clinical normalization of respiratory signals.")],
    )

    response = await retrieval_service.discover(
        DiscoverRequest(
            mode=RetrievalMode.SEMANTIC,
            query="zetaword accumulator catalog",
            use_hybrid=False,
        )
    )

    [hit] = [h for h in response.results if h.document.id == _id("surface_outranks")]
    [control] = [h for h in response.results if h.document.id == _id("passage_only")]
    assert hit.matched_chunk_count == 1, "the document matched one passage"
    # The score is the document's best across both surfaces, and the handoff
    # moves the excerpt alone. The control carries the same passage and no
    # surface, so its score is what this document's would collapse to if the
    # handoff took the passage's score along with its text.
    assert hit.relevance_score > control.relevance_score, (
        "the handoff moved the score as well as the excerpt"
    )
    assert hit.heading_path == "Section 1", (
        "the hit kept the surface row's absent heading despite matching a passage"
    )
    assert hit.chunk_content == "Clinical normalization of respiratory signals.", (
        "the hit carries no excerpt for the passage it reports having matched"
    )


async def test_a_headingless_passage_reaches_the_service_tally_as_a_passage(
    graph_store, stub_content_store, seeded_embedding_provider, retrieval_service
):
    """A passage carrying no heading is still tallied as a passage.

    The service tests provenance by the row's own flag rather than by an empty
    heading path, and a headingless passage is the input that separates the
    two: ingestion writes one for any source with no headings at all
    (``_build_chunks``), so a tally reading the heading path would stop
    counting it.

    The fixture pairs a headed passage with a headingless one to isolate the
    tally rule, which is narrower than what ingestion emits -- a real
    headingless document has exactly one chunk. With only that one chunk the
    substitution is unobservable here, because the row's own carried count is
    one and the service takes the larger of the two; it is the tally, not the
    carried value, that this fixture is about. The fusion's half of the same
    rule is covered in ``test_rrf_counts_a_headingless_passage_as_a_passage``.
    """
    doc = _make_doc(_id("headless_service"))
    doc.title = "Unrelated Title"
    await graph_store.insert_document(doc)
    await _index_doc_chunks(
        stub_content_store,
        seeded_embedding_provider,
        _id("headless_service"),
        [
            ("Section 1", "zetaword catalog in a passage that carries a heading"),
            ("", "zetaword catalog in a passage that carries none"),
        ],
    )

    response = await retrieval_service.discover(
        DiscoverRequest(
            mode=RetrievalMode.SEMANTIC,
            query="zetaword catalog passage",
            use_hybrid=False,
        )
    )

    [hit] = [h for h in response.results if h.document.id == _id("headless_service")]
    assert hit.matched_chunk_count == 2, (
        "the headingless passage was not tallied, so the count dropped to the headed one alone"
    )


async def test_document_level_hit_carries_no_excerpt(
    graph_store, stub_content_store, seeded_embedding_provider, retrieval_service
):
    """A document-level hit exposes no index-side expansion as its excerpt.

    The stored document surface widens its authored half to a superset of the
    title's renderings so a caller reaching for one form finds the other. That
    widening is an index-side artifact: served back as ``chunk_content`` it
    reads as duplicated tokens rather than as anything the document says. A
    document-level hit is not a passage and carries no excerpt at all.

    The title is a CamelCase compound, so the stored surface demonstrably
    carries a split rendering the raw title does not. Asserting the absence of
    that split form -- not merely that the excerpt is short -- is what makes
    this a statement about the expansion.
    """
    await _seed_surface_only_match(
        graph_store,
        stub_content_store,
        seeded_embedding_provider,
        "no_excerpt",
        title="ZetawordAccumulator_Catalog",
    )

    response = await retrieval_service.discover(
        DiscoverRequest(mode=RetrievalMode.SEMANTIC, query="zetaword accumulator catalog")
    )

    hit = _document_level_hit(response, _id("no_excerpt"))
    assert hit is not None, (
        "the document surface contributed nothing to this result, so the "
        "excerpt assertion below would say nothing about it"
    )
    assert not (hit.chunk_content or ""), "a document-level hit carries no excerpt"
    assert "zetaword accumulator" not in (hit.chunk_content or "").lower(), (
        "the hit exposes the index-side expansion's split rendering"
    )


async def test_bh_087_response_mode_unset_preserves_chunk_content(
    graph_store,
    stub_content_store,
    seeded_embedding_provider,
    retrieval_service,
):
    """Default response_mode (omitted) preserves chunk content on
    semantic/keyword (after the response_level retirement)."""
    await _seed_response_level_docs(
        graph_store,
        stub_content_store,
        seeded_embedding_provider,
    )

    # No response_mode specified -- should default to chunks-equivalent
    request = DiscoverRequest(
        mode=RetrievalMode.SEMANTIC,
        query="integration",
        limit=10,
    )
    response = await retrieval_service.discover(request)

    assert len(response.results) > 0
    # At least one hit should have chunk content (non-boosted hits)
    content_hits = [h for h in response.results if h.chunk_content is not None]
    assert len(content_hits) > 0
    # Verify heading paths and matched_chunk_count present on content hits
    for hit in content_hits:
        assert hit.heading_path is not None
        assert hit.matched_chunk_count is not None
        assert hit.matched_chunk_count >= 1


async def test_bh_088_response_mode_full_ignored_by_catalog_chunk_content(
    graph_store,
    retrieval_service,
):
    """Catalog mode never emits chunk_content regardless of response_mode
    (after the response_level retirement)."""
    for doc_id in (_id("cat_a"), _id("cat_b")):
        doc = _make_doc(doc_id, doc_type="design_spec")
        await graph_store.insert_document(doc)

    # Explicitly request full -- catalog should still return no chunk content
    request = DiscoverRequest(
        mode=RetrievalMode.CATALOG,
        response_mode=ResponseMode.FULL,
        limit=10,
    )
    response = await retrieval_service.discover(request)

    assert len(response.results) >= 2
    for hit in response.results:
        assert hit.chunk_content is None
        assert hit.heading_path is None
        assert hit.relevance_score is None
        # Catalog mode doesn't go through _results_to_hits, no chunk counting
        assert hit.matched_chunk_count is None


# ---------------------------------------------------------------------------
# Semantic Abstract Consumer Tests (CAS-ADR-011, Phase 1)
# ---------------------------------------------------------------------------


# BH-101: Semantic discover returns semantic_abstract on DocumentSummary
async def test_bh_101_semantic_discover_returns_abstract(
    graph_store,
    stub_content_store,
    seeded_embedding_provider,
    retrieval_service,
):
    """DocumentSummary in semantic discover results includes semantic_abstract."""
    abstract_text = "This document analyzes report claim structures for Example Portfolio."
    doc = _make_doc(_id("abs_doc"), semantic_abstract=abstract_text)
    await graph_store.insert_document(doc)

    await _index_doc_chunks(
        stub_content_store,
        seeded_embedding_provider,
        _id("abs_doc"),
        [("Section 1", "Report claim structures and prior art analysis.")],
    )

    request = DiscoverRequest(
        mode=RetrievalMode.SEMANTIC,
        query="report claims",
        include_abstracts=True,
    )
    response = await retrieval_service.discover(request)

    hits_by_id = {h.document.id: h for h in response.results}
    assert _id("abs_doc") in hits_by_id
    assert hits_by_id[_id("abs_doc")].document.semantic_abstract == abstract_text


# BH-102: Discover returns None abstract for abstraction-skipped documents
async def test_bh_102_discover_returns_none_abstract_when_skipped(
    graph_store,
    stub_content_store,
    seeded_embedding_provider,
    retrieval_service,
):
    """Documents with abstraction_skipped have semantic_abstract=None in results."""
    doc = _make_doc(
        _id("no_abs_doc"),
        pipeline_status=PipelineStatus.ABSTRACTION_SKIPPED,
        semantic_abstract=None,
    )
    await graph_store.insert_document(doc)

    await _index_doc_chunks(
        stub_content_store,
        seeded_embedding_provider,
        _id("no_abs_doc"),
        [("Section 1", "Regulatory compliance framework for medical devices.")],
    )

    request = DiscoverRequest(mode=RetrievalMode.SEMANTIC, query="regulatory compliance")
    response = await retrieval_service.discover(request)

    hits_by_id = {h.document.id: h for h in response.results}
    assert _id("no_abs_doc") in hits_by_id
    assert hits_by_id[_id("no_abs_doc")].document.semantic_abstract is None


# BH-103: Catalog mode returns semantic_abstract on DocumentSummary
async def test_bh_103_catalog_returns_abstract(
    graph_store,
    retrieval_service,
):
    """Catalog mode includes semantic_abstract on DocumentSummary for all documents."""
    doc_with = _make_doc(
        _id("cat_abs"),
        doc_type="design_spec",
        semantic_abstract="Summary of design draft for metabolic monitoring.",
    )
    doc_without = _make_doc(
        _id("cat_no_abs"),
        doc_type="design_spec",
        pipeline_status=PipelineStatus.ABSTRACTION_SKIPPED,
    )
    await graph_store.insert_document(doc_with)
    await graph_store.insert_document(doc_without)

    request = DiscoverRequest(
        mode=RetrievalMode.CATALOG,
        filters=RetrievalFilters(doc_type="design_spec"),
        limit=10,
        include_abstracts=True,
    )
    response = await retrieval_service.discover(request)

    hits_by_id = {h.document.id: h for h in response.results}
    assert hits_by_id[_id("cat_abs")].document.semantic_abstract == (
        "Summary of design draft for metabolic monitoring."
    )
    assert hits_by_id[_id("cat_no_abs")].document.semantic_abstract is None


# ---------------------------------------------------------------------------
# Tier3_metadata round-trips through the DocumentSummary projection
# in all three retrieval modes (catalog, semantic, keyword). The data is
# already populated on the underlying Document; the projection used to drop
# it, forcing callers to issue N filtered queries or per-document reads to
# recover fields like ``ticket_priority`` or ``failure_class``.
# ---------------------------------------------------------------------------


async def test_catalog_includes_tier3_metadata(
    graph_store,
    retrieval_service,
):
    """Catalog mode populates tier3_metadata on every hit where present."""
    doc_high = _make_doc(
        _id("t0090_high"),
        doc_type="ticket",
        tier3_metadata={"ticket_id": "T-0001", "ticket_priority": "high"},
    )
    doc_low = _make_doc(
        _id("t0090_low"),
        doc_type="ticket",
        tier3_metadata={"ticket_id": "T-0002", "ticket_priority": "low"},
    )
    doc_no_tier3 = _make_doc(
        _id("t0090_plain"),
        doc_type="note",
    )
    await graph_store.insert_document(doc_high)
    await graph_store.insert_document(doc_low)
    await graph_store.insert_document(doc_no_tier3)

    request = DiscoverRequest(
        mode=RetrievalMode.CATALOG,
        filters=RetrievalFilters(doc_type="ticket"),
    )
    response = await retrieval_service.discover(request)

    hits_by_id = {h.document.id: h for h in response.results}
    assert hits_by_id[_id("t0090_high")].document.tier3_metadata == {
        "ticket_id": "T-0001",
        "ticket_priority": "high",
    }
    assert hits_by_id[_id("t0090_low")].document.tier3_metadata == {
        "ticket_id": "T-0002",
        "ticket_priority": "low",
    }


async def test_catalog_tier3_metadata_null_when_unset(
    graph_store,
    retrieval_service,
):
    """tier3_metadata is None (not {}, not omitted) for docs without tier3."""
    doc = _make_doc(_id("t0090_plain_only"), doc_type="note")
    await graph_store.insert_document(doc)

    request = DiscoverRequest(
        mode=RetrievalMode.CATALOG,
        filters=RetrievalFilters(doc_type="note"),
    )
    response = await retrieval_service.discover(request)

    assert len(response.results) == 1
    hit = response.results[0]
    assert hit.document.id == _id("t0090_plain_only")
    assert hit.document.tier3_metadata is None


async def test_catalog_tier3_filter_round_trips_projection(
    graph_store,
    retrieval_service,
):
    """Catalog filter + projection are consistent: every returned hit's
    tier3 fields match the filter that selected it. This catches the bug
    where the filter pushes down correctly but the projection silently
    drops the field, leaving the caller unable to verify what they got.
    """
    docs = [
        _make_doc(
            _id(f"t0090_filt_{n}"),
            doc_type="ticket",
            tier3_metadata={"ticket_id": f"T-010{n}", "ticket_priority": prio},
        )
        for n, prio in enumerate(["high", "medium", "high"])
    ]
    for doc in docs:
        await graph_store.insert_document(doc)

    request = DiscoverRequest(
        mode=RetrievalMode.CATALOG,
        filters=RetrievalFilters(
            doc_type="ticket",
            tier3_metadata={"ticket_priority": "high"},
        ),
    )
    response = await retrieval_service.discover(request)

    assert len(response.results) == 2
    for hit in response.results:
        assert hit.document.tier3_metadata is not None
        assert hit.document.tier3_metadata["ticket_priority"] == "high"


async def test_semantic_includes_tier3_metadata(
    graph_store,
    stub_content_store,
    seeded_embedding_provider,
    retrieval_service,
):
    """Semantic mode populates tier3_metadata on each hit."""
    doc = _make_doc(
        _id("t0090_sem"),
        doc_type="ticket",
        tier3_metadata={"ticket_id": "T-0501", "ticket_priority": "high"},
    )
    await graph_store.insert_document(doc)

    await _index_doc_chunks(
        stub_content_store,
        seeded_embedding_provider,
        _id("t0090_sem"),
        [("Section 1", "Insulin pump telemetry calibration procedures.")],
    )

    request = DiscoverRequest(
        mode=RetrievalMode.SEMANTIC,
        query="insulin pump",
    )
    response = await retrieval_service.discover(request)

    hits_by_id = {h.document.id: h for h in response.results}
    assert _id("t0090_sem") in hits_by_id
    assert hits_by_id[_id("t0090_sem")].document.tier3_metadata == {
        "ticket_id": "T-0501",
        "ticket_priority": "high",
    }


async def test_keyword_includes_tier3_metadata(
    graph_store,
    stub_content_store,
    seeded_embedding_provider,
    retrieval_service,
):
    """Keyword mode populates tier3_metadata on each hit."""
    doc = _make_doc(
        _id("t0090_kw"),
        doc_type="ticket",
        tier3_metadata={"ticket_id": "T-0502", "ticket_priority": "medium"},
    )
    await graph_store.insert_document(doc)

    await _index_doc_chunks(
        stub_content_store,
        seeded_embedding_provider,
        _id("t0090_kw"),
        [("Section 1", "Glucose sensor electrochemistry whitepaper.")],
    )

    request = DiscoverRequest(
        mode=RetrievalMode.KEYWORD,
        query="glucose sensor",
    )
    response = await retrieval_service.discover(request)

    hits_by_id = {h.document.id: h for h in response.results}
    assert _id("t0090_kw") in hits_by_id
    assert hits_by_id[_id("t0090_kw")].document.tier3_metadata == {
        "ticket_id": "T-0502",
        "ticket_priority": "medium",
    }


# ---------------------------------------------------------------------------
# Catalog mode budget-hint surfacing
# ---------------------------------------------------------------------------


async def _seed_portfolio(
    graph_store,
    n: int,
    *,
    with_tier3: bool = True,
    id_prefix: str = "t0091_p",
    tied_date: str | None = None,
) -> list[str]:
    """Seed ``n`` ticket-shaped documents with realistic projection-weight fields.

    Mirrors the projection shape of real CAS tickets (title, tags,
    source_path, tier3_metadata) so byte counts approximate production
    payload weight per record. Returns the inserted document IDs in order.

    ``tied_date`` puts every record on one ``document_date`` instead of the
    rotating one, so the catalog sort ties everywhere rather than in groups of
    two or three. Callers that need the ordering to have something to be total
    over pass it; the default keeps the rotating dates the byte-weight tests
    were calibrated against.
    """
    base_date = datetime(2026, 5, 1, tzinfo=timezone.utc)
    inserted: list[str] = []
    for i in range(n):
        doc_id = _id(f"{id_prefix}_{i:04d}")
        tier3 = (
            {
                "ticket_id": f"T-9{i:04d}",
                "ticket_type": "feature",
                "ticket_priority": "medium",
            }
            if with_tier3
            else None
        )
        doc = _make_doc(
            doc_id,
            doc_type="ticket",
            tags=["ticket", "phase-2", "sage", "retrieval"],
            document_date=tied_date or f"2026-05-{(i % 28) + 1:02d}",
            source_modified_at=base_date + timedelta(hours=i),
            tier3_metadata=tier3,
        )
        await graph_store.insert_document(doc)
        inserted.append(doc_id)
    return inserted


async def test_catalog_emits_budget_hint_when_response_exceeds_budget(
    graph_store,
    retrieval_service,
    monkeypatch,
):
    """Catalog mode emits a budget hint when the serialized response > budget."""
    monkeypatch.setenv("SAGE_MCP_INLINE_BUDGET_BYTES", "4096")
    await _seed_portfolio(graph_store, 60)

    request = DiscoverRequest(
        mode=RetrievalMode.CATALOG,
        filters=RetrievalFilters(doc_type="ticket"),
        limit=100,
    )
    response = await retrieval_service.discover(request)

    assert response.hints is not None
    assert response.hints.get("reason") == "response_exceeds_inline_budget"
    assert response.hints.get("budget_bytes") == 4096
    assert response.hints.get("response_size_bytes", 0) >= 4096
    recommended = response.hints.get("recommended_limit")
    assert isinstance(recommended, int)
    assert 1 <= recommended < 100
    assert recommended < len(response.results)


async def test_catalog_no_budget_hint_when_under_budget(
    graph_store,
    retrieval_service,
):
    """Small responses do not carry a budget hint at the production budget."""
    await _seed_portfolio(graph_store, 3)

    request = DiscoverRequest(
        mode=RetrievalMode.CATALOG,
        filters=RetrievalFilters(doc_type="ticket"),
        limit=10,
    )
    response = await retrieval_service.discover(request)

    assert response.hints is None or "recommended_limit" not in response.hints


async def test_recommended_limit_re_pages_within_budget(
    graph_store,
    retrieval_service,
    monkeypatch,
):
    """Re-querying at the recommended limit produces an inline response."""
    monkeypatch.setenv("SAGE_MCP_INLINE_BUDGET_BYTES", "4096")
    await _seed_portfolio(graph_store, 60)

    first = await retrieval_service.discover(
        DiscoverRequest(
            mode=RetrievalMode.CATALOG,
            filters=RetrievalFilters(doc_type="ticket"),
            limit=100,
        )
    )
    assert first.hints is not None
    recommended = first.hints["recommended_limit"]
    assert recommended >= 1

    second = await retrieval_service.discover(
        DiscoverRequest(
            mode=RetrievalMode.CATALOG,
            filters=RetrievalFilters(doc_type="ticket"),
            limit=recommended,
        )
    )
    assert second.hints is None or "recommended_limit" not in second.hints
    assert len(second.results) == recommended


#: Rows to seed so a catalog page overruns the production budget on weight
#: alone. Derived from the budget at a deliberately low per-row estimate --
#: a seeded ticket record projects well above 500 bytes, so the page clears
#: the budget with the same roughly-twofold headroom the facets fixtures
#: keep, and keeps it when the budget is recalibrated.
_OVER_BUDGET_ROW_COUNT = DEFAULT_MCP_INLINE_BUDGET_BYTES // 500


async def test_recommended_limit_re_pages_within_the_production_budget(
    graph_store,
    retrieval_service,
    monkeypatch,
):
    """The catalog hint's re-page fits at the shipped budget, not a pinned one.

    Its sibling above runs the same path against a 4,096-byte override,
    which exercises the plumbing but says nothing about the value
    callers actually meet. Both facets fixtures cross the production
    budget; without this the catalog half of that guarantee rests on a
    number no caller ever sees.

    Anti-coincidental-pass: the crossing is asserted on the measured
    response rather than assumed from the row count, so a seeding change
    that stopped overrunning the budget reddens here instead of passing
    vacuously. The recommendation is asserted strictly above the floor
    and strictly below the rows in hand -- the fit assertion alone is
    satisfied by an implementation that always names 1, which would hand
    a caller one row out of ninety, and equally by one that names the
    count it was given and shrinks nothing.
    """
    monkeypatch.delenv("SAGE_MCP_INLINE_BUDGET_BYTES", raising=False)
    await _seed_portfolio(graph_store, _OVER_BUDGET_ROW_COUNT)

    first = await retrieval_service.discover(
        DiscoverRequest(
            mode=RetrievalMode.CATALOG,
            filters=RetrievalFilters(doc_type="ticket"),
            limit=_OVER_BUDGET_ROW_COUNT,
        )
    )
    # Keyed on the reason rather than on hints being present: the dict is
    # shared with the vocabulary warnings, so a page that did not cross can
    # still arrive carrying one, and testing presence alone reports the miss
    # as a KeyError several lines later instead of as the fixture's own.
    assert first.hints is not None and (
        first.hints.get("reason") == "response_exceeds_inline_budget"
    ), (
        f"{_OVER_BUDGET_ROW_COUNT} seeded rows did not overrun the "
        f"{DEFAULT_MCP_INLINE_BUDGET_BYTES}-byte budget; the fixture no longer "
        "crosses the line it exists to cross"
    )
    assert first.hints["budget_bytes"] == DEFAULT_MCP_INLINE_BUDGET_BYTES
    assert first.hints["response_size_bytes"] > DEFAULT_MCP_INLINE_BUDGET_BYTES

    recommended = first.hints["recommended_limit"]
    assert 1 < recommended < _OVER_BUDGET_ROW_COUNT

    second = await retrieval_service.discover(
        DiscoverRequest(
            mode=RetrievalMode.CATALOG,
            filters=RetrievalFilters(doc_type="ticket"),
            limit=recommended,
        )
    )
    assert second.hints is None or "recommended_limit" not in second.hints, (
        f"the re-page at the recommended limit {recommended} still carries a "
        "budget hint, so the recommendation did not fit"
    )
    assert len(second.results) == recommended

    # Fitting is not the promise; the largest fitting page is. One more row
    # must not fit, or a recommendation far below the maximum satisfies every
    # assertion above while costing the caller pages it did not need -- the
    # same defect as the floor collapse, just less visible.
    one_more = await retrieval_service.discover(
        DiscoverRequest(
            mode=RetrievalMode.CATALOG,
            filters=RetrievalFilters(doc_type="ticket"),
            limit=recommended + 1,
        )
    )
    assert one_more.hints is not None and (
        one_more.hints.get("reason") == "response_exceeds_inline_budget"
    ), (
        f"a page of {recommended + 1} rows also fits the budget, so "
        f"{recommended} was not the largest fitting re-page"
    )


async def _perturb_scan_order(graph_store, doc_id: str) -> None:
    """Move a row's physical position without changing anything it returns.

    Rewriting a column to the value it already holds is a semantic no-op, but
    Postgres answers it with a new tuple version at a new position, so a
    subsequent scan hands the sorter a different input order. Under a total
    order that cannot change the result; under one with ties it is what lets
    two calls disagree. ``last_modified_by`` is neither a sortable column nor
    part of the default clause, and the write goes through the store's own
    update path.
    """
    doc = await graph_store.get_document(doc_id)
    await graph_store.update_document(doc_id, {"last_modified_by": doc.last_modified_by})


def _delivered_bytes(resp) -> int:
    """The byte count the MCP runtime puts on the wire for this response.

    Reproduces both halves of the delivery path -- the tool layer's
    ``model_dump(mode="json", exclude_none=True)`` and the runtime's
    ``to_json(..., indent=2)`` -- here in the test rather than calling the
    service's own ``_serialized_response_bytes``. Calling that helper would
    measure the recommendation against the same expression that produced it,
    which is exactly the circularity this assertion exists to escape.
    """
    return len(
        pydantic_core.to_json(
            resp.model_dump(mode="json", exclude_none=True), fallback=str, indent=2
        )
    )


def _row_bytes(hit) -> int:
    """One result row's own encoded weight, envelope excluded.

    Used only to rank rows by weight; the absolute number is meaningless on its
    own because the response envelope is not in it.
    """
    return len(pydantic_core.to_json(hit.model_dump(mode="json", exclude_none=True)))


async def _seed_uneven_tied_portfolio(graph_store) -> list[str]:
    """A tied portfolio whose records differ sharply in byte weight.

    Two properties the ordinary ``_seed_portfolio`` does not have, and the test
    below needs both. Every record shares one ``document_date`` and one
    ``lifecycle_status``, so the catalog sort ties across the whole set and has
    nothing but a tiebreak to order it by. And the records come in two weights:
    thirty light ones carrying no tags and no tier3, then thirty heavy ones
    carrying six long tags apiece.

    The weight asymmetry is what makes reordering *cost* something. A uniform
    portfolio can be reordered freely and every prefix of a given length weighs
    the same, so a simulation over the wrong rows still lands on the right byte
    count -- the defect would be real but invisible. Here two selections of the
    same size can differ by more than the ceiling, and the test asserts that
    rather than assuming it.

    Note what the two halves do *not* do once the ordering is total: they do
    not divide the result. The tiebreak orders by document id, which is a hash,
    so the halves interleave and any prefix is a mixture. Seeding order governs
    only the physical scan order the perturbation below disturbs.

    Returns the light ids, in seeding order.
    """
    light: list[str] = []
    for i in range(30):
        doc_id = _id(f"uneven_light_{i:04d}")
        await graph_store.insert_document(
            _make_doc(doc_id, doc_type="ticket", document_date="2026-05-01")
        )
        light.append(doc_id)
    for i in range(30):
        await graph_store.insert_document(
            _make_doc(
                _id(f"uneven_heavy_{i:04d}"),
                doc_type="ticket",
                document_date="2026-05-01",
                tags=[f"retrieval-conformance-dimension-{i:04d}-{n}" for n in range(6)],
                tier3_metadata={
                    "ticket_id": f"T-8{i:04d}",
                    "ticket_type": "feature",
                    "ticket_priority": "medium",
                },
            )
        )
    return light


async def test_recommended_limit_re_pages_within_delivered_bytes(
    graph_store,
    retrieval_service,
    monkeypatch,
):
    """A re-page at ``recommended_limit`` returns that prefix, and fits the ceiling.

    Two assertions doing two different jobs, and it is worth being exact about
    which one carries which.

    The **prefix assertion** is the regression. The hint recommends the largest
    row count whose re-page fits, found by truncating the response it already
    has -- sound only if a lower limit returns the same rows in the same order,
    which is what ``_response_at_row_count`` states and what a non-total order
    does not supply. Perturbing the scan order between the two calls is enough
    to break it, and this assertion fails against the ordering as it stood.

    The **byte assertion** is the guarantee, stated on the bytes the runtime
    delivers rather than on the service's own measurement, which cannot catch
    a recommendation that is wrong in the same way twice. It is not a
    discriminator against the old ordering -- a reordered prefix still fits the
    ceiling whenever the rows it drew from weigh about the same -- and it is
    not being claimed as one. What it pins is that the number the hint names is
    honest at the wire.

    The fixture's weight asymmetry is what keeps the byte assertion from being
    vacuous: over a uniform portfolio every prefix of a given length weighs the
    same, so the check would hold no matter which rows came back.
    """
    monkeypatch.setenv("SAGE_MCP_INLINE_BUDGET_BYTES", "4096")
    light = await _seed_uneven_tied_portfolio(graph_store)

    request = DiscoverRequest(
        mode=RetrievalMode.CATALOG,
        filters=RetrievalFilters(doc_type="ticket"),
        limit=100,
    )
    first = await retrieval_service.discover(request)
    assert first.hints is not None
    recommended = first.hints["recommended_limit"]
    assert recommended > 1, (
        "a hint that always names 1 satisfies every assertion below without "
        "measuring anything; the light rows must admit a prefix wider than one"
    )

    # The weight asymmetry, read rather than asserted in prose. Some selection
    # of exactly this many rows overruns the ceiling that the selection
    # actually returned fits inside, which is what makes the byte assertion
    # below sensitive to *which* rows come back rather than only to how many.
    # Without this the asymmetry is inert setup: flattening the fixture to one
    # weight would leave the test green and its docstring's claim about
    # non-vacuity unchecked.
    by_weight = sorted(first.results, key=_row_bytes, reverse=True)
    heaviest = first.model_copy(update={"results": by_weight[:recommended]})
    assert _delivered_bytes(heaviest) > 4096, (
        f"the {recommended} heaviest rows must not fit the ceiling, or the "
        "fixture carries no weight asymmetry for the byte assertion to detect"
    )

    for doc_id in light:
        await _perturb_scan_order(graph_store, doc_id)

    second = await retrieval_service.discover(
        DiscoverRequest(
            mode=RetrievalMode.CATALOG,
            filters=RetrievalFilters(doc_type="ticket"),
            limit=recommended,
        )
    )

    assert len(second.results) == recommended
    assert [h.document.id for h in first.results[:recommended]] == [
        h.document.id for h in second.results
    ], "the re-page must return the prefix the recommendation was measured on"
    assert _delivered_bytes(second) <= 4096, (
        f"re-page at the recommended limit {recommended} delivered "
        f"{_delivered_bytes(second)} bytes against a 4096-byte ceiling"
    )


async def test_facets_value_order_breaks_a_count_tie_on_the_value(
    graph_store,
    retrieval_service,
):
    """The facets arm needs no document tiebreak, and this is what says so.

    Facet values order by ``doc_count DESC, value ASC`` over a GROUP BY on the
    field, so a value appears once per row and ``value ASC`` leaves no tie
    behind. That is the premise ``_facets_response_at_cap`` relies on to
    simulate a re-call by truncating, and it holds for a reason the document
    query's ordering does not share -- which is why this arm takes no
    primary-key tiebreak. Green before the document-side one lands as well as
    after; a red here would mean the analysis of this arm is wrong, not that
    this arm needs fixing.

    The assertion is on the *order*, not on two calls agreeing. Agreement is
    the weaker claim and the one a stable-by-accident order also satisfies:
    with ``value ASC`` deleted from the aggregate, two calls still match each
    other while the order is no longer total. So the fixture ties two values on
    count, and the assertion is that they come back ascending.

    Be exact about what that buys, because the obvious stronger reading is
    wrong. With ``value ASC`` deleted the rows reach the sort in the hash
    aggregate's own emission order -- a function of the two strings and the
    hash table's size, which neither this fixture nor any ordering of its
    inserts controls. Deleting the term was *observed* to turn this red here
    (``ticket`` ahead of ``adr``), so what the test is is a removal guard
    verified by mutation, not a discriminator by construction: on a server
    whose buckets fall the other way the mutant would pass and nothing in the
    test would say so. Widening the tie to five or six values would make an
    accidentally ascending emission improbable rather than a coin toss, and
    still would not make it impossible.
    """
    for n in range(4):
        await graph_store.insert_document(
            _make_doc(_id(f"facettie_{n:02d}"), doc_type="adr" if n < 2 else "ticket")
        )

    response = await retrieval_service.discover(
        DiscoverRequest(
            mode=RetrievalMode.CATALOG,
            target=RetrievalTarget.FACETS,
            facet_fields=["doc_type"],
        )
    )

    counts = response.results[0].values
    assert sorted(counts.values()) == [2, 2], (
        "the fixture must tie two facet values on count, or the order has no "
        f"tie to be total over: {counts}"
    )
    assert list(counts) == ["adr", "ticket"], (
        f"count-tied facet values must come back in ascending value order: {counts}"
    )


async def test_response_size_bytes_matches_serialization(
    graph_store,
    retrieval_service,
    monkeypatch,
):
    """response_size_bytes matches the length the runtime delivers.

    Exact, not within a tolerance, and denominated in the delivered
    encoding rather than a compact one. The earlier form restated
    ``json.dumps(model_dump(...))`` — the same expression the
    implementation used — inside a 1% band, so it could confirm only
    that the reported number matched a second copy of the measurement,
    and not that either matched the wire. The runtime encodes the dict
    the tool layer returns with ``to_json(..., indent=2)``, which on a
    high-cardinality payload runs 17% above the compact form.
    """
    monkeypatch.setenv("SAGE_MCP_INLINE_BUDGET_BYTES", "4096")
    await _seed_portfolio(graph_store, 60)

    response = await retrieval_service.discover(
        DiscoverRequest(
            mode=RetrievalMode.CATALOG,
            filters=RetrievalFilters(doc_type="ticket"),
            limit=100,
        )
    )

    assert response.hints is not None
    reported = response.hints["response_size_bytes"]

    def delivered(resp) -> int:
        return len(
            pydantic_core.to_json(
                resp.model_dump(mode="json", exclude_none=True), fallback=str, indent=2
            )
        )

    # The size is measured before the hint is attached, so the response
    # it describes is this one minus the hint's own four keys.
    without_hint = response.model_copy(
        update={
            "hints": {
                k: v
                for k, v in response.hints.items()
                if k not in ("reason", "response_size_bytes", "budget_bytes", "recommended_limit")
            }
            or None
        }
    )
    assert reported == delivered(without_hint), (
        f"reported={reported}, delivered={delivered(without_hint)}"
    )

    # Two anti-coincidental checks. The encodings must actually differ
    # on this payload, or the equality above holds against the compact
    # one too and says nothing about which was measured. And the hint
    # must cost something, or the "before it is attached" clause above
    # is untested scaffolding rather than the reason for the exclusion.
    compact = len(json.dumps(response.model_dump(mode="json", exclude_none=True)).encode("utf-8"))
    assert delivered(without_hint) > compact
    assert delivered(response) > reported


async def test_budget_hint_respects_env_override(
    graph_store,
    retrieval_service,
    monkeypatch,
):
    """SAGE_MCP_INLINE_BUDGET_BYTES env var controls when the hint fires."""
    await _seed_portfolio(graph_store, 30)

    request = DiscoverRequest(
        mode=RetrievalMode.CATALOG,
        filters=RetrievalFilters(doc_type="ticket"),
        limit=100,
    )

    monkeypatch.setenv("SAGE_MCP_INLINE_BUDGET_BYTES", "1024")
    tight = await retrieval_service.discover(request)
    assert tight.hints is not None
    assert tight.hints.get("budget_bytes") == 1024

    monkeypatch.delenv("SAGE_MCP_INLINE_BUDGET_BYTES", raising=False)
    loose = await retrieval_service.discover(request)
    assert loose.hints is None or "recommended_limit" not in loose.hints


def test_inline_budget_stays_below_the_measured_ceiling():
    """The default budget sits under the ceiling, by the margin claimed.

    The budget is a measurement minus a margin, and both halves of that
    are constants a later edit can move independently. Nothing else
    would notice a budget raised to or past the ceiling it was derived
    from: every hint would keep firing, every fixture would keep
    passing, and the only symptom would be responses the hints called
    safe arriving by disk round-trip instead.

    The margin is asserted as a floor rather than an equality so that
    re-measuring against a new client can move the ceiling without
    editing this, while a budget that creeps up toward whatever ceiling
    is recorded still reddens.
    """
    from sage.services.retrieval import _MEASURED_INLINE_CEILING_BYTES

    assert DEFAULT_MCP_INLINE_BUDGET_BYTES < _MEASURED_INLINE_CEILING_BYTES, (
        f"budget {DEFAULT_MCP_INLINE_BUDGET_BYTES} is not below the measured "
        f"ceiling {_MEASURED_INLINE_CEILING_BYTES}; a response the hints "
        "declare inline would fall back to the disk round-trip"
    )
    # Integer arithmetic on purpose: the intended budget is exactly nine
    # tenths of the ceiling, and in binary floating point that difference
    # lands a hair under 0.10 and reddens a margin that is in fact met.
    assert DEFAULT_MCP_INLINE_BUDGET_BYTES * 10 <= _MEASURED_INLINE_CEILING_BYTES * 9, (
        f"budget {DEFAULT_MCP_INLINE_BUDGET_BYTES} leaves less than the 10% "
        f"below {_MEASURED_INLINE_CEILING_BYTES} that the constant's comment "
        "claims; either restore the margin or restate the claim"
    )


async def test_budget_hint_accounts_for_tier3_metadata(
    graph_store,
    retrieval_service,
    monkeypatch,
):
    """tier3_metadata projection growth pushes recommended_limit lower."""
    monkeypatch.setenv("SAGE_MCP_INLINE_BUDGET_BYTES", "2048")

    ids_yes = await _seed_portfolio(graph_store, 40, with_tier3=True, id_prefix="t0091_t3_yes")
    with_t3 = await retrieval_service.discover(
        DiscoverRequest(
            mode=RetrievalMode.CATALOG,
            filters=RetrievalFilters(doc_type="ticket", document_ids=ids_yes),
            limit=100,
        )
    )

    ids_no = await _seed_portfolio(graph_store, 40, with_tier3=False, id_prefix="t0091_t3_no")
    without_t3 = await retrieval_service.discover(
        DiscoverRequest(
            mode=RetrievalMode.CATALOG,
            filters=RetrievalFilters(doc_type="ticket", document_ids=ids_no),
            limit=100,
        )
    )

    assert with_t3.hints is not None
    assert without_t3.hints is not None
    assert with_t3.hints["response_size_bytes"] > without_t3.hints["response_size_bytes"]
    assert with_t3.hints["recommended_limit"] < without_t3.hints["recommended_limit"]


async def test_budget_hint_absent_on_empty_results(
    graph_store,
    retrieval_service,
    monkeypatch,
):
    """No budget hint fires when there are no results, even at a tiny budget."""
    monkeypatch.setenv("SAGE_MCP_INLINE_BUDGET_BYTES", "256")

    request = DiscoverRequest(
        mode=RetrievalMode.CATALOG,
        filters=RetrievalFilters(doc_type="never_matches_anything"),
        limit=100,
    )
    response = await retrieval_service.discover(request)

    assert len(response.results) == 0
    if response.hints is not None:
        assert "recommended_limit" not in response.hints
        assert "response_size_bytes" not in response.hints


async def test_conformance_full_ticket_portfolio_fits_inline_at_default_limit(
    graph_store,
    retrieval_service,
):
    """120-doc ticket portfolio at default limit=10 fits inline.

    Anchored to the budget rather than to a number of its own, so that
    recalibrating the budget re-poses this question instead of leaving
    it asserting against a figure nothing uses. The absent hint and the
    measured size are two readings of the same claim: the first is what
    the service concluded, the second what a caller would weigh.
    """
    await _seed_portfolio(graph_store, 120)

    request = DiscoverRequest(
        mode=RetrievalMode.CATALOG,
        filters=RetrievalFilters(doc_type="ticket"),
    )
    response = await retrieval_service.discover(request)

    assert response.hints is None or "recommended_limit" not in response.hints
    payload_size = len(
        json.dumps(response.model_dump(mode="json", exclude_none=True)).encode("utf-8")
    )
    assert payload_size < DEFAULT_MCP_INLINE_BUDGET_BYTES, (
        f"Default-limit portfolio response {payload_size}B exceeds the "
        f"{DEFAULT_MCP_INLINE_BUDGET_BYTES}-byte inline budget."
    )


# BH-104: Document-level response mode preserves semantic_abstract
async def test_bh_104_response_mode_light_preserves_abstract(
    graph_store,
    stub_content_store,
    seeded_embedding_provider,
    retrieval_service,
):
    """response_mode=light suppresses chunk_content but preserves
    semantic_abstract (after the response_level retirement)."""
    abstract_text = "Overview of insulin pump safety requirements."
    doc = _make_doc(_id("rl_doc"), semantic_abstract=abstract_text)
    await graph_store.insert_document(doc)

    await _index_doc_chunks(
        stub_content_store,
        seeded_embedding_provider,
        _id("rl_doc"),
        [("Section 1", "Insulin pump safety and regulatory requirements.")],
    )

    request = DiscoverRequest(
        mode=RetrievalMode.SEMANTIC,
        query="insulin pump safety",
        response_mode=ResponseMode.LIGHT,
        include_abstracts=True,
    )
    response = await retrieval_service.discover(request)

    hits_by_id = {h.document.id: h for h in response.results}
    assert _id("rl_doc") in hits_by_id
    hit = hits_by_id[_id("rl_doc")]
    # chunk_content suppressed by response_mode=light
    assert hit.chunk_content is None
    # semantic_abstract preserved because include_abstracts=True
    assert hit.document.semantic_abstract == abstract_text


# ---------------------------------------------------------------------------
# Semantic Abstract Consumer Tests (CAS-ADR-011, Phase 2: Two-Pass Retrieval)
# ---------------------------------------------------------------------------


# BH-105: Abstract prefilter boosts documents whose abstract matches query
async def test_bh_105_abstract_prefilter_boosts_matching_abstract(
    graph_store,
    stub_content_store,
    seeded_embedding_provider,
    retrieval_service,
):
    """Documents whose abstract matches the query rank above those whose abstract does not.

    doc_b is inserted first and has slightly better BM25 content for the query
    (more matching terms), so without the abstract boost it would rank first.
    The abstract boost on doc_a should override this natural advantage.
    """
    # Both documents have the same chunk content (identical BM25 and vector
    # scores). doc_b is inserted first so it would naturally rank first or
    # tie. The abstract boost on doc_a should push it above doc_b.
    shared_content = "Glucose monitoring sensor accuracy and calibration data."

    doc_b = _make_doc(
        _id("abs_nomatch"),
        semantic_abstract="Overview of regulatory filing procedures for medical devices.",
    )
    await graph_store.insert_document(doc_b)
    await _index_doc_chunks(
        stub_content_store,
        seeded_embedding_provider,
        _id("abs_nomatch"),
        [("Section 1", shared_content)],
    )

    doc_a = _make_doc(
        _id("abs_match"),
        semantic_abstract=(
            "Comprehensive review of glucose monitoring technologies and sensor calibration."
        ),
    )
    await graph_store.insert_document(doc_a)
    await _index_doc_chunks(
        stub_content_store,
        seeded_embedding_provider,
        _id("abs_match"),
        [("Section 1", shared_content)],
    )

    request = DiscoverRequest(
        mode=RetrievalMode.SEMANTIC,
        query="glucose monitoring",
    )
    response = await retrieval_service.discover(request)

    ids = [h.document.id for h in response.results]
    assert _id("abs_match") in ids
    assert _id("abs_nomatch") in ids
    assert ids.index(_id("abs_match")) < ids.index(_id("abs_nomatch"))


# BH-106: Abstract prefilter does not exclude documents without abstracts
async def test_bh_106_abstract_prefilter_does_not_exclude_abstractless_docs(
    graph_store,
    stub_content_store,
    seeded_embedding_provider,
    retrieval_service,
):
    """Documents with no abstract still appear in results from chunk search."""
    doc_with = _make_doc(
        _id("has_abs"),
        semantic_abstract="Report claim analysis for metabolic biomarkers.",
    )
    doc_without = _make_doc(
        _id("no_abs"),
        pipeline_status=PipelineStatus.ABSTRACTION_SKIPPED,
        semantic_abstract=None,
    )
    await graph_store.insert_document(doc_with)
    await graph_store.insert_document(doc_without)

    shared_content = "Metabolic biomarker detection and claim drafting."
    for doc_id in (_id("has_abs"), _id("no_abs")):
        await _index_doc_chunks(
            stub_content_store,
            seeded_embedding_provider,
            doc_id,
            [("Section 1", shared_content)],
        )

    request = DiscoverRequest(
        mode=RetrievalMode.SEMANTIC,
        query="metabolic biomarker",
    )
    response = await retrieval_service.discover(request)

    ids = [h.document.id for h in response.results]
    assert _id("has_abs") in ids
    assert _id("no_abs") in ids


# BH-107: Abstract prefilter respects scope gating
async def test_bh_107_abstract_prefilter_respects_scope(
    graph_store,
    stub_content_store,
    seeded_embedding_provider,
    retrieval_service,
):
    """Abstract-matched documents failing scope gating are excluded."""
    doc_auth = _make_doc(
        _id("auth_abs"),
        authority_scope="example_vault",
        semantic_abstract="Insulin delivery systems safety analysis.",
    )
    doc_noauth = _make_doc(
        _id("noauth_abs"),
        authority_scope=None,
        semantic_abstract="Insulin delivery systems safety analysis.",
    )
    await graph_store.insert_document(doc_auth)
    await graph_store.insert_document(doc_noauth)

    shared_content = "Insulin pump delivery mechanisms and safety margins."
    for doc_id in (_id("auth_abs"), _id("noauth_abs")):
        await _index_doc_chunks(
            stub_content_store,
            seeded_embedding_provider,
            doc_id,
            [("Section 1", shared_content)],
        )

    request = DiscoverRequest(
        mode=RetrievalMode.SEMANTIC,
        query="insulin delivery",
        scope=RetrievalScope.AUTHORITATIVE,
    )
    response = await retrieval_service.discover(request)

    ids = [h.document.id for h in response.results]
    assert _id("auth_abs") in ids
    assert _id("noauth_abs") not in ids


# BH-108: use_abstract_prefilter=False disables abstract boost
async def test_bh_108_abstract_prefilter_disabled(
    graph_store,
    stub_content_store,
    seeded_embedding_provider,
    retrieval_service,
):
    """With use_abstract_prefilter=False, no abstract-derived boost is applied."""
    shared_content = "Continuous glucose monitor calibration procedures."

    doc_a = _make_doc(
        _id("boost_a"),
        semantic_abstract="Continuous glucose monitor calibration and accuracy.",
    )
    doc_b = _make_doc(
        _id("boost_b"),
        semantic_abstract="Unrelated: marine biology research protocols.",
    )
    await graph_store.insert_document(doc_a)
    await graph_store.insert_document(doc_b)

    for doc_id in (_id("boost_a"), _id("boost_b")):
        await _index_doc_chunks(
            stub_content_store,
            seeded_embedding_provider,
            doc_id,
            [("Section 1", shared_content)],
        )

    # With prefilter disabled, identical chunk content => similar scores
    request = DiscoverRequest(
        mode=RetrievalMode.SEMANTIC,
        query="glucose monitor calibration",
        use_abstract_prefilter=False,
    )
    response = await retrieval_service.discover(request)

    hits_by_id = {h.document.id: h for h in response.results}
    assert _id("boost_a") in hits_by_id
    assert _id("boost_b") in hits_by_id
    # Scores should be approximately equal (no abstract boost)
    score_a = hits_by_id[_id("boost_a")].relevance_score
    score_b = hits_by_id[_id("boost_b")].relevance_score
    assert score_a is not None and score_b is not None
    # Allow small difference from salience reranking, but no large abstract boost
    assert abs(score_a - score_b) / max(score_a, score_b) < 0.25


# BH-109: Keyword mode benefits from abstract prefilter
async def test_bh_109_keyword_mode_abstract_prefilter(
    graph_store,
    stub_content_store,
    seeded_embedding_provider,
    retrieval_service,
):
    """Abstract prefilter boosts keyword (BM25) results the same as semantic.

    doc_b is inserted first with better BM25 content for the query. Without the
    abstract boost, doc_b would rank first. The abstract boost on doc_a should
    override this.
    """
    # Both documents share identical chunk content. doc_b is inserted first
    # so it naturally appears first. The abstract boost on doc_a should
    # push it above doc_b.
    shared_content = "Blood pressure monitoring wearable device specifications."

    doc_b = _make_doc(
        _id("kw_abs_nomatch"),
        semantic_abstract="Dental imaging software architecture overview.",
    )
    await graph_store.insert_document(doc_b)
    await _index_doc_chunks(
        stub_content_store,
        seeded_embedding_provider,
        _id("kw_abs_nomatch"),
        [("Section 1", shared_content)],
    )

    doc_a = _make_doc(
        _id("kw_abs_match"),
        semantic_abstract="Blood pressure monitoring device accuracy and specifications.",
    )
    await graph_store.insert_document(doc_a)
    await _index_doc_chunks(
        stub_content_store,
        seeded_embedding_provider,
        _id("kw_abs_match"),
        [("Section 1", shared_content)],
    )

    request = DiscoverRequest(
        mode=RetrievalMode.KEYWORD,
        query="blood pressure monitoring",
    )
    response = await retrieval_service.discover(request)

    ids = [h.document.id for h in response.results]
    assert _id("kw_abs_match") in ids
    assert _id("kw_abs_nomatch") in ids
    assert ids.index(_id("kw_abs_match")) < ids.index(_id("kw_abs_nomatch"))


# BH-110: Abstract prefilter integrates with hybrid RRF
async def test_bh_110_abstract_prefilter_with_hybrid_rrf(
    graph_store,
    stub_content_store,
    seeded_embedding_provider,
    retrieval_service,
):
    """Abstract boost applied after RRF fusion composes with fused scores.

    doc_b is inserted first with better BM25/vector content. Without the
    abstract boost, doc_b would rank first after RRF fusion.
    """
    # Both documents share identical chunk content. doc_b is inserted first
    # so it naturally appears first. The abstract boost on doc_a should
    # push it above doc_b after RRF fusion.
    shared_content = "Wearable ECG arrhythmia detection algorithms."

    doc_b = _make_doc(
        _id("rrf_abs_nomatch"),
        semantic_abstract="Supply chain logistics for pharmaceutical distribution.",
    )
    await graph_store.insert_document(doc_b)
    await _index_doc_chunks(
        stub_content_store,
        seeded_embedding_provider,
        _id("rrf_abs_nomatch"),
        [("Section 1", shared_content)],
    )

    doc_a = _make_doc(
        _id("rrf_abs_match"),
        semantic_abstract="ECG arrhythmia detection in wearable cardiac monitors.",
    )
    await graph_store.insert_document(doc_a)
    await _index_doc_chunks(
        stub_content_store,
        seeded_embedding_provider,
        _id("rrf_abs_match"),
        [("Section 1", shared_content)],
    )

    request = DiscoverRequest(
        mode=RetrievalMode.SEMANTIC,
        query="ECG arrhythmia detection",
        use_hybrid=True,
    )
    response = await retrieval_service.discover(request)

    ids = [h.document.id for h in response.results]
    assert _id("rrf_abs_match") in ids
    assert _id("rrf_abs_nomatch") in ids
    assert ids.index(_id("rrf_abs_match")) < ids.index(_id("rrf_abs_nomatch"))


# BH-111: Abstract boost composes with lifecycle tier sort
async def test_bh_111_abstract_boost_composes_with_lifecycle_tier(
    graph_store,
    stub_content_store,
    seeded_embedding_provider,
    retrieval_service,
):
    """Lifecycle tier sort (BH-069) places active above draft even when both
    receive the abstract boost."""
    shared_content = "Respiratory rate estimation from photoplethysmography."
    shared_abstract = "Respiratory rate analysis using PPG signal processing."

    doc_active = _make_doc(
        _id("sal_active"),
        lifecycle_status="active",
        semantic_abstract=shared_abstract,
    )
    doc_draft = _make_doc(
        _id("sal_draft"),
        lifecycle_status="draft",
        semantic_abstract=shared_abstract,
    )
    await graph_store.insert_document(doc_active)
    await graph_store.insert_document(doc_draft)

    for doc_id in (_id("sal_active"), _id("sal_draft")):
        await _index_doc_chunks(
            stub_content_store,
            seeded_embedding_provider,
            doc_id,
            [("Section 1", shared_content)],
        )

    request = DiscoverRequest(
        mode=RetrievalMode.SEMANTIC,
        query="respiratory rate PPG",
    )
    response = await retrieval_service.discover(request)

    ids = [h.document.id for h in response.results]
    assert _id("sal_active") in ids
    assert _id("sal_draft") in ids
    # Active document ranks first via lifecycle tier sort
    assert ids.index(_id("sal_active")) < ids.index(_id("sal_draft"))


# ---------------------------------------------------------------------------
# BH-112: document_ids filter constrains keyword search to specified documents
# ---------------------------------------------------------------------------


async def test_bh_112_document_ids_filter_keyword_prefilter(
    graph_store, stub_content_store, seeded_embedding_provider, retrieval_service
):
    """document_ids filter constrains keyword BM25 to specified documents.

    Document A has the target term once among many chunks (low BM25 score).
    Documents B and C repeat the term heavily (high BM25 score).
    Without pre-filtering, A would be ranked off the candidate list.
    """
    doc_a = _make_doc(_id("kw_filter_a"))
    doc_b = _make_doc(_id("kw_filter_b"))
    doc_c = _make_doc(_id("kw_filter_c"))
    await graph_store.insert_document(doc_a)
    await graph_store.insert_document(doc_b)
    await graph_store.insert_document(doc_c)

    # Doc A: target term buried among many unrelated chunks
    chunks_a = [("Section 1", "Introduction to clinical normalization.")]
    for i in range(20):
        chunks_a.append((f"Section {i + 2}", f"Unrelated content block {i}."))
    chunks_a.append(("Section 22", "The MLPAO orchestrator handles adjudication."))
    await _index_doc_chunks(
        stub_content_store,
        seeded_embedding_provider,
        _id("kw_filter_a"),
        chunks_a,
    )

    # Docs B and C: term appears in every chunk (high term frequency)
    for doc_id in (_id("kw_filter_b"), _id("kw_filter_c")):
        await _index_doc_chunks(
            stub_content_store,
            seeded_embedding_provider,
            doc_id,
            [(f"S{i}", f"MLPAO MLPAO MLPAO analysis {i}") for i in range(5)],
        )

    request = DiscoverRequest(
        mode=RetrievalMode.KEYWORD,
        query="MLPAO",
        filters=RetrievalFilters(document_ids=[_id("kw_filter_a")]),
    )
    response = await retrieval_service.discover(request)

    doc_ids = [h.document.id for h in response.results]
    assert _id("kw_filter_a") in doc_ids
    assert _id("kw_filter_b") not in doc_ids
    assert _id("kw_filter_c") not in doc_ids


# ---------------------------------------------------------------------------
# BH-113: document_ids filter constrains semantic search to specified documents
# ---------------------------------------------------------------------------


async def test_bh_113_document_ids_filter_semantic_prefilter(
    graph_store, stub_content_store, seeded_embedding_provider, retrieval_service
):
    """document_ids filter constrains semantic search to specified documents."""
    doc_a = _make_doc(_id("sem_filter_a"))
    doc_b = _make_doc(_id("sem_filter_b"))
    doc_c = _make_doc(_id("sem_filter_c"))
    await graph_store.insert_document(doc_a)
    await graph_store.insert_document(doc_b)
    await graph_store.insert_document(doc_c)

    await _index_doc_chunks(
        stub_content_store,
        seeded_embedding_provider,
        _id("sem_filter_a"),
        [("Section 1", "Clinical normalization with MLPAO orchestrator.")],
    )
    await _index_doc_chunks(
        stub_content_store,
        seeded_embedding_provider,
        _id("sem_filter_b"),
        [("Section 1", "Clinical normalization with MLPAO orchestrator.")],
    )
    await _index_doc_chunks(
        stub_content_store,
        seeded_embedding_provider,
        _id("sem_filter_c"),
        [("Section 1", "Gardening tips for spring planting.")],
    )

    request = DiscoverRequest(
        mode=RetrievalMode.SEMANTIC,
        query="MLPAO orchestrator",
        filters=RetrievalFilters(document_ids=[_id("sem_filter_a")]),
    )
    response = await retrieval_service.discover(request)

    doc_ids = [h.document.id for h in response.results]
    assert _id("sem_filter_a") in doc_ids
    assert _id("sem_filter_b") not in doc_ids
    assert _id("sem_filter_c") not in doc_ids


# ---------------------------------------------------------------------------
# BH-114: document_ids filter works with scope ALL (post-filter)
# ---------------------------------------------------------------------------


async def test_bh_114_document_ids_filter_scope_all(
    graph_store, stub_content_store, seeded_embedding_provider, retrieval_service
):
    """document_ids filter applies with default scope ALL, not just SPECIFIC."""
    doc_a = _make_doc(_id("scope_all_a"))
    doc_b = _make_doc(_id("scope_all_b"))
    await graph_store.insert_document(doc_a)
    await graph_store.insert_document(doc_b)

    for doc_id in (_id("scope_all_a"), _id("scope_all_b")):
        await _index_doc_chunks(
            stub_content_store,
            seeded_embedding_provider,
            doc_id,
            [("Section 1", "Report claims analysis for PV07.")],
        )

    request = DiscoverRequest(
        mode=RetrievalMode.KEYWORD,
        query="PV07",
        scope=RetrievalScope.ALL,
        filters=RetrievalFilters(document_ids=[_id("scope_all_a")]),
    )
    response = await retrieval_service.discover(request)

    doc_ids = [h.document.id for h in response.results]
    assert _id("scope_all_a") in doc_ids
    assert _id("scope_all_b") not in doc_ids


# ---------------------------------------------------------------------------
# BH-115: document_ids filter with multiple IDs returns all matching documents
# ---------------------------------------------------------------------------


async def test_bh_115_document_ids_filter_multiple_ids(
    graph_store, stub_content_store, seeded_embedding_provider, retrieval_service
):
    """document_ids with multiple IDs returns all matching, excludes others."""
    doc_a = _make_doc(_id("multi_a"))
    doc_b = _make_doc(_id("multi_b"))
    doc_c = _make_doc(_id("multi_c"))
    await graph_store.insert_document(doc_a)
    await graph_store.insert_document(doc_b)
    await graph_store.insert_document(doc_c)

    for doc_id in (_id("multi_a"), _id("multi_b"), _id("multi_c")):
        await _index_doc_chunks(
            stub_content_store,
            seeded_embedding_provider,
            doc_id,
            [("Section 1", "Report claims analysis for PV07.")],
        )

    request = DiscoverRequest(
        mode=RetrievalMode.KEYWORD,
        query="PV07",
        filters=RetrievalFilters(document_ids=[_id("multi_a"), _id("multi_b")]),
    )
    response = await retrieval_service.discover(request)

    doc_ids = [h.document.id for h in response.results]
    assert _id("multi_a") in doc_ids
    assert _id("multi_b") in doc_ids
    assert _id("multi_c") not in doc_ids


# ---------------------------------------------------------------------------
# Empty-result hints: semantic mode
# ---------------------------------------------------------------------------


async def test_semantic_empty_results_no_hints_when_no_raw_matches(
    graph_store, stub_content_store, seeded_embedding_provider, retrieval_service
):
    """When content store returns zero raw results, hints is None."""
    doc = _make_doc(_id("doc_empty_sem"))
    await graph_store.insert_document(doc)
    # No chunks indexed -- content store will return zero raw results.

    request = DiscoverRequest(
        mode=RetrievalMode.SEMANTIC,
        query="something completely unrelated",
    )
    response = await retrieval_service.discover(request)
    assert response.results == []
    assert response.hints is None


async def test_semantic_empty_results_with_hints_when_filtered_out(
    graph_store, stub_content_store, seeded_embedding_provider, retrieval_service
):
    """When raw results exist but filters exclude all, hints shows the gap."""
    doc = _make_doc(_id("doc_filtered_sem"), project="alpha")
    await graph_store.insert_document(doc)
    await _index_doc_chunks(
        stub_content_store,
        seeded_embedding_provider,
        _id("doc_filtered_sem"),
        [("Section 1", "Interesting report content about claims.")],
    )

    # Search matches content but filter by a non-matching project.
    # With pre-filter resolution, the project filter resolves to zero
    # matching documents and the search short-circuits before calling
    # the content store. total_before_filtering is 0 (no chunks were
    # fetched), but hints still surface the active filters so the
    # caller can see why their search was empty.
    request = DiscoverRequest(
        mode=RetrievalMode.SEMANTIC,
        query="report claims",
        filters=RetrievalFilters(project="nonexistent_project"),
    )
    response = await retrieval_service.discover(request)
    assert response.results == []
    assert response.hints is not None
    assert response.hints["total_before_filtering"] == 0
    assert "active_filters" in response.hints
    assert response.hints["active_filters"].get("project") == "nonexistent_project"


# ---------------------------------------------------------------------------
# Empty-result hints: keyword mode
# ---------------------------------------------------------------------------


async def test_keyword_empty_results_with_hints_when_filtered_out(
    graph_store, stub_content_store, seeded_embedding_provider, retrieval_service
):
    """Keyword mode: raw BM25 results exist but scope filter excludes all."""
    doc = _make_doc(_id("doc_filtered_kw"), project="beta")
    await graph_store.insert_document(doc)
    await _index_doc_chunks(
        stub_content_store,
        seeded_embedding_provider,
        _id("doc_filtered_kw"),
        [("Section 1", "Specific keyword content for testing.")],
    )

    request = DiscoverRequest(
        mode=RetrievalMode.KEYWORD,
        query="keyword content",
        filters=RetrievalFilters(project="wrong_project"),
    )
    response = await retrieval_service.discover(request)
    assert response.results == []
    assert response.hints is not None
    # Pre-filter short-circuits: total_before_filtering is 0 (no chunks
    # fetched) but hints still surface the active_filters that culled.
    assert response.hints["total_before_filtering"] == 0
    assert "active_filters" in response.hints


async def test_keyword_nonempty_results_no_hints(
    graph_store, stub_content_store, seeded_embedding_provider, retrieval_service
):
    """When results are returned, hints is None (not needed)."""
    doc = _make_doc(_id("doc_has_results"))
    await graph_store.insert_document(doc)
    await _index_doc_chunks(
        stub_content_store,
        seeded_embedding_provider,
        _id("doc_has_results"),
        [("Section 1", "Matching content for keyword search.")],
    )

    request = DiscoverRequest(
        mode=RetrievalMode.KEYWORD,
        query="matching content",
    )
    response = await retrieval_service.discover(request)
    assert len(response.results) > 0
    assert response.hints is None


# ---------------------------------------------------------------------------
# Default mode: semantic
# ---------------------------------------------------------------------------


async def test_discover_defaults_to_semantic_mode(
    graph_store, stub_content_store, seeded_embedding_provider, retrieval_service
):
    """DiscoverRequest without explicit mode defaults to semantic."""
    doc = _make_doc(_id("doc_default_mode"))
    await graph_store.insert_document(doc)
    await _index_doc_chunks(
        stub_content_store,
        seeded_embedding_provider,
        _id("doc_default_mode"),
        [("Section 1", "Content for default mode testing.")],
    )

    request = DiscoverRequest(query="default mode testing")
    response = await retrieval_service.discover(request)
    assert response.mode == RetrievalMode.SEMANTIC
    assert len(response.results) > 0


# ---------------------------------------------------------------------------
# include_abstracts: default False suppresses abstracts in search results
# ---------------------------------------------------------------------------


async def test_include_abstracts_false_suppresses_in_semantic(
    graph_store, stub_content_store, seeded_embedding_provider, retrieval_service
):
    """Default include_abstracts=False nulls out semantic_abstract in results."""
    doc = _make_doc(
        _id("doc_no_abs"),
        semantic_abstract="This abstract should be suppressed.",
    )
    await graph_store.insert_document(doc)
    await _index_doc_chunks(
        stub_content_store,
        seeded_embedding_provider,
        _id("doc_no_abs"),
        [("Section 1", "Content for abstract suppression test.")],
    )

    request = DiscoverRequest(query="abstract suppression")
    response = await retrieval_service.discover(request)
    assert len(response.results) > 0
    assert response.results[0].document.semantic_abstract is None


async def test_include_abstracts_true_preserves_in_semantic(
    graph_store, stub_content_store, seeded_embedding_provider, retrieval_service
):
    """Explicit include_abstracts=True returns semantic_abstract."""
    abstract_text = "This abstract should be preserved."
    doc = _make_doc(_id("doc_yes_abs"), semantic_abstract=abstract_text)
    await graph_store.insert_document(doc)
    await _index_doc_chunks(
        stub_content_store,
        seeded_embedding_provider,
        _id("doc_yes_abs"),
        [("Section 1", "Content for abstract preservation test.")],
    )

    request = DiscoverRequest(query="abstract preservation", include_abstracts=True)
    response = await retrieval_service.discover(request)
    assert len(response.results) > 0
    assert response.results[0].document.semantic_abstract == abstract_text


async def test_include_abstracts_false_suppresses_in_catalog(graph_store, retrieval_service):
    """Catalog mode with default include_abstracts=False suppresses abstracts."""
    doc = _make_doc(
        _id("doc_cat_no_abs"),
        doc_type="note",
        semantic_abstract="Catalog abstract should be suppressed.",
    )
    await graph_store.insert_document(doc)

    request = DiscoverRequest(
        mode=RetrievalMode.CATALOG,
        filters=RetrievalFilters(doc_type="note"),
    )
    response = await retrieval_service.discover(request)
    hits_by_id = {h.document.id: h for h in response.results}
    assert _id("doc_cat_no_abs") in hits_by_id
    assert hits_by_id[_id("doc_cat_no_abs")].document.semantic_abstract is None


async def test_include_abstracts_true_preserves_in_catalog(graph_store, retrieval_service):
    """Catalog mode with include_abstracts=True preserves abstracts."""
    abstract_text = "Catalog abstract should be preserved."
    doc = _make_doc(
        _id("doc_cat_yes_abs"),
        doc_type="note",
        semantic_abstract=abstract_text,
    )
    await graph_store.insert_document(doc)

    request = DiscoverRequest(
        mode=RetrievalMode.CATALOG,
        filters=RetrievalFilters(doc_type="note"),
        include_abstracts=True,
    )
    response = await retrieval_service.discover(request)
    hits_by_id = {h.document.id: h for h in response.results}
    assert _id("doc_cat_yes_abs") in hits_by_id
    assert hits_by_id[_id("doc_cat_yes_abs")].document.semantic_abstract == abstract_text


async def test_include_abstracts_false_suppresses_in_keyword(
    graph_store, stub_content_store, seeded_embedding_provider, retrieval_service
):
    """Keyword mode with default include_abstracts=False suppresses abstracts."""
    doc = _make_doc(
        _id("doc_kw_no_abs"),
        semantic_abstract="Keyword abstract should be suppressed.",
    )
    await graph_store.insert_document(doc)
    await _index_doc_chunks(
        stub_content_store,
        seeded_embedding_provider,
        _id("doc_kw_no_abs"),
        [("Section 1", "Keyword mode abstract suppression content.")],
    )

    request = DiscoverRequest(
        mode=RetrievalMode.KEYWORD,
        query="keyword mode abstract",
    )
    response = await retrieval_service.discover(request)
    assert len(response.results) > 0
    assert response.results[0].document.semantic_abstract is None


# ---------------------------------------------------------------------------
# min_relevance: relevance threshold filtering
# ---------------------------------------------------------------------------


async def test_min_relevance_filters_low_scoring_results(
    graph_store, stub_content_store, seeded_embedding_provider, retrieval_service
):
    """Results below min_relevance are excluded from response."""
    doc = _make_doc(_id("doc_threshold"))
    await graph_store.insert_document(doc)
    await _index_doc_chunks(
        stub_content_store,
        seeded_embedding_provider,
        _id("doc_threshold"),
        [("Section 1", "Glucose monitoring calibration data.")],
    )

    # First get unfiltered results to learn the score
    baseline = DiscoverRequest(query="glucose monitoring")
    baseline_response = await retrieval_service.discover(baseline)
    assert len(baseline_response.results) > 0
    actual_score = baseline_response.results[0].relevance_score
    assert actual_score is not None

    # Set threshold above the actual score -- should filter it out
    request = DiscoverRequest(
        query="glucose monitoring",
        min_relevance=actual_score + 0.1,
    )
    response = await retrieval_service.discover(request)
    assert len(response.results) == 0


async def test_min_relevance_keeps_high_scoring_results(
    graph_store, stub_content_store, seeded_embedding_provider, retrieval_service
):
    """Results at or above min_relevance are preserved."""
    doc = _make_doc(_id("doc_above"))
    await graph_store.insert_document(doc)
    await _index_doc_chunks(
        stub_content_store,
        seeded_embedding_provider,
        _id("doc_above"),
        [("Section 1", "Report claim analysis for biosensor calibration.")],
    )

    # Set a very low threshold -- result should survive
    request = DiscoverRequest(
        query="biosensor calibration",
        min_relevance=0.01,
    )
    response = await retrieval_service.discover(request)
    assert len(response.results) > 0
    assert response.results[0].relevance_score >= 0.01


async def test_min_relevance_none_disables_filtering(
    graph_store, stub_content_store, seeded_embedding_provider, retrieval_service
):
    """Default min_relevance=None returns all results regardless of score."""
    doc = _make_doc(_id("doc_no_thresh"))
    await graph_store.insert_document(doc)
    await _index_doc_chunks(
        stub_content_store,
        seeded_embedding_provider,
        _id("doc_no_thresh"),
        [("Section 1", "Content for threshold default test.")],
    )

    request = DiscoverRequest(query="threshold default test")
    response = await retrieval_service.discover(request)
    assert len(response.results) > 0


async def test_min_relevance_does_not_apply_to_catalog(graph_store, retrieval_service):
    """Catalog mode has no relevance scores, so min_relevance has no effect."""
    doc = _make_doc(_id("doc_cat_thresh"), doc_type="note")
    await graph_store.insert_document(doc)

    request = DiscoverRequest(
        mode=RetrievalMode.CATALOG,
        filters=RetrievalFilters(doc_type="note"),
        min_relevance=0.5,
    )
    response = await retrieval_service.discover(request)
    assert len(response.results) > 0


# ---------------------------------------------------------------------------
# Pre-filter resolution: doc-level filters (lifecycle/project/tags/pipeline)
# must be resolved to a document_ids set BEFORE the LanceDB top-K cutoff,
# not post-filtered against the result. Cowork-reported failure: many
# archived design_spec chunks dominate top-K vector ranking, the
# lifecycle_status=active post-filter drops them all, returning zero hits
# even though active design_specs exist that match the query.
# ---------------------------------------------------------------------------


async def test_lifecycle_filter_pre_resolves_against_archived_dominance(
    graph_store, stub_content_store, seeded_embedding_provider, retrieval_service
):
    """When archived versions outnumber active versions in the chunk store
    such that the LanceDB top-K returns only archived chunks, the
    lifecycle_status=active filter must still surface the active document.
    The filter must be resolved BEFORE the top-K cutoff, not as a
    post-filter discarding the entire fetched set.
    """
    # 20 archived design_specs that match the query, inserted first so
    # they dominate the stable-sort tie-breaking on identical embeddings.
    for i in range(20):
        doc = _make_doc(
            _id(f"archived_{i}"),
            lifecycle_status="archived",
            doc_type="design_spec",
        )
        await graph_store.insert_document(doc)
        await _index_doc_chunks(
            stub_content_store,
            seeded_embedding_provider,
            _id(f"archived_{i}"),
            [("Fraud Screening Module", "fraud screening risk score detection")],
            doc_type="design_spec",
            lifecycle_status="archived",
        )

    # 1 active design_spec with the same matching content.
    active_doc = _make_doc(
        _id("active_target"),
        lifecycle_status="active",
        doc_type="design_spec",
    )
    await graph_store.insert_document(active_doc)
    await _index_doc_chunks(
        stub_content_store,
        seeded_embedding_provider,
        _id("active_target"),
        [("Fraud Screening Module", "fraud screening risk score detection")],
        doc_type="design_spec",
        lifecycle_status="active",
    )

    # limit=1, so fetch_limit = 1*10 = 10. Without pre-resolution, the
    # top-10 are all archived (insertion order); the active filter culls
    # all of them → zero results. With pre-resolution, the doc_id pre-
    # filter limits LanceDB's corpus to just `active_target` and the
    # query returns it.
    request = DiscoverRequest(
        mode=RetrievalMode.SEMANTIC,
        query="fraud screening",
        limit=1,
        filters=RetrievalFilters(
            doc_type="design_spec",
            lifecycle_status="active",
        ),
    )
    response = await retrieval_service.discover(request)

    doc_ids = [h.document.id for h in response.results]
    assert _id("active_target") in doc_ids, (
        f"Active design_spec must surface even when archived versions "
        f"dominate top-K. Got doc_ids: {doc_ids}"
    )


async def test_keyword_lifecycle_filter_pre_resolves_against_archived_dominance(
    graph_store, stub_content_store, seeded_embedding_provider, retrieval_service
):
    """Same pre-resolution requirement applies to keyword (BM25) mode."""
    for i in range(20):
        doc = _make_doc(
            _id(f"archived_kw_{i}"),
            lifecycle_status="archived",
            doc_type="design_spec",
        )
        await graph_store.insert_document(doc)
        await _index_doc_chunks(
            stub_content_store,
            seeded_embedding_provider,
            _id(f"archived_kw_{i}"),
            [("Fraud Screening Module", "fraud screening risk score detection")],
            doc_type="design_spec",
            lifecycle_status="archived",
        )

    active_doc = _make_doc(
        _id("active_kw_target"),
        lifecycle_status="active",
        doc_type="design_spec",
    )
    await graph_store.insert_document(active_doc)
    await _index_doc_chunks(
        stub_content_store,
        seeded_embedding_provider,
        _id("active_kw_target"),
        [("Fraud Screening Module", "fraud screening risk score detection")],
        doc_type="design_spec",
        lifecycle_status="active",
    )

    request = DiscoverRequest(
        mode=RetrievalMode.KEYWORD,
        query="fraud screening",
        limit=1,
        filters=RetrievalFilters(
            doc_type="design_spec",
            lifecycle_status="active",
        ),
    )
    response = await retrieval_service.discover(request)

    doc_ids = [h.document.id for h in response.results]
    assert _id("active_kw_target") in doc_ids


async def test_pre_resolved_filters_return_empty_when_zero_docs_match(
    graph_store, stub_content_store, seeded_embedding_provider, retrieval_service
):
    """When the metadata filters (e.g. lifecycle_status) match zero
    documents, the search returns an empty result without querying
    LanceDB at all (or via an impossible ID list)."""
    doc = _make_doc(_id("doc_only"), lifecycle_status="active", doc_type="design_spec")
    await graph_store.insert_document(doc)
    await _index_doc_chunks(
        stub_content_store,
        seeded_embedding_provider,
        _id("doc_only"),
        [("Section", "fraud screening content")],
        doc_type="design_spec",
    )

    # Filter excludes the only document — no docs match.
    request = DiscoverRequest(
        mode=RetrievalMode.SEMANTIC,
        query="fraud screening",
        filters=RetrievalFilters(
            doc_type="design_spec",
            lifecycle_status="filed",  # no doc has this status
        ),
    )
    response = await retrieval_service.discover(request)
    assert response.results == []


# ---------------------------------------------------------------------------
# DocumentSummary.from_document factory consolidates 5 retrieval-site
# constructions and folds the two near-duplicate date-parse helpers into
# sage.utils.date_parsing.parse_document_date. The exhaustive-fields test
# (T1) is the structural F4 closure: it fails closed when a new field is
# added to DocumentSummary without a matching factory update.
# ---------------------------------------------------------------------------


def _doc_with_every_summary_field() -> Document:
    """Build a Document with every DocumentSummary-mapped field set to a
    distinct non-default sentinel. Used by the exhaustive-fields test to
    verify ``DocumentSummary.from_document`` populates each field; a
    default-only Document would let the test pass coincidentally on
    list/dict fields whose defaults are ``[]`` / ``None``."""
    return _make_doc(
        _id("doc_all_fields"),
        lifecycle_status="archived",
        project="proj-X",
        doc_type="ticket",
        tags=["alpha", "beta"],
        document_date="2026-05-15",
        source_modified_at=datetime(2026, 5, 20, 12, 0, tzinfo=timezone.utc),
        semantic_abstract="an abstract for testing",
        tier3_metadata={"ticket_id": "T-0096", "ticket_priority": "medium"},
        version_label="v1.2",
    )


# T1: exhaustive fields — the keystone F4-closure test.
def test_from_document_populates_every_summary_field():
    doc = _doc_with_every_summary_field()
    summary = DocumentSummary.from_document(doc)
    # Three-branch closure-test idiom. DocumentSummary has no
    # non-None-default scalar fields today; the elif is forward defense.
    for field_name, field_info in DocumentSummary.model_fields.items():
        value = getattr(summary, field_name)
        annotation = field_info.annotation
        default = field_info.default
        if annotation == list[str] or annotation == (dict | None):
            assert value, (
                f"DocumentSummary.{field_name} not populated by from_document "
                "(empty/falsy default would pass a naive 'is not None' check)"
            )
        elif default is not PydanticUndefined and default is not None:
            assert value != default, (
                f"DocumentSummary.{field_name} matches its default ({default!r}) — "
                "from_document may have dropped this field (coincidental pass)"
            )
        else:
            assert value is not None, f"DocumentSummary.{field_name} not populated by from_document"


# T2: document_date passes through as a bare calendar-date string — the
# projection does not promote it to a UTC-anchored datetime. Promoting a
# calendar date to a UTC instant introduces a non-invertible shift for
# consumers in non-UTC zones (the source of the search-page off-by-one
# class of bug); the projection keeps the calendar-date shape, and any
# consumer needing a datetime parses at the use site.
def test_from_document_passes_document_date_through_as_string():
    doc = _make_doc(_id("doc_dated"), document_date="2026-05-15")
    summary = DocumentSummary.from_document(doc)
    # `type(...) is str` rather than `isinstance(..., str)` — a datetime
    # subclassing str (hypothetical) would slip past isinstance. The intent
    # is "the projection did not promote the type", and `type(...) is str`
    # says exactly that.
    assert type(summary.document_date) is str
    assert summary.document_date == "2026-05-15"


# T2a: DocumentSummary serializes document_date as a bare YYYY-MM-DD string
# on the wire, not as an ISO datetime. Catches a regression in which a
# Pydantic field decorator or serializer is added that re-promotes the
# value before serialization.
def test_documentsummary_serializes_document_date_as_bare_calendar_string():
    doc = _make_doc(_id("doc_dated_wire"), document_date="2026-05-15")
    summary = DocumentSummary.from_document(doc)
    payload = summary.model_dump_json()
    # Exact-string match (not regex prefix). A regex like
    # r'"document_date":"\d{4}-\d{2}-\d{2}' would pass on the
    # category-error form "2026-05-15T00:00:00Z" because the prefix matches.
    assert '"document_date":"2026-05-15"' in payload
    assert '"2026-05-15T' not in payload  # no datetime form anywhere in the payload


# T2b: RetrievalService._resolve_document_date parses the bare calendar-date
# string into a UTC datetime for recency scoring. The parse helper lives at
# the consumer, where calendar-to-instant conversion is semantically
# appropriate (the caller does date math: ``(now - ref).total_seconds()``).
def test_resolve_document_date_parses_string_document_date():
    doc = _make_doc(_id("doc_resolve"), document_date="2026-05-15")
    summary = DocumentSummary.from_document(doc)
    now = datetime(2026, 5, 20, tzinfo=timezone.utc)
    resolved = RetrievalService._resolve_document_date(summary, now)
    # Anti-coincidental: assert the *result type and value* match a datetime
    # — if the consumer forgot to parse and returned the raw string, the
    # equality comparison against a datetime would raise TypeError on the
    # caller's `(now - ref_date).total_seconds()` expression in production,
    # but the assertion below would simply fail the == on its own.
    assert isinstance(resolved, datetime)
    assert resolved == datetime(2026, 5, 15, tzinfo=timezone.utc)


# T3: None document_date round-trips to None without raising.
def test_from_document_handles_none_document_date():
    doc = _make_doc(_id("doc_no_date"))
    summary = DocumentSummary.from_document(doc)
    assert summary.document_date is None


# T4: source_modified_at passes through unchanged.
def test_from_document_passes_source_modified_at():
    smt = datetime(2026, 5, 20, 12, 0, tzinfo=timezone.utc)
    doc = _make_doc(_id("doc_smt"), source_modified_at=smt)
    summary = DocumentSummary.from_document(doc)
    assert summary.source_modified_at == smt


# T5: defaults round-trip transparently — factory must not invent values.
def test_from_document_round_trips_defaults():
    doc = _make_doc(_id("doc_defaults"))
    summary = DocumentSummary.from_document(doc)
    assert summary.tags == []
    assert summary.project is None
    assert summary.doc_type is None
    assert summary.version_label is None
    assert summary.document_date is None
    assert summary.source_modified_at is None
    assert summary.semantic_abstract is None
    assert summary.tier3_metadata is None


# T6: None input → None.
def test_parse_document_date_none_returns_none():
    from sage.utils.date_parsing import parse_document_date

    assert parse_document_date(None) is None


# T7: Empty string → None.
def test_parse_document_date_empty_string_returns_none():
    from sage.utils.date_parsing import parse_document_date

    assert parse_document_date("") is None


# T8: ISO date string → UTC datetime at midnight.
def test_parse_document_date_iso_date_returns_utc_midnight():
    from sage.utils.date_parsing import parse_document_date

    assert parse_document_date("2026-05-15") == datetime(2026, 5, 15, tzinfo=timezone.utc)


# T9: ISO datetime with trailing Z → UTC datetime.
def test_parse_document_date_iso_with_z_returns_utc():
    from sage.utils.date_parsing import parse_document_date

    assert parse_document_date("2026-05-15T10:30:00Z") == datetime(
        2026, 5, 15, 10, 30, tzinfo=timezone.utc
    )


# T10: aware datetime in non-UTC offset → normalized to UTC.
def test_parse_document_date_aware_offset_normalizes_to_utc():
    from sage.utils.date_parsing import parse_document_date

    assert parse_document_date("2026-05-15T10:30:00-04:00") == datetime(
        2026, 5, 15, 14, 30, tzinfo=timezone.utc
    )


# T11: malformed string → None (does not raise).
def test_parse_document_date_malformed_returns_none():
    from sage.utils.date_parsing import parse_document_date

    assert parse_document_date("not-a-date") is None


# ---------------------------------------------------------------------------
# DiscoverHit.from_summary factory consolidates 5 retrieval-site
# constructions of DiscoverHit (sage/services/retrieval.py lines 462, 600,
# 784, 847, 1059). The exhaustive-fields test below is the structural F4
# closure: it fails closed when a new field is added to DiscoverHit
# without a matching update to from_summary.
# ---------------------------------------------------------------------------


def _summary_with_every_discover_hit_field() -> DocumentSummary:
    """Build a DocumentSummary with every field set to a non-default sentinel.

    Reuses ``_doc_with_every_summary_field`` (the sentinel Document)
    routed through ``DocumentSummary.from_document`` so the document side of
    the projection is non-default at every field. Combined with the
    non-default chunk-field kwargs supplied by the caller, this ensures the
    exhaustive-fields test cannot pass coincidentally on a default/None
    value at any field.
    """
    return DocumentSummary.from_document(_doc_with_every_summary_field())


# T1: exhaustive fields — the keystone F4-closure test.
def test_from_summary_populates_every_discover_hit_field():
    summary = _summary_with_every_discover_hit_field()
    hit = DiscoverHit.from_summary(
        summary,
        chunk_content="sentinel chunk content",
        heading_path="Section 1 > Sentinel Heading",
        relevance_score=0.875,
        matched_chunk_count=7,
    )
    # Three-branch closure-test idiom. DiscoverHit has no
    # non-None-default scalar fields today; the elif is forward defense.
    for field_name, field_info in DiscoverHit.model_fields.items():
        value = getattr(hit, field_name)
        annotation = field_info.annotation
        default = field_info.default
        if annotation == list[str] or annotation == (dict | None):
            assert value, (
                f"DiscoverHit.{field_name} not populated by from_summary "
                "(empty/falsy default would pass a naive 'is not None' check)"
            )
        elif default is not PydanticUndefined and default is not None:
            assert value != default, (
                f"DiscoverHit.{field_name} matches its default ({default!r}) — "
                "from_summary may have dropped this field (coincidental pass)"
            )
        else:
            assert value is not None, f"DiscoverHit.{field_name} not populated by from_summary"


# ---------------------------------------------------------------------------
# Edge enumeration via search(target="edges")
# ---------------------------------------------------------------------------


async def _seed_edge_fixture(graph_store, *, total_edges: int = 8):
    """Insert `total_edges` references edges from src to a fan of targets.

    Used by service-layer tests to drive the threshold rule by controlling
    the result count exactly. Returns the source doc id (canonical form).
    """
    src = _id("src_doc")
    await graph_store.insert_document(_make_doc(src))
    edge_ids = []
    for i in range(total_edges):
        target = _id(f"tgt_doc_{i:03d}")
        await graph_store.insert_document(_make_doc(target))
        eid = str(_uuid.uuid4())
        await graph_store.insert_edge(
            Edge(
                id=eid,
                source_id=src,
                target_id=target,
                edge_type=EdgeType.REFERENCES,
                rationale=f"seeded edge {i}",
                created_at=datetime.now(timezone.utc) + timedelta(seconds=i),
            )
        )
        edge_ids.append(eid)
    return src, edge_ids


async def test_catalog_edges_returns_edge_hit_rows(graph_store, retrieval_service):
    """11. target=edges + mode=catalog returns EdgeHit rows, not DiscoverHit."""
    src, _ = await _seed_edge_fixture(graph_store, total_edges=3)

    req = DiscoverRequest(
        mode=RetrievalMode.CATALOG,
        target=RetrievalTarget.EDGES,
        filters=RetrievalFilters(source_id=src),
        response_mode=ResponseMode.FULL,
    )
    resp = await retrieval_service.discover(req)

    assert resp.target == RetrievalTarget.EDGES
    assert resp.mode == RetrievalMode.CATALOG
    assert resp.total_available == 3
    assert len(resp.results) == 3
    for hit in resp.results:
        assert isinstance(hit, EdgeHit), (
            f"target=edges must return EdgeHit rows, got {type(hit).__name__}"
        )
        # Full envelope: every required field populated.
        assert hit.edge_id and hit.source_id == src
        assert hit.target_id
        assert hit.edge_type == "references"
        assert hit.rationale is not None
        assert hit.rationale_kind is not None
        # retraction state is null for live edges.
        assert hit.retracted_at is None
        assert hit.retracted_by_edge_id is None


async def test_catalog_edges_light_strips_to_identity_columns(graph_store, retrieval_service):
    """12. response_mode=light returns only the identity columns.

    Anti-coincidental: assert via model_dump(exclude_unset=True) that
    optional fields are GENUINELY absent (not present-but-null).
    """
    src, _ = await _seed_edge_fixture(graph_store, total_edges=2)

    req = DiscoverRequest(
        mode=RetrievalMode.CATALOG,
        target=RetrievalTarget.EDGES,
        filters=RetrievalFilters(source_id=src),
        response_mode=ResponseMode.LIGHT,
    )
    resp = await retrieval_service.discover(req)
    assert len(resp.results) == 2
    for hit in resp.results:
        dump = hit.model_dump(exclude_unset=True)
        identity_keys = {"edge_id", "source_id", "target_id", "edge_type"}
        assert set(dump.keys()) == identity_keys, (
            f"light must yield exactly identity fields, got {set(dump.keys())}"
        )


async def test_catalog_edges_full_carries_complete_envelope(graph_store, retrieval_service):
    """13. response_mode=full carries the complete envelope.

    Anti-coincidental: assert that EVERY full-envelope field is present
    in the dump — a serializer that silently dropped a field would fail.
    """
    src, _ = await _seed_edge_fixture(graph_store, total_edges=1)
    req = DiscoverRequest(
        mode=RetrievalMode.CATALOG,
        target=RetrievalTarget.EDGES,
        filters=RetrievalFilters(source_id=src),
        response_mode=ResponseMode.FULL,
    )
    resp = await retrieval_service.discover(req)
    hit = resp.results[0]
    dump = hit.model_dump(exclude_unset=True)
    # Every EdgeHit field must appear in the dump even when its value is null.
    full_keys = set(EdgeHit.model_fields.keys())
    assert set(dump.keys()) == full_keys, (
        f"full envelope must contain every EdgeHit field, missing: {full_keys - set(dump.keys())}"
    )


async def test_catalog_edges_default_threshold_at_or_below_five_is_full(
    graph_store, retrieval_service
):
    """14. Default-threshold rule: <=5 results -> full envelope."""
    src, _ = await _seed_edge_fixture(graph_store, total_edges=5)
    req = DiscoverRequest(
        mode=RetrievalMode.CATALOG,
        target=RetrievalTarget.EDGES,
        filters=RetrievalFilters(source_id=src),
        # No explicit response_mode -- threshold rule applies.
    )
    resp = await retrieval_service.discover(req)
    assert resp.total_available == 5
    dump = resp.results[0].model_dump(exclude_unset=True)
    # Full mode populates every field.
    assert set(dump.keys()) == set(EdgeHit.model_fields.keys()), (
        f"5 results (<= threshold) must default to full, got keys: {set(dump.keys())}"
    )


async def test_catalog_edges_default_threshold_above_five_is_light(graph_store, retrieval_service):
    """15. Default-threshold rule: >5 results -> light envelope.

    Anti-coincidental: the boundary (5 vs 6) catches a threshold whose
    direction is inverted (`<=5 light` instead of `>5 light`).
    """
    src, _ = await _seed_edge_fixture(graph_store, total_edges=6)
    req = DiscoverRequest(
        mode=RetrievalMode.CATALOG,
        target=RetrievalTarget.EDGES,
        filters=RetrievalFilters(source_id=src),
    )
    resp = await retrieval_service.discover(req)
    assert resp.total_available == 6
    dump = resp.results[0].model_dump(exclude_unset=True)
    identity_keys = {"edge_id", "source_id", "target_id", "edge_type"}
    assert set(dump.keys()) == identity_keys, (
        f"6 results (> threshold) must default to light, got keys: {set(dump.keys())}"
    )


async def test_catalog_edges_explicit_response_mode_overrides_threshold(
    graph_store, retrieval_service
):
    """16. Explicit response_mode wins over the default-threshold rule.

    Anti-coincidental: count 10 (above threshold) + explicit full means
    any output other than full proves the override is ignored.
    """
    src, _ = await _seed_edge_fixture(graph_store, total_edges=10)
    req = DiscoverRequest(
        mode=RetrievalMode.CATALOG,
        target=RetrievalTarget.EDGES,
        filters=RetrievalFilters(source_id=src),
        response_mode=ResponseMode.FULL,
    )
    resp = await retrieval_service.discover(req)
    assert resp.total_available == 10
    dump = resp.results[0].model_dump(exclude_unset=True)
    assert set(dump.keys()) == set(EdgeHit.model_fields.keys()), (
        f"explicit full must override threshold, got keys: {set(dump.keys())}"
    )


async def test_catalog_edges_total_available_unpaginated(graph_store, retrieval_service):
    """17. total_available reports the unpaginated edge count."""
    src, _ = await _seed_edge_fixture(graph_store, total_edges=20)
    req = DiscoverRequest(
        mode=RetrievalMode.CATALOG,
        target=RetrievalTarget.EDGES,
        filters=RetrievalFilters(source_id=src),
        limit=5,
        response_mode=ResponseMode.LIGHT,
    )
    resp = await retrieval_service.discover(req)
    assert len(resp.results) == 5
    assert resp.total_available == 20


async def test_catalog_edges_sort_default_is_created_at_desc(graph_store, retrieval_service):
    """18. Catalog edges sort by created_at DESC (most recent first).

    Anti-coincidental: insert with explicit out-of-order timestamps; assert
    response ordering matches timestamp order, not insertion order.
    """
    src = _id("sort_src")
    await graph_store.insert_document(_make_doc(src))
    targets = [_id(f"sort_tgt_{i}") for i in range(3)]
    for t in targets:
        await graph_store.insert_document(_make_doc(t))

    # Insert in order: middle, oldest, newest. After sort: newest, middle, oldest.
    timestamps = [
        datetime(2026, 5, 15, tzinfo=timezone.utc),  # middle
        datetime(2026, 5, 1, tzinfo=timezone.utc),  # oldest
        datetime(2026, 5, 23, tzinfo=timezone.utc),  # newest
    ]
    inserted_ids = []
    for t, ts in zip(targets, timestamps):
        eid = str(_uuid.uuid4())
        await graph_store.insert_edge(
            Edge(
                id=eid,
                source_id=src,
                target_id=t,
                edge_type=EdgeType.REFERENCES,
                created_at=ts,
            )
        )
        inserted_ids.append(eid)

    req = DiscoverRequest(
        mode=RetrievalMode.CATALOG,
        target=RetrievalTarget.EDGES,
        filters=RetrievalFilters(source_id=src),
        response_mode=ResponseMode.FULL,
    )
    resp = await retrieval_service.discover(req)
    returned_ids = [r.edge_id for r in resp.results]
    # Expected order: newest (inserted last, idx 2), middle (idx 0), oldest (idx 1).
    expected = [inserted_ids[2], inserted_ids[0], inserted_ids[1]]
    assert returned_ids == expected, (
        f"results must be ordered by created_at DESC, got insertion order? "
        f"returned={returned_ids} expected={expected}"
    )


async def test_target_documents_default_preserves_catalog_behavior(graph_store, retrieval_service):
    """19. Backward compat: catalog without target/response_mode returns
    documents and matches the historical shape (DiscoverHit, document field).
    """
    await graph_store.insert_document(_make_doc(_id("d1"), doc_type="ticket"))
    await graph_store.insert_document(_make_doc(_id("d2"), doc_type="ticket"))

    req = DiscoverRequest(
        mode=RetrievalMode.CATALOG,
        filters=RetrievalFilters(doc_type="ticket"),
    )
    resp = await retrieval_service.discover(req)
    assert resp.target == RetrievalTarget.DOCUMENTS
    assert resp.total_available == 2
    assert len(resp.results) == 2
    for hit in resp.results:
        assert isinstance(hit, DiscoverHit), (
            f"target=documents must keep returning DiscoverHit, got {type(hit).__name__}"
        )
        assert hit.document is not None


# ---------------------------------------------------------------------------
# DiscoverRequest validator (mode_parameter_mismatch for new combos)
# ---------------------------------------------------------------------------


def _ctx(exc: ValidationError) -> dict:
    return exc.errors()[0].get("ctx", {})


def test_target_edges_with_semantic_mode_is_mode_parameter_mismatch():
    """21. target=edges + mode=semantic raises mode_parameter_mismatch."""
    with pytest.raises(ValidationError) as info:
        DiscoverRequest(mode=RetrievalMode.SEMANTIC, query="x", target=RetrievalTarget.EDGES)
    err = info.value.errors()[0]
    assert err["type"] == "mode_parameter_mismatch"
    assert err["ctx"]["forbidden_param"] == "target"
    assert err["ctx"]["allowed_modes"] == [RetrievalMode.CATALOG.value]


def test_target_edges_with_keyword_mode_is_mode_parameter_mismatch():
    """22. target=edges + mode=keyword raises mode_parameter_mismatch."""
    with pytest.raises(ValidationError) as info:
        DiscoverRequest(mode=RetrievalMode.KEYWORD, query="x", target=RetrievalTarget.EDGES)
    assert info.value.errors()[0]["type"] == "mode_parameter_mismatch"


def test_target_edges_with_deterministic_mode_is_mode_parameter_mismatch():
    """23. target=edges + mode=deterministic raises mode_parameter_mismatch."""
    with pytest.raises(ValidationError) as info:
        DiscoverRequest(
            mode=RetrievalMode.DETERMINISTIC,
            document_id=_id("d1"),
            heading_path="x",
            target=RetrievalTarget.EDGES,
        )
    assert info.value.errors()[0]["type"] == "mode_parameter_mismatch"


@pytest.mark.parametrize(
    "filter_kwargs,key",
    [
        ({"doc_type": "ticket"}, "doc_type"),
        ({"project": "proj"}, "project"),
        ({"lifecycle_status": "active"}, "lifecycle_status"),
        ({"pipeline_status": "abstraction_complete"}, "pipeline_status"),
        ({"tags": ["a"]}, "tags"),
        ({"document_ids": [_id("d1")]}, "document_ids"),
        ({"tier3_metadata": {"k": "v"}}, "tier3_metadata"),
        ({"source_type": "markdown"}, "source_type"),
    ],
)
def test_target_edges_rejects_document_only_filter_keys(filter_kwargs, key):
    """24. target=edges rejects every document-only filter key with
    mode_parameter_mismatch carrying the offending key.
    """
    with pytest.raises(ValidationError) as info:
        DiscoverRequest(
            mode=RetrievalMode.CATALOG,
            target=RetrievalTarget.EDGES,
            filters=RetrievalFilters(**filter_kwargs),
        )
    err = info.value.errors()[0]
    assert err["type"] == "mode_parameter_mismatch"
    assert err["ctx"]["forbidden_param"] == f"filters.{key}"


@pytest.mark.parametrize(
    "filter_kwargs,key",
    [
        ({"source_id": _id("d1")}, "source_id"),
        ({"target_id": _id("d1")}, "target_id"),
        ({"edge_type": "references"}, "edge_type"),
    ],
)
def test_target_documents_rejects_edge_only_filter_keys(filter_kwargs, key):
    """25. target=documents rejects every edge-only filter key.
    Default target is documents; setting an edge filter without
    target=edges must error rather than silently ignore.
    """
    with pytest.raises(ValidationError) as info:
        DiscoverRequest(
            mode=RetrievalMode.CATALOG,
            filters=RetrievalFilters(**filter_kwargs),
        )
    err = info.value.errors()[0]
    assert err["type"] == "mode_parameter_mismatch"
    assert err["ctx"]["forbidden_param"] == f"filters.{key}"


@pytest.mark.parametrize(
    "field,value",
    [
        ("query", "anything"),
        ("document_id", _id("d1")),
        ("heading_path", "Section 1"),
        ("min_relevance", 0.5),
        ("include_abstracts", True),
    ],
)
def test_target_edges_rejects_doc_only_request_parameters(field, value):
    """26. target=edges rejects every document-only DiscoverRequest parameter
    that has an explicit non-default value.
    """
    kwargs = {"mode": RetrievalMode.CATALOG, "target": RetrievalTarget.EDGES, field: value}
    with pytest.raises(ValidationError) as info:
        DiscoverRequest(**kwargs)
    err = info.value.errors()[0]
    assert err["type"] == "mode_parameter_mismatch"
    assert err["ctx"]["forbidden_param"] == field


def test_invalid_target_value_raises_enum_validation_error():
    """27. target="invalid" rejected at enum coercion, not as a mode mismatch.
    The error loc must point at the target field so the typed error
    translator can map it.
    """
    with pytest.raises(ValidationError) as info:
        DiscoverRequest(mode=RetrievalMode.CATALOG, target="invalid")
    err = info.value.errors()[0]
    assert err["type"] == "enum"
    assert err["loc"] == ("target",)


# ---------------------------------------------------------------------------
# Facet aggregation via search(target="facets")
# ---------------------------------------------------------------------------

# Deliberately a literal rather than an import of the implementation's
# field constant: the tests assert the contract vocabulary independently.
_FACET_FIELDS = (
    "doc_type",
    "lifecycle_status",
    "source_type",
    "pipeline_status",
    "tags",
)


async def _seed_facet_docs(graph_store) -> None:
    """Three documents using vault-configured doc_types (note, memo).

    doc_type: 2x note, 1x memo. tags: "alpha" on two docs, "beta" on
    one multi-tag doc. Configured doc_types keep the vocabulary-warning
    hint quiet in the no-warning tests.
    """
    await graph_store.insert_document(
        _make_doc(_id("fct_1"), doc_type="note", tags=["alpha", "beta"])
    )
    await graph_store.insert_document(_make_doc(_id("fct_2"), doc_type="note", tags=["alpha"]))
    await graph_store.insert_document(_make_doc(_id("fct_3"), doc_type="memo"))


async def test_catalog_facets_returns_facet_hit_rows(graph_store, retrieval_service):
    """target=facets returns one FacetHit per facet field with exact counts."""
    await _seed_facet_docs(graph_store)

    req = DiscoverRequest(mode=RetrievalMode.CATALOG, target=RetrievalTarget.FACETS)
    resp = await retrieval_service.discover(req)

    assert resp.target == RetrievalTarget.FACETS
    assert resp.mode == RetrievalMode.CATALOG
    assert resp.total_available == 3
    for hit in resp.results:
        assert isinstance(hit, FacetHit), (
            f"target=facets must return FacetHit rows, got {type(hit).__name__}"
        )
    assert [hit.field for hit in resp.results] == list(_FACET_FIELDS)
    by_field = {hit.field: hit.values for hit in resp.results}
    assert by_field["doc_type"] == {"note": 2, "memo": 1}
    assert by_field["lifecycle_status"] == {"active": 3}
    assert by_field["source_type"] == {"markdown": 3}
    assert by_field["pipeline_status"] == {"abstraction_complete": 3}
    assert by_field["tags"] == {"alpha": 2, "beta": 1}


async def test_catalog_facets_respects_filters(graph_store, retrieval_service):
    """Facets are computed within the filter slice, not vault-wide."""
    await _seed_facet_docs(graph_store)

    req = DiscoverRequest(
        mode=RetrievalMode.CATALOG,
        target=RetrievalTarget.FACETS,
        filters=RetrievalFilters(doc_type="note"),
    )
    resp = await retrieval_service.discover(req)

    assert resp.total_available == 2
    by_field = {hit.field: hit.values for hit in resp.results}
    assert by_field["doc_type"] == {"note": 2}
    assert by_field["tags"] == {"alpha": 2, "beta": 1}


async def test_catalog_facets_empty_vault(graph_store, retrieval_service):
    """Empty vault: every facet field present with empty values, zero total."""
    req = DiscoverRequest(mode=RetrievalMode.CATALOG, target=RetrievalTarget.FACETS)
    resp = await retrieval_service.discover(req)

    assert resp.total_available == 0
    assert [hit.field for hit in resp.results] == list(_FACET_FIELDS)
    assert all(hit.values == {} for hit in resp.results)


async def test_catalog_facets_surfaces_vocabulary_warnings(graph_store, retrieval_service):
    """An out-of-vocabulary doc_type filter yields the warnings hint.

    Proves the facets dispatch branch runs the vocabulary advisory --
    a branch that returned before _apply_vocabulary_warnings would
    yield empty facets with no explanation.
    """
    await _seed_facet_docs(graph_store)

    req = DiscoverRequest(
        mode=RetrievalMode.CATALOG,
        target=RetrievalTarget.FACETS,
        filters=RetrievalFilters(doc_type="template"),
    )
    resp = await retrieval_service.discover(req)

    assert resp.total_available == 0
    assert all(hit.values == {} for hit in resp.results)
    assert resp.hints is not None
    assert resp.hints.get("warnings"), "an undefined doc_type must surface a hints warning"


async def test_facets_budget_hint_never_names_the_rejected_limit_parameter(
    graph_store, retrieval_service
):
    """The facets budget hint recommends facet_value_limit, never limit.

    Deliberate deviation from the edges branch, which reuses the catalog
    hint wholesale: ``limit`` is rejected on this target, so a hint
    naming ``recommended_limit`` would recommend a re-call the validator
    refuses. Carries forward the assertion of the test that pinned the
    deliberate skip before this hint existed -- the absence is still the
    point -- alongside the presence of the key that replaced it.

    Runs on a fixture where a smaller cap genuinely fits, so the
    presence half asserts something reachable. A budget too tight for
    any cap reaches the omission branch, where the presence half would
    be asserting the wrong thing about a correct response.
    """
    await _seed_many_tag_docs(graph_store, DEFAULT_FACET_VALUE_LIMIT, tag_width=_WIDE_TAG_WIDTH)

    req = DiscoverRequest(mode=RetrievalMode.CATALOG, target=RetrievalTarget.FACETS)
    resp = await retrieval_service.discover(req)

    assert resp.hints is not None
    assert resp.hints["reason"] == "facets_response_exceeds_inline_budget"
    assert "recommended_limit" not in resp.hints
    assert "recommended_facet_value_limit" in resp.hints


async def _seed_many_tag_docs(
    graph_store, count: int, offset: int = 0, *, tag_width: int | None = None
) -> None:
    """One document per unique tag, ``count`` of them, all counts 1.

    Uniform counts make the top-of-ordering prefix purely value-ASC,
    so a capped enumeration has exactly one correct answer.

    ``tag_width``, when given, pads each tag to a fixed character width
    without disturbing that ordering -- the ``tag-NNN`` prefix stays
    leftmost, so the sort is unchanged and only the payload's byte
    weight moves. Callers separating a value bound from a byte bound
    need the two to vary independently; callers asserting the exact
    value prefix leave it unset and get the bare ``tag-NNN`` form.
    """
    for i in range(offset, offset + count):
        tag = f"tag-{i:03d}"
        if tag_width is not None:
            tag = tag.ljust(tag_width, "x")
        await graph_store.insert_document(
            _make_doc(_id(f"cap_{i:03d}"), doc_type="note", tags=[tag])
        )


async def test_catalog_facets_default_cap_applied_without_opt_in(graph_store, retrieval_service):
    """A default facets call caps tags at 50 values with the true total.

    The acceptance-criterion core: the caller least equipped to set an
    option -- an agent's first call against an unfamiliar vault -- gets
    the bounded shape without asking. Trap coverage: an opt-in cap
    (None passed through as uncapped) returns 55 values; a cap applied
    by slicing an unordered dict fails the exact-prefix assertion; a
    total computed after capping fails the == 55.
    """
    await _seed_many_tag_docs(graph_store, 55)

    req = DiscoverRequest(mode=RetrievalMode.CATALOG, target=RetrievalTarget.FACETS)
    resp = await retrieval_service.discover(req)

    tags = next(hit for hit in resp.results if hit.field == "tags")
    assert len(tags.values) == 50
    assert tags.total_distinct == 55
    assert list(tags.values.keys()) == [f"tag-{i:03d}" for i in range(50)]


async def test_catalog_facets_field_selection_returns_only_selected_rows(
    graph_store, retrieval_service
):
    """facet_fields returns exactly the selected rows in canonical order.

    The request deliberately reverses the canonical order; honoring
    caller order (or returning all five rows with empties) fails.
    """
    await _seed_facet_docs(graph_store)

    req = DiscoverRequest(
        mode=RetrievalMode.CATALOG,
        target=RetrievalTarget.FACETS,
        facet_fields=["tags", "doc_type"],
    )
    resp = await retrieval_service.discover(req)

    assert [hit.field for hit in resp.results] == ["doc_type", "tags"]
    by_field = {hit.field: hit.values for hit in resp.results}
    assert by_field["tags"] == {"alpha": 2, "beta": 1}


async def test_catalog_facets_explicit_limit_reaches_full_vocabulary(
    graph_store, retrieval_service
):
    """The documented two-step opt reads a full vocabulary: a default
    call reports the true total, and a re-call with facet_value_limit
    set to it returns every value. Trap coverage: an upper bound on
    facet_value_limit below the vocabulary size, or an off-by-one in
    the storage LIMIT, breaks the second step.
    """
    await _seed_many_tag_docs(graph_store, 55)

    first = await retrieval_service.discover(
        DiscoverRequest(mode=RetrievalMode.CATALOG, target=RetrievalTarget.FACETS)
    )
    reported = next(hit for hit in first.results if hit.field == "tags").total_distinct

    second = await retrieval_service.discover(
        DiscoverRequest(
            mode=RetrievalMode.CATALOG,
            target=RetrievalTarget.FACETS,
            facet_value_limit=reported,
        )
    )
    tags = next(hit for hit in second.results if hit.field == "tags")
    assert reported == 55
    assert len(tags.values) == 55
    assert tags.total_distinct == 55


async def test_catalog_facets_total_distinct_on_small_and_empty_vaults(
    graph_store, retrieval_service
):
    """total_distinct is always populated: equal to the value count when
    nothing was truncated, zero on the empty vault. Trap coverage: an
    implementation that populates the total only when truncating (or
    models it nullable and drops it) fails on the untruncated rows; one
    that returns zero facet rows on an empty vault fails the field-set
    guard, without which the all() below is vacuously true.
    """
    empty = await retrieval_service.discover(
        DiscoverRequest(mode=RetrievalMode.CATALOG, target=RetrievalTarget.FACETS)
    )
    assert [hit.field for hit in empty.results] == list(_FACET_FIELDS)
    assert all(hit.values == {} and hit.total_distinct == 0 for hit in empty.results)

    await _seed_facet_docs(graph_store)
    small = await retrieval_service.discover(
        DiscoverRequest(mode=RetrievalMode.CATALOG, target=RetrievalTarget.FACETS)
    )
    assert all(hit.total_distinct == len(hit.values) for hit in small.results)
    assert next(h for h in small.results if h.field == "tags").total_distinct == 2


# ---------------------------------------------------------------------------
# The facets size guarantee, denominated in bytes
#
# The per-field value cap makes a facets response independent of corpus
# size and tagging density, but only in the unit it counts: values. The
# failure it was built against was denominated in bytes. These tests fix
# the two units against each other -- a payload that satisfies the count
# bound completely and busts the byte one -- so neither can be mistaken
# for the other.
# ---------------------------------------------------------------------------

#: Tag width at which fifty values alone overrun the production inline
#: budget. Derived from the budget rather than fixed, so the guard in
#: the test below keeps its room to spare when the budget is
#: recalibrated; a literal width silently walks toward the margin, where
#: a serialization detail rather than the property under test decides
#: the outcome. Two budgets' worth of tag content across the fifty
#: values the cap admits, which is the ratio the original literal had.
_WIDE_TAG_WIDTH = 2 * DEFAULT_MCP_INLINE_BUDGET_BYTES // DEFAULT_FACET_VALUE_LIMIT


async def test_facets_budget_hint_fires_on_wide_tags_at_the_production_budget(
    graph_store, retrieval_service
):
    """Fifty wide tags clear the value cap and still bust the byte ceiling.

    The regression the count bound does not cover. Runs at the
    production budget rather than a lowered one, so it measures the
    guarantee a caller actually depends on instead of the plumbing.

    Anti-coincidental-pass: the tags row is asserted to carry exactly
    ``DEFAULT_FACET_VALUE_LIMIT`` values with a ``total_distinct`` equal
    to it -- the count bound is satisfied and truncated nothing -- so a
    hint can only be firing on measured bytes. The arithmetic guard
    makes the overrun structural rather than incidental to whatever the
    seeded corpus happens to weigh.
    """
    assert DEFAULT_FACET_VALUE_LIMIT * _WIDE_TAG_WIDTH > DEFAULT_MCP_INLINE_BUDGET_BYTES, (
        "the fixture must overrun the budget on tag width alone; a narrower "
        "tag would make this test pass or fail on unrelated payload weight"
    )
    await _seed_many_tag_docs(graph_store, DEFAULT_FACET_VALUE_LIMIT, tag_width=_WIDE_TAG_WIDTH)

    resp = await retrieval_service.discover(
        DiscoverRequest(mode=RetrievalMode.CATALOG, target=RetrievalTarget.FACETS)
    )

    tags_row = next(h for h in resp.results if h.field == "tags")
    assert len(tags_row.values) == DEFAULT_FACET_VALUE_LIMIT
    assert tags_row.total_distinct == DEFAULT_FACET_VALUE_LIMIT

    assert resp.hints is not None
    assert resp.hints["reason"] == "facets_response_exceeds_inline_budget"
    assert resp.hints["budget_bytes"] == DEFAULT_MCP_INLINE_BUDGET_BYTES
    assert resp.hints["response_size_bytes"] > DEFAULT_MCP_INLINE_BUDGET_BYTES


async def test_facets_budget_hint_absent_when_the_same_row_count_is_narrow(
    graph_store, retrieval_service
):
    """The same fifty values, narrow, emit nothing.

    The control for the test above: identical document count, identical
    value count, identical ``total_distinct`` -- only the byte weight
    differs. A hint keyed on cardinality rather than on bytes would fire
    here too.
    """
    await _seed_many_tag_docs(graph_store, DEFAULT_FACET_VALUE_LIMIT)

    resp = await retrieval_service.discover(
        DiscoverRequest(mode=RetrievalMode.CATALOG, target=RetrievalTarget.FACETS)
    )

    tags_row = next(h for h in resp.results if h.field == "tags")
    assert len(tags_row.values) == DEFAULT_FACET_VALUE_LIMIT
    assert tags_row.total_distinct == DEFAULT_FACET_VALUE_LIMIT
    assert resp.hints is None or resp.hints.get("reason") != "facets_response_exceeds_inline_budget"


async def test_facets_recommended_value_limit_re_calls_within_budget(
    graph_store, retrieval_service
):
    """Re-calling at the recommended cap produces an inline response.

    Without this the recommendation is decorative: a hint can name any
    number below the cap and look right. Mirrors the catalog side's
    ``test_recommended_limit_re_pages_within_budget``.

    Anti-coincidental-pass: the re-call assertion alone passes against
    an implementation that collapses every recommendation to the floor
    of 1, which fits inline trivially and would leave a caller reading
    one tag value out of fifty. The fixture sits only about twice over
    budget, so a proportional recommendation is well clear of the floor
    -- asserting that excludes the collapse without pinning a brittle
    exact number.
    """
    await _seed_many_tag_docs(graph_store, DEFAULT_FACET_VALUE_LIMIT, tag_width=_WIDE_TAG_WIDTH)

    first = await retrieval_service.discover(
        DiscoverRequest(mode=RetrievalMode.CATALOG, target=RetrievalTarget.FACETS)
    )
    assert first.hints is not None
    recommended = first.hints["recommended_facet_value_limit"]
    assert isinstance(recommended, int)
    assert 1 < recommended < DEFAULT_FACET_VALUE_LIMIT

    second = await retrieval_service.discover(
        DiscoverRequest(
            mode=RetrievalMode.CATALOG,
            target=RetrievalTarget.FACETS,
            facet_value_limit=recommended,
        )
    )
    assert (
        second.hints is None
        or second.hints.get("reason") != "facets_response_exceeds_inline_budget"
    )
    assert len(next(h for h in second.results if h.field == "tags").values) == recommended


async def test_facets_budget_hint_omits_the_recommendation_when_it_could_not_shrink(
    graph_store, retrieval_service, monkeypatch
):
    """A cap of one gets the size fields and no recommendation.

    The degenerate branch: the response is over budget for a reason
    lowering the cap cannot fix, and a ``recommended_facet_value_limit``
    equal to the cap already in force would name a re-call that changes
    nothing. Pins the omission so a later implementation that always
    names a recommendation reddens.

    The seeded corpus is load-bearing, not scenery: the tags assertion
    below fails without it, and a cap of one that never bit on a real
    two-value vocabulary would leave the cap in force at zero, reaching
    the same omission for a reason the test does not mean to pin.
    """
    monkeypatch.setenv("SAGE_MCP_INLINE_BUDGET_BYTES", "1")
    await _seed_facet_docs(graph_store)

    resp = await retrieval_service.discover(
        DiscoverRequest(
            mode=RetrievalMode.CATALOG, target=RetrievalTarget.FACETS, facet_value_limit=1
        )
    )

    assert len(next(h for h in resp.results if h.field == "tags").values) == 1
    assert resp.hints is not None
    assert resp.hints["reason"] == "facets_response_exceeds_inline_budget"
    assert resp.hints["budget_bytes"] == 1
    assert "recommended_facet_value_limit" not in resp.hints


# ---------------------------------------------------------------------------
# The recommendation, at payload shapes a real vault cannot reach
#
# A vault's four vocabulary facets are closed enumerations, so no corpus
# puts enough bytes outside the tags row to separate a recommendation
# that accounts for the response's fixed part from one that does not,
# and none makes a vocabulary row wider than the tag row while carrying
# fewer bytes. Both shapes are reachable in principle and neither is
# reachable through the graph store, so these build the payload
# directly. Each asserts the property that matters -- the simulated
# re-call fits -- alongside the arithmetic the rejected model would
# have produced, so the fixture carries its own discrimination instead
# of pinning a number whose significance has to be taken on trust.
# ---------------------------------------------------------------------------


#: The budget the synthetic payloads below were dimensioned against by
#: hand. Recorded as the origin of a scale rather than used as a bound:
#: what those fixtures need is a proportion -- how much of the response
#: is heavy row and how much is fixed part -- and scaling every width
#: from a single origin preserves it exactly, where re-tuning one width
#: against a new budget would not. Nothing asserts against this figure.
_SYNTHETIC_ORIGIN_BUDGET = 24576


def _scaled_width(width: int) -> int:
    """A hand-dimensioned width carried to the budget now in force.

    Widths fixed in characters stop being over budget the moment the
    budget rises past them, and a fixture that no longer crosses the
    line it was built to cross does not fail -- it passes for a reason
    that has nothing to do with the property it names.
    """
    return max(1, round(width * DEFAULT_MCP_INLINE_BUDGET_BYTES / _SYNTHETIC_ORIGIN_BUDGET))


def _facets_payload(rows: dict[str, dict[str, int]]) -> DiscoverResponse:
    """A facets response over the given field -> values mapping."""
    return DiscoverResponse(
        mode=RetrievalMode.CATALOG,
        target=RetrievalTarget.FACETS,
        results=[
            FacetHit(field=field, values=values, total_distinct=len(values))
            for field, values in rows.items()
        ],
        total_available=1000,
    )


def _payload_bytes(response: DiscoverResponse) -> int:
    """Delivered size, reached through the runtime rather than restated.

    Independence here has to be from the *runtime's* encoding, not from
    the implementation's spelling of it. An earlier version of this
    helper claimed the latter and re-expressed
    ``json.dumps(model_dump(...))`` -- the same expression the
    implementation used, spelled twice -- so it could report nothing the
    implementation would not, and did not report that the implementation
    was measuring a compact encoding the runtime does not deliver.
    ``pydantic_core.to_json(..., indent=2)`` on the dict the tool layer
    hands over is what actually reaches the wire.
    """
    dumped = response.model_dump(mode="json", exclude_none=True)
    return len(pydantic_core.to_json(dumped, fallback=str, indent=2))


def _payload_at_cap(response: DiscoverResponse, cap: int) -> DiscoverResponse:
    """The payload a re-call at ``cap`` would carry.

    ``total_distinct`` is carried through rather than recomputed: the
    real re-call reports the true distinct count, which is computed
    before any cap and does not move with it. Rebuilding it from the
    truncated length costs a byte per row whose digit count changes,
    which is small and in the safe direction, but makes the helper
    something other than the re-call its name claims.
    """
    return DiscoverResponse(
        mode=RetrievalMode.CATALOG,
        target=RetrievalTarget.FACETS,
        results=[
            FacetHit(
                field=row.field,
                values=dict(list(row.values.items())[:cap]),
                total_distinct=row.total_distinct,
            )
            for row in response.results
        ],
        total_available=response.total_available,
    )


def test_facets_recommendation_accounts_for_the_response_fixed_part(monkeypatch):
    """The recommended re-call fits where a proportional one would not.

    Scaling the cap in force by ``budget / size`` treats the response's
    fixed part -- the envelope, and every vocabulary row already shorter
    than the cap -- as though it shrank with the cap, so it names more
    values than fit. The error is the fixed part times the fraction the
    response was over, which the tag payload alone never makes large
    enough to escape a safety margin.

    Anti-coincidental-pass: the second assertion computes what the
    proportional model would have recommended and shows that re-call
    landing over budget, so the fixture is proved to discriminate on
    the run rather than asserted to. A fixture on which the two models
    agree passes the first assertion and fails the second. The
    ``> 1`` guard excludes the rival every fit assertion admits --
    always naming the floor, which fits trivially and would hand a
    caller one value out of fifty.
    """
    monkeypatch.delenv("SAGE_MCP_INLINE_BUDGET_BYTES", raising=False)
    budget = DEFAULT_MCP_INLINE_BUDGET_BYTES
    payload = _facets_payload(
        {
            "tags": {f"tag-{i:03d}-".ljust(_scaled_width(1000), "x"): 3 for i in range(50)},
            "doc_type": {f"vocab-{i:03d}-".ljust(_scaled_width(70), "y"): 7 for i in range(25)},
            "source_type": {f"other-{i:03d}-".ljust(_scaled_width(70), "z"): 7 for i in range(25)},
        }
    )
    size = _payload_bytes(payload)
    assert size > budget

    _apply_facets_budget_hint(payload)
    assert payload.hints is not None
    recommended = payload.hints["recommended_facet_value_limit"]
    assert recommended > 1
    assert _payload_bytes(_payload_at_cap(payload, recommended)) <= budget

    proportional = max(1, int(50 * budget / size * 0.95))
    assert proportional > recommended
    assert _payload_bytes(_payload_at_cap(payload, proportional)) > budget


def test_facets_recommendation_shrinks_the_heaviest_row_not_the_widest(monkeypatch):
    """A wide cheap row does not hide a narrow expensive one.

    The cap in force is the widest row's entry count, but the bytes can
    sit in a narrower row. A recommendation derived by scaling that
    count leaves the expensive row untouched until the cap falls below
    its own width, costing a round trip per step; here that walk is
    50 -> 22 -> ... where one call would do.

    Anti-coincidental-pass: the recommendation is asserted below the
    heavy row's width, which is the property that makes the re-call fit
    at all -- an implementation reading only the widest row lands above
    it and is caught by the fit assertion in the same breath -- and
    above the floor, which every fit assertion admits.
    """
    monkeypatch.delenv("SAGE_MCP_INLINE_BUDGET_BYTES", raising=False)
    budget = DEFAULT_MCP_INLINE_BUDGET_BYTES
    heavy_width = 12
    payload = _facets_payload(
        {
            "doc_type": {f"cheap-{i:03d}": 4 for i in range(50)},
            "tags": {
                f"tag-{i:03d}-".ljust(_scaled_width(2500), "x"): 9 for i in range(heavy_width)
            },
        }
    )
    assert _payload_bytes(payload) > budget

    _apply_facets_budget_hint(payload)
    assert payload.hints is not None
    recommended = payload.hints["recommended_facet_value_limit"]
    assert 1 < recommended < heavy_width
    assert _payload_bytes(_payload_at_cap(payload, recommended)) <= budget


def test_facets_recommendation_omitted_when_a_single_value_busts_the_budget(monkeypatch):
    """No cap helps when one value alone exceeds the ceiling.

    The other way into the omission branch, distinct from the cap of
    one: the cap in force is well above one and every candidate below
    it still overruns, so there is no re-call to name. Pins that the
    search reports absence rather than falling back on its floor.
    """
    monkeypatch.delenv("SAGE_MCP_INLINE_BUDGET_BYTES", raising=False)
    payload = _facets_payload(
        {"tags": {f"tag-{i}-".ljust(DEFAULT_MCP_INLINE_BUDGET_BYTES * 2, "x"): 1 for i in range(4)}}
    )

    _apply_facets_budget_hint(payload)
    assert payload.hints is not None
    assert payload.hints["reason"] == "facets_response_exceeds_inline_budget"
    assert "recommended_facet_value_limit" not in payload.hints


async def test_facets_budget_hint_absent_under_budget_at_default(graph_store, retrieval_service):
    """An ordinary facets call at the production budget carries no hint.

    The negative control for the whole change: real vaults sit far below
    the ceiling, so a hint that fired unconditionally would annotate
    every orientation call with advice to shrink a response that fits.
    """
    await _seed_facet_docs(graph_store)

    resp = await retrieval_service.discover(
        DiscoverRequest(mode=RetrievalMode.CATALOG, target=RetrievalTarget.FACETS)
    )

    assert resp.hints is None or resp.hints.get("reason") != "facets_response_exceeds_inline_budget"


# ---------------------------------------------------------------------------
# DiscoverRequest validator: target="facets" combinations
# ---------------------------------------------------------------------------


def test_target_facets_with_semantic_mode_is_mode_parameter_mismatch():
    """target=facets + mode=semantic raises mode_parameter_mismatch."""
    with pytest.raises(ValidationError) as info:
        DiscoverRequest(mode=RetrievalMode.SEMANTIC, query="x", target="facets")
    err = info.value.errors()[0]
    assert err["type"] == "mode_parameter_mismatch"
    assert err["ctx"]["forbidden_param"] == "target"
    assert err["ctx"]["allowed_modes"] == [RetrievalMode.CATALOG.value]


def test_target_facets_with_keyword_mode_is_mode_parameter_mismatch():
    """target=facets + mode=keyword raises mode_parameter_mismatch."""
    with pytest.raises(ValidationError) as info:
        DiscoverRequest(mode=RetrievalMode.KEYWORD, query="x", target="facets")
    assert info.value.errors()[0]["type"] == "mode_parameter_mismatch"


def test_target_facets_with_deterministic_mode_is_mode_parameter_mismatch():
    """target=facets + mode=deterministic raises mode_parameter_mismatch."""
    with pytest.raises(ValidationError) as info:
        DiscoverRequest(
            mode=RetrievalMode.DETERMINISTIC,
            document_id=_id("d1"),
            heading_path="x",
            target="facets",
        )
    assert info.value.errors()[0]["type"] == "mode_parameter_mismatch"


@pytest.mark.parametrize(
    "filter_kwargs,key",
    [
        ({"source_id": _id("d1")}, "source_id"),
        ({"target_id": _id("d1")}, "target_id"),
        ({"edge_type": "references"}, "edge_type"),
    ],
)
def test_target_facets_rejects_edge_only_filter_keys(filter_kwargs, key):
    """target=facets rejects every edge-only filter key with
    mode_parameter_mismatch carrying the offending key.
    """
    with pytest.raises(ValidationError) as info:
        DiscoverRequest(
            mode=RetrievalMode.CATALOG,
            target="facets",
            filters=RetrievalFilters(**filter_kwargs),
        )
    err = info.value.errors()[0]
    assert err["type"] == "mode_parameter_mismatch"
    assert err["ctx"]["forbidden_param"] == f"filters.{key}"


@pytest.mark.parametrize(
    "field,value",
    [
        ("query", "anything"),
        ("document_id", _id("d1")),
        ("heading_path", "Section 1"),
        ("min_relevance", 0.5),
        ("sort_by", "title"),
        ("sort_order", "desc"),
        ("include_abstracts", True),
        ("response_mode", "light"),
        ("limit", 11),
        ("offset", 5),
    ],
)
def test_target_facets_rejects_non_default_request_parameters(field, value):
    """target=facets rejects every request parameter that has no facet
    semantics when it carries an explicit non-default value. Unlike
    target=edges, that includes the pagination knobs: a facets response
    is one fixed row per field, so limit/offset/sort/response_mode have
    nothing to act on.

    Asserting ctx["forbidden_param"] (not just the error type) keeps a
    different validator branch from passing this test for the wrong
    reason.
    """
    kwargs = {"mode": RetrievalMode.CATALOG, "target": "facets", field: value}
    with pytest.raises(ValidationError) as info:
        DiscoverRequest(**kwargs)
    err = info.value.errors()[0]
    assert err["type"] == "mode_parameter_mismatch"
    assert err["ctx"]["forbidden_param"] == field


def test_target_facets_accepts_document_filters_with_default_parameters():
    """target=facets + document-only filters + all-default knobs
    constructs successfully — guards against over-rejection making the
    rejection tests pass trivially.
    """
    req = DiscoverRequest(
        mode=RetrievalMode.CATALOG,
        target="facets",
        filters=RetrievalFilters(
            doc_type="ticket",
            tags=["a"],
            tier3_metadata={"k": "v"},
        ),
    )
    assert req.target.value == "facets"


def test_facet_params_accepted_with_target_facets():
    """facet_fields and facet_value_limit construct under target=facets.

    Guards against the new parameters landing in the facet-forbidden
    list by accident, which would reject the only target they exist for.
    """
    req = DiscoverRequest(
        mode=RetrievalMode.CATALOG,
        target="facets",
        facet_fields=["tags", "doc_type"],
        facet_value_limit=200,
    )
    assert [f.value for f in req.facet_fields] == ["tags", "doc_type"]
    assert req.facet_value_limit == 200

    # No upper bound: full-vocabulary reachability requires that a
    # caller can always set the limit to the reported total_distinct,
    # however large the corpus has grown. A hidden ceiling below a real
    # vocabulary size would strand the documented two-step opt.
    huge = DiscoverRequest(mode=RetrievalMode.CATALOG, target="facets", facet_value_limit=1_000_000)
    assert huge.facet_value_limit == 1_000_000


@pytest.mark.parametrize("target", ["documents", "edges"])
@pytest.mark.parametrize(
    "field,value",
    [
        ("facet_fields", ["tags"]),
        ("facet_value_limit", 5),
    ],
)
def test_facet_params_rejected_for_non_facet_targets(target, field, value):
    """Facet-only parameters are rejected for the document and edge
    targets rather than silently ignored.

    Asserting ctx["forbidden_param"] (not just the error type) keeps a
    different validator branch from passing this test for the wrong
    reason.
    """
    with pytest.raises(ValidationError) as info:
        DiscoverRequest(mode=RetrievalMode.CATALOG, target=target, **{field: value})
    err = info.value.errors()[0]
    assert err["type"] == "mode_parameter_mismatch"
    assert err["ctx"]["forbidden_param"] == field


def test_facet_fields_unknown_name_and_empty_list_rejected():
    """facet_fields is a closed vocabulary and may not be empty.

    An untyped list[str] annotation would accept the bogus name and
    fail here; the typed enum makes the valid set part of the error.
    """
    with pytest.raises(ValidationError) as info:
        DiscoverRequest(mode=RetrievalMode.CATALOG, target="facets", facet_fields=["bogus"])
    err = info.value.errors()[0]
    assert err["type"] == "enum"
    assert err["loc"][0] == "facet_fields"

    with pytest.raises(ValidationError) as info:
        DiscoverRequest(mode=RetrievalMode.CATALOG, target="facets", facet_fields=[])
    assert info.value.errors()[0]["type"] == "too_short"


@pytest.mark.parametrize("value", [0, -1])
def test_facet_value_limit_below_one_rejected(value):
    """facet_value_limit has no zero-or-negative meaning: there is no
    unlimited sentinel (a plausible misreading of 0 as give me
    everything would reproduce the unbounded-response failure the cap
    exists to prevent). Full vocabularies are reached by re-calling
    with the reported total_distinct instead.
    """
    with pytest.raises(ValidationError) as info:
        DiscoverRequest(mode=RetrievalMode.CATALOG, target="facets", facet_value_limit=value)
    assert info.value.errors()[0]["type"] == "greater_than_equal"


# ---------------------------------------------------------------------------
# / Response_mode unified across search targets;
# response_level retired.
# ---------------------------------------------------------------------------


def test_response_mode_light_accepted_with_target_documents():
    """Response_mode=light + target=documents is now accepted
    (was rejected in by design)."""
    req = DiscoverRequest(
        mode=RetrievalMode.CATALOG,
        target=RetrievalTarget.DOCUMENTS,
        response_mode=ResponseMode.LIGHT,
    )
    assert req.response_mode == ResponseMode.LIGHT
    assert req.target == RetrievalTarget.DOCUMENTS


def test_response_mode_full_accepted_with_target_documents():
    """Response_mode=full + target=documents is now accepted."""
    req = DiscoverRequest(
        mode=RetrievalMode.CATALOG,
        target=RetrievalTarget.DOCUMENTS,
        response_mode=ResponseMode.FULL,
    )
    assert req.response_mode == ResponseMode.FULL


def test_response_mode_unset_with_target_documents_preserves_defaults():
    """When response_mode is not passed, the request builds with
    response_mode defaulting to None (unset). (Response_level
    field has been removed from DiscoverRequest.)"""
    req = DiscoverRequest(mode=RetrievalMode.SEMANTIC, query="x")
    assert req.response_mode is None


async def test_catalog_documents_light_returns_stripped_summary(graph_store, retrieval_service):
    """Catalog + target=documents + response_mode=light returns
    a stripped DocumentSummaryLight carrying only id, title, doc_type,
    lifecycle_status, tier3_metadata.

    Anti-coincidental: seed a doc with non-trivial source_path / tags /
    document_date / semantic_abstract / version_label, then assert each
    of those fields is ABSENT from the returned model (not present-but-
    null). An implementation that accepts response_mode=light but
    ignores it in _catalog would return full DocumentSummary and the
    absence checks would fail.
    """
    from sage.models.schemas import DocumentSummaryLight

    doc = _make_doc(
        _id("light_doc"),
        doc_type="adr",
        tags=["t1", "t2"],
        document_date="2026-05-23",
        semantic_abstract="LLM abstract text.",
        version_label="v1",
        tier3_metadata={"ticket_id": "T-0158"},
    )
    await graph_store.insert_document(doc)

    req = DiscoverRequest(
        mode=RetrievalMode.CATALOG,
        target=RetrievalTarget.DOCUMENTS,
        response_mode=ResponseMode.LIGHT,
        filters=RetrievalFilters(doc_type="adr"),
    )
    resp = await retrieval_service.discover(req)
    assert len(resp.results) >= 1
    hit = next(h for h in resp.results if h.document.id == doc.id)
    assert isinstance(hit.document, DocumentSummaryLight)
    light_keys = set(DocumentSummaryLight.model_fields.keys())
    dump_keys = set(hit.document.model_dump().keys())
    assert dump_keys == light_keys, (
        f"light shape must expose exactly the DocumentSummaryLight fields, got {dump_keys}"
    )
    # Concrete absence checks for the stripped fields.
    forbidden = {
        "source_type",
        "source_path",
        "version_label",
        "project",
        "tags",
        "document_date",
        "source_modified_at",
        "semantic_abstract",
    }
    assert forbidden.isdisjoint(dump_keys), (
        f"light shape must drop {forbidden & dump_keys} from DocumentSummary"
    )
    # The light fields that DO survive must carry the seeded values.
    assert hit.document.id == doc.id
    assert hit.document.title == doc.title
    assert hit.document.doc_type == "adr"
    assert hit.document.lifecycle_status == "active"
    assert hit.document.tier3_metadata == {"ticket_id": "T-0158"}


async def test_catalog_documents_full_returns_current_summary(graph_store, retrieval_service):
    """Catalog + target=documents + response_mode=full returns
    the current DocumentSummary unchanged.

    Anti-coincidental: seed a doc with every nullable field populated,
    assert every DocumentSummary field appears in the dump. An
    implementation that returned DocumentSummaryLight for catalog+
    documents unconditionally would lose source_path / tags / etc.
    """
    doc = _make_doc(
        _id("full_doc"),
        doc_type="adr",
        tags=["t1", "t2"],
        document_date="2026-05-23",
        semantic_abstract="LLM abstract text.",
        version_label="v1",
        tier3_metadata={"ticket_id": "T-0158"},
        source_modified_at=datetime(2026, 5, 23, tzinfo=timezone.utc),
        project="CAS",
    )
    await graph_store.insert_document(doc)

    req = DiscoverRequest(
        mode=RetrievalMode.CATALOG,
        target=RetrievalTarget.DOCUMENTS,
        response_mode=ResponseMode.FULL,
        filters=RetrievalFilters(doc_type="adr"),
        include_abstracts=True,
    )
    resp = await retrieval_service.discover(req)
    hit = next(h for h in resp.results if h.document.id == doc.id)
    assert isinstance(hit.document, DocumentSummary)
    dump_keys = set(hit.document.model_dump().keys())
    assert dump_keys == set(DocumentSummary.model_fields.keys()), (
        f"full shape must contain every DocumentSummary field, missing: "
        f"{set(DocumentSummary.model_fields.keys()) - dump_keys}"
    )
    # Anti-coincidental concrete checks: non-trivial fields must round-trip.
    assert hit.document.source_path == doc.source_path
    assert hit.document.tags == ["t1", "t2"]
    assert hit.document.semantic_abstract == "LLM abstract text."
    assert hit.document.version_label == "v1"
    assert hit.document.project == "CAS"


async def test_catalog_documents_light_empty_result_does_not_crash(graph_store, retrieval_service):
    """Catalog + target=documents + response_mode=light with zero
    matching rows returns an empty response envelope without exception.

    Pins the empty-result branch that the ticket calls out as the path
    that worked pre-fix. Guards against a future refactor that would,
    for example, call the post-filter mutation loop or the budget-hint
    helper unconditionally on empty result lists.
    """
    # Seed one non-matching doc so the graph store isn't trivially empty;
    # filter asks for ticket, doc is adr.
    other = _make_doc(_id("other_doc"), doc_type="adr")
    await graph_store.insert_document(other)

    req = DiscoverRequest(
        mode=RetrievalMode.CATALOG,
        target=RetrievalTarget.DOCUMENTS,
        response_mode=ResponseMode.LIGHT,
        filters=RetrievalFilters(doc_type="ticket", lifecycle_status="active"),
    )
    resp = await retrieval_service.discover(req)
    assert resp.results == []
    assert resp.total_available == 0
    assert resp.mode == RetrievalMode.CATALOG
    assert resp.target == RetrievalTarget.DOCUMENTS


async def test_catalog_documents_light_nonempty_does_not_crash_on_post_projection_mutation(
    graph_store, retrieval_service
):
    """Catalog + target=documents + response_mode=light with one
    or more matching rows does NOT raise ``ValueError`` from the
    post-filter ``hit.document.semantic_abstract = None`` mutation site
    at ``sage/services/retrieval.py``.

    Anti-coincidental: the seeded Document carries a populated
    ``semantic_abstract`` (plus every other field DocumentSummaryLight
    strips) so the projection must produce a DocumentSummaryLight whose
    post-projection mutation, if unguarded, would raise
    ``"DocumentSummaryLight" object has no field "semantic_abstract"``.
    ``include_abstracts`` is left at its default (False) so the
    post-filter loop actually iterates ``response.results``. Removing
    the ``isinstance(hit.document, DocumentSummary)`` guard at the
    mutation site makes ``discover`` raise on the await, failing this
    test on the call line.
    """
    doc = _make_doc(
        _id("t0160_doc"),
        doc_type="ticket",
        lifecycle_status="active",
        tags=["regression", "t-0160"],
        document_date="2026-05-23",
        semantic_abstract="LLM abstract for T-0160 regression doc.",
        version_label="v1",
        tier3_metadata={
            "ticket_id": "T-0160",
            "ticket_type": "fix",
            "ticket_priority": "high",
        },
        source_modified_at=datetime(2026, 5, 23, tzinfo=timezone.utc),
        project="CAS",
    )
    await graph_store.insert_document(doc)

    req = DiscoverRequest(
        mode=RetrievalMode.CATALOG,
        target=RetrievalTarget.DOCUMENTS,
        response_mode=ResponseMode.LIGHT,
        filters=RetrievalFilters(doc_type="ticket", lifecycle_status="active"),
    )
    resp = await retrieval_service.discover(req)
    assert resp.total_available >= 1
    hit = next(h for h in resp.results if h.document.id == doc.id)
    from sage.models.schemas import DocumentSummaryLight

    assert isinstance(hit.document, DocumentSummaryLight)
    assert not isinstance(hit.document, DocumentSummary)
    assert "semantic_abstract" not in hit.document.model_dump().keys()
    assert hit.document.tier3_metadata == {
        "ticket_id": "T-0160",
        "ticket_type": "fix",
        "ticket_priority": "high",
    }


async def test_catalog_documents_light_explicit_include_abstracts_false_does_not_crash(
    graph_store, retrieval_service
):
    """Same regression as the default-include_abstracts test,
    but with ``include_abstracts=False`` set explicitly on the request.

    Pins the contract: post-projection mutation safety must hold
    regardless of whether the caller relies on the default or passes
    the parameter explicitly. Future-proofs against a default flip
    that would otherwise make the companion test stop exercising the
    post-filter loop.
    """
    doc = _make_doc(
        _id("t0160_explicit_doc"),
        doc_type="ticket",
        lifecycle_status="active",
        tags=["regression", "t-0160"],
        document_date="2026-05-23",
        semantic_abstract="LLM abstract for T-0160 explicit-flag doc.",
        version_label="v1",
        tier3_metadata={
            "ticket_id": "T-0160",
            "ticket_type": "fix",
            "ticket_priority": "high",
        },
        source_modified_at=datetime(2026, 5, 23, tzinfo=timezone.utc),
        project="CAS",
    )
    await graph_store.insert_document(doc)

    req = DiscoverRequest(
        mode=RetrievalMode.CATALOG,
        target=RetrievalTarget.DOCUMENTS,
        response_mode=ResponseMode.LIGHT,
        filters=RetrievalFilters(doc_type="ticket", lifecycle_status="active"),
        include_abstracts=False,
    )
    resp = await retrieval_service.discover(req)
    assert resp.total_available >= 1
    hit = next(h for h in resp.results if h.document.id == doc.id)
    from sage.models.schemas import DocumentSummaryLight

    assert isinstance(hit.document, DocumentSummaryLight)
    assert not isinstance(hit.document, DocumentSummary)
    assert "semantic_abstract" not in hit.document.model_dump().keys()


async def test_semantic_response_mode_light_suppresses_chunk_content(
    graph_store,
    stub_content_store,
    seeded_embedding_provider,
    retrieval_service,
):
    """Semantic + response_mode=light suppresses chunk_content
    but preserves heading_path, matched_chunk_count, and the surrounding
    full DocumentSummary (the catalog stripped-summary shape does NOT
    apply to semantic/keyword).

    Anti-coincidental: assert hit.document is a DocumentSummary (not
    Light) -- catches an implementation that misroutes semantic+light
    through the stripped-summary projection.
    """
    await _seed_response_level_docs(graph_store, stub_content_store, seeded_embedding_provider)
    req = DiscoverRequest(
        mode=RetrievalMode.SEMANTIC,
        query="integration",
        response_mode=ResponseMode.LIGHT,
        limit=10,
    )
    resp = await retrieval_service.discover(req)
    assert len(resp.results) > 0
    for hit in resp.results:
        assert hit.chunk_content is None
        assert hit.heading_path is not None
        assert hit.matched_chunk_count is not None
        assert hit.matched_chunk_count >= 1
        assert isinstance(hit.document, DocumentSummary)


async def test_semantic_response_mode_full_includes_chunk_content(
    graph_store,
    stub_content_store,
    seeded_embedding_provider,
    retrieval_service,
):
    """Semantic + response_mode=full includes chunk_content."""
    await _seed_response_level_docs(graph_store, stub_content_store, seeded_embedding_provider)
    req = DiscoverRequest(
        mode=RetrievalMode.SEMANTIC,
        query="integration",
        response_mode=ResponseMode.FULL,
        limit=10,
    )
    resp = await retrieval_service.discover(req)
    assert len(resp.results) > 0
    for hit in resp.results:
        assert hit.chunk_content is not None and hit.chunk_content != ""


async def test_keyword_response_mode_light_suppresses_chunk_content(
    graph_store,
    stub_content_store,
    seeded_embedding_provider,
    retrieval_service,
):
    """Keyword + response_mode=light mirror of the semantic
    light-mode test.
    """
    await _seed_response_level_docs(graph_store, stub_content_store, seeded_embedding_provider)
    req = DiscoverRequest(
        mode=RetrievalMode.KEYWORD,
        query="integration",
        response_mode=ResponseMode.LIGHT,
        limit=10,
    )
    resp = await retrieval_service.discover(req)
    assert len(resp.results) > 0
    for hit in resp.results:
        assert hit.chunk_content is None
        assert hit.heading_path is not None
        assert isinstance(hit.document, DocumentSummary)


async def test_keyword_response_mode_full_includes_chunk_content(
    graph_store,
    stub_content_store,
    seeded_embedding_provider,
    retrieval_service,
):
    """Keyword + response_mode=full includes chunk_content."""
    await _seed_response_level_docs(graph_store, stub_content_store, seeded_embedding_provider)
    req = DiscoverRequest(
        mode=RetrievalMode.KEYWORD,
        query="integration",
        response_mode=ResponseMode.FULL,
        limit=10,
    )
    resp = await retrieval_service.discover(req)
    assert len(resp.results) > 0
    for hit in resp.results:
        assert hit.chunk_content is not None and hit.chunk_content != ""


async def test_deterministic_response_mode_light_returns_chunk_content(
    graph_store, stub_content_store, seeded_embedding_provider, retrieval_service
):
    """Deterministic mode ignores response_mode (always returns
    chunk content).

    Anti-coincidental: ensures an implementation that uniformly applies
    light->suppress-chunks doesn't accidentally strip deterministic
    output.
    """
    doc_id = _id("det_doc")
    doc = _make_doc(doc_id)
    await graph_store.insert_document(doc)
    await _index_doc_chunks(
        stub_content_store,
        seeded_embedding_provider,
        doc_id,
        [("Section 1", "Deterministic mode integration body.")],
    )
    req = DiscoverRequest(
        mode=RetrievalMode.DETERMINISTIC,
        document_id=doc_id,
        heading_path="Section 1",
        response_mode=ResponseMode.LIGHT,
    )
    resp = await retrieval_service.discover(req)
    assert len(resp.results) >= 1
    for hit in resp.results:
        assert hit.chunk_content is not None and hit.chunk_content != ""


async def test_catalog_documents_neither_param_set_returns_full_shape_above_threshold(
    graph_store, retrieval_service
):
    """Catalog + target=documents with response_mode unset must
    return full DocumentSummary shape even when the result count
    exceeds the edge-side >5 default-light threshold.

    Anti-coincidental: seeds 6 docs (above the threshold) and asserts
    the returned hits carry full DocumentSummary instances populated
    with the non-trivial fields. An implementation that copied the
    >5-default-light rule from _catalog_edges into _catalog would
    return DocumentSummaryLight and the isinstance check would fail.
    """
    for i in range(6):
        d = _make_doc(
            _id(f"threshold_doc_{i}"),
            doc_type="adr",
            tags=[f"t{i}"],
            semantic_abstract=f"Abstract {i}",
        )
        await graph_store.insert_document(d)

    req = DiscoverRequest(
        mode=RetrievalMode.CATALOG,
        target=RetrievalTarget.DOCUMENTS,
        filters=RetrievalFilters(doc_type="adr"),
        limit=20,
    )
    resp = await retrieval_service.discover(req)
    assert resp.total_available >= 6
    for hit in resp.results:
        assert isinstance(hit.document, DocumentSummary), (
            "catalog+documents default must return full DocumentSummary "
            "regardless of result count; the >5-default-light rule is "
            "edge-only by design."
        )
        # Confirm full-shape fields are populated (sanity).
        assert hit.document.source_type is not None


def test_response_level_field_removed_from_discover_request():
    """The response_level Field has been removed from
    DiscoverRequest. Anti-coincidental: assert the absence of the
    field rather than rely on a [DEPRECATED] marker that no longer
    exists.
    """
    assert "response_level" not in DiscoverRequest.model_fields


# ---------------------------------------------------------------------------
# source_type as a document filter key, and out-of-vocabulary filter-value
# diagnostics on doc_type / lifecycle_status.
# ---------------------------------------------------------------------------


async def _seed_mixed_source_type_docs(graph_store):
    """Seed 4 documents spanning two source types.

    Two markdown, two docx, and no pdf. The complement matters: a
    source_type filter that is silently dropped returns all four, so a
    homogeneous seed could not tell a working filter from a no-op one.
    """
    docs = {
        _id("st_md_one"): _make_doc(_id("st_md_one"), source_type=SourceType.MARKDOWN),
        _id("st_md_two"): _make_doc(_id("st_md_two"), source_type=SourceType.MARKDOWN),
        _id("st_docx_one"): _make_doc(_id("st_docx_one"), source_type=SourceType.DOCX),
        _id("st_docx_two"): _make_doc(_id("st_docx_two"), source_type=SourceType.DOCX),
    }
    for doc in docs.values():
        await graph_store.insert_document(doc)
    return docs


async def test_catalog_source_type_filter_hit(graph_store, retrieval_service):
    """Catalog mode filters on source_type, excluding the complement.

    Anti-coincidental: asserts the markdown ids are ABSENT, not merely
    that the docx ids are present. A dropped filter returns all four.
    """
    await _seed_mixed_source_type_docs(graph_store)

    request = DiscoverRequest(
        mode=RetrievalMode.CATALOG,
        filters=RetrievalFilters(source_type=SourceType.DOCX),
    )
    response = await retrieval_service.discover(request)

    result_ids = {h.document.id for h in response.results}
    assert result_ids == {_id("st_docx_one"), _id("st_docx_two")}
    assert _id("st_md_one") not in result_ids
    assert _id("st_md_two") not in result_ids
    assert response.total_available == 2


async def test_catalog_source_type_filter_miss(graph_store, retrieval_service):
    """A valid-but-unrepresented source_type returns empty WITHOUT a warning.

    Anti-coincidental for the vocabulary warning: pdf is a real
    SourceType, so emptiness here is a genuine zero-match, not a
    vocabulary error. An implementation that warns on emptiness rather
    than on vocabulary fails this.
    """
    await _seed_mixed_source_type_docs(graph_store)

    request = DiscoverRequest(
        mode=RetrievalMode.CATALOG,
        filters=RetrievalFilters(source_type=SourceType.PDF),
    )
    response = await retrieval_service.discover(request)

    assert response.results == []
    assert response.total_available == 0
    assert response.hints is None or "warnings" not in response.hints


async def _seed_mixed_source_type_chunks(
    graph_store, stub_content_store, seeded_embedding_provider
):
    """Seed the mixed-source docs with searchable chunks."""
    docs = await _seed_mixed_source_type_docs(graph_store)
    for doc_id in docs:
        await _index_doc_chunks(
            stub_content_store,
            seeded_embedding_provider,
            doc_id,
            [("Section 1", "Quarterly compliance narrative for the filing.")],
        )
    return docs


@pytest.mark.parametrize("mode", [RetrievalMode.SEMANTIC, RetrievalMode.KEYWORD])
async def test_source_type_pushdown_scoped_to_matching_docs(
    graph_store, stub_content_store, seeded_embedding_provider, retrieval_service, mode
):
    """Semantic and keyword modes honor source_type via document resolution."""
    await _seed_mixed_source_type_chunks(graph_store, stub_content_store, seeded_embedding_provider)

    request = DiscoverRequest(
        mode=mode,
        query="quarterly compliance filing",
        filters=RetrievalFilters(source_type=SourceType.DOCX),
    )
    response = await retrieval_service.discover(request)

    result_ids = {h.document.id for h in response.results}
    assert result_ids, "expected the docx-sourced documents to match the query"
    assert result_ids <= {_id("st_docx_one"), _id("st_docx_two")}


@pytest.mark.parametrize("mode", [RetrievalMode.SEMANTIC, RetrievalMode.KEYWORD])
async def test_source_type_pushdown_does_not_call_list_all_documents(
    graph_store, stub_content_store, seeded_embedding_provider, retrieval_service, mode
):
    """source_type resolves through a filtered query, never a full scan.

    Anti-coincidental, and it takes a pair of observables. Refusing
    ``list_all_documents`` alone excludes the obvious rival -- a Python
    post-filter over every document -- but not the one that costs the
    same: querying on the OTHER filters and narrowing on source_type in
    Python touches ``query_documents``, never ``list_all_documents``,
    and so would satisfy a call-avoidance assertion while still reading
    the whole vault. The filter dict handed to the store is therefore
    asserted too. Neither observable discriminates alone; together they
    pin the predicate to the store.
    """
    await _seed_mixed_source_type_chunks(graph_store, stub_content_store, seeded_embedding_provider)

    async def _explode(*args, **kwargs):
        raise AssertionError("list_all_documents must not be called for a source_type filter")

    graph_store.list_all_documents = _explode

    seen_filters: list[dict | None] = []
    original_query_documents = graph_store.query_documents

    async def _recording_query_documents(filters=None, *args, **kwargs):
        seen_filters.append(filters)
        return await original_query_documents(filters, *args, **kwargs)

    graph_store.query_documents = _recording_query_documents

    request = DiscoverRequest(
        mode=mode,
        query="quarterly compliance filing",
        filters=RetrievalFilters(source_type=SourceType.DOCX),
    )
    response = await retrieval_service.discover(request)

    assert {h.document.id for h in response.results} <= {
        _id("st_docx_one"),
        _id("st_docx_two"),
    }
    assert any((f or {}).get("source_type") == "docx" for f in seen_filters), (
        "source_type must reach the store as a predicate; resolving it in "
        f"Python costs a full read. Filters seen: {seen_filters!r}"
    )


async def test_source_type_in_active_filters_hints(
    graph_store, stub_content_store, seeded_embedding_provider, retrieval_service
):
    """An active source_type filter is surfaced in the empty-result hints."""
    doc = _make_doc(_id("st_hint_md"), source_type=SourceType.MARKDOWN)
    await graph_store.insert_document(doc)
    await _index_doc_chunks(
        stub_content_store,
        seeded_embedding_provider,
        _id("st_hint_md"),
        [("Section 1", "Interesting report content about claims.")],
    )

    request = DiscoverRequest(
        mode=RetrievalMode.SEMANTIC,
        query="report claims",
        filters=RetrievalFilters(source_type=SourceType.PPTX),
    )
    response = await retrieval_service.discover(request)

    assert response.results == []
    assert response.hints is not None
    assert response.hints["active_filters"].get("source_type") == "pptx"


# --- out-of-vocabulary filter values ---------------------------------------
#
# ``minimal_config`` declares doc_types {note, memo} and lifecycle states
# {active, completed, archived}. Values outside those sets are
# out-of-vocabulary for this vault.


async def test_undefined_doc_type_emits_warning(graph_store, retrieval_service):
    """An undefined doc_type value yields a warning naming it and the vocabulary."""
    await graph_store.insert_document(_make_doc(_id("oov_doc_a"), doc_type="note"))

    request = DiscoverRequest(
        mode=RetrievalMode.CATALOG,
        filters=RetrievalFilters(doc_type="template"),
    )
    response = await retrieval_service.discover(request)

    assert response.results == []
    assert response.hints is not None
    warnings = response.hints.get("warnings")
    assert warnings, "an undefined doc_type must surface a hints warning"
    joined = " ".join(warnings)
    assert "template" in joined
    assert "note" in joined and "memo" in joined


async def test_defined_doc_type_no_warning(graph_store, retrieval_service):
    """A defined doc_type with zero matches must NOT warn.

    Anti-coincidental trap for "always warn when doc_type is set".
    """
    await graph_store.insert_document(_make_doc(_id("oov_doc_b"), doc_type="note"))

    request = DiscoverRequest(
        mode=RetrievalMode.CATALOG,
        filters=RetrievalFilters(doc_type="memo"),
    )
    response = await retrieval_service.discover(request)

    assert response.results == []
    assert response.hints is None or "warnings" not in response.hints


async def test_undefined_lifecycle_status_emits_warning(graph_store, retrieval_service):
    """An undefined lifecycle_status value yields a warning."""
    await graph_store.insert_document(_make_doc(_id("oov_lc_a")))

    request = DiscoverRequest(
        mode=RetrievalMode.CATALOG,
        filters=RetrievalFilters(lifecycle_status="filed"),
    )
    response = await retrieval_service.discover(request)

    assert response.results == []
    assert response.hints is not None
    warnings = response.hints.get("warnings")
    assert warnings, "an undefined lifecycle_status must surface a hints warning"
    joined = " ".join(warnings)
    assert "filed" in joined
    assert "archived" in joined


async def test_defined_lifecycle_status_no_warning(graph_store, retrieval_service):
    """A defined lifecycle_status with zero matches must NOT warn."""
    await graph_store.insert_document(_make_doc(_id("oov_lc_b"), lifecycle_status="active"))

    request = DiscoverRequest(
        mode=RetrievalMode.CATALOG,
        filters=RetrievalFilters(lifecycle_status="completed"),
    )
    response = await retrieval_service.discover(request)

    assert response.results == []
    assert response.hints is None or "warnings" not in response.hints


# --- keyword conjunction advisory ------------------------------------------
#
# Keyword mode is conjunctive at the port: every term must appear somewhere in
# the document, though not necessarily together in one passage (CAS-ADR-048).
# An empty result therefore has two indistinguishable readings -- the vault
# holds nothing, or the query asked for too much at once. These cover the
# advisory that separates them. The conjunction itself is pinned per binding,
# against Postgres in test_content_store_postgres.py and against the double in
# test_stub_content_store.py; what these turn on is the sentence the service
# attaches once a search has come back empty.


async def test_keyword_multi_term_empty_result_warns(
    graph_store, stub_content_store, seeded_embedding_provider, retrieval_service
):
    """A multi-term keyword query matching nothing names its terms and the conjunction."""
    await graph_store.insert_document(_make_doc(_id("kw_warn_a")))
    await _index_doc_chunks(
        stub_content_store,
        seeded_embedding_provider,
        _id("kw_warn_a"),
        [("Section 1", "alphaword content only.")],
    )

    # Positive control: the corpus is reachable in keyword mode, so the empty
    # result below is the query's doing rather than an empty vault -- which is
    # the distinction the advisory exists to draw.
    control = await retrieval_service.discover(
        DiscoverRequest(mode=RetrievalMode.KEYWORD, query="alphaword")
    )
    assert control.results, "precondition: the indexed corpus must be searchable"

    request = DiscoverRequest(mode=RetrievalMode.KEYWORD, query="deltaword epsilonword")
    response = await retrieval_service.discover(request)

    assert response.results == []
    assert response.hints is not None, "an empty multi-term keyword result must carry hints"
    warnings = response.hints.get("warnings")
    assert warnings, "an empty multi-term keyword result must surface a warning"
    joined = " ".join(warnings)
    assert "deltaword" in joined and "epsilonword" in joined, (
        "the warning must name the terms the query required"
    )
    assert "document" in joined, "and must name the unit the conjunction is scoped to"
    # Paired with the positive assertion above so the two absence checks below
    # cannot be satisfied by an advisory that simply says less.
    assert "chunk" not in joined.lower(), (
        "the advisory must not claim the terms had to share a chunk; the match "
        "is over the document, and a sentence asserting otherwise sends the "
        "caller looking for a constraint that is not there"
    )
    assert "quote a phrase" not in joined, (
        "a phrase adds an adjacency requirement inside one passage, so it can "
        "only narrow an already-empty result; no remedy offered here may make "
        "the query stricter"
    )


async def test_keyword_single_term_empty_result_stays_silent(
    graph_store, stub_content_store, seeded_embedding_provider, retrieval_service
):
    """A single-term miss is a true zero and must NOT warn.

    Anti-coincidental trap for "warn on any empty keyword result": with one
    term there is no conjunction to explain, and a warning would misdirect.
    """
    await graph_store.insert_document(_make_doc(_id("kw_warn_b")))
    await _index_doc_chunks(
        stub_content_store,
        seeded_embedding_provider,
        _id("kw_warn_b"),
        [("Section 1", "alphaword content only.")],
    )

    # Positive control: the corpus answers a term it does carry, so the silence
    # below is a single-term miss on a reachable vault rather than an empty one.
    control = await retrieval_service.discover(
        DiscoverRequest(mode=RetrievalMode.KEYWORD, query="alphaword")
    )
    assert control.results, "precondition: the indexed corpus must be searchable"

    request = DiscoverRequest(mode=RetrievalMode.KEYWORD, query="deltaword")
    response = await retrieval_service.discover(request)

    assert response.results == []
    assert response.hints is None or "warnings" not in response.hints


async def test_keyword_multi_term_with_hits_has_no_warning(
    graph_store, stub_content_store, seeded_embedding_provider, retrieval_service
):
    """A multi-term query that matches must NOT warn.

    Anti-coincidental trap for "warn whenever the query is multi-term".
    """
    await graph_store.insert_document(_make_doc(_id("kw_warn_c")))
    await _index_doc_chunks(
        stub_content_store,
        seeded_embedding_provider,
        _id("kw_warn_c"),
        [("Section 1", "alphaword betaword together here.")],
    )

    request = DiscoverRequest(mode=RetrievalMode.KEYWORD, query="alphaword betaword")
    response = await retrieval_service.discover(request)

    assert response.results, "precondition: the query must match"
    assert response.hints is None or "warnings" not in response.hints


# The advisory reports what the keyword search returned; the metadata boost then
# admits documents that search never saw, matching a title or a tag by substring
# in the graph store rather than by lexeme in the content store. The two disagree
# wherever a substring cuts a word, and a response can carry the boost's
# documents beside a sentence asserting there are none. These turn on the gate
# that keeps the advisory to the empty responses it was written to explain.
#
# The fixture pairing is what makes the divergence reachable in-suite: the
# content-store double never consults the document surface, so a title reaches
# the response only through the boost, while the graph store here is the real
# binding and its substring match is the real one.

_BOOST_ONLY_TITLE = "Keyword Advisory Reconciliation Notes"

# Two terms, so the conjunction advisory is reachable at all -- a single term
# never reaches it. A contiguous substring of the title, so the boost answers.
# And the second term is a word cut mid-way, which is the only thing that keeps
# the content store from answering too: no text in the fixture carries it.
_BOOST_ONLY_QUERY = "Advisory Reconcil"


async def test_keyword_advisory_suppressed_when_boost_supplied_results(
    graph_store, stub_content_store, seeded_embedding_provider, retrieval_service
):
    """A response the boost filled carries no advisory claiming nothing matched.

    Both halves share one fixture, because what is at stake is a condition and
    not the sentence: a service that stopped attaching the advisory at all
    satisfies the first assertion and fails the second.

    What this pair does not settle is *which* condition. A service suppressing
    on the boost's own search having answered passes both halves here, because
    on this fixture the boost answering and the caller receiving coincide. The
    sibling below separates them, and is the reason that rival is excluded.
    """
    doc = _make_doc(_id("kw_boost_advisory"))
    doc.title = _BOOST_ONLY_TITLE
    await graph_store.insert_document(doc)
    await _index_doc_chunks(
        stub_content_store,
        seeded_embedding_provider,
        _id("kw_boost_advisory"),
        [("Section 1", "alphaword content only.")],
    )

    # Positive control: the corpus is reachable in keyword mode, so the boosted
    # hit below is a document the content search could have returned and did
    # not, rather than the only thing in a vault nothing else can reach.
    control = await retrieval_service.discover(
        DiscoverRequest(mode=RetrievalMode.KEYWORD, query="alphaword")
    )
    assert control.results, "precondition: the indexed corpus must be searchable"

    boosted = await retrieval_service.discover(
        DiscoverRequest(mode=RetrievalMode.KEYWORD, query=_BOOST_ONLY_QUERY)
    )

    assert boosted.results, (
        "precondition: the query must be one the content store misses and the "
        "metadata boost answers -- without a result there is no contradiction "
        "to suppress"
    )
    assert [h.document.id for h in boosted.results] == [_id("kw_boost_advisory")]
    assert boosted.results[0].chunk_content is None and boosted.results[0].heading_path is None, (
        "precondition: the hit must be one the boost injected rather than one a "
        "retrieval surface matched"
    )
    assert boosted.hints is None or "warnings" not in boosted.hints, (
        "a response carrying results must not also assert that nothing matched"
    )

    # Control: the same service, the same corpus, a query neither the content
    # store nor the boost can answer. The advisory explains an empty result and
    # must still do so, with its wording and its term reporting unchanged.
    empty = await retrieval_service.discover(
        DiscoverRequest(mode=RetrievalMode.KEYWORD, query="deltaword epsilonword")
    )

    assert empty.results == []
    assert empty.hints is not None
    joined = " ".join(empty.hints.get("warnings") or [])
    assert "conjunctive" in joined, "an empty multi-term result must still be explained"
    assert "deltaword" in joined and "epsilonword" in joined, (
        "and must still name the terms the query required"
    )


async def test_keyword_advisory_fires_when_the_boosted_document_is_filtered_out(
    graph_store, stub_content_store, seeded_embedding_provider, retrieval_service
):
    """The gate reads the response, not whether the boost's own search answered.

    Anti-coincidental trap for suppressing whenever ``search_metadata`` returned
    rows. Here it returns one -- the title matches exactly as it does above --
    and the boost's failed-pipeline gate then drops it, so nothing reaches the
    caller. An empty response is the case the advisory exists for, and a rival
    keyed on the boost having found something goes silent on it.
    """
    doc_failed = _make_doc(_id("kw_boost_failed"), pipeline_status=PipelineStatus.FAILED)
    doc_failed.title = _BOOST_ONLY_TITLE
    await graph_store.insert_document(doc_failed)
    await graph_store.insert_document(_make_doc(_id("kw_boost_healthy")))
    await _index_doc_chunks(
        stub_content_store,
        seeded_embedding_provider,
        _id("kw_boost_healthy"),
        [("Section 1", "alphaword content only.")],
    )

    # Positive control, as above: the silence being ruled out below is a
    # suppressed advisory rather than an unsearchable vault.
    control = await retrieval_service.discover(
        DiscoverRequest(mode=RetrievalMode.KEYWORD, query="alphaword")
    )
    assert control.results, "precondition: the indexed corpus must be searchable"

    # Precondition on the trap itself: the boost's search must answer, or the
    # test would discriminate nothing. Asserted through the surface that reveals
    # it -- with the caller asking for failed documents the boost admits the
    # document, so its absence from the unfiltered response below is the
    # pipeline gate's doing and not an unmatched title.
    admitted = await retrieval_service.discover(
        DiscoverRequest(
            mode=RetrievalMode.KEYWORD,
            query=_BOOST_ONLY_QUERY,
            filters=RetrievalFilters(pipeline_status="failed"),
        )
    )
    assert [h.document.id for h in admitted.results] == [_id("kw_boost_failed")]

    response = await retrieval_service.discover(
        DiscoverRequest(mode=RetrievalMode.KEYWORD, query=_BOOST_ONLY_QUERY)
    )

    assert response.results == []
    assert response.hints is not None
    joined = " ".join(response.hints.get("warnings") or [])
    assert "conjunctive" in joined, (
        "the boost answering is not the caller receiving; an empty response is "
        "still owed its explanation"
    )


async def test_semantic_multi_term_empty_result_stays_silent(
    graph_store, stub_content_store, seeded_embedding_provider, retrieval_service
):
    """The advisory is keyword-only: semantic mode has no conjunction to explain.

    Deliberately indexes nothing. Semantic search returns nearest neighbours,
    so any indexed chunk would answer any query and the empty result this
    asserts on could not arise. Nothing here stands in for a positive control:
    what discriminates is the mode, and a rival missing the mode check warns on
    this same empty response.
    """
    request = DiscoverRequest(mode=RetrievalMode.SEMANTIC, query="deltaword epsilonword")
    response = await retrieval_service.discover(request)

    assert response.results == []
    assert response.hints is None or "warnings" not in response.hints


async def test_keyword_warning_reports_the_backend_parse_not_a_word_split(
    graph_store, stub_content_store, seeded_embedding_provider, retrieval_service, monkeypatch
):
    """The advisory names the terms the backend parsed, not a split of the query text.

    Anti-coincidental trap for computing the terms in the service as
    ``query.split()``. The double's own parse is a word split, so that rival
    is indistinguishable from the real one in every other test here; only a
    parse whose output cannot be confused with a split separates them. What is
    at stake is the advisory's truthfulness -- a word split would name
    stopwords as required terms, which is the misdirection the advisory exists
    to remove.
    """
    await graph_store.insert_document(_make_doc(_id("kw_warn_f")))
    await _index_doc_chunks(
        stub_content_store,
        seeded_embedding_provider,
        _id("kw_warn_f"),
        [("Section 1", "alphaword content only.")],
    )

    control = await retrieval_service.discover(
        DiscoverRequest(mode=RetrievalMode.KEYWORD, query="alphaword")
    )
    assert control.results, "precondition: the indexed corpus must be searchable"

    async def _parse(query: str) -> KeywordQueryParse:
        return KeywordQueryParse(
            terms=("stemmedlexeme", "otherlexeme"),
            excluded=(),
            all_required=True,
            adjacent=False,
        )

    monkeypatch.setattr(stub_content_store, "parse_keyword_query", _parse)

    response = await retrieval_service.discover(
        DiscoverRequest(mode=RetrievalMode.KEYWORD, query="deltaword epsilonword")
    )

    assert response.results == []
    joined = " ".join(response.hints.get("warnings") or [])
    assert "stemmedlexeme" in joined and "otherlexeme" in joined, (
        "the advisory must report the backend's parse"
    )
    assert "deltaword" not in joined, (
        "reporting the raw query words means the service split the text itself"
    )


async def test_keyword_no_advisory_when_post_filtering_emptied_the_result(
    graph_store, stub_content_store, seeded_embedding_provider, retrieval_service
):
    """A result emptied by min_relevance must not be told no chunk carries the terms.

    The search ran and matched; ``min_relevance`` then dropped what it found. A
    rival keyed on the response being empty asserts "No chunk does" when a chunk
    plainly does, and sends the caller to drop a term that was never the problem.
    """
    await graph_store.insert_document(_make_doc(_id("kw_warn_g")))
    await _index_doc_chunks(
        stub_content_store,
        seeded_embedding_provider,
        _id("kw_warn_g"),
        [("Section 1", "alphaword betaword together here.")],
    )

    matched = await retrieval_service.discover(
        DiscoverRequest(mode=RetrievalMode.KEYWORD, query="alphaword betaword")
    )
    assert matched.results, "precondition: the query must match before thresholding"

    response = await retrieval_service.discover(
        DiscoverRequest(mode=RetrievalMode.KEYWORD, query="alphaword betaword", min_relevance=99.0)
    )

    assert response.results == [], "precondition: the threshold must empty the result"
    joined = " ".join((response.hints or {}).get("warnings") or [])
    assert "conjunctive" not in joined, (
        "the search matched; the threshold emptied it, so the conjunction is not the story"
    )


async def test_keyword_no_advisory_when_filters_short_circuited_the_search(
    graph_store, stub_content_store, seeded_embedding_provider, retrieval_service
):
    """No term search ran, so nothing is known about chunk contents.

    A document-level filter resolving to zero documents returns before the
    content store is touched. A rival keyed on response emptiness asserts a fact
    about chunks that was never checked.
    """
    await graph_store.insert_document(_make_doc(_id("kw_warn_h")))
    await _index_doc_chunks(
        stub_content_store,
        seeded_embedding_provider,
        _id("kw_warn_h"),
        [("Section 1", "alphaword betaword together here.")],
    )

    # Positive control: unfiltered, this very query matches. The empty result
    # below is therefore the filter's doing and not the terms'.
    control = await retrieval_service.discover(
        DiscoverRequest(mode=RetrievalMode.KEYWORD, query="alphaword betaword")
    )
    assert control.results, "precondition: the query matches when the filter is absent"

    response = await retrieval_service.discover(
        DiscoverRequest(
            mode=RetrievalMode.KEYWORD,
            query="alphaword betaword",
            filters=RetrievalFilters(document_ids=[_id("absent_doc")]),
        )
    )

    assert response.results == []
    joined = " ".join((response.hints or {}).get("warnings") or [])
    assert "conjunctive" not in joined, "the filter culled every candidate before any search"


async def test_keyword_all_stopword_query_gets_its_own_advisory(
    graph_store, stub_content_store, seeded_embedding_provider, retrieval_service, monkeypatch
):
    """A query whose every word was discarded is the emptiest zero, and must speak.

    The conjunction advisory is gated on two or more terms, so a query parsing to
    none would fall through it and return the bare ``hints: null`` this work
    exists to remove -- for the case where the caller's words were never searched
    for at all.
    """
    await graph_store.insert_document(_make_doc(_id("kw_warn_i")))
    await _index_doc_chunks(
        stub_content_store,
        seeded_embedding_provider,
        _id("kw_warn_i"),
        # The double scores on substring containment, so the indexed text must
        # share no fragment with the query words -- "or" sits inside
        # "alphaword", and "a" inside almost anything.
        [("Section 1", "zzz qqq vvv.")],
    )

    async def _parse_to_nothing(query: str) -> KeywordQueryParse:
        return KeywordQueryParse(terms=(), excluded=(), all_required=True, adjacent=False)

    # Positive control, taken before the parse is replaced: the corpus is
    # searchable, so the empty result below is about the query, not the vault.
    control = await retrieval_service.discover(
        DiscoverRequest(mode=RetrievalMode.KEYWORD, query="zzz")
    )
    assert control.results, "precondition: the indexed corpus must be searchable"

    monkeypatch.setattr(stub_content_store, "parse_keyword_query", _parse_to_nothing)

    response = await retrieval_service.discover(
        DiscoverRequest(mode=RetrievalMode.KEYWORD, query="the a of")
    )

    assert response.results == []
    joined = " ".join((response.hints or {}).get("warnings") or [])
    assert "stopword" in joined, "an all-stopword query must not return a silent zero"
    assert "conjunctive" not in joined, "there is no conjunction to explain"


async def test_keyword_or_query_gets_no_conjunction_advisory(
    graph_store, stub_content_store, seeded_embedding_provider, retrieval_service, monkeypatch
):
    """An alternation is not conjunctive, and must not be described as one.

    Anti-coincidental trap for reading the parsed terms while ignoring how they
    are joined: the terms are identical either way, so only the flag separates a
    conjunction from an alternation.
    """
    await graph_store.insert_document(_make_doc(_id("kw_warn_j")))
    await _index_doc_chunks(
        stub_content_store,
        seeded_embedding_provider,
        _id("kw_warn_j"),
        # The double scores on substring containment, so the indexed text must
        # share no fragment with the query words -- "or" sits inside
        # "alphaword", and "a" inside almost anything.
        [("Section 1", "zzz qqq vvv.")],
    )

    async def _parse_alternation(query: str) -> KeywordQueryParse:
        return KeywordQueryParse(
            terms=("deltaword", "epsilonword"),
            excluded=(),
            all_required=False,
            adjacent=False,
        )

    # Positive control, taken before the parse is replaced: the corpus is
    # searchable, so the empty result below is about the query, not the vault.
    control = await retrieval_service.discover(
        DiscoverRequest(mode=RetrievalMode.KEYWORD, query="zzz")
    )
    assert control.results, "precondition: the indexed corpus must be searchable"

    monkeypatch.setattr(stub_content_store, "parse_keyword_query", _parse_alternation)

    response = await retrieval_service.discover(
        DiscoverRequest(mode=RetrievalMode.KEYWORD, query="deltaword or epsilonword")
    )

    assert response.results == []
    joined = " ".join((response.hints or {}).get("warnings") or [])
    assert "conjunctive" not in joined, "a document can satisfy an alternation with one term"


async def test_keyword_exclusion_only_query_is_not_called_all_stopwords(
    graph_store, stub_content_store, seeded_embedding_provider, retrieval_service, monkeypatch
):
    """A query asking only for absences searched; it must not be told it did not.

    ``-alphaword`` renders a negation, so the required-term set is empty for a
    reason entirely unlike an all-stopword query: the backend searched, for
    chunks *lacking* that term. A rival reading only the emptiness of ``terms``
    reports "every word was discarded", which is false.
    """
    await graph_store.insert_document(_make_doc(_id("kw_warn_l")))
    await _index_doc_chunks(
        stub_content_store,
        seeded_embedding_provider,
        _id("kw_warn_l"),
        [("Section 1", "zzz qqq vvv.")],
    )

    control = await retrieval_service.discover(
        DiscoverRequest(mode=RetrievalMode.KEYWORD, query="zzz")
    )
    assert control.results, "precondition: the indexed corpus must be searchable"

    async def _parse_exclusion_only(query: str) -> KeywordQueryParse:
        return KeywordQueryParse(
            terms=(), excluded=("alphaword",), all_required=True, adjacent=False
        )

    monkeypatch.setattr(stub_content_store, "parse_keyword_query", _parse_exclusion_only)

    response = await retrieval_service.discover(
        DiscoverRequest(mode=RetrievalMode.KEYWORD, query="-alphaword")
    )

    assert response.results == []
    joined = " ".join((response.hints or {}).get("warnings") or [])
    assert "stopword" not in joined, "a search ran; nothing was discarded"
    assert "absences" in joined and "alphaword" in joined, (
        "the advisory must name what the query excluded"
    )


async def test_keyword_phrase_query_reports_adjacency_not_bare_conjunction(
    graph_store, stub_content_store, seeded_embedding_provider, retrieval_service, monkeypatch
):
    """A phrase demands more than carrying every term, and the advice must not invert.

    ``"a b"`` renders adjacency. A chunk can carry both terms apart and still
    miss, so "matches only a chunk carrying all of them" understates the
    condition -- and telling the caller to quote a phrase names the very thing
    they already did.
    """
    await graph_store.insert_document(_make_doc(_id("kw_warn_m")))
    await _index_doc_chunks(
        stub_content_store,
        seeded_embedding_provider,
        _id("kw_warn_m"),
        [("Section 1", "zzz qqq vvv.")],
    )

    control = await retrieval_service.discover(
        DiscoverRequest(mode=RetrievalMode.KEYWORD, query="zzz")
    )
    assert control.results, "precondition: the indexed corpus must be searchable"

    async def _parse_phrase(query: str) -> KeywordQueryParse:
        return KeywordQueryParse(
            terms=("alphaword", "betaword"), excluded=(), all_required=True, adjacent=True
        )

    monkeypatch.setattr(stub_content_store, "parse_keyword_query", _parse_phrase)

    response = await retrieval_service.discover(
        DiscoverRequest(mode=RetrievalMode.KEYWORD, query='"alphaword betaword"')
    )

    assert response.results == []
    joined = " ".join((response.hints or {}).get("warnings") or [])
    assert "adjacent" in joined, "the advisory must name the condition that actually failed"
    assert "quote a phrase" not in joined, (
        "advising a caller to quote what they already quoted inverts the fix"
    )


async def test_keyword_empty_filters_object_does_not_claim_a_filter_scope(
    graph_store, stub_content_store, seeded_embedding_provider, retrieval_service
):
    """An empty filters object constrains nothing, so the claim must stay unscoped.

    A Pydantic model instance is truthy whatever its fields hold. A rival
    testing the object rather than its contents scopes the sentence to filters
    the same response does not list -- the two halves of one response
    contradicting each other.
    """
    await graph_store.insert_document(_make_doc(_id("kw_warn_n")))
    await _index_doc_chunks(
        stub_content_store,
        seeded_embedding_provider,
        _id("kw_warn_n"),
        [("Section 1", "alphaword content only.")],
    )

    # Positive control: the corpus is reachable through the same empty filters
    # object, so the empty result below is the query's doing and the sentence
    # under test describes a real slice.
    control = await retrieval_service.discover(
        DiscoverRequest(mode=RetrievalMode.KEYWORD, query="alphaword", filters=RetrievalFilters())
    )
    assert control.results, "precondition: an empty filters object constrains nothing"

    response = await retrieval_service.discover(
        DiscoverRequest(
            mode=RetrievalMode.KEYWORD,
            query="deltaword epsilonword",
            filters=RetrievalFilters(),
        )
    )

    assert response.results == []
    assert "active_filters" not in (response.hints or {}), (
        "precondition: an empty filters object lists no active filters"
    )
    joined = " ".join((response.hints or {}).get("warnings") or [])
    assert "within the active filters" not in joined, (
        "the response must not name a filter scope it does not report"
    )


async def test_keyword_advisory_scopes_its_claim_to_the_active_filters(
    graph_store, stub_content_store, seeded_embedding_provider, retrieval_service
):
    """Under a filter the miss holds only within the filtered slice, and says so."""
    await graph_store.insert_document(_make_doc(_id("kw_warn_k"), doc_type="note"))
    await _index_doc_chunks(
        stub_content_store,
        seeded_embedding_provider,
        _id("kw_warn_k"),
        [("Section 1", "alphaword content only.")],
        doc_type="note",
    )

    # Positive control: the filtered slice is non-empty, so "within the active
    # filters" narrows a real slice rather than naming an empty one.
    control = await retrieval_service.discover(
        DiscoverRequest(
            mode=RetrievalMode.KEYWORD,
            query="alphaword",
            filters=RetrievalFilters(doc_type="note"),
        )
    )
    assert control.results, "precondition: the filtered slice must contain the corpus"

    unfiltered = await retrieval_service.discover(
        DiscoverRequest(mode=RetrievalMode.KEYWORD, query="deltaword epsilonword")
    )
    filtered = await retrieval_service.discover(
        DiscoverRequest(
            mode=RetrievalMode.KEYWORD,
            query="deltaword epsilonword",
            filters=RetrievalFilters(doc_type="note"),
        )
    )

    assert "within the active filters" not in " ".join(unfiltered.hints["warnings"])
    assert "within the active filters" in " ".join(filtered.hints["warnings"]), (
        "an absolute claim overreaches when a filter selected the slice searched"
    )


async def test_keyword_term_warning_composes_with_vocabulary_warning(
    graph_store, stub_content_store, seeded_embedding_provider, retrieval_service
):
    """Both advisories survive together.

    Anti-coincidental trap for merging ``{"warnings": [...]}`` into hints a
    second time: a dict merge replaces the key, so whichever advisory is
    attached last would silently discard the other.
    """
    await graph_store.insert_document(_make_doc(_id("kw_warn_e"), doc_type="note"))
    await _index_doc_chunks(
        stub_content_store,
        seeded_embedding_provider,
        _id("kw_warn_e"),
        [("Section 1", "alphaword content only.")],
        doc_type="note",
    )

    # Positive control: the corpus is reachable under its real doc_type, so
    # both advisories below describe the request rather than an empty vault.
    control = await retrieval_service.discover(
        DiscoverRequest(
            mode=RetrievalMode.KEYWORD,
            query="alphaword",
            filters=RetrievalFilters(doc_type="note"),
        )
    )
    assert control.results, "precondition: the indexed corpus must be searchable"

    request = DiscoverRequest(
        mode=RetrievalMode.KEYWORD,
        query="deltaword epsilonword",
        filters=RetrievalFilters(doc_type="template"),
    )
    response = await retrieval_service.discover(request)

    assert response.results == []
    assert response.hints is not None
    joined = " ".join(response.hints.get("warnings") or [])
    assert "template" in joined, "the out-of-vocabulary doc_type advisory must survive"
    assert "deltaword" in joined, "the keyword-conjunction advisory must survive"


@pytest.mark.parametrize(
    "mode",
    [RetrievalMode.CATALOG, RetrievalMode.SEMANTIC, RetrievalMode.KEYWORD],
)
async def test_warning_emitted_in_every_mode(
    graph_store, stub_content_store, seeded_embedding_provider, retrieval_service, mode
):
    """The vocabulary warning is mode-independent.

    Anti-coincidental: hooking ``_build_hints`` satisfies semantic and
    keyword but not catalog, which never calls it -- and catalog is the
    mode a bare existence question uses.
    """
    await graph_store.insert_document(_make_doc(_id("oov_mode_a"), doc_type="note"))
    await _index_doc_chunks(
        stub_content_store,
        seeded_embedding_provider,
        _id("oov_mode_a"),
        [("Section 1", "Interesting report content about claims.")],
    )

    request = DiscoverRequest(
        mode=mode,
        query=None if mode == RetrievalMode.CATALOG else "report claims",
        filters=RetrievalFilters(doc_type="template"),
    )
    response = await retrieval_service.discover(request)

    assert response.hints is not None, f"{mode.value} produced no hints"
    assert response.hints.get("warnings"), f"{mode.value} produced no vocabulary warning"


async def test_warning_preserves_response_shape(
    graph_store, stub_content_store, seeded_embedding_provider, retrieval_service
):
    """The warning merges into hints; it does not replace them.

    Anti-coincidental: an implementation that assigns rather than merges
    drops total_before_filtering and still passes every "warning
    present" assertion.
    """
    await graph_store.insert_document(_make_doc(_id("oov_shape_a"), doc_type="note"))
    await _index_doc_chunks(
        stub_content_store,
        seeded_embedding_provider,
        _id("oov_shape_a"),
        [("Section 1", "Interesting report content about claims.")],
    )

    request = DiscoverRequest(
        mode=RetrievalMode.SEMANTIC,
        query="report claims",
        filters=RetrievalFilters(doc_type="template"),
    )
    response = await retrieval_service.discover(request)

    assert response.mode == RetrievalMode.SEMANTIC
    assert response.results == []
    assert response.total_available == 0
    assert response.hints is not None
    assert "total_before_filtering" in response.hints
    assert response.hints["active_filters"].get("doc_type") == "template"
    assert response.hints.get("warnings")


async def test_no_warnings_key_when_all_values_valid(graph_store, retrieval_service):
    """A fully in-vocabulary filter set carries no ``warnings`` key at all.

    Absent, not an empty list -- so a caller can test membership.
    """
    await graph_store.insert_document(_make_doc(_id("oov_ok_a"), doc_type="note"))

    request = DiscoverRequest(
        mode=RetrievalMode.CATALOG,
        filters=RetrievalFilters(doc_type="note", lifecycle_status="active"),
    )
    response = await retrieval_service.discover(request)

    assert len(response.results) == 1
    assert response.hints is None or "warnings" not in response.hints


# ---------------------------------------------------------------------------
# Every target-axis forbidden-parameter row can actually fire
# ---------------------------------------------------------------------------


def _reachability_value(param: str, default: object) -> object:
    """A non-default value for one forbidden parameter."""
    overrides: dict[str, object] = {
        "document_id": _id("d1"),
        "min_relevance": 0.5,
        "sort_by": "title",
        "sort_order": "asc",
        "include_abstracts": True,
        "response_mode": ResponseMode.LIGHT,
        "limit": 5,
        "offset": 3,
    }
    value = overrides[param]
    assert value != default, f"{param}: probe value equals the default, so nothing is set"
    return value


_REACHABILITY_CASES = [
    (target, name, default, allowed)
    for target, table in (
        (RetrievalTarget.EDGES, _EDGE_FORBIDDEN_PARAMS),
        (RetrievalTarget.FACETS, _FACET_FORBIDDEN_PARAMS),
    )
    for name, default, allowed in table
]


@pytest.mark.parametrize(
    ("target", "param", "default", "allowed"),
    _REACHABILITY_CASES,
    ids=[f"{t.value}-{n}" for t, n, _, _ in _REACHABILITY_CASES],
)
def test_every_target_forbidden_parameter_is_reachable(target, param, default, allowed):
    """Each row of the two target-axis tables can actually fire.

    A row goes unreachable when a mode-axis check above the loop answers
    every combination that would otherwise reach it. That is what befell
    ``query`` and ``heading_path``: both are refused on the mode axis,
    and both targets require catalog mode, which neither parameter
    permits -- so no request could ever be rejected against a target for
    them. An unreachable row is not inert; it states a second, invisible
    copy of a rule, free to drift from the one that fires.

    Asserting the axis is what makes this discriminating. Every request
    below is rejected either way, so a check on the code or on
    ``forbidden_param`` would pass just as well for a row answered by a
    mode-axis branch -- which is how the unreachable rows survived the
    existing per-parameter tests. Only ``allowed_targets`` separates the
    row under test having fired from something above it firing first.
    """
    # The table restates each field's default. Pin that copy to the model,
    # or a later change to a default leaves the table declaring a value the
    # field no longer has -- the row would then fire for every caller who
    # left the knob alone, and the reachability assertion below would stay
    # green throughout, since it only ever probes the non-default direction.
    assert DiscoverRequest.model_fields[param].default == default

    value = _reachability_value(param, default)
    with pytest.raises(ValidationError) as info:
        DiscoverRequest(mode=RetrievalMode.CATALOG, target=target, **{param: value})
    raw = info.value.errors()[0]
    assert raw["type"] == "mode_parameter_mismatch"
    ctx = raw["ctx"]
    assert ctx["forbidden_param"] == param
    assert ctx.get("allowed_targets") == allowed, (
        f"{target.value}/{param} was answered on the mode axis "
        f"({ctx.get('allowed_modes')!r}), so its row in the target table "
        "cannot fire and states a rule nothing enforces."
    )
    assert ctx["target"] == target.value
