"""Artifact invalidation settles superseded work and returns its final evidence."""

import asyncio

import pytest

from sage.adapters.content_store_postgres import PostgresContentStore
from sage.models.schemas import BulkMetadataRequest, IngestRequest
from sage.services.ingestion import IngestionService
from sage.services.metadata import MetadataService
from tests.helpers.pipeline_wait import drain_abstraction_queue


@pytest.mark.parametrize("patch", [{"title": "Renamed"}, {"tags": {"add": ["fresh"]}}])
@pytest.mark.parametrize("stage", ["initial_index", "replacement_index", "abstract"])
async def test_metadata_during_active_processing_settles_the_owned_attempt(
    ingestion_service, pg_pool, tmp_vault_dir, monkeypatch, caplog, patch, stage
):
    service = ingestion_service
    service._content_store = PostgresContentStore(pg_pool)
    metadata = MetadataService(
        service._store, service._locks, service._config, service._content_store
    )
    source = tmp_vault_dir / "processing.md"
    source.write_text("# Original\n\n## Part\n\nSource body.")
    seeded = None
    if stage == "replacement_index":
        seeded = await service.ingest(IngestRequest(source=str(source)))
    entered, release = asyncio.Event(), asyncio.Event()
    calls = []
    target = "_generate_abstract_text" if stage == "abstract" else "_embed_chunks"
    original = getattr(service, target)

    async def held(*args, **kwargs):
        calls.append(True)
        entered.set()
        await release.wait()
        return await original(*args, **kwargs)

    async def no_backoff(seconds):
        pass

    monkeypatch.setattr(service, target, held)
    monkeypatch.setattr(service, "_sleep_for_backoff", no_backoff)
    if seeded:
        document_id = seeded.document.id
        await service.recompute_pipeline(document_id)
    else:
        result = await service.ingest(IngestRequest(source=str(source)), wait_for_pipeline=False)
        document_id = result.document.id
    try:
        await asyncio.wait_for(entered.wait(), 5)
        before = await service._store.get_document(document_id)
        changed = await metadata.bulk_update_metadata(
            BulkMetadataRequest(items=[{"document_id": document_id, **patch}], response_mode="full")
        )
        assert changed.success_count == 1
    finally:
        release.set()
    await drain_abstraction_queue(service)
    after = await service._store.get_document(document_id)
    assert not service._inflight
    if "title" in patch:
        assert after.title == "Renamed"
    else:
        assert "fresh" in after.tags
    assert not any("unhandled error" in r.message for r in caplog.records)
    assert len(calls) == 1
    if stage == "initial_index":
        assert after.pipeline_status.value == "abstraction_complete"
    else:
        assert after.pipeline_status.value == "abstraction_interrupted"
        assert after.processing_coverage.attempt > before.processing_coverage.attempt
        assert after.pipeline_error and "artifact" in after.pipeline_error
        assert after.semantic_abstract == before.semantic_abstract
        assert await service.recover_incomplete_documents() == 1
        await drain_abstraction_queue(service)
        recovered = await service._store.get_document(document_id)
        assert recovered.pipeline_status.value == "abstraction_complete"
        assert recovered.pipeline_error is None
        assert recovered.processing_coverage.attempt > after.processing_coverage.attempt


@pytest.mark.parametrize("established", [False, True])
@pytest.mark.parametrize("patch", [{"title": "Renamed"}, {"tags": {"add": ["fresh"]}}])
async def test_metadata_full_response_is_captured_after_invalidation_inside_the_lock(
    ingestion_service, pg_pool, tmp_vault_dir, monkeypatch, established, patch
):
    from sage.models.coverage import CoverageDimension
    from sage.models.wire import to_wire

    service = ingestion_service
    store = PostgresContentStore(pg_pool)
    service._content_store = store
    metadata = MetadataService(service._store, service._locks, service._config, store)
    source = tmp_vault_dir / "response.md"
    source.write_text("# Original\n\nSource body.")
    result = await service.ingest(IngestRequest(source=str(source)))
    document_id = result.document.id
    if established:
        coverage, chunks = await store.read_processing_snapshot(document_id)
        attempt = await store.begin_processing_attempt(document_id)
        assert await store.publish_processing(
            document_id,
            attempt,
            {
                n: CoverageDimension(scope="limited", identity=coverage.source_retention.identity)
                for n in (
                    "projection",
                    "keyword_indexing",
                    "passage_embeddings",
                    "abstraction_evidence",
                )
            },
            chunks=chunks,
        )
    before = await service._store.get_document(document_id)
    assert before.processing_coverage.keyword_indexing.scope == (
        "limited" if established else "unknown"
    )
    changed = []
    reads = []
    in_call = True
    update = store.update_indexed_structure
    read = service._store.get_document

    async def track_update(*args, **kwargs):
        value = await update(*args, **kwargs)
        changed.append(True)
        return value

    async def track_read(doc_id):
        if in_call and changed and doc_id == document_id:
            reads.append(service._locks.lock(doc_id).locked())
        return await read(doc_id)

    monkeypatch.setattr(store, "update_indexed_structure", track_update)
    monkeypatch.setattr(service._store, "get_document", track_read)
    response = await metadata.bulk_update_metadata(
        BulkMetadataRequest(items=[{"document_id": document_id, **patch}], response_mode="full")
    )
    in_call = False
    current = await service._store.get_document(document_id)
    delivered = response.results[0].document
    assert delivered.processing_coverage == current.processing_coverage
    assert reads and all(reads)
    assert current.processing_coverage.attempt > before.processing_coverage.attempt
    wire = to_wire(response)["results"][0]["document"]
    assert wire["processing_coverage"] == to_wire(current)["processing_coverage"]
    assert delivered.title == current.title and delivered.tags == current.tags


