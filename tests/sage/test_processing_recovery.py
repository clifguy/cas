"""Recovery must distinguish retained passages from the requested projection."""

import asyncio

import pytest

from sage.adapters.content_store_postgres import PostgresContentStore
from sage.models.schemas import BulkMetadataRequest, IngestRequest
from sage.services.ingestion import IngestionService
from sage.services.metadata import MetadataService
from tests.helpers.pipeline_wait import drain_abstraction_queue


@pytest.mark.parametrize(
    "entry,stage,interruption",
    [
        ("recompute", 2, "none"),
        ("recompute", 2, "shutdown"),
        ("recompute", 2, "metadata"),
        ("force", 2, "shutdown"),
        ("force", 2, "metadata"),
        ("initial", 2, "shutdown"),
        ("recompute", 3, "shutdown"),
        ("recompute", 3, "metadata"),
        ("reabstract", 3, "shutdown"),
        ("reabstract", 3, "metadata"),
    ],
)
async def test_recovery_publishes_the_requested_generation_without_repeating_completed_indexing(
    ingestion_service, pg_pool, tmp_vault_dir, monkeypatch, entry, stage, interruption
):
    import sage.services.ingestion as module

    service = ingestion_service
    service._content_store = PostgresContentStore(pg_pool)
    source = tmp_vault_dir / "replacement.md"
    source.write_text("CURRENT SOURCE body.\n")
    real_project = module._run_projection
    project_calls = []

    async def old_adapter(*args, **kwargs):
        result = await real_project(*args, **kwargs)
        result.text = result.text.replace("CURRENT SOURCE", "OLD ADAPTER")
        return result

    async def current_adapter(*args, **kwargs):
        project_calls.append(True)
        return await real_project(*args, **kwargs)

    if entry != "initial":
        monkeypatch.setattr(module, "_run_projection", old_adapter)
        seeded = await service.ingest(IngestRequest(source=str(source)))
        document_id = seeded.document.id
        before = await service._store.get_document(document_id)
        assert any(
            "OLD ADAPTER" in c.content
            for c in await service._content_store.get_all_chunks(document_id)
        )
    else:
        document_id = None
        before = None
    monkeypatch.setattr(module, "_run_projection", current_adapter)
    entered, release = asyncio.Event(), asyncio.Event()
    real_embed, real_generate = service._embed_chunks, service._generate_abstract_text
    expected = "OLD ADAPTER" if entry == "reabstract" else "CURRENT SOURCE"

    async def held_embed(chunks):
        assert any(expected in c.content for c in chunks)
        if stage == 2:
            entered.set()
            await release.wait()
        return await real_embed(chunks)

    async def held_generate(text, *args, **kwargs):
        assert expected in text
        if stage == 3:
            entered.set()
            await release.wait()
        return await real_generate(text, *args, **kwargs)

    monkeypatch.setattr(service, "_embed_chunks", held_embed)
    monkeypatch.setattr(service, "_generate_abstract_text", held_generate)
    if entry == "recompute":
        await service.recompute_pipeline(document_id)
    elif entry == "reabstract":
        await service.reabstract(document_id)
    else:
        response = await service.ingest(
            IngestRequest(source=str(source), force=entry == "force"), wait_for_pipeline=False
        )
        document_id = response.document.id
    await asyncio.wait_for(entered.wait(), 5)
    held_row = await service._store.get_document(document_id)
    held_chunks = await service._content_store.get_all_chunks(document_id)
    if before and stage == 2:
        assert (
            held_row.processing_coverage.projection.generation
            > before.processing_coverage.projection.generation
        )
        assert any("CURRENT SOURCE" in c.content for c in held_chunks)
        assert held_row.processing_coverage.keyword_indexing.scope == "complete"
        assert not await service._content_store.read_embedding_vectors(
            document_id,
            [c.chunk_index for c in held_chunks],
        )
    elif stage == 3:
        assert any(expected in c.content for c in held_chunks)

    if interruption == "shutdown":
        await service.stop_worker()
        release.set()
    else:
        if interruption == "metadata":
            metadata = MetadataService(
                service._store, service._locks, service._config, service._content_store
            )
            await metadata.bulk_update_metadata(
                BulkMetadataRequest(items=[{"document_id": document_id, "title": "New title"}])
            )
        release.set()
        await drain_abstraction_queue(service)
    if interruption != "none":
        interrupted = await service._store.get_document(document_id)
        assert interrupted.pipeline_status.value == "abstraction_interrupted"
        # A fresh owner has neither the old projection nor any stage-selection memory.
        recovered = IngestionService(
            service._store,
            service._locks,
            PostgresContentStore(pg_pool),
            service._embedding,
            service._abstraction,
            service._config,
            source_adapters=service._adapters,
        )
        recovery_embeds = []
        recovery_embed = recovered._embed_chunks

        async def observe_embed(chunks):
            recovery_embeds.append([c.content for c in chunks])
            return await recovery_embed(chunks)

        monkeypatch.setattr(recovered, "_embed_chunks", observe_embed)
        old_project_calls = len(project_calls)
        assert await recovered.recover_incomplete_documents() == 1
        await drain_abstraction_queue(recovered)
        assert len(project_calls) - old_project_calls == (1 if stage == 2 else 0)
        assert len(recovery_embeds) == (1 if stage == 2 else 0)
        await recovered.stop_worker()
    current = await service._store.get_document(document_id)
    chunks = await service._content_store.get_all_chunks(document_id)
    assert current.pipeline_status.value == "abstraction_complete"
    assert any(expected in c.content for c in chunks)
    if entry != "reabstract":
        assert not any("OLD ADAPTER" in c.content for c in chunks)
    if before and entry != "reabstract":
        assert (
            current.processing_coverage.projection.generation
            > before.processing_coverage.projection.generation
        )
    if stage == 3 and entry != "reabstract":
        assert (
            current.processing_coverage.projection.generation
            == held_row.processing_coverage.projection.generation
        )
    assert current.indexed_at is not None
    assert current.indexed_at >= current.projected_at
    await service.stop_worker()


