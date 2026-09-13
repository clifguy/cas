"""Both retrieval arms reach the text a document carries before its first heading.

Two documents are ingested from sources differing only in a closing line under
the heading, since identical bytes are refused as a duplicate. One is projected by the
markdown adapter as shipped; the other by the same adapter with the preamble
withheld, which is how every document was stored before that text had a passage.
Their titles, abstracts and document-level text agree, so a term found in the
first and not the second was found in a passage and nowhere else.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from sage.adapters.content_store_postgres import PostgresContentStore
from sage.adapters.stubs import SeededEmbeddingProvider, StubAbstractionProvider
from sage.models.enums import RetrievalMode, SourceType
from sage.models.schemas import DiscoverRequest, IngestRequest
from sage.services.ingestion import IngestionService
from sage.services.retrieval import RetrievalService
from sage.source_adapters.base import ProjectionResult
from sage.source_adapters.markdown_adapter import MarkdownAdapter
from tests.helpers.pipeline_wait import await_pipeline_idle

pytestmark = pytest.mark.asyncio

LEAD = "Larkspur branch holds the quarterly archive."
SOURCE = f"{LEAD}\n\n# Guide\n\nTamarack backups run nightly.\n"


class _PreambleWithheld(MarkdownAdapter):
    """The shipped adapter, reporting no text before the first heading."""

    async def project(self, source_path: Path, config: dict | None = None) -> ProjectionResult:
        projection = await super().project(source_path, config)
        projection.preamble = ""
        return projection


@pytest.fixture
async def indexed(
    graph_store, lock_manager, pg_pool, minimal_config, lifecycle_service, tmp_vault_dir
):
    store = PostgresContentStore(pg_pool)
    embedder = SeededEmbeddingProvider()
    sources = Path(tmp_vault_dir) / "sources"
    ids = {}
    for name, adapter in (("stored", MarkdownAdapter()), ("withheld", _PreambleWithheld())):
        (sources / f"{name}.md").write_text(f"{SOURCE}\nCopy {name}.\n")
        ingestion = IngestionService(
            graph_store=graph_store,
            lock_manager=lock_manager,
            content_store=store,
            embedding_provider=embedder,
            abstraction_provider=StubAbstractionProvider(),
            config=minimal_config,
            source_adapters={SourceType.MARKDOWN: adapter},
            lifecycle_service=lifecycle_service,
        )
        result = await ingestion.ingest(
            IngestRequest(source=f"{name}.md", source_type=SourceType.MARKDOWN)
        )
        await await_pipeline_idle(graph_store, result.document.id, service=ingestion)
        ids[name] = result.document.id
    retrieval = RetrievalService(
        graph_store=graph_store,
        content_store=store,
        embedding_provider=embedder,
        config=minimal_config,
    )
    return retrieval, ids


def _passage_hits(response, document_id: str):
    return [
        hit
        for hit in response.results
        if hit.document.id == document_id and hit.chunk_content is not None
    ]


async def test_a_keyword_found_only_before_the_first_heading_matches_the_document(indexed):
    retrieval, ids = indexed

    below = await retrieval.discover(
        DiscoverRequest(mode=RetrievalMode.KEYWORD, query="Tamarack", limit=10)
    )
    above = await retrieval.discover(
        DiscoverRequest(mode=RetrievalMode.KEYWORD, query="Larkspur", limit=10)
    )

    assert {h.document.id for h in below.results} >= set(ids.values()), (
        "control: a term below the heading must match both documents"
    )
    matched = {h.document.id for h in above.results}
    assert ids["stored"] in matched
    assert ids["withheld"] not in matched, "the term was reachable without its passage"
    [hit] = [h for h in above.results if h.document.id == ids["stored"]]
    assert hit.heading_path is None
    assert "Larkspur" in (hit.chunk_content or "")


async def test_a_semantic_query_reaches_the_passage_before_the_first_heading(indexed):
    retrieval, ids = indexed

    response = await retrieval.discover(
        DiscoverRequest(
            mode=RetrievalMode.SEMANTIC,
            query=LEAD,
            use_hybrid=False,
            use_abstract_prefilter=False,
            limit=10,
        )
    )

    assert response.results, "control: the semantic arm must return something"
    top = response.results[0]
    assert top.document.id == ids["stored"]
    assert top.heading_path is None
    assert top.chunk_content == LEAD
    assert all(h.chunk_content != LEAD for h in _passage_hits(response, ids["withheld"]))
