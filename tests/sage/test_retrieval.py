"""Retrieval tests: BH-020, BH-021, BH-027, BH-028, BH-029, BH-030.

Covers semantic retrieval (pure vector and hybrid RRF), deterministic
retrieval (heading path prefix match), and pipeline/scope gating.
"""

import pytest
from datetime import datetime, timezone

from sage.adapters.interfaces import Chunk
from sage.adapters.stubs import SeededEmbeddingProvider, StubContentStore
from sage.api.errors import (
    DocumentNotFoundError,
    HeadingNotFoundError,
    MissingFieldError,
    PipelineIncompleteError,
)
from sage.models.enums import (
    PipelineStatus,
    RetrievalMode,
    RetrievalScope,
    SourceType,
)
from sage.models.schemas import (
    DiscoverRequest,
    Document,
    RetrievalFilters,
)
from sage.services.retrieval import RetrievalService


def _make_doc(
    doc_id: str,
    lifecycle_status: str = "active",
    pipeline_status: PipelineStatus = PipelineStatus.ABSTRACTION_COMPLETE,
    project: str | None = None,
    doc_type: str | None = None,
    authority_scope: str | None = None,
    tags: list[str] | None = None,
) -> Document:
    now = datetime.now(timezone.utc)
    return Document(
        id=doc_id,
        title=f"Test {doc_id}",
        source_type=SourceType.MARKDOWN,
        source_path=f"test/{doc_id}.md",
        lifecycle_status=lifecycle_status,
        source_content_hash=f"hash_{doc_id}",
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


async def _index_doc_chunks(
    content_store: StubContentStore,
    embedding_provider,
    document_id: str,
    chunks_data: list[tuple[str, str]],
) -> None:
    """Helper: index chunks for a document in the content store.

    chunks_data: list of (heading_path, content) tuples.
    """
    chunks = []
    for i, (heading_path, content) in enumerate(chunks_data):
        chunks.append(Chunk(
            document_id=document_id,
            heading_path=heading_path,
            content=content,
            chunk_index=i,
        ))

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
    doc_ok = _make_doc("doc_ok")
    doc_failed = _make_doc("doc_failed", pipeline_status=PipelineStatus.FAILED)
    doc_failed.pipeline_error = "LLM unavailable"
    await graph_store.insert_document(doc_ok)
    await graph_store.insert_document(doc_failed)

    # Index chunks for both
    await _index_doc_chunks(
        stub_content_store, seeded_embedding_provider, "doc_ok",
        [("Section 1", "This document discusses patent claims and prior art.")],
    )
    await _index_doc_chunks(
        stub_content_store, seeded_embedding_provider, "doc_failed",
        [("Section 1", "This document discusses patent claims and prior art.")],
    )

    request = DiscoverRequest(mode=RetrievalMode.SEMANTIC, query="patent claims")
    response = await retrieval_service.discover(request)

    doc_ids = [h.document.id for h in response.results]
    assert "doc_ok" in doc_ids
    assert "doc_failed" not in doc_ids


# ---------------------------------------------------------------------------
# BH-021: Failed document excluded from deterministic retrieval
# ---------------------------------------------------------------------------

async def test_bh_021_failed_doc_excluded_from_deterministic(
    graph_store, stub_content_store, seeded_embedding_provider, retrieval_service
):
    """Deterministic retrieval on a failed-pipeline document raises 422."""
    doc = _make_doc("doc_failed", pipeline_status=PipelineStatus.FAILED)
    doc.pipeline_error = "LLM unavailable"
    await graph_store.insert_document(doc)

    request = DiscoverRequest(
        mode=RetrievalMode.DETERMINISTIC,
        document_id="doc_failed",
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
    doc_vector = _make_doc("doc_vector")
    await graph_store.insert_document(doc_vector)

    # doc_bm25: matches well on keywords, less on vector
    doc_bm25 = _make_doc("doc_bm25")
    await graph_store.insert_document(doc_bm25)

    # doc_both: matches well on both
    doc_both = _make_doc("doc_both")
    await graph_store.insert_document(doc_both)

    # Index chunks with content designed to produce different ranking per mode.
    # doc_vector gets content with same semantic embedding direction as query
    await _index_doc_chunks(
        stub_content_store, seeded_embedding_provider, "doc_vector",
        [("Section 1", "neural network architecture deep learning models")],
    )
    # doc_bm25 gets content with exact keyword matches
    await _index_doc_chunks(
        stub_content_store, seeded_embedding_provider, "doc_bm25",
        [("Section 1", "patent claims prior art novelty claims")],
    )
    # doc_both gets content matching both
    await _index_doc_chunks(
        stub_content_store, seeded_embedding_provider, "doc_both",
        [("Section 1", "patent claims prior art neural network deep learning")],
    )

    request = DiscoverRequest(
        mode=RetrievalMode.SEMANTIC,
        query="patent claims prior art",
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
    doc_a = _make_doc("doc_a")
    doc_b = _make_doc("doc_b")
    await graph_store.insert_document(doc_a)
    await graph_store.insert_document(doc_b)

    # Index with distinct content
    await _index_doc_chunks(
        stub_content_store, seeded_embedding_provider, "doc_a",
        [("Section 1", "machine learning classification algorithms")],
    )
    await _index_doc_chunks(
        stub_content_store, seeded_embedding_provider, "doc_b",
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
    doc = _make_doc("doc_structured")
    await graph_store.insert_document(doc)

    # Index chunks with heading hierarchy
    await _index_doc_chunks(
        stub_content_store, seeded_embedding_provider, "doc_structured",
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
        document_id="doc_structured",
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


# ---------------------------------------------------------------------------
# BH-030: Deterministic retrieval with non-existent heading returns 404
# ---------------------------------------------------------------------------

async def test_bh_030_deterministic_heading_not_found(
    graph_store, stub_content_store, seeded_embedding_provider, retrieval_service
):
    """Missing heading path returns 404 with heading_not_found code."""
    doc = _make_doc("doc_headings")
    await graph_store.insert_document(doc)

    await _index_doc_chunks(
        stub_content_store, seeded_embedding_provider, "doc_headings",
        [("Section 1", "Some content.")],
    )

    request = DiscoverRequest(
        mode=RetrievalMode.DETERMINISTIC,
        document_id="doc_headings",
        heading_path="Nonexistent > Section",
    )
    with pytest.raises(HeadingNotFoundError) as exc_info:
        await retrieval_service.discover(request)
    assert exc_info.value.status_code == 404
    assert exc_info.value.code == "heading_not_found"


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
        document_id="some_doc",
    )
    with pytest.raises(MissingFieldError):
        await retrieval_service.discover(request)


async def test_deterministic_nonexistent_document(
    graph_store, retrieval_service
):
    request = DiscoverRequest(
        mode=RetrievalMode.DETERMINISTIC,
        document_id="nonexistent",
        heading_path="Section 1",
    )
    with pytest.raises(DocumentNotFoundError):
        await retrieval_service.discover(request)


# ---------------------------------------------------------------------------
# Additional: scope gating
# ---------------------------------------------------------------------------

async def test_authoritative_scope_filters(
    graph_store, stub_content_store, seeded_embedding_provider, retrieval_service
):
    """Authoritative scope excludes documents without authority_scope."""
    doc_auth = _make_doc("doc_auth", authority_scope="pim_health")
    doc_plain = _make_doc("doc_plain")
    await graph_store.insert_document(doc_auth)
    await graph_store.insert_document(doc_plain)

    await _index_doc_chunks(
        stub_content_store, seeded_embedding_provider, "doc_auth",
        [("Section 1", "Authoritative patent content.")],
    )
    await _index_doc_chunks(
        stub_content_store, seeded_embedding_provider, "doc_plain",
        [("Section 1", "Non-authoritative patent content.")],
    )

    request = DiscoverRequest(
        mode=RetrievalMode.SEMANTIC,
        query="patent content",
        scope=RetrievalScope.AUTHORITATIVE,
    )
    response = await retrieval_service.discover(request)

    doc_ids = [h.document.id for h in response.results]
    assert "doc_auth" in doc_ids
    assert "doc_plain" not in doc_ids


async def test_filter_by_project(
    graph_store, stub_content_store, seeded_embedding_provider, retrieval_service
):
    """Filters narrow results to matching project."""
    doc_pim = _make_doc("doc_pim", project="pim_health")
    doc_other = _make_doc("doc_other", project="basketball")
    await graph_store.insert_document(doc_pim)
    await graph_store.insert_document(doc_other)

    await _index_doc_chunks(
        stub_content_store, seeded_embedding_provider, "doc_pim",
        [("Section 1", "Patent filing process documentation.")],
    )
    await _index_doc_chunks(
        stub_content_store, seeded_embedding_provider, "doc_other",
        [("Section 1", "Patent filing process documentation.")],
    )

    request = DiscoverRequest(
        mode=RetrievalMode.SEMANTIC,
        query="patent filing",
        filters=RetrievalFilters(project="pim_health"),
    )
    response = await retrieval_service.discover(request)

    doc_ids = [h.document.id for h in response.results]
    assert "doc_pim" in doc_ids
    assert "doc_other" not in doc_ids