async def test_failed_replacement_recovery_keeps_the_prior_served_generation(
    ingestion_service, pg_pool, tmp_vault_dir, monkeypatch
):
    service = ingestion_service
    service._content_store = PostgresContentStore(pg_pool)
    source = tmp_vault_dir / "recover-failure.md"
    source.write_text("Prior valid body.\n")
    result = await service.ingest(IngestRequest(source=str(source)))
    document_id = result.document.id
    before = await service._store.get_document(document_id)
    old_chunks = await service._content_store.get_all_chunks(document_id)
    entered = asyncio.Event()

    async def hold(chunks):
        entered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(service, "_embed_chunks", hold)
    await service.recompute_pipeline(document_id)
    await asyncio.wait_for(entered.wait(), 5)
    await service.stop_worker()

    service._config.abstraction.max_attempts = 1

    async def fail(chunks):
        raise RuntimeError("controlled replacement embedding failure")

    monkeypatch.setattr(service, "_embed_chunks", fail)
    assert await service.recover_incomplete_documents() == 1
    await drain_abstraction_queue(service)
    after = await service._store.get_document(document_id)
    assert after.pipeline_status.value == "failed"
    assert "controlled replacement embedding failure" in after.pipeline_error
    assert (
        after.processing_coverage.projection.identity
        == before.processing_coverage.projection.identity
    )
    assert after.processing_coverage.projection.scope == "complete"
    assert (
        after.processing_coverage.projection.generation
        > before.processing_coverage.projection.generation
    )
    assert after.indexed_at == before.indexed_at
    assert await service._content_store.get_all_chunks(document_id) == old_chunks
    await service.stop_worker()


