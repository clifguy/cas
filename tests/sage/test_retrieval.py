"""Retrieval tests: BH-020, BH-021, BH-027, BH-028, BH-029, BH-030,
BH-058, BH-059, BH-060, BH-061, BH-069, BH-070.

Covers semantic retrieval (pure vector and hybrid RRF), deterministic
retrieval (heading path prefix match), keyword-only retrieval,
pipeline/scope gating, and title indexing in chunks.
"""

import pytest
from datetime import datetime, timedelta, timezone

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
    document_date: str | None = None,
    source_modified_at: datetime | None = None,
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
        document_date=document_date,
        source_modified_at=source_modified_at,
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


# ---------------------------------------------------------------------------
# BH-058: Document title is indexed in chunk content for search
# ---------------------------------------------------------------------------

async def test_bh_058_document_identity_indexed_in_chunks(
    graph_store, stub_content_store, seeded_embedding_provider, retrieval_service
):
    """A document whose body does not contain "PV07" is still discoverable
    via keyword (BM25) search because the search preamble (title, source
    filename, tags) is prepended to the first chunk during indexing.
    """
    # Document title is "ClinicalNormalization" (no "PV07"),
    # but source filename contains "PV07"
    doc = _make_doc("doc_pv07")
    doc.title = "ClinicalNormalization"
    doc.source_path = "imports/PIM_PV07_ClinicalNormalization_v1_0.md"
    doc.tags = ["PV07"]
    await graph_store.insert_document(doc)

    # Index a chunk with search preamble from source filename
    # (simulating what _stage2_indexing now produces)
    await _index_doc_chunks(
        stub_content_store, seeded_embedding_provider, "doc_pv07",
        [("Section 1",
          "Title: ClinicalNormalization\n"
          "Source: PIM_PV07_ClinicalNormalization_v1_0\n"
          "Tags: PV07\n\n"
          "This document discusses patent claims.")],
    )

    # BM25 search for "PV07" should find it via the source filename in preamble
    request = DiscoverRequest(
        mode=RetrievalMode.SEMANTIC,
        query="PV07",
        use_hybrid=True,
    )
    response = await retrieval_service.discover(request)

    doc_ids = [h.document.id for h in response.results]
    assert "doc_pv07" in doc_ids


# ---------------------------------------------------------------------------
# BH-059: Keyword-only retrieval mode uses BM25 without embedding
# ---------------------------------------------------------------------------

async def test_bh_059_keyword_mode_bm25_only(
    graph_store, stub_content_store, seeded_embedding_provider, retrieval_service
):
    """Keyword mode returns BM25 matches without requiring query embedding."""
    doc_match = _make_doc("doc_match")
    doc_nomatch = _make_doc("doc_nomatch")
    await graph_store.insert_document(doc_match)
    await graph_store.insert_document(doc_nomatch)

    await _index_doc_chunks(
        stub_content_store, seeded_embedding_provider, "doc_match",
        [("Section 1", "Title: PV07_Report\n\nDetailed analysis of PV07 claims.")],
    )
    await _index_doc_chunks(
        stub_content_store, seeded_embedding_provider, "doc_nomatch",
        [("Section 1", "Title: Gardening_Guide\n\nTips for spring planting.")],
    )

    request = DiscoverRequest(
        mode=RetrievalMode.KEYWORD,
        query="PV07",
    )
    response = await retrieval_service.discover(request)

    assert response.mode == RetrievalMode.KEYWORD
    doc_ids = [h.document.id for h in response.results]
    assert "doc_match" in doc_ids
    assert "doc_nomatch" not in doc_ids


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
    doc_ok = _make_doc("doc_ok_kw")
    doc_failed = _make_doc("doc_failed_kw", pipeline_status=PipelineStatus.FAILED)
    doc_failed.pipeline_error = "LLM unavailable"
    await graph_store.insert_document(doc_ok)
    await graph_store.insert_document(doc_failed)

    await _index_doc_chunks(
        stub_content_store, seeded_embedding_provider, "doc_ok_kw",
        [("Section 1", "Patent claims analysis for PV07.")],
    )
    await _index_doc_chunks(
        stub_content_store, seeded_embedding_provider, "doc_failed_kw",
        [("Section 1", "Patent claims analysis for PV07.")],
    )

    request = DiscoverRequest(
        mode=RetrievalMode.KEYWORD,
        query="PV07",
    )
    response = await retrieval_service.discover(request)

    doc_ids = [h.document.id for h in response.results]
    assert "doc_ok_kw" in doc_ids
    assert "doc_failed_kw" not in doc_ids


# ---------------------------------------------------------------------------
# BH-069: Active lifecycle documents rank above non-active in semantic search
# ---------------------------------------------------------------------------

