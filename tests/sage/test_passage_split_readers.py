"""A section split into several passages reads exactly as it did whole.

Splitting is an indexing concern. Every read that returns a document's text or
structure must answer identically whether a section was split or not, so each
test here writes one document's passages twice -- once under a bound nothing
exceeds, once under a bound that splits the long section -- and compares.

Anti-coincidental-pass: invariance holds vacuously when nothing split. The
``written`` fixture asserts that the bounded write produced more passages than
the unbounded one before any comparison runs.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from sage.adapters.content_store_postgres import PostgresContentStore
from sage.adapters.interfaces import Chunk
from sage.adapters.stubs import StubContentStore, StubEmbeddingProvider
from sage.models.enums import PipelineStatus, RetrievalMode, SourceType
from sage.models.schemas import DiscoverRequest, Document
from sage.services.retrieval import RetrievalService
from sage.services.utilities import UtilitiesService
from sage.source_adapters.base import HeadingNode, ProjectionResult
from sage.storage.postgres.schema import EMBEDDING_DIM

pytestmark = pytest.mark.asyncio

DOC_ID = "0000abcd_split_reader"
LONG = "Doc > Long"


def _long_body() -> str:
    lines = "\n".join(f"log line {i:04d} " + "x" * 30 for i in range(40))
    paragraphs = "\n\n".join(f"Paragraph {i} " + "word " * 15 for i in range(10))
    return f"{paragraphs}\n\n```\n{lines}\n```"


def _projection() -> ProjectionResult:
    return ProjectionResult(
        text="unused",
        headings=[
            HeadingNode(level=1, text="Doc", path="Doc", content="Opening."),
            HeadingNode(level=2, text="Long", path=LONG, content=_long_body()),
            HeadingNode(level=3, text="Child", path=f"{LONG} > Child", content="Child body."),
            HeadingNode(level=2, text="Tail", path="Doc > Tail", content="Closing."),
        ],
        content_hash="sha256:readers",
        adapter_version="0.1.0",
        title="Doc",
    )


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


def _chunks(ingestion_service, bound: int) -> list[Chunk]:
    ingestion_service._embedding = StubEmbeddingProvider(max_input_tokens=bound)
    chunks = ingestion_service._chunk_projection(DOC_ID, _projection())
    for chunk in chunks:
        chunk.embedding = [0.0] * EMBEDDING_DIM
    return chunks


@pytest.fixture(params=["stub", "postgres"])
def store(request):
    if request.param == "stub":
        return StubContentStore()
    return PostgresContentStore(request.getfixturevalue("pg_pool"))


@pytest.fixture
async def written(ingestion_service, graph_store, store):
    """Return a coroutine factory that rewrites the document's passages."""
    await graph_store.insert_document(_doc())
    whole = _chunks(ingestion_service, bound=100_000)
    split = _chunks(ingestion_service, bound=300)
    assert len(split) > len(whole), "the bounded write must split, or nothing here is evidence"
    assert len({c.section_index for c in split}) == len(whole)

    async def write(which: str) -> None:
        await store.index_chunks(DOC_ID, whole if which == "whole" else split)

    return write


def _utilities(graph_store, store, minimal_config) -> UtilitiesService:
    return UtilitiesService(
        graph_store=graph_store,
        content_store=store,
        embedding_provider=StubEmbeddingProvider(),
        config=minimal_config,
    )


async def test_projection_text_is_identical(written, graph_store, store, minimal_config):
    utilities = _utilities(graph_store, store, minimal_config)

    await written("whole")
    whole = (await utilities.read_projection(DOC_ID)).projection_text
    await written("split")
    split = (await utilities.read_projection(DOC_ID)).projection_text

    assert split == whole
    assert f"## Long\n\n{_long_body()}\n\n### Child" in whole


@pytest.mark.parametrize("heading", [LONG, "Doc"])
async def test_section_read_is_identical(written, graph_store, store, minimal_config, heading):
    utilities = _utilities(graph_store, store, minimal_config)

    await written("whole")
    whole = await utilities.read_section(DOC_ID, heading)
    await written("split")
    split = await utilities.read_section(DOC_ID, heading)

    assert split.section_text == whole.section_text
    assert split.chunk_count == whole.chunk_count


async def test_heading_listing_is_identical(written, graph_store, store, minimal_config):
    utilities = _utilities(graph_store, store, minimal_config)

    await written("whole")
    whole = list((await utilities.list_headings(DOC_ID)).headings)
    await written("split")
    split = list((await utilities.list_headings(DOC_ID)).headings)

    assert split == whole
    assert whole == ["Doc", LONG, f"{LONG} > Child", "Doc > Tail"]


async def test_deterministic_search_is_identical(written, graph_store, store, minimal_config):
    retrieval = RetrievalService(
        graph_store=graph_store,
        content_store=store,
        embedding_provider=StubEmbeddingProvider(),
        config=minimal_config,
    )
    request = DiscoverRequest(
        mode=RetrievalMode.DETERMINISTIC, document_id=DOC_ID, heading_path=LONG
    )

    await written("whole")
    whole = await retrieval.discover(request)
    await written("split")
    split = await retrieval.discover(request)

    def shape(response):
        return [(h.heading_path, h.chunk_content) for h in response.results]

    assert shape(split) == shape(whole)
    assert split.total_available == whole.total_available == 2


async def test_abstraction_input_is_identical(ingestion_service, graph_store):
    await graph_store.insert_document(_doc())
    seen: list[str] = []

    async def record(text, doc_type, *, document_id=None):
        seen.append(text)
        return "abstract"

    whole = _chunks(ingestion_service, bound=100_000)
    split = _chunks(ingestion_service, bound=300)
    assert len(split) > len(whole)
    ingestion_service._generate_abstract_text = record

    for chunks in (whole, split):
        await ingestion_service._content_store.index_chunks(DOC_ID, chunks)
        await ingestion_service._execute_abstract_from_chunks(DOC_ID, "misc")

    assert len(seen) == 2
    assert seen[1] == seen[0]


async def test_legacy_passages_sharing_a_heading_stay_separate(graph_store, store, minimal_config):
    """Rows written before sections were numbered carry no section index.

    Each such row is its own section. Two consecutive sections whose headings
    render the same path must not be merged into one: grouping on the heading
    path instead of the section would pass every other test in this module.
    """
    await graph_store.insert_document(_doc())
    await store.index_chunks(
        DOC_ID,
        [
            Chunk(
                document_id=DOC_ID,
                heading_path="Doc > Notes",
                content=f"## Notes\n\nnote body {i}",
                embedding=[0.0] * EMBEDDING_DIM,
                chunk_index=i,
            )
            for i in range(2)
        ],
    )
    retrieval = RetrievalService(
        graph_store=graph_store,
        content_store=store,
        embedding_provider=StubEmbeddingProvider(),
        config=minimal_config,
    )
    utilities = _utilities(graph_store, store, minimal_config)

    response = await retrieval.discover(
        DiscoverRequest(
            mode=RetrievalMode.DETERMINISTIC, document_id=DOC_ID, heading_path="Doc > Notes"
        )
    )
    section = await utilities.read_section(DOC_ID, "Doc > Notes")
    projection = (await utilities.read_projection(DOC_ID)).projection_text

    assert [h.chunk_content for h in response.results] == [
        "## Notes\n\nnote body 0",
        "## Notes\n\nnote body 1",
    ]
    assert section.chunk_count == 2
    assert projection == "## Notes\n\nnote body 0\n\n## Notes\n\nnote body 1"