@pytest.mark.parametrize("outcome", ["publish", "stale", "rollback"])
async def test_index_timestamp_and_passages_share_the_publication_fence_and_transaction(
    pg_pool, postgres_graph_store, outcome
):
    from psycopg.errors import RaiseException

    from tests.sage.test_processing_coverage import _chunks, _doc, _evidence

    document_id = _doc(1).id
    await postgres_graph_store.insert_document(_doc(1))
    store = PostgresContentStore(pg_pool)
    attempt = await store.begin_processing_attempt(document_id)
    assert await store.publish_processing(document_id, attempt, _evidence(), chunks=_chunks("old"))
    before = await postgres_graph_store.get_document(document_id)
    assert before.indexed_at is not None
    attempt = await store.begin_processing_attempt(document_id)
    if outcome == "stale":
        await store.begin_processing_attempt(document_id)
    if outcome == "rollback":
        async with pg_pool.connection() as conn:
            await conn.execute(
                "CREATE FUNCTION refuse_index_timestamp() RETURNS trigger LANGUAGE plpgsql AS $$ "
                "BEGIN RAISE EXCEPTION 'publication refused'; END $$"
            )
            await conn.execute(
                "CREATE TRIGGER refuse_index_timestamp BEFORE UPDATE ON documents "
                "FOR EACH ROW EXECUTE FUNCTION refuse_index_timestamp()"
            )
    try:
        if outcome == "rollback":
            with pytest.raises(RaiseException, match="publication refused"):
                await store.publish_processing(
                    document_id, attempt, _evidence(), chunks=_chunks("new")
                )
        else:
            assert await store.publish_processing(
                document_id, attempt, _evidence(), chunks=_chunks("new")
            ) == (outcome == "publish")
    finally:
        if outcome == "rollback":
            async with pg_pool.connection() as conn:
                await conn.execute("DROP TRIGGER refuse_index_timestamp ON documents")
                await conn.execute("DROP FUNCTION refuse_index_timestamp()")
    after = await postgres_graph_store.get_document(document_id)
    if outcome == "publish":
        assert after.indexed_at > before.indexed_at
        assert (await store.get_all_chunks(document_id))[0].content == "new"
        assert after.processing_coverage.projection.generation == attempt
    else:
        assert after.indexed_at == before.indexed_at
        assert (await store.get_all_chunks(document_id))[0].content == "old"
        assert after.processing_coverage.projection == before.processing_coverage.projection


async def test_source_reprojection_cannot_overwrite_a_newer_attempt(
    ingestion_service, pg_pool, tmp_vault_dir
):
    service = ingestion_service
    service._content_store = PostgresContentStore(pg_pool)
    source = tmp_vault_dir / "stale-source.md"
    source.write_text("A retained source.\n")
    result = await service.ingest(IngestRequest(source=str(source)))
    document_id = result.document.id
    old = await service._content_store.begin_processing_attempt(document_id)
    newer = await service._content_store.begin_processing_attempt(document_id)
    assert await service._content_store.update_processing_status(
        document_id,
        newer,
        {"pipeline_status": "abstraction_in_progress", "pipeline_error": "new owner"},
    )
    before = await service._store.get_document(document_id)
    with pytest.raises(RuntimeError, match="stale processing observation"):
        await service._reproject_from_source(document_id, old)
    after = await service._store.get_document(document_id)
    assert after.pipeline_status == before.pipeline_status
    assert after.pipeline_error == "new owner"
    assert after.projected_at == before.projected_at
    assert after.adapter_version == before.adapter_version
    assert after.processing_coverage == before.processing_coverage


async def test_explicit_reabstract_uses_legacy_passages_without_reprojecting_source(
    ingestion_service, pg_pool, postgres_graph_store, monkeypatch
):
    from tests.sage.test_processing_coverage import _chunks, _doc

    service = ingestion_service
    service._content_store = PostgresContentStore(pg_pool)
    doc = _doc(1)
    await postgres_graph_store.insert_document(doc)
    await service._content_store.index_chunks(doc.id, _chunks("LEGACY PASSAGES"))
    assert await service._content_store.has_chunks(doc.id)
    before = await postgres_graph_store.get_document(doc.id)
    assert before.indexed_at is None and before.projected_at is None
    generated = []
    real_generate = service._generate_abstract_text

    async def observe(text, *args, **kwargs):
        generated.append(text)
        return await real_generate(text, *args, **kwargs)

    monkeypatch.setattr(service, "_generate_abstract_text", observe)
    await service.reabstract(doc.id)
    await drain_abstraction_queue(service)
    after = await postgres_graph_store.get_document(doc.id)
    assert after.pipeline_status.value == "abstraction_complete"
    assert generated == ["LEGACY PASSAGES"]
    assert after.indexed_at is None and after.projected_at is None
    assert await service._content_store.get_all_chunks(doc.id) == _chunks("LEGACY PASSAGES")
    assert after.processing_coverage.projection == before.processing_coverage.projection
    await service.stop_worker()
