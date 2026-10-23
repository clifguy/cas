"""Every served generated value retains its publication's evidence and identity."""

from copy import deepcopy

import pytest

from sage.adapters.content_store_postgres import PostgresContentStore
from sage.adapters.interfaces import DocumentSurface
from sage.models.coverage import CoverageDimension
from sage.models.schemas import DiscoverRequest, IngestRequest
from sage.services.retrieval import RetrievalService
from sage.storage.postgres.schema import EMBEDDING_DIM
from tests.sage.test_processing_coverage import _chunks, _doc, _identity


@pytest.mark.parametrize(
    "writer",
    [
        "chunks",
        "surface",
        "remove_surface",
        "surface_text",
        "structure",
        "migration",
        "rebuild",
        "remove_document",
    ],
)
async def test_unknown_publication_is_fenced_by_every_artifact_writer(
    pg_pool, postgres_graph_store, monkeypatch, writer
):
    document_id = _doc(1).id
    await postgres_graph_store.insert_document(_doc(1))
    store, delayed = PostgresContentStore(pg_pool), PostgresContentStore(pg_pool)
    a = await store.begin_processing_attempt(document_id)
    dimensions = ("projection", "keyword_indexing", "passage_embeddings", "abstraction_evidence")
    evidence = {
        name: CoverageDimension(limitations=["producer_evidence_unavailable"])
        for name in dimensions
    }
    assert await store.publish_processing(
        document_id,
        a,
        evidence,
        chunks=_chunks("old"),
        surface=DocumentSurface(document_id=document_id, matchable="old", orienting="old abstract"),
    )
    if writer == "chunks":
        await store.index_chunks(document_id, _chunks("new"))
    elif writer == "surface":
        await store.upsert_document_surface(
            DocumentSurface(document_id=document_id, matchable="new", orienting="new abstract")
        )
    elif writer == "remove_surface":
        await store.remove_document_surface(document_id)
    elif writer == "surface_text":
        assert await store.update_document_surface_text(document_id, "new", "new abstract")
    elif writer == "structure":
        assert await store.update_indexed_structure(document_id, [("Section", "new")]) == 1
    elif writer == "migration":
        assert await store.migrate_indexed_structure([(document_id, "Section", "new")]) == 1
    elif writer == "rebuild":

        async def old_vector(conn):
            return False

        monkeypatch.setattr(store, "_vector_is_current", old_vector)
        await store.migrate_indexed_structure([])
    else:
        await store.remove_document(document_id)
    after, chunks = await store.read_processing_snapshot(document_id)
    assert after.attempt > a
    assert after.keyword_indexing.scope == "unknown"
    assert after.keyword_indexing.generation == after.attempt
    assert after.keyword_indexing.limitations == ["artifact_rewritten_without_coverage_evidence"]
    assert not await delayed.publish_processing(document_id, a, evidence, chunks=_chunks("stale"))
    assert (await store.read_processing_snapshot(document_id)) == (after, chunks)
    assert not await delayed.update_processing_status(
        document_id, a, {"pipeline_status": "abstraction_complete"}
    )
    if writer not in ("chunks", "remove_document"):
        assert after.projection.generation == a


@pytest.mark.parametrize("mode", ["keyword", "semantic", "hybrid"])
@pytest.mark.parametrize("old_abstract", [None, "abstract old"])
async def test_scored_reads_keep_abstract_and_evidence_on_the_same_snapshot(
    pg_pool,
    postgres_graph_store,
    minimal_config,
    stub_embedding_provider,
    monkeypatch,
    mode,
    old_abstract,
):
    document_id = _doc(1).id
    await postgres_graph_store.insert_document(_doc(1))
    reader, writer = PostgresContentStore(pg_pool), PostgresContentStore(pg_pool)
    vector = [1.0] + [0.0] * (EMBEDDING_DIM - 1)
    chunks = _chunks("needle")
    chunks[0].embedding = vector

    def dim():
        return CoverageDimension(scope="limited", identity=_identity())

    a = await writer.begin_processing_attempt(document_id)
    assert await writer.publish_processing(
        document_id,
        a,
        {
            name: dim()
            for name in (
                "projection",
                "keyword_indexing",
                "passage_embeddings",
                "abstraction_evidence",
            )
        },
        chunks=chunks,
        surface=DocumentSurface(
            document_id=document_id,
            matchable="needle",
            orienting=old_abstract or "",
            embedding=vector,
        ),
        semantic_abstract=old_abstract,
    )

    async def embed(texts):
        return [vector for _ in texts]

    monkeypatch.setattr(stub_embedding_provider, "embed", embed)
    service = RetrievalService(
        postgres_graph_store, reader, stub_embedding_provider, minimal_config
    )
    target, name = (
        (service, "_hybrid_rrf")
        if mode == "hybrid"
        else (reader, "search_bm25" if mode == "keyword" else "search_semantic")
    )
    original = getattr(target, name)

    async def replace_after_search(*args, **kwargs):
        results = await original(*args, **kwargs)
        assert results
        b = await writer.begin_processing_attempt(document_id)
        assert await writer.publish_processing(
            document_id,
            b,
            {"abstraction_evidence": dim()},
            surface=DocumentSurface(
                document_id=document_id,
                matchable="needle",
                orienting="abstract new",
                embedding=vector,
            ),
            semantic_abstract="abstract new",
        )
        return results

    monkeypatch.setattr(target, name, replace_after_search)
    response = await service.discover(
        DiscoverRequest(
            mode="keyword" if mode == "keyword" else "semantic",
            use_hybrid=mode == "hybrid",
            query="needle",
            response_mode="full",
            include_abstracts=True,
        )
    )
    hit = response.results[0]
    assert hit.document.semantic_abstract == old_abstract
    assert hit.document.processing_coverage.abstraction_evidence.generation == a
    latest = await postgres_graph_store.get_document(document_id)
    assert latest.semantic_abstract == "abstract new"
    assert latest.processing_coverage.abstraction_evidence.generation > a


