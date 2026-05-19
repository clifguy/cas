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
import re
from datetime import datetime, timedelta, timezone

import pytest

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
    ResponseLevel,
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
    doc_type: str | None = None,
    authority_scope: str | None = None,
    tags: list[str] | None = None,
    document_date: str | None = None,
    source_modified_at: datetime | None = None,
    semantic_abstract: str | None = None,
) -> Document:
    now = datetime.now(timezone.utc)
    return Document(
        id=doc_id,
        title=f"Test {doc_id}",
        source_type=SourceType.MARKDOWN,
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
    lifecycle_status: str | None = None,
    project: str | None = None,
) -> None:
    """Helper: index chunks for a document in the content store.

    chunks_data: list of (heading_path, content) tuples.
    doc_type, lifecycle_status, project: optional document-level scalars
    to stamp on the chunks for pre-filter testing. Production ingest
    stamps all three from the parent ``Document`` (T-0050 for doc_type,
    T-0077 for the other two); tests opt in per-call.
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
        [("Section 1", "This document discusses patent claims and prior art.")],
    )
    await _index_doc_chunks(
        stub_content_store,
        seeded_embedding_provider,
        _id("doc_failed"),
        [("Section 1", "This document discusses patent claims and prior art.")],
    )

    request = DiscoverRequest(mode=RetrievalMode.SEMANTIC, query="patent claims")
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
        [("Section 1", "patent claims prior art novelty claims")],
    )
    # doc_both gets content matching both
    await _index_doc_chunks(
        stub_content_store,
        seeded_embedding_provider,
        _id("doc_both"),
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
    doc = _make_doc(_id("doc_structured"))
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


async def test_get_heading_paths_excludes_synthetic_header(
    stub_content_store, seeded_embedding_provider
):
    """The synthetic header chunk's marker heading_path (T-0038) must
    not appear in get_heading_paths output — it is an internal
    retrieval surface, not a real heading."""
    from sage.adapters.interfaces import SYNTHETIC_HEADER_HEADING_PATH

    await _index_doc_chunks(
        stub_content_store,
        seeded_embedding_provider,
        _id("doc_hp_synth"),
        [
            (SYNTHETIC_HEADER_HEADING_PATH, "Title: X\nSource: x\n"),
            ("Overview", "Intro."),
            ("Conclusion", "End."),
        ],
    )

    paths = await stub_content_store.get_heading_paths(_id("doc_hp_synth"))
    assert SYNTHETIC_HEADER_HEADING_PATH not in paths
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
# Additional: scope gating
# ---------------------------------------------------------------------------


async def test_authoritative_scope_filters(
    graph_store, stub_content_store, seeded_embedding_provider, retrieval_service
):
    """Authoritative scope excludes documents without authority_scope."""
    doc_auth = _make_doc(_id("doc_auth"), authority_scope="pim_health")
    doc_plain = _make_doc(_id("doc_plain"))
    await graph_store.insert_document(doc_auth)
    await graph_store.insert_document(doc_plain)

    await _index_doc_chunks(
        stub_content_store,
        seeded_embedding_provider,
        _id("doc_auth"),
        [("Section 1", "Authoritative patent content.")],
    )
    await _index_doc_chunks(
        stub_content_store,
        seeded_embedding_provider,
        _id("doc_plain"),
        [("Section 1", "Non-authoritative patent content.")],
    )

    request = DiscoverRequest(
        mode=RetrievalMode.SEMANTIC,
        query="patent content",
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
    doc_pim = _make_doc(_id("doc_pim"), project="pim_health")
    doc_other = _make_doc(_id("doc_other"), project="basketball")
    await graph_store.insert_document(doc_pim)
    await graph_store.insert_document(doc_other)

    await _index_doc_chunks(
        stub_content_store,
        seeded_embedding_provider,
        _id("doc_pim"),
        [("Section 1", "Patent filing process documentation.")],
        project="pim_health",
    )
    await _index_doc_chunks(
        stub_content_store,
        seeded_embedding_provider,
        _id("doc_other"),
        [("Section 1", "Patent filing process documentation.")],
        project="basketball",
    )

    request = DiscoverRequest(
        mode=RetrievalMode.SEMANTIC,
        query="patent filing",
        filters=RetrievalFilters(project="pim_health"),
    )
    response = await retrieval_service.discover(request)

    doc_ids = [h.document.id for h in response.results]
    assert _id("doc_pim") in doc_ids
    assert _id("doc_other") not in doc_ids


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
    doc = _make_doc(_id("doc_pv07"))
    doc.title = "ClinicalNormalization"
    doc.source_path = "imports/PIM_PV07_ClinicalNormalization_v1_0.md"
    doc.tags = ["PV07"]
    await graph_store.insert_document(doc)

    # Index a chunk with search preamble from source filename
    # (simulating what _stage2_indexing now produces)
    await _index_doc_chunks(
        stub_content_store,
        seeded_embedding_provider,
        _id("doc_pv07"),
        [
            (
                "Section 1",
                "Title: ClinicalNormalization\n"
                "Source: PIM_PV07_ClinicalNormalization_v1_0\n"
                "Tags: PV07\n\n"
                "This document discusses patent claims.",
            )
        ],
    )

    # BM25 search for "PV07" should find it via the source filename in preamble
    request = DiscoverRequest(
        mode=RetrievalMode.SEMANTIC,
        query="PV07",
        use_hybrid=True,
    )
    response = await retrieval_service.discover(request)

    doc_ids = [h.document.id for h in response.results]
    assert _id("doc_pv07") in doc_ids


# ---------------------------------------------------------------------------
# T-0038: Documents whose identifying terms live in title/semantic_abstract
# or in CamelCase compounds are discoverable via the synthetic header chunk
# ---------------------------------------------------------------------------


async def test_t_0038_camelcase_title_searchable_via_split_tokens(
    graph_store, stub_content_store, seeded_embedding_provider, retrieval_service
):
    """A document whose title is a CamelCase compound and whose body is
    sparse placeholder content is retrievable by natural-language queries
    against the constituent words. The synthetic header chunk carries a
    case-split identifier-token line that unblocks BM25 matching."""
    from sage.adapters.interfaces import SYNTHETIC_HEADER_HEADING_PATH

    doc = _make_doc(_id("doc_portfolio"))
    doc.title = "PortfolioDashboard_Template"
    doc.source_path = "imports/2026-05-11_PIM_REF_PortfolioDashboard_Template_v3.xlsx"
    doc.tags = ["REF", "template"]
    doc.semantic_abstract = (
        "Authoritative template for the PIM Health Portfolio Dashboard "
        "with rows per patent and columns per filing pipeline stage."
    )
    await graph_store.insert_document(doc)

    # Synthetic header chunk content mirrors what _build_header_chunk_content
    # produces in production. The case-split identifier line is what makes
    # "dashboard" match against "PortfolioDashboard".
    header_content = (
        "Title: PortfolioDashboard_Template\n"
        "Source: 2026-05-11_PIM_REF_PortfolioDashboard_Template_v3\n"
        "Tags: REF, template\n"
        "Abstract: " + doc.semantic_abstract + "\n\n"
        "Identifier tokens: portfolio dashboard template v3 pim ref\n"
    )
    body_content = "[Placeholder content. Template body is structurally minimal.]"

    await _index_doc_chunks(
        stub_content_store,
        seeded_embedding_provider,
        _id("doc_portfolio"),
        [
            (SYNTHETIC_HEADER_HEADING_PATH, header_content),
            ("Sheet1", body_content),
        ],
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

    # The diagnostic query: should land doc_portfolio in BM25, semantic,
    # and hybrid modes.
    for mode_kwargs in (
        {"mode": RetrievalMode.KEYWORD, "query": "dashboard template"},
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


async def test_t_0038_semantic_abstract_drives_keyword_match(
    graph_store, stub_content_store, seeded_embedding_provider, retrieval_service
):
    """A document with sparse body but a descriptive semantic_abstract is
    retrievable by BM25 queries against the abstract's terms — because the
    abstract lives in the synthetic header chunk's indexed content."""
    from sage.adapters.interfaces import SYNTHETIC_HEADER_HEADING_PATH

    doc = _make_doc(_id("doc_abstract_only"))
    doc.title = "Catalog_2026"
    doc.semantic_abstract = (
        "Cryptographic accumulator seals govern every commit boundary in this catalog."
    )
    await graph_store.insert_document(doc)

    header_content = (
        "Title: Catalog_2026\n"
        "Source: \n"
        "Tags: \n"
        "Abstract: " + doc.semantic_abstract + "\n\n"
        "Identifier tokens: catalog 2026\n"
    )
    await _index_doc_chunks(
        stub_content_store,
        seeded_embedding_provider,
        _id("doc_abstract_only"),
        [
            (SYNTHETIC_HEADER_HEADING_PATH, header_content),
            ("Sheet1", "[empty]"),
        ],
    )

    request = DiscoverRequest(
        mode=RetrievalMode.KEYWORD,
        query="cryptographic accumulator",
    )
    response = await retrieval_service.discover(request)
    doc_ids = [h.document.id for h in response.results]
    assert _id("doc_abstract_only") in doc_ids


