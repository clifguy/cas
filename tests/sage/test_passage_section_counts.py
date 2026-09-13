"""``matched_chunk_count`` counts sections, however they were divided.

A section too long for the embedder is stored as several passages sharing its
heading path and its section index. The count is a reranking signal about how
much of a document bears on a query, so it must not grow because a section
happened to be long: every scored arm, and the fusion of the two, counts
distinct sections.

Two sections can render the same heading path, so the section index -- not the
path -- is what identifies one. The fusion test built on that case is the one a
path-keyed count passes wrongly in the other direction.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from sage.adapters.content_store_postgres import PostgresContentStore
from sage.adapters.interfaces import Chunk, SearchResult
from sage.adapters.stubs import SeededEmbeddingProvider, StubContentStore
from sage.models.enums import PipelineStatus, RetrievalMode, SourceType
from sage.models.schemas import DiscoverRequest, Document
from sage.services.retrieval import RetrievalService
from sage.storage.postgres.schema import EMBEDDING_DIM
from sage.utils.rrf import rrf_fuse

DOC_ID = "0000beef_section_counts"


def _row(section_index: int | None, heading_path: str = "Doc > S") -> SearchResult:
    return SearchResult(
        document_id=DOC_ID,
        heading_path=heading_path,
        content="alphaword",
        score=0.5,
        section_index=section_index,
    )


def test_fusion_counts_sub_passages_of_one_section_once() -> None:
    vector = [_row(1, "Doc > A"), _row(1, "Doc > A"), _row(2, "Doc > B")]

    [fused] = rrf_fuse(vector, [], limit=10)

    assert fused.matched_chunk_count == 2


def test_fusion_counts_two_sections_sharing_a_heading_path_twice() -> None:
    vector = [_row(1, "Doc > Notes"), _row(4, "Doc > Notes")]

    [fused] = rrf_fuse(vector, [], limit=10)

    assert fused.matched_chunk_count == 2


def _split_sections(*, shared_path: bool = False) -> list[Chunk]:
    """Two sections, each stored as two passages, every one carrying the term.

    With ``shared_path`` both sections render the same heading path, which is
    the case separating a count of sections from a count of distinct paths.
    """
    first, second = ("Doc > Notes", "Doc > Notes") if shared_path else ("Doc > A", "Doc > B")
    rows = [
        (first, 0, "## A\n\nalphaword one "),
        (first, 0, "alphaword two"),
        (second, 1, "## B\n\nalphaword three "),
        (second, 1, "alphaword four"),
    ]
    return [
        Chunk(
            document_id=DOC_ID,
            heading_path=path,
            content=content,
            chunk_index=index,
            section_index=section,
            embedding=[0.1] * EMBEDDING_DIM,
        )
        for index, (path, section, content) in enumerate(rows)
    ]


def _doc() -> Document:
    now = datetime.now(timezone.utc)
    return Document(
        id=DOC_ID,
        title="Doc",
        source_type=SourceType.MARKDOWN,
        source_path=f"imports/{DOC_ID}.md",
        lifecycle_status="active",
        source_content_hash=f"sha256:{0:064x}",
        adapter_version="1",
        created_by="t",
        created_at=now,
        last_modified_by="t",
        updated_at=now,
        doc_type="misc",
        pipeline_status=PipelineStatus.ABSTRACTION_COMPLETE,
    )


@pytest.fixture(params=["stub", "postgres"])
def store(request):
    if request.param == "stub":
        return StubContentStore()
    return PostgresContentStore(request.getfixturevalue("pg_pool"))


@pytest.mark.asyncio
@pytest.mark.parametrize("shared_path", [False, True])
async def test_keyword_arm_counts_sections_not_passages(store, shared_path) -> None:
    await store.index_chunks(DOC_ID, _split_sections(shared_path=shared_path))

    [row] = await store.search_bm25("alphaword", limit=10)

    assert row.matched_chunk_count == 2, "four passages carry the term, in two sections"


@pytest.mark.asyncio
async def test_a_heading_term_counts_its_section_once(store) -> None:
    """A term in the heading reaches every passage of the section through its
    heading path, so a passage-level count reports the split, not the match."""
    chunks = [
        Chunk(
            document_id=DOC_ID,
            heading_path="Doc > Zetaheading",
            content=f"plain body part {i}",
            chunk_index=i,
            section_index=0,
            embedding=[0.0] * EMBEDDING_DIM,
        )
        for i in range(5)
    ]
    await store.index_chunks(DOC_ID, chunks)

    [row] = await store.search_bm25("zetaheading", limit=10)

    assert row.matched_chunk_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("use_hybrid", [False, True])
@pytest.mark.parametrize("shared_path", [False, True])
async def test_scored_retrieval_counts_sections(
    graph_store, store, minimal_config, use_hybrid, shared_path
) -> None:
    await graph_store.insert_document(_doc())
    await store.index_chunks(DOC_ID, _split_sections(shared_path=shared_path))
    retrieval = RetrievalService(
        graph_store=graph_store,
        content_store=store,
        embedding_provider=SeededEmbeddingProvider(),
        config=minimal_config,
    )

    response = await retrieval.discover(
        DiscoverRequest(mode=RetrievalMode.SEMANTIC, query="alphaword", use_hybrid=use_hybrid)
    )

    [hit] = [h for h in response.results if h.document.id == DOC_ID]
    assert hit.matched_chunk_count == 2