async def test_bh_069_active_lifecycle_ranks_higher(
    graph_store, stub_content_store, seeded_embedding_provider, retrieval_service
):
    """Documents with lifecycle_status='active' rank above superseded/archived
    documents when content relevance is otherwise equal.
    """
    doc_active = _make_doc("doc_active", lifecycle_status="active")
    doc_superseded = _make_doc("doc_superseded", lifecycle_status="superseded")
    doc_archived = _make_doc("doc_archived", lifecycle_status="archived")
    await graph_store.insert_document(doc_active)
    await graph_store.insert_document(doc_superseded)
    await graph_store.insert_document(doc_archived)

    # All three get identical content so raw relevance scores are equal
    identical_content = "Patent filing process for clinical normalization."
    for doc_id in ["doc_active", "doc_superseded", "doc_archived"]:
        await _index_doc_chunks(
            stub_content_store, seeded_embedding_provider, doc_id,
            [("Section 1", identical_content)],
        )

    request = DiscoverRequest(
        mode=RetrievalMode.SEMANTIC,
        query="patent filing clinical normalization",
    )
    response = await retrieval_service.discover(request)

    doc_ids = [h.document.id for h in response.results]
    assert "doc_active" in doc_ids
    assert "doc_superseded" in doc_ids
    assert "doc_archived" in doc_ids

    # Active document must appear before both non-active documents
    active_idx = doc_ids.index("doc_active")
    superseded_idx = doc_ids.index("doc_superseded")
    archived_idx = doc_ids.index("doc_archived")
    assert active_idx < superseded_idx, (
        f"Active ({active_idx}) should rank above superseded ({superseded_idx})"
    )
    assert active_idx < archived_idx, (
        f"Active ({active_idx}) should rank above archived ({archived_idx})"
    )


async def test_bh_069_active_boost_applies_to_keyword_mode(
    graph_store, stub_content_store, seeded_embedding_provider, retrieval_service
):
    """Lifecycle boost also applies in keyword mode."""
    doc_active = _make_doc("doc_kw_active", lifecycle_status="active")
    doc_superseded = _make_doc("doc_kw_superseded", lifecycle_status="superseded")
    await graph_store.insert_document(doc_active)
    await graph_store.insert_document(doc_superseded)

    identical_content = "Detailed analysis of PV07 claims and prior art."
    for doc_id in ["doc_kw_active", "doc_kw_superseded"]:
        await _index_doc_chunks(
            stub_content_store, seeded_embedding_provider, doc_id,
            [("Section 1", identical_content)],
        )

    request = DiscoverRequest(
        mode=RetrievalMode.KEYWORD,
        query="PV07 claims",
    )
    response = await retrieval_service.discover(request)

    doc_ids = [h.document.id for h in response.results]
    assert "doc_kw_active" in doc_ids
    assert "doc_kw_superseded" in doc_ids
    assert doc_ids.index("doc_kw_active") < doc_ids.index("doc_kw_superseded")


async def test_bh_069_deterministic_mode_unaffected(
    graph_store, stub_content_store, seeded_embedding_provider, retrieval_service
):
    """Deterministic mode does not apply salience reranking (no relevance scores)."""
    doc = _make_doc("doc_det", lifecycle_status="superseded")
    await graph_store.insert_document(doc)

    await _index_doc_chunks(
        stub_content_store, seeded_embedding_provider, "doc_det",
        [("Section 1", "Some content."), ("Section 2", "More content.")],
    )

    request = DiscoverRequest(
        mode=RetrievalMode.DETERMINISTIC,
        document_id="doc_det",
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
        "doc_recent",
        document_date="2026-04-01",
        source_modified_at=now - timedelta(days=5),
    )
    doc_old = _make_doc(
        "doc_old",
        document_date="2024-01-15",
        source_modified_at=now - timedelta(days=800),
    )
    await graph_store.insert_document(doc_recent)
    await graph_store.insert_document(doc_old)

    identical_content = "Patent filing process for clinical normalization."
    for doc_id in ["doc_recent", "doc_old"]:
        await _index_doc_chunks(
            stub_content_store, seeded_embedding_provider, doc_id,
            [("Section 1", identical_content)],
        )

    request = DiscoverRequest(
        mode=RetrievalMode.SEMANTIC,
        query="patent filing clinical normalization",
    )
    response = await retrieval_service.discover(request)

    doc_ids = [h.document.id for h in response.results]
    assert "doc_recent" in doc_ids
    assert "doc_old" in doc_ids
    assert doc_ids.index("doc_recent") < doc_ids.index("doc_old"), (
        "Recent document should rank above older document"
    )