@pytest.mark.parametrize("replacement", ["metadata", "passages"])
@pytest.mark.parametrize("old_outcome", ["success", "retryable", "nonretryable"])
async def test_superseded_worker_cannot_settle_a_newer_live_worker(
    ingestion_service, pg_pool, tmp_vault_dir, monkeypatch, caplog, replacement, old_outcome
):
    service = ingestion_service
    store = PostgresContentStore(pg_pool)
    service._content_store = store
    source = tmp_vault_dir / "ownership.md"
    source.write_text("# Original\n\nSource body.")
    result = await service.ingest(IngestRequest(source=str(source)))
    document_id = result.document.id
    old_started, old_release, new_started, new_release = (asyncio.Event() for _ in range(4))

    async def old_generate(*args, **kwargs):
        old_started.set()
        await old_release.wait()
        if old_outcome == "retryable":
            raise RuntimeError("controlled obsolete provider error")
        if old_outcome == "nonretryable":
            from sage.adapters.interfaces import NonRetryableAbstractionError

            raise NonRetryableAbstractionError("controlled obsolete refusal")
        return "Obsolete abstract."

    async def new_generate(*args, **kwargs):
        new_started.set()
        await new_release.wait()
        return "Current abstract."

    async def no_backoff(seconds):
        pass

    monkeypatch.setattr(service, "_generate_abstract_text", old_generate)
    monkeypatch.setattr(service, "_sleep_for_backoff", no_backoff)
    newer = IngestionService(
        graph_store=service._store,
        lock_manager=service._locks,
        content_store=PostgresContentStore(pg_pool),
        embedding_provider=service._embedding,
        abstraction_provider=service._abstraction,
        config=service._config,
        source_adapters=service._adapters,
    )
    monkeypatch.setattr(newer, "_generate_abstract_text", new_generate)
    await service.reabstract(document_id)
    try:
        await asyncio.wait_for(old_started.wait(), 5)
        if replacement == "metadata":
            metadata = MetadataService(service._store, service._locks, service._config, store)
            await metadata.bulk_update_metadata(
                BulkMetadataRequest(items=[{"document_id": document_id, "title": "Renamed"}])
            )
        else:
            chunks = await store.get_all_chunks(document_id)
            chunks[0].content = "Replacement body."
            await store.index_chunks(document_id, chunks)
        interrupted = await service._store.get_document(document_id)
        assert interrupted.pipeline_status.value == "abstraction_interrupted"
        await newer.reabstract(document_id)
        await asyncio.wait_for(new_started.wait(), 5)
        current = await service._store.get_document(document_id)
        old_release.set()
        await drain_abstraction_queue(service)
        still_current = await service._store.get_document(document_id)
        assert still_current.processing_coverage.attempt == current.processing_coverage.attempt
        assert still_current.pipeline_status.value == "abstraction_in_progress"
        assert still_current.pipeline_error == current.pipeline_error
        assert newer._inflight and not service._inflight
        assert still_current.semantic_abstract != "Obsolete abstract."
        assert not any("unhandled error" in r.message for r in caplog.records)
    finally:
        old_release.set()
        new_release.set()
        await drain_abstraction_queue(service)
        await drain_abstraction_queue(newer)
        await newer.stop_worker()
    final = await service._store.get_document(document_id)
    assert final.pipeline_status.value == "abstraction_complete"
    assert final.semantic_abstract == "Current abstract."