async def test_t_0038_hit_heading_path_masks_synthetic_marker(
    graph_store, stub_content_store, seeded_embedding_provider, retrieval_service
):
    """When a hit's winning chunk is the synthetic header (T-0038), the
    hit's user-visible heading_path is None — never the internal
    ``__document_header__`` sentinel."""
    from sage.adapters.interfaces import SYNTHETIC_HEADER_HEADING_PATH

    doc = _make_doc(_id("doc_mask"))
    doc.title = "MaskProbe_Template"
    await graph_store.insert_document(doc)

    header_content = (
        "Title: MaskProbe_Template\n"
        "Source: maskprobe\n"
        "Tags: \n"
        "Abstract: probe content\n\n"
        "Identifier tokens: mask probe template\n"
    )
    await _index_doc_chunks(
        stub_content_store,
        seeded_embedding_provider,
        _id("doc_mask"),
        [
            (SYNTHETIC_HEADER_HEADING_PATH, header_content),
            ("Sheet1", "[empty body]"),
        ],
    )

    request = DiscoverRequest(mode=RetrievalMode.KEYWORD, query="probe template")
    response = await retrieval_service.discover(request)

    masked = [hit for hit in response.results if hit.document.id == _id("doc_mask")]
    assert masked, "doc_mask was not in results"
    assert masked[0].heading_path != SYNTHETIC_HEADER_HEADING_PATH


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
        [("Section 1", "Patent claims analysis for PV07.")],
    )
    await _index_doc_chunks(
        stub_content_store,
        seeded_embedding_provider,
        _id("doc_failed_kw"),
        [("Section 1", "Patent claims analysis for PV07.")],
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
    active_content = "Patent filing process."
    archived_content = "Patent filing process for clinical normalization review."
    completed_content = "Patent filing process for clinical normalization."
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
        query="patent filing clinical normalization",
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

    identical_content = "Patent filing process for clinical normalization."
    for doc_id in [_id("doc_recent"), _id("doc_old")]:
        await _index_doc_chunks(
            stub_content_store,
            seeded_embedding_provider,
            doc_id,
            [("Section 1", identical_content)],
        )

    request = DiscoverRequest(
        mode=RetrievalMode.SEMANTIC,
        query="patent filing clinical normalization",
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

    identical_content = "Patent filing process for clinical normalization."
    for doc_id in [_id("doc_recent_fb"), _id("doc_old_fb")]:
        await _index_doc_chunks(
            stub_content_store,
            seeded_embedding_provider,
            doc_id,
            [("Section 1", identical_content)],
        )

    request = DiscoverRequest(
        mode=RetrievalMode.SEMANTIC,
        query="patent filing clinical normalization",
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

    identical_content = "Patent filing process for clinical normalization."
    for doc_id in [_id("doc_dated"), _id("doc_undated")]:
        await _index_doc_chunks(
            stub_content_store,
            seeded_embedding_provider,
            doc_id,
            [("Section 1", identical_content)],
        )

    request = DiscoverRequest(
        mode=RetrievalMode.SEMANTIC,
        query="patent filing clinical normalization",
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

    identical_content = "Patent filing process for clinical normalization."
    for doc_id in [_id("doc_ar"), _id("doc_ao"), _id("doc_sr")]:
        await _index_doc_chunks(
            stub_content_store,
            seeded_embedding_provider,
            doc_id,
            [("Section 1", identical_content)],
        )

    request = DiscoverRequest(
        mode=RetrievalMode.SEMANTIC,
        query="patent filing clinical normalization",
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
    doc_patent = _make_doc(_id("doc_patent"), doc_type="patent_draft")
    doc_report = _make_doc(_id("doc_report"), doc_type="report")
    doc_ref = _make_doc(_id("doc_ref"), doc_type="reference_document")
    await graph_store.insert_document(doc_patent)
    await graph_store.insert_document(doc_report)
    await graph_store.insert_document(doc_ref)

    # All documents get identical content so only the filter differentiates
    identical_content = "Clinical pathway integration and normalization process."
    for doc_id, doc_type in [
        (_id("doc_patent"), "patent_draft"),
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
        filters=RetrievalFilters(doc_type="patent_draft"),
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
    doc_patent = _make_doc(_id("doc_patent_kw"), doc_type="patent_draft")
    doc_report = _make_doc(_id("doc_report_kw"), doc_type="report")
    await graph_store.insert_document(doc_patent)
    await graph_store.insert_document(doc_report)

    identical_content = "Detailed analysis of PV07 claims and prior art."
    for doc_id, doc_type in [
        (_id("doc_patent_kw"), "patent_draft"),
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
        filters=RetrievalFilters(doc_type="patent_draft"),
    )
    response = await retrieval_service.discover(request)

    doc_ids = [h.document.id for h in response.results]
    assert _id("doc_patent_kw") in doc_ids
    assert _id("doc_report_kw") not in doc_ids


async def test_prefilter_doc_type_hybrid(
    graph_store, stub_content_store, seeded_embedding_provider, retrieval_service
):
    """Hybrid RRF search with doc_type filter only returns matching documents."""
    doc_patent = _make_doc(_id("doc_patent_hyb"), doc_type="patent_draft")
    doc_report = _make_doc(_id("doc_report_hyb"), doc_type="report")
    await graph_store.insert_document(doc_patent)
    await graph_store.insert_document(doc_report)

    identical_content = "Patent filing process for clinical normalization."
    for doc_id, doc_type in [
        (_id("doc_patent_hyb"), "patent_draft"),
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
        query="patent filing",
        use_hybrid=True,
        filters=RetrievalFilters(doc_type="patent_draft"),
    )
    response = await retrieval_service.discover(request)

    doc_ids = [h.document.id for h in response.results]
    assert _id("doc_patent_hyb") in doc_ids
    assert _id("doc_report_hyb") not in doc_ids


async def test_prefilter_no_filter_returns_all_doc_types(
    graph_store, stub_content_store, seeded_embedding_provider, retrieval_service
):
    """Without a doc_type filter, all document types appear in results."""
    doc_patent = _make_doc(_id("doc_patent_all"), doc_type="patent_draft")
    doc_report = _make_doc(_id("doc_report_all"), doc_type="report")
    await graph_store.insert_document(doc_patent)
    await graph_store.insert_document(doc_report)

    identical_content = "Patent filing process for clinical normalization."
    for doc_id, doc_type in [
        (_id("doc_patent_all"), "patent_draft"),
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
        query="patent filing",
    )
    response = await retrieval_service.discover(request)

    doc_ids = [h.document.id for h in response.results]
    assert _id("doc_patent_all") in doc_ids
    assert _id("doc_report_all") in doc_ids


async def test_postfilter_project_still_applies_with_prefilter(
    graph_store, stub_content_store, seeded_embedding_provider, retrieval_service
):
    """Post-filter fields (project) still work alongside doc_type pre-filter."""
    doc_match = _make_doc(_id("doc_match_both"), doc_type="patent_draft", project="pim_health")
    doc_wrong_project = _make_doc(_id("doc_wrong_proj"), doc_type="patent_draft", project="other")
    doc_wrong_type = _make_doc(_id("doc_wrong_type"), doc_type="report", project="pim_health")
    await graph_store.insert_document(doc_match)
    await graph_store.insert_document(doc_wrong_project)
    await graph_store.insert_document(doc_wrong_type)

    identical_content = "Patent filing process for clinical normalization."
    for doc_id, doc_type, project in [
        (_id("doc_match_both"), "patent_draft", "pim_health"),
        (_id("doc_wrong_proj"), "patent_draft", "other"),
        (_id("doc_wrong_type"), "report", "pim_health"),
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
        query="patent filing",
        filters=RetrievalFilters(doc_type="patent_draft", project="pim_health"),
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
    doc_typed = _make_doc(_id("doc_typed"), doc_type="patent_draft")
    doc_untyped = _make_doc(_id("doc_untyped"), doc_type="patent_draft")
    await graph_store.insert_document(doc_typed)
    await graph_store.insert_document(doc_untyped)

    identical_content = "Patent filing process for clinical normalization."
    # doc_typed has doc_type on its chunks
    await _index_doc_chunks(
        stub_content_store,
        seeded_embedding_provider,
        _id("doc_typed"),
        [("Section 1", identical_content)],
        doc_type="patent_draft",
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
        query="patent filing",
        filters=RetrievalFilters(doc_type="patent_draft"),
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
    await metadata_service.update_metadata(
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
        [("Section 1", "Patent filing process documentation.")],
    )

    request = DiscoverRequest(
        mode=RetrievalMode.SEMANTIC,
        query="patent filing",
    )
    response = await retrieval_service.discover(request)

    hit = next(h for h in response.results if h.document.id == _id("doc_date_summary"))
    # document_date is now datetime, not str
    assert isinstance(hit.document.document_date, datetime)
    assert hit.document.document_date == datetime(2026, 3, 15, tzinfo=timezone.utc)


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
        [("Section 1", "Patent filing process documentation.")],
    )

    request = DiscoverRequest(
        mode=RetrievalMode.SEMANTIC,
        query="patent filing",
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

    identical_content = "Patent filing process for clinical normalization."
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
        query="patent filing clinical normalization",
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
        _id("doc_a"): _make_doc(_id("doc_a"), doc_type="patent_draft", tags=["PV07"]),
        _id("doc_b"): _make_doc(_id("doc_b"), doc_type="patent_draft", tags=["PV08"]),
        _id("doc_c"): _make_doc(_id("doc_c"), doc_type="glossary", tags=["PV07"]),
        _id("doc_d"): _make_doc(
            _id("doc_d"),
            doc_type="patent_draft",
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
        filters=RetrievalFilters(doc_type="patent_draft"),
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


async def test_bh_076_catalog_excludes_failed_pipeline(
    graph_store,
    retrieval_service,
):
    """Catalog mode excludes failed pipeline documents."""
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
    assert _id("doc_fail") not in result_ids
    assert response.total_available == 1


async def test_bh_077_catalog_no_filters_returns_all(
    graph_store,
    retrieval_service,
):
    """Catalog mode with no filters returns all non-failed documents."""
    await _seed_catalog_docs(graph_store)
    # Add one failed doc
    doc_fail = _make_doc(_id("doc_fail"), pipeline_status=PipelineStatus.FAILED)
    await graph_store.insert_document(doc_fail)

    request = DiscoverRequest(mode=RetrievalMode.CATALOG)
    response = await retrieval_service.discover(request)

    assert response.total_available == 5  # 5 healthy, 1 failed excluded
    assert len(response.results) == 5


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
            doc_type="patent_draft",
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
            doc_type="patent_draft",
        ),
        _id("sort_b"): _make_doc(
            _id("sort_b"),
            lifecycle_status="archived",
            document_date="2026-04-01",
            doc_type="patent_draft",
        ),
        _id("sort_c"): _make_doc(
            _id("sort_c"),
            lifecycle_status="active",
            document_date="2026-02-10",
            doc_type="patent_draft",
        ),
        _id("sort_d"): _make_doc(
            _id("sort_d"),
            lifecycle_status="archived",
            document_date="2026-04-10",
            doc_type="patent_draft",
        ),
        _id("sort_e"): _make_doc(
            _id("sort_e"),
            lifecycle_status="active",
            document_date=None,
            doc_type="patent_draft",
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
        [("Intro", "Test text about patents")],
    )

    request = DiscoverRequest(
        mode=RetrievalMode.KEYWORD,
        query="patents",
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
        doc = _make_doc(doc_id, doc_type="patent_draft")
        await graph_store.insert_document(doc)
        await _index_doc_chunks(
            content_store,
            embedding_provider,
            doc_id,
            [("Section 1", f"Integration testing for {doc_id} document.")],
            doc_type="patent_draft",
        )


async def test_bh_084_semantic_response_level_documents(
    graph_store,
    stub_content_store,
    seeded_embedding_provider,
    retrieval_service,
):
    """Semantic search with response_level=documents omits chunk content but
    preserves heading_path and matched_chunk_count."""
    await _seed_response_level_docs(
        graph_store,
        stub_content_store,
        seeded_embedding_provider,
    )

    request = DiscoverRequest(
        mode=RetrievalMode.SEMANTIC,
        query="integration",
        response_level=ResponseLevel.DOCUMENTS,
        limit=10,
    )
    response = await retrieval_service.discover(request)

    assert len(response.results) > 0
    for hit in response.results:
        assert hit.chunk_content is None
        # heading_path preserved as "why this matched" context
        assert hit.heading_path is not None
        assert hit.relevance_score is not None
        assert hit.relevance_score > 0
        assert hit.matched_chunk_count is not None
        assert hit.matched_chunk_count >= 1
        assert hit.document.id is not None
        assert hit.document.title is not None


async def test_bh_085_keyword_response_level_documents(
    graph_store,
    stub_content_store,
    seeded_embedding_provider,
    retrieval_service,
):
    """Keyword search with response_level=documents omits chunk content but
    preserves heading_path and matched_chunk_count."""
    await _seed_response_level_docs(
        graph_store,
        stub_content_store,
        seeded_embedding_provider,
    )

    request = DiscoverRequest(
        mode=RetrievalMode.KEYWORD,
        query="integration",
        response_level=ResponseLevel.DOCUMENTS,
        limit=10,
    )
    response = await retrieval_service.discover(request)

    assert len(response.results) > 0
    for hit in response.results:
        assert hit.chunk_content is None
        assert hit.heading_path is not None
        assert hit.relevance_score is not None
        assert hit.relevance_score > 0
        assert hit.matched_chunk_count is not None
        assert hit.matched_chunk_count >= 1
        assert hit.document.id is not None


async def test_bh_086_response_level_documents_preserves_scores_and_order(
    graph_store,
    stub_content_store,
    seeded_embedding_provider,
    retrieval_service,
):
    """response_level=documents produces the same scores, order, heading paths,
    and matched_chunk_counts as chunks mode."""
    await _seed_response_level_docs(
        graph_store,
        stub_content_store,
        seeded_embedding_provider,
    )

    chunks_request = DiscoverRequest(
        mode=RetrievalMode.SEMANTIC,
        query="integration",
        response_level=ResponseLevel.CHUNKS,
        limit=10,
    )
    docs_request = DiscoverRequest(
        mode=RetrievalMode.SEMANTIC,
        query="integration",
        response_level=ResponseLevel.DOCUMENTS,
        limit=10,
    )

    chunks_response = await retrieval_service.discover(chunks_request)
    docs_response = await retrieval_service.discover(docs_request)

    # Same documents in same order
    chunks_ids = [h.document.id for h in chunks_response.results]
    docs_ids = [h.document.id for h in docs_response.results]
    assert chunks_ids == docs_ids

    # Same scores
    chunks_scores = [h.relevance_score for h in chunks_response.results]
    docs_scores = [h.relevance_score for h in docs_response.results]
    assert chunks_scores == docs_scores

    # Same heading paths
    chunks_headings = [h.heading_path for h in chunks_response.results]
    docs_headings = [h.heading_path for h in docs_response.results]
    assert chunks_headings == docs_headings

    # Same matched_chunk_counts
    chunks_counts = [h.matched_chunk_count for h in chunks_response.results]
    docs_counts = [h.matched_chunk_count for h in docs_response.results]
    assert chunks_counts == docs_counts

    # Chunks response has content; documents response does not
    assert any(h.chunk_content is not None for h in chunks_response.results)
    assert all(h.chunk_content is None for h in docs_response.results)


async def test_bh_084_multi_chunk_matched_chunk_count(
    graph_store,
    stub_content_store,
    seeded_embedding_provider,
    retrieval_service,
):
    """matched_chunk_count reflects multiple matching chunks per document."""
    doc = _make_doc(_id("multi_chunk"), doc_type="patent_draft")
    await graph_store.insert_document(doc)
    # Index 3 chunks for the same document
    await _index_doc_chunks(
        stub_content_store,
        seeded_embedding_provider,
        _id("multi_chunk"),
        [
            ("Section 1", "Integration testing for claims."),
            ("Section 2", "Integration of prior art references."),
            ("Section 3", "Integration with existing patent family."),
        ],
        doc_type="patent_draft",
    )

    request = DiscoverRequest(
        mode=RetrievalMode.SEMANTIC,
        query="integration",
        response_level=ResponseLevel.DOCUMENTS,
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
    doc = _make_doc(_id("dual_match"), doc_type="patent_draft")
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
        doc_type="patent_draft",
    )

    request = DiscoverRequest(
        mode=RetrievalMode.SEMANTIC,
        query="integration",
        response_level=ResponseLevel.DOCUMENTS,
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

    When a document code (e.g., "PV13") appears in the source_path and the
    document is also found by content search at a low relevance score, the
    metadata boost should promote its score above the highest content score.
    Previously, metadata boost skipped documents already in content results,
    leaving code-based lookups at near-zero relevance.
    """
    # Target doc: code "PV13" is in the source_path, content barely matches
    target = _make_doc(_id("target_pv13"), doc_type="patent_draft")
    target.source_path = "patents/PV13_AuthoritativeAccumulator.docx"
    await graph_store.insert_document(target)
    await _index_doc_chunks(
        stub_content_store,
        seeded_embedding_provider,
        _id("target_pv13"),
        [("Section 1", "Clinical normalization of respiratory signals.")],
        doc_type="patent_draft",
    )

    # Distractor doc: strong content match for "PV13" but no metadata match
    distractor = _make_doc(_id("distractor"), doc_type="patent_draft")
    distractor.source_path = "patents/PV99_Unrelated.docx"
    await graph_store.insert_document(distractor)
    await _index_doc_chunks(
        stub_content_store,
        seeded_embedding_provider,
        _id("distractor"),
        [("Section 1", "PV13 is referenced in the prior art analysis.")],
        doc_type="patent_draft",
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
        "Document with code in source_path should rank above "
        "document that merely mentions the code in body text"
    )

    # Target's score should be above the distractor's content score
    target_hit = next(h for h in response.results if h.document.id == _id("target_pv13"))
    distractor_hit = next(h for h in response.results if h.document.id == _id("distractor"))
    assert target_hit.relevance_score > distractor_hit.relevance_score


async def test_bh_087_response_level_chunks_default(
    graph_store,
    stub_content_store,
    seeded_embedding_provider,
    retrieval_service,
):
    """Default response_level (omitted) preserves current chunk behavior."""
    await _seed_response_level_docs(
        graph_store,
        stub_content_store,
        seeded_embedding_provider,
    )

    # No response_level specified -- should default to chunks
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


async def test_bh_088_response_level_ignored_by_catalog(
    graph_store,
    retrieval_service,
):
    """Catalog mode ignores response_level; always returns document-level."""
    for doc_id in (_id("cat_a"), _id("cat_b")):
        doc = _make_doc(doc_id, doc_type="patent_draft")
        await graph_store.insert_document(doc)

    # Explicitly request chunks -- catalog should still return no chunk content
    request = DiscoverRequest(
        mode=RetrievalMode.CATALOG,
        response_level=ResponseLevel.CHUNKS,
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
    abstract_text = "This document analyzes patent claim structures for PIM Health."
    doc = _make_doc(_id("abs_doc"), semantic_abstract=abstract_text)
    await graph_store.insert_document(doc)

    await _index_doc_chunks(
        stub_content_store,
        seeded_embedding_provider,
        _id("abs_doc"),
        [("Section 1", "Patent claim structures and prior art analysis.")],
    )

    request = DiscoverRequest(
        mode=RetrievalMode.SEMANTIC,
        query="patent claims",
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
        doc_type="patent_draft",
        semantic_abstract="Summary of patent draft for metabolic monitoring.",
    )
    doc_without = _make_doc(
        _id("cat_no_abs"),
        doc_type="patent_draft",
        pipeline_status=PipelineStatus.ABSTRACTION_SKIPPED,
    )
    await graph_store.insert_document(doc_with)
    await graph_store.insert_document(doc_without)

    request = DiscoverRequest(
        mode=RetrievalMode.CATALOG,
        filters=RetrievalFilters(doc_type="patent_draft"),
        limit=10,
        include_abstracts=True,
    )
    response = await retrieval_service.discover(request)

    hits_by_id = {h.document.id: h for h in response.results}
    assert hits_by_id[_id("cat_abs")].document.semantic_abstract == (
        "Summary of patent draft for metabolic monitoring."
    )
    assert hits_by_id[_id("cat_no_abs")].document.semantic_abstract is None


# BH-104: Document-level response mode preserves semantic_abstract
async def test_bh_104_response_level_documents_preserves_abstract(
    graph_store,
    stub_content_store,
    seeded_embedding_provider,
    retrieval_service,
):
    """response_level=documents suppresses chunk_content but preserves semantic_abstract."""
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
        response_level=ResponseLevel.DOCUMENTS,
        include_abstracts=True,
    )
    response = await retrieval_service.discover(request)

    hits_by_id = {h.document.id: h for h in response.results}
    assert _id("rl_doc") in hits_by_id
    hit = hits_by_id[_id("rl_doc")]
    # chunk_content suppressed by response_level=documents
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
        semantic_abstract="Patent claim analysis for metabolic biomarkers.",
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
        authority_scope="pim_health",
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
            [("Section 1", "Patent claims analysis for PV07.")],
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
            [("Section 1", "Patent claims analysis for PV07.")],
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
        [("Section 1", "Interesting patent content about claims.")],
    )

    # Search matches content but filter by a non-matching project.
    # With pre-filter resolution, the project filter resolves to zero
    # matching documents and the search short-circuits before calling
    # the content store. total_before_filtering is 0 (no chunks were
    # fetched), but hints still surface the active filters so the
    # caller can see why their search was empty.
    request = DiscoverRequest(
        mode=RetrievalMode.SEMANTIC,
        query="patent claims",
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
        [("Section 1", "Patent claim analysis for biosensor calibration.")],
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
# archived patent_draft chunks dominate top-K vector ranking, the
# lifecycle_status=active post-filter drops them all, returning zero hits
# even though active patent_drafts exist that match the query.
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
    # 20 archived patent_drafts that match the query, inserted first so
    # they dominate the stable-sort tie-breaking on identical embeddings.
    for i in range(20):
        doc = _make_doc(
            _id(f"archived_{i}"),
            lifecycle_status="archived",
            doc_type="patent_draft",
        )
        await graph_store.insert_document(doc)
        await _index_doc_chunks(
            stub_content_store,
            seeded_embedding_provider,
            _id(f"archived_{i}"),
            [("Fraud Screening Module", "fraud screening risk score detection")],
            doc_type="patent_draft",
            lifecycle_status="archived",
        )

    # 1 active patent_draft with the same matching content.
    active_doc = _make_doc(
        _id("active_target"),
        lifecycle_status="active",
        doc_type="patent_draft",
    )
    await graph_store.insert_document(active_doc)
    await _index_doc_chunks(
        stub_content_store,
        seeded_embedding_provider,
        _id("active_target"),
        [("Fraud Screening Module", "fraud screening risk score detection")],
        doc_type="patent_draft",
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
            doc_type="patent_draft",
            lifecycle_status="active",
        ),
    )
    response = await retrieval_service.discover(request)

    doc_ids = [h.document.id for h in response.results]
    assert _id("active_target") in doc_ids, (
        f"Active patent_draft must surface even when archived versions "
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
            doc_type="patent_draft",
        )
        await graph_store.insert_document(doc)
        await _index_doc_chunks(
            stub_content_store,
            seeded_embedding_provider,
            _id(f"archived_kw_{i}"),
            [("Fraud Screening Module", "fraud screening risk score detection")],
            doc_type="patent_draft",
            lifecycle_status="archived",
        )

    active_doc = _make_doc(
        _id("active_kw_target"),
        lifecycle_status="active",
        doc_type="patent_draft",
    )
    await graph_store.insert_document(active_doc)
    await _index_doc_chunks(
        stub_content_store,
        seeded_embedding_provider,
        _id("active_kw_target"),
        [("Fraud Screening Module", "fraud screening risk score detection")],
        doc_type="patent_draft",
        lifecycle_status="active",
    )

    request = DiscoverRequest(
        mode=RetrievalMode.KEYWORD,
        query="fraud screening",
        limit=1,
        filters=RetrievalFilters(
            doc_type="patent_draft",
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
    doc = _make_doc(_id("doc_only"), lifecycle_status="active", doc_type="patent_draft")
    await graph_store.insert_document(doc)
    await _index_doc_chunks(
        stub_content_store,
        seeded_embedding_provider,
        _id("doc_only"),
        [("Section", "fraud screening content")],
        doc_type="patent_draft",
    )

    # Filter excludes the only document — no docs match.
    request = DiscoverRequest(
        mode=RetrievalMode.SEMANTIC,
        query="fraud screening",
        filters=RetrievalFilters(
            doc_type="patent_draft",
            lifecycle_status="filed",  # no doc has this status
        ),
    )
    response = await retrieval_service.discover(request)
    assert response.results == []


# ---------------------------------------------------------------------------
# _parse_document_date: tolerate ISO-with-time form alongside YYYY-MM-DD.
# Pre-fix, the helper used strptime("%Y-%m-%d") and silently dropped
# `2026-05-05T00:00:00Z` to None. The fix accepts both shapes via
# datetime.fromisoformat.
# ---------------------------------------------------------------------------


def test_parse_document_date_accepts_iso_with_z():
    from sage.services.retrieval import _parse_document_date

    result = _parse_document_date("2026-05-05T00:00:00Z")
    assert result == datetime(2026, 5, 5, tzinfo=timezone.utc)