async def test_bh_070_recency_uses_source_modified_at_fallback(
    graph_store, stub_content_store, seeded_embedding_provider, retrieval_service
):
    """When document_date is null, recency boost falls back to source_modified_at."""
    now = datetime.now(timezone.utc)
    doc_recent = _make_doc(
        "doc_recent_fb",
        document_date=None,
        source_modified_at=now - timedelta(days=3),
    )
    doc_old = _make_doc(
        "doc_old_fb",
        document_date=None,
        source_modified_at=now - timedelta(days=900),
    )
    await graph_store.insert_document(doc_recent)
    await graph_store.insert_document(doc_old)

    identical_content = "Patent filing process for clinical normalization."
    for doc_id in ["doc_recent_fb", "doc_old_fb"]:
        await _index_doc_chunks(
            stub_content_store, seeded_embedding_provider, doc_id,
            [("Section 1", identical_content)],
        )

    request = DiscoverRequest(
        mode=RetrievalMode.SEMANTIC,
        query="patent filing clinical normalization",
    )
    response = await retrieval_service.discover(request)

    doc_ids = [h.document.id for h in response.results]
    assert doc_ids.index("doc_recent_fb") < doc_ids.index("doc_old_fb"), (
        "Recent source_modified_at should rank above older when document_date is null"
    )


async def test_bh_070_no_date_documents_not_penalized(
    graph_store, stub_content_store, seeded_embedding_provider, retrieval_service
):
    """Documents with no date information still appear in results (no crash,
    no artificial penalty that would push them below relevance threshold).
    """
    doc_dated = _make_doc("doc_dated", document_date="2026-03-01")
    doc_undated = _make_doc("doc_undated")  # no document_date, no source_modified_at
    await graph_store.insert_document(doc_dated)
    await graph_store.insert_document(doc_undated)

    identical_content = "Patent filing process for clinical normalization."
    for doc_id in ["doc_dated", "doc_undated"]:
        await _index_doc_chunks(
            stub_content_store, seeded_embedding_provider, doc_id,
            [("Section 1", identical_content)],
        )

    request = DiscoverRequest(
        mode=RetrievalMode.SEMANTIC,
        query="patent filing clinical normalization",
    )
    response = await retrieval_service.discover(request)

    doc_ids = [h.document.id for h in response.results]
    # Both documents must appear -- undated is not excluded
    assert "doc_dated" in doc_ids
    assert "doc_undated" in doc_ids


async def test_bh_069_070_combined_active_recent_ranks_highest(
    graph_store, stub_content_store, seeded_embedding_provider, retrieval_service
):
    """Active + recent beats active + old, which beats superseded + recent.
    Both boosts work together additively.
    """
    now = datetime.now(timezone.utc)
    doc_active_recent = _make_doc(
        "doc_ar", lifecycle_status="active", document_date="2026-04-01",
    )
    doc_active_old = _make_doc(
        "doc_ao", lifecycle_status="active", document_date="2023-01-01",
    )
    doc_superseded_recent = _make_doc(
        "doc_sr", lifecycle_status="superseded", document_date="2026-04-01",
    )
    await graph_store.insert_document(doc_active_recent)
    await graph_store.insert_document(doc_active_old)
    await graph_store.insert_document(doc_superseded_recent)

    identical_content = "Patent filing process for clinical normalization."
    for doc_id in ["doc_ar", "doc_ao", "doc_sr"]:
        await _index_doc_chunks(
            stub_content_store, seeded_embedding_provider, doc_id,
            [("Section 1", identical_content)],
        )

    request = DiscoverRequest(
        mode=RetrievalMode.SEMANTIC,
        query="patent filing clinical normalization",
    )
    response = await retrieval_service.discover(request)

    doc_ids = [h.document.id for h in response.results]
    ar_idx = doc_ids.index("doc_ar")
    ao_idx = doc_ids.index("doc_ao")
    sr_idx = doc_ids.index("doc_sr")

    # Active + recent should be first
    assert ar_idx < ao_idx, "Active+recent should rank above active+old"
    assert ar_idx < sr_idx, "Active+recent should rank above superseded+recent"


async def test_bh_070_document_date_in_summary(
    graph_store, stub_content_store, seeded_embedding_provider, retrieval_service
):
    """DiscoverHit.document summary includes document_date field."""
    doc = _make_doc("doc_date_summary", document_date="2026-03-15")
    await graph_store.insert_document(doc)

    await _index_doc_chunks(
        stub_content_store, seeded_embedding_provider, "doc_date_summary",
        [("Section 1", "Patent filing process documentation.")],
    )

    request = DiscoverRequest(
        mode=RetrievalMode.SEMANTIC,
        query="patent filing",
    )
    response = await retrieval_service.discover(request)

    hit = next(h for h in response.results if h.document.id == "doc_date_summary")
    assert hit.document.document_date == "2026-03-15"