@pytest.mark.parametrize(
    "defaults,overrides,expected",
    [
        ({"dialect": "pandoc"}, None, {"dialect": "pandoc"}),
        ({}, {"dialect": "gfm"}, {"dialect": "gfm"}),
        (
            {"dialect": "pandoc", "nested": {"base": 1, "collision": "vault"}},
            {"nested": {"extra": 2, "collision": "request"}},
            {"dialect": "pandoc", "nested": {"base": 1, "extra": 2, "collision": "request"}},
        ),
        (
            {"dialect": "pandoc", "nested": {"base": 1}},
            None,
            {"dialect": "pandoc", "nested": {"base": 1}},
        ),
    ],
)
async def test_retention_identity_records_the_effective_attempt_configuration(
    ingestion_service, tmp_vault_dir, monkeypatch, defaults, overrides, expected
):
    import sage.services.ingestion as ingestion

    ingestion_service._config.adapter_defaults = {"markdown": deepcopy(defaults)}
    original = ingestion._run_projection
    captured = []

    async def project(adapter, path, config):
        captured.append(deepcopy(config or {}))
        ingestion_service._config.adapter_defaults["markdown"]["changed_after_attempt"] = True
        if "nested" in defaults:
            ingestion_service._config.adapter_defaults["markdown"]["nested"]["base"] = 99
        return await original(adapter, path, config)

    monkeypatch.setattr(ingestion, "_run_projection", project)
    source = tmp_vault_dir / "identity.md"
    source.write_text("# Source\n\nConfiguration provenance.")
    result = await ingestion_service.ingest(IngestRequest(source=str(source), config=overrides))
    actual = result.document.processing_coverage.source_retention.identity.configuration
    assert captured and actual == captured[0] == expected
    assert actual != ingestion_service._merge_adapter_config(result.document.source_type, overrides)
    assert result.document.adapter_config == overrides


async def test_deterministic_summary_uses_the_passage_snapshot_abstract(
    pg_pool, postgres_graph_store, minimal_config, stub_embedding_provider, monkeypatch
):
    document_id = _doc(1).id
    await postgres_graph_store.insert_document(_doc(1))
    reader, writer = PostgresContentStore(pg_pool), PostgresContentStore(pg_pool)

    def dim():
        return CoverageDimension(scope="limited", identity=_identity())

    a = await writer.begin_processing_attempt(document_id)
    assert await writer.publish_processing(
        document_id,
        a,
        {
            name: dim()
            for name in (
                "projection",
                "keyword_indexing",
                "passage_embeddings",
                "abstraction_evidence",
            )
        },
        chunks=_chunks("old"),
        semantic_abstract="abstract old",
    )
    original = postgres_graph_store.get_document
    published = []

    async def replace_after_document_read(doc_id):
        doc = await original(doc_id)
        if not published:
            b = await writer.begin_processing_attempt(document_id)
            published.append(b)
            assert await writer.publish_processing(
                document_id, b, {"abstraction_evidence": dim()}, semantic_abstract="abstract new"
            )
        return doc

    monkeypatch.setattr(postgres_graph_store, "get_document", replace_after_document_read)
    service = RetrievalService(
        postgres_graph_store, reader, stub_embedding_provider, minimal_config
    )
    response = await service.discover(
        DiscoverRequest(
            mode="deterministic",
            document_id=document_id,
            heading_path="Section",
            response_mode="full",
            include_abstracts=True,
        )
    )
    assert response.results[0].document.semantic_abstract == "abstract new"
    assert (
        response.results[0].document.processing_coverage.abstraction_evidence.generation
        == published[0]
        > a
    )


async def test_indexing_reconciles_metadata_without_fencing_its_own_publication(
    ingestion_service, pg_pool, tmp_vault_dir, monkeypatch
):
    from sage.services.passage_structure import indexed_structure
    from tests.helpers.pipeline_wait import drain_abstraction_queue

    ingestion_service._content_store = PostgresContentStore(pg_pool)
    original = ingestion_service._embedding.embed
    changed = []

    async def patch_during_embedding(texts):
        if not changed:
            docs, _ = await ingestion_service._store.query_documents()
            assert len(docs) == 1
            document_id = docs[0].id
            changed.append(document_id)
            await ingestion_service._store.update_document(
                document_id, {"title": "Renamed", "doc_type": "memo"}
            )
        return await original(texts)

    monkeypatch.setattr(ingestion_service._embedding, "embed", patch_during_embedding)
    source = tmp_vault_dir / "midway.md"
    source.write_text("# Original\n\n## Part\n\nBody.")
    result = await ingestion_service.ingest(IngestRequest(source=str(source)))
    await drain_abstraction_queue(ingestion_service)
    document = await ingestion_service._store.get_document(result.document.id)
    coverage, chunks = await ingestion_service._content_store.read_processing_snapshot(document.id)
    assert changed == [document.id] and document.title == "Renamed"
    assert document.pipeline_status.value == "abstraction_complete"
    assert chunks and {chunk.doc_type for chunk in chunks} == {"memo"}
    assert all(
        chunk.indexed_structure == indexed_structure(chunk.heading_path, "Renamed")
        for chunk in chunks
    )
    assert (
        coverage.attempt == coverage.projection.generation == coverage.keyword_indexing.generation
    )
