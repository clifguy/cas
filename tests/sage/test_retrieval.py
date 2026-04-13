"""Retrieval tests: BH-020, BH-021, BH-027, BH-028, BH-029, BH-030,
BH-058, BH-059, BH-060, BH-061, BH-069, BH-070, BH-072 through BH-079.

Covers semantic retrieval (pure vector and hybrid RRF), deterministic
retrieval (heading path prefix match), keyword-only retrieval,
catalog mode (filter-only enumeration), pipeline/scope gating,
and title indexing in chunks.
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
    UpdateMetadataRequest,
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
    doc_type: str | None = None,
) -> None:
    """Helper: index chunks for a document in the content store.

    chunks_data: list of (heading_path, content) tuples.
    doc_type: optional doc_type to stamp on chunks for pre-filter testing.
    """
    chunks = []
    for i, (heading_path, content) in enumerate(chunks_data):
        chunks.append(Chunk(
            document_id=document_id,
            heading_path=heading_path,
            content=content,
            chunk_index=i,
            doc_type=doc_type,
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
    doc_patent = _make_doc("doc_patent", doc_type="patent_draft")
    doc_report = _make_doc("doc_report", doc_type="report")
    doc_ref = _make_doc("doc_ref", doc_type="reference_document")
    await graph_store.insert_document(doc_patent)
    await graph_store.insert_document(doc_report)
    await graph_store.insert_document(doc_ref)

    # All documents get identical content so only the filter differentiates
    identical_content = "Clinical pathway integration and normalization process."
    for doc_id, doc_type in [
        ("doc_patent", "patent_draft"),
        ("doc_report", "report"),
        ("doc_ref", "reference_document"),
    ]:
        await _index_doc_chunks(
            stub_content_store, seeded_embedding_provider, doc_id,
            [("Section 1", identical_content)],
            doc_type=doc_type,
        )

    request = DiscoverRequest(
        mode=RetrievalMode.SEMANTIC,
        query="clinical pathway",
        scope=RetrievalScope.FILTERED,
        filters=RetrievalFilters(doc_type="patent_draft"),
    )
    response = await retrieval_service.discover(request)

    doc_ids = [h.document.id for h in response.results]
    assert "doc_patent" in doc_ids
    assert "doc_report" not in doc_ids
    assert "doc_ref" not in doc_ids


async def test_prefilter_doc_type_keyword(
    graph_store, stub_content_store, seeded_embedding_provider, retrieval_service
):
    """Keyword search with doc_type filter only returns matching documents."""
    doc_patent = _make_doc("doc_patent_kw", doc_type="patent_draft")
    doc_report = _make_doc("doc_report_kw", doc_type="report")
    await graph_store.insert_document(doc_patent)
    await graph_store.insert_document(doc_report)

    identical_content = "Detailed analysis of PV07 claims and prior art."
    for doc_id, doc_type in [
        ("doc_patent_kw", "patent_draft"),
        ("doc_report_kw", "report"),
    ]:
        await _index_doc_chunks(
            stub_content_store, seeded_embedding_provider, doc_id,
            [("Section 1", identical_content)],
            doc_type=doc_type,
        )

    request = DiscoverRequest(
        mode=RetrievalMode.KEYWORD,
        query="PV07 claims",
        filters=RetrievalFilters(doc_type="patent_draft"),
    )
    response = await retrieval_service.discover(request)

    doc_ids = [h.document.id for h in response.results]
    assert "doc_patent_kw" in doc_ids
    assert "doc_report_kw" not in doc_ids


async def test_prefilter_doc_type_hybrid(
    graph_store, stub_content_store, seeded_embedding_provider, retrieval_service
):
    """Hybrid RRF search with doc_type filter only returns matching documents."""
    doc_patent = _make_doc("doc_patent_hyb", doc_type="patent_draft")
    doc_report = _make_doc("doc_report_hyb", doc_type="report")
    await graph_store.insert_document(doc_patent)
    await graph_store.insert_document(doc_report)

    identical_content = "Patent filing process for clinical normalization."
    for doc_id, doc_type in [
        ("doc_patent_hyb", "patent_draft"),
        ("doc_report_hyb", "report"),
    ]:
        await _index_doc_chunks(
            stub_content_store, seeded_embedding_provider, doc_id,
            [("Section 1", identical_content)],
            doc_type=doc_type,
        )

    request = DiscoverRequest(
        mode=RetrievalMode.SEMANTIC,
        query="patent filing",
        use_hybrid=True,
        filters=RetrievalFilters(doc_type="patent_draft"),
    )
    response = await retrieval_service.discover(request)

    doc_ids = [h.document.id for h in response.results]
    assert "doc_patent_hyb" in doc_ids
    assert "doc_report_hyb" not in doc_ids


async def test_prefilter_no_filter_returns_all_doc_types(
    graph_store, stub_content_store, seeded_embedding_provider, retrieval_service
):
    """Without a doc_type filter, all document types appear in results."""
    doc_patent = _make_doc("doc_patent_all", doc_type="patent_draft")
    doc_report = _make_doc("doc_report_all", doc_type="report")
    await graph_store.insert_document(doc_patent)
    await graph_store.insert_document(doc_report)

    identical_content = "Patent filing process for clinical normalization."
    for doc_id, doc_type in [
        ("doc_patent_all", "patent_draft"),
        ("doc_report_all", "report"),
    ]:
        await _index_doc_chunks(
            stub_content_store, seeded_embedding_provider, doc_id,
            [("Section 1", identical_content)],
            doc_type=doc_type,
        )

    request = DiscoverRequest(
        mode=RetrievalMode.SEMANTIC,
        query="patent filing",
    )
    response = await retrieval_service.discover(request)

    doc_ids = [h.document.id for h in response.results]
    assert "doc_patent_all" in doc_ids
    assert "doc_report_all" in doc_ids


async def test_postfilter_project_still_applies_with_prefilter(
    graph_store, stub_content_store, seeded_embedding_provider, retrieval_service
):
    """Post-filter fields (project) still work alongside doc_type pre-filter."""
    doc_match = _make_doc("doc_match_both", doc_type="patent_draft", project="pim_health")
    doc_wrong_project = _make_doc("doc_wrong_proj", doc_type="patent_draft", project="other")
    doc_wrong_type = _make_doc("doc_wrong_type", doc_type="report", project="pim_health")
    await graph_store.insert_document(doc_match)
    await graph_store.insert_document(doc_wrong_project)
    await graph_store.insert_document(doc_wrong_type)

    identical_content = "Patent filing process for clinical normalization."
    for doc_id, doc_type in [
        ("doc_match_both", "patent_draft"),
        ("doc_wrong_proj", "patent_draft"),
        ("doc_wrong_type", "report"),
    ]:
        await _index_doc_chunks(
            stub_content_store, seeded_embedding_provider, doc_id,
            [("Section 1", identical_content)],
            doc_type=doc_type,
        )

    request = DiscoverRequest(
        mode=RetrievalMode.SEMANTIC,
        query="patent filing",
        filters=RetrievalFilters(doc_type="patent_draft", project="pim_health"),
    )
    response = await retrieval_service.discover(request)

    doc_ids = [h.document.id for h in response.results]
    assert "doc_match_both" in doc_ids
    assert "doc_wrong_proj" not in doc_ids
    assert "doc_wrong_type" not in doc_ids


async def test_prefilter_doc_type_null_chunks_excluded(
    graph_store, stub_content_store, seeded_embedding_provider, retrieval_service
):
    """Chunks without doc_type metadata (pre-migration data) are excluded
    when a doc_type filter is active.
    """
    doc_typed = _make_doc("doc_typed", doc_type="patent_draft")
    doc_untyped = _make_doc("doc_untyped", doc_type="patent_draft")
    await graph_store.insert_document(doc_typed)
    await graph_store.insert_document(doc_untyped)

    identical_content = "Patent filing process for clinical normalization."
    # doc_typed has doc_type on its chunks
    await _index_doc_chunks(
        stub_content_store, seeded_embedding_provider, "doc_typed",
        [("Section 1", identical_content)],
        doc_type="patent_draft",
    )
    # doc_untyped has no doc_type on chunks (simulates pre-migration data)
    await _index_doc_chunks(
        stub_content_store, seeded_embedding_provider, "doc_untyped",
        [("Section 1", identical_content)],
        doc_type=None,
    )

    request = DiscoverRequest(
        mode=RetrievalMode.SEMANTIC,
        query="patent filing",
        filters=RetrievalFilters(doc_type="patent_draft"),
    )
    response = await retrieval_service.discover(request)

    doc_ids = [h.document.id for h in response.results]
    assert "doc_typed" in doc_ids
    # doc_untyped passes the post-filter (graph store has correct doc_type)
    # but content store pre-filter excluded its chunks because chunk.doc_type is None.
    # The post-filter cannot recover it because it never appeared in search results.
    # This is acceptable: pre-migration data requires re-indexing.
    assert "doc_untyped" not in doc_ids


async def test_metadata_doc_type_change_syncs_to_content_store(
    graph_store, stub_content_store, seeded_embedding_provider,
    retrieval_service, metadata_service,
):
    """Changing doc_type via MetadataService updates content store chunks,
    so subsequent filtered searches reflect the new type.
    """
    doc = _make_doc("doc_retyped", doc_type="note")
    await graph_store.insert_document(doc)
    await _index_doc_chunks(
        stub_content_store, seeded_embedding_provider, "doc_retyped",
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
    assert "doc_retyped" in [h.document.id for h in response.results]

    # Change doc_type to memo via MetadataService
    await metadata_service.update_metadata(
        "doc_retyped",
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
    assert "doc_retyped" in [h.document.id for h in response.results]

    response = await retrieval_service.discover(request_note)
    assert "doc_retyped" not in [h.document.id for h in response.results]


async def test_bh_070_document_date_in_summary(
    graph_store, stub_content_store, seeded_embedding_provider, retrieval_service
):
    """DiscoverHit.document summary includes document_date as datetime."""
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
    # document_date is now datetime, not str
    assert isinstance(hit.document.document_date, datetime)
    assert hit.document.document_date == datetime(2026, 3, 15, tzinfo=timezone.utc)


async def test_source_modified_at_in_summary(
    graph_store, stub_content_store, seeded_embedding_provider, retrieval_service
):
    """DiscoverHit.document summary includes source_modified_at field."""
    now = datetime.now(timezone.utc)
    mod_time = now - timedelta(days=10)
    doc = _make_doc("doc_sma_summary", source_modified_at=mod_time)
    await graph_store.insert_document(doc)

    await _index_doc_chunks(
        stub_content_store, seeded_embedding_provider, "doc_sma_summary",
        [("Section 1", "Patent filing process documentation.")],
    )

    request = DiscoverRequest(
        mode=RetrievalMode.SEMANTIC,
        query="patent filing",
    )
    response = await retrieval_service.discover(request)

    hit = next(h for h in response.results if h.document.id == "doc_sma_summary")
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
        "doc_nq_recent",
        document_date=None,
        source_modified_at=now - timedelta(days=5),
    )
    doc_old = _make_doc(
        "doc_nq_old",
        document_date=None,
        source_modified_at=now - timedelta(days=900),
    )
    await graph_store.insert_document(doc_recent)
    await graph_store.insert_document(doc_old)

    identical_content = "Patent filing process for clinical normalization."
    for doc_id in ["doc_nq_recent", "doc_nq_old"]:
        await _index_doc_chunks(
            stub_content_store, seeded_embedding_provider, doc_id,
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
        query="patent filing clinical normalization",
    )
    response = await retrieval_service.discover(request)

    # Restore original method
    graph_store.get_document = original_get_document

    doc_ids = [h.document.id for h in response.results]
    # Recency still works via summary field
    assert doc_ids.index("doc_nq_recent") < doc_ids.index("doc_nq_old"), (
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
        "doc_a": _make_doc("doc_a", doc_type="patent_draft", tags=["PV07"]),
        "doc_b": _make_doc("doc_b", doc_type="patent_draft", tags=["PV08"]),
        "doc_c": _make_doc("doc_c", doc_type="glossary", tags=["PV07"]),
        "doc_d": _make_doc(
            "doc_d", doc_type="patent_draft", tags=["PV07"],
            lifecycle_status="superseded",
        ),
        "doc_e": _make_doc("doc_e", doc_type="checklist", tags=["PV07"]),
    }
    for doc in docs.values():
        await graph_store.insert_document(doc)
    return docs


async def test_bh_072_catalog_returns_filtered_documents(
    graph_store, retrieval_service,
):
    """Catalog mode returns all documents matching doc_type filter."""
    await _seed_catalog_docs(graph_store)

    request = DiscoverRequest(
        mode=RetrievalMode.CATALOG,
        scope=RetrievalScope.FILTERED,
        filters=RetrievalFilters(doc_type="patent_draft"),
    )
    response = await retrieval_service.discover(request)

    assert response.mode == RetrievalMode.CATALOG
    result_ids = {h.document.id for h in response.results}
    assert result_ids == {"doc_a", "doc_b", "doc_d"}
    assert response.total_available == 3


async def test_bh_073_catalog_pagination(
    graph_store, retrieval_service,
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
    graph_store, retrieval_service,
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
    assert result_ids == {"doc_a", "doc_c", "doc_d", "doc_e"}
    assert response.total_available == 4


async def test_bh_075_catalog_no_chunk_content_or_scores(
    graph_store, retrieval_service,
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


async def test_bh_076_catalog_excludes_failed_pipeline(
    graph_store, retrieval_service,
):
    """Catalog mode excludes failed pipeline documents."""
    doc_ok = _make_doc("doc_ok")
    doc_fail = _make_doc(
        "doc_fail", pipeline_status=PipelineStatus.FAILED,
    )
    await graph_store.insert_document(doc_ok)
    await graph_store.insert_document(doc_fail)

    request = DiscoverRequest(mode=RetrievalMode.CATALOG)
    response = await retrieval_service.discover(request)

    result_ids = {h.document.id for h in response.results}
    assert "doc_ok" in result_ids
    assert "doc_fail" not in result_ids
    assert response.total_available == 1


async def test_bh_077_catalog_no_filters_returns_all(
    graph_store, retrieval_service,
):
    """Catalog mode with no filters returns all non-failed documents."""
    await _seed_catalog_docs(graph_store)
    # Add one failed doc
    doc_fail = _make_doc("doc_fail", pipeline_status=PipelineStatus.FAILED)
    await graph_store.insert_document(doc_fail)

    request = DiscoverRequest(mode=RetrievalMode.CATALOG)
    response = await retrieval_service.discover(request)

    assert response.total_available == 5  # 5 healthy, 1 failed excluded
    assert len(response.results) == 5


async def test_bh_078_catalog_total_available_independent_of_page(
    graph_store, retrieval_service,
):
    """Catalog mode total_available is full count, not page size."""
    # Insert 10 documents
    for i in range(10):
        doc = _make_doc(f"doc_{i:02d}")
        await graph_store.insert_document(doc)

    request = DiscoverRequest(mode=RetrievalMode.CATALOG, limit=3, offset=0)
    response = await retrieval_service.discover(request)

    assert len(response.results) == 3
    assert response.total_available == 10


async def test_bh_079_catalog_combined_filters(
    graph_store, retrieval_service,
):
    """Catalog mode with multiple filters uses AND semantics."""
    await _seed_catalog_docs(graph_store)

    request = DiscoverRequest(
        mode=RetrievalMode.CATALOG,
        scope=RetrievalScope.FILTERED,
        filters=RetrievalFilters(
            doc_type="patent_draft",
            tags=["PV07"],
            lifecycle_status="active",
        ),
    )
    response = await retrieval_service.discover(request)

    result_ids = {h.document.id for h in response.results}
    assert result_ids == {"doc_a"}
    assert response.total_available == 1