@pytest.mark.parametrize("provider_error", [False, True])
async def test_inline_invalidation_returns_interruption_without_obsolete_failure_stamp(
    ingestion_service, pg_pool, tmp_vault_dir, monkeypatch, caplog, provider_error
):
    service = ingestion_service
    service._content_store = PostgresContentStore(pg_pool)
    entered, release = asyncio.Event(), asyncio.Event()

    async def held(*args, **kwargs):
        entered.set()
        await release.wait()
        if provider_error:
            raise RuntimeError("controlled provider error after invalidation")
        return "Obsolete abstract."

    monkeypatch.setattr(service, "_generate_abstract_text", held)
    source = tmp_vault_dir / "inline.md"
    source.write_text("# Original\n\nSource body.")
    task = asyncio.create_task(service.ingest(IngestRequest(source=str(source))))
    try:
        await asyncio.wait_for(entered.wait(), 5)
        docs = await service._store.list_all_documents()
        assert len(docs) == 1
        metadata = MetadataService(
            service._store, service._locks, service._config, service._content_store
        )
        await metadata.bulk_update_metadata(
            BulkMetadataRequest(items=[{"document_id": docs[0].id, "title": "Renamed"}])
        )
    finally:
        release.set()
    result = await task
    assert result.document.pipeline_status.value == "abstraction_interrupted"
    assert "artifact" in result.document.pipeline_error
    assert result.document.semantic_abstract != "Obsolete abstract."
    if provider_error:
        assert any(
            record.exc_info
            and "controlled provider error after invalidation" in str(record.exc_info[1])
            for record in caplog.records
        )
    assert not service._inflight


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
async def test_each_artifact_writer_settles_invalidated_execution_atomically(
    pg_pool, postgres_graph_store, monkeypatch, writer
):
    from sage.adapters.interfaces import DocumentSurface
    from tests.sage.test_processing_coverage import _chunks, _doc, _evidence

    document_id = _doc(1).id
    await postgres_graph_store.insert_document(_doc(1))
    store = PostgresContentStore(pg_pool)
    attempt = await store.begin_processing_attempt(document_id)
    assert await store.publish_processing(
        document_id,
        attempt,
        _evidence(),
        chunks=_chunks("old"),
        surface=DocumentSurface(document_id=document_id, matchable="old", orienting="old"),
    )
    if writer == "chunks":
        await store.index_chunks(document_id, _chunks("new"))
    elif writer == "surface":
        await store.upsert_document_surface(
            DocumentSurface(document_id=document_id, matchable="new", orienting="new")
        )
    elif writer == "remove_surface":
        await store.remove_document_surface(document_id)
    elif writer == "surface_text":
        assert await store.update_document_surface_text(document_id, "new", "new")
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
    after = await postgres_graph_store.get_document(document_id)
    assert after.pipeline_status.value == "abstraction_interrupted"
    assert "artifact" in after.pipeline_error
    assert after.processing_coverage.attempt > attempt
    assert not await store.update_processing_status(
        document_id, attempt, {"pipeline_status": "abstraction_complete"}
    )


async def test_failed_artifact_rewrite_rolls_back_interruption_and_fence(
    pg_pool, postgres_graph_store
):
    from psycopg.errors import RaiseException

    from tests.sage.test_processing_coverage import _chunks, _doc, _evidence

    document_id = _doc(1).id
    await postgres_graph_store.insert_document(_doc(1))
    store = PostgresContentStore(pg_pool)
    attempt = await store.begin_processing_attempt(document_id)
    assert await store.publish_processing(document_id, attempt, _evidence(), chunks=_chunks("old"))
    assert await store.update_processing_status(
        document_id, attempt, {"pipeline_status": "abstraction_in_progress"}
    )
    async with pg_pool.connection() as conn:
        await conn.execute(
            "CREATE FUNCTION refuse_rewrite() RETURNS trigger LANGUAGE plpgsql AS $$ "
            "BEGIN RAISE EXCEPTION 'injected artifact rewrite failure'; END $$"
        )
        await conn.execute(
            "CREATE TRIGGER refuse_rewrite BEFORE DELETE ON chunks FOR EACH ROW "
            "EXECUTE FUNCTION refuse_rewrite()"
        )
    try:
        with pytest.raises(RaiseException, match="injected artifact rewrite failure"):
            await store.index_chunks(document_id, _chunks("new"))
        after = await postgres_graph_store.get_document(document_id)
        assert after.processing_coverage.attempt == attempt
        assert after.pipeline_status.value == "abstraction_in_progress"
        assert (await store.get_all_chunks(document_id))[0].content == "old"
        assert await store.update_processing_status(
            document_id, attempt, {"pipeline_status": "abstraction_complete"}
        )
    finally:
        async with pg_pool.connection() as conn:
            await conn.execute("DROP TRIGGER refuse_rewrite ON chunks")
            await conn.execute("DROP FUNCTION refuse_rewrite()")


async def test_interruption_report_does_not_infer_provider_failure_from_supersession(
    ingestion_service, pg_pool, tmp_vault_dir, monkeypatch, caplog
):
    from tests.sage.test_reabstract_deferred_service import _build_maintenance

    service = ingestion_service
    service._content_store = PostgresContentStore(pg_pool)
    service._config.abstraction.enabled = False
    source = tmp_vault_dir / "report.md"
    source.write_text("# Original\n\nSource body.")
    result = await service.ingest(IngestRequest(source=str(source)))
    assert result.document.pipeline_status.value == "abstraction_skipped"
    service._config.abstraction.enabled = True
    entered, release = asyncio.Event(), asyncio.Event()

    async def held(*args, **kwargs):
        entered.set()
        await release.wait()
        return "Obsolete abstract."

    monkeypatch.setattr(service, "_generate_abstract_text", held)
    maintenance = _build_maintenance(
        graph_store=service._store,
        config=service._config,
        content_store=service._content_store,
        ingestion_service=service,
    )
    sweep = asyncio.create_task(maintenance.reabstract_deferred())
    try:
        await asyncio.wait_for(entered.wait(), 5)
        metadata = MetadataService(
            service._store, service._locks, service._config, service._content_store
        )
        await metadata.bulk_update_metadata(
            BulkMetadataRequest(items=[{"document_id": result.document.id, "title": "Renamed"}])
        )
    finally:
        release.set()
    report = await asyncio.wait_for(sweep, 10)
    await drain_abstraction_queue(service)
    assert not service._inflight
    assert not any("unhandled error" in record.message for record in caplog.records)
    assert report.failed_count == 1 and report.reabstracted_count == 0
    assert report.entries[0].outcome.value == "interrupted"
    assert "No provider failure is inferred" in report.entries[0].error_message
    assert "No provider was reached" not in report.entries[0].error_message


async def test_bulk_mutation_items_keep_their_own_final_snapshot(
    pg_pool, postgres_graph_store, lock_manager, minimal_config
):
    from tests.sage.test_processing_coverage import _chunks, _doc, _evidence

    document_id = _doc(1).id
    await postgres_graph_store.insert_document(_doc(1))
    store = PostgresContentStore(pg_pool)
    attempt = await store.begin_processing_attempt(document_id)
    assert await store.publish_processing(document_id, attempt, _evidence(), chunks=_chunks("old"))
    metadata = MetadataService(postgres_graph_store, lock_manager, minimal_config, store)
    response = await metadata.bulk_update_metadata(
        BulkMetadataRequest(
            items=[
                {"document_id": document_id, "title": "First"},
                {"document_id": document_id, "title": "Second"},
            ],
            response_mode="full",
        )
    )
    assert len(response.results) == 2
    first, second = (item.document for item in response.results)
    assert first.title == "First" and second.title == "Second"
    assert attempt < first.processing_coverage.attempt < second.processing_coverage.attempt
    current = await postgres_graph_store.get_document(document_id)
    assert current.processing_coverage == second.processing_coverage
    assert (
        first.processing_coverage.keyword_indexing.generation
        < current.processing_coverage.keyword_indexing.generation
    )


@pytest.mark.parametrize(
    "status,interrupted",
    [
        ("projection_complete", True),
        ("indexing_in_progress", True),
        ("indexing_complete", True),
        ("abstraction_in_progress", True),
        ("abstraction_complete", False),
        ("abstraction_skipped", False),
        ("abstraction_interrupted", False),
        ("failed", False),
    ],
)
async def test_invalidation_preserves_terminal_outcomes_and_settles_pending_states(
    pg_pool, postgres_graph_store, status, interrupted
):
    from tests.sage.test_processing_coverage import _chunks, _doc, _evidence

    document_id = _doc(1).id
    await postgres_graph_store.insert_document(_doc(1))
    store = PostgresContentStore(pg_pool)
    attempt = await store.begin_processing_attempt(document_id)
    assert await store.publish_processing(document_id, attempt, _evidence(), chunks=_chunks("old"))
    assert await store.update_processing_status(
        document_id, attempt, {"pipeline_status": status, "pipeline_error": "prior cause"}
    )
    await store.index_chunks(document_id, _chunks("new"))
    after = await postgres_graph_store.get_document(document_id)
    assert (await store.get_all_chunks(document_id))[0].content == "new"
    assert after.processing_coverage.attempt > attempt
    if interrupted:
        assert after.pipeline_status.value == "abstraction_interrupted"
        assert after.pipeline_error == "processing attempt was superseded by an artifact rewrite"
    else:
        assert after.pipeline_status.value == status
        assert after.pipeline_error == "prior cause"
