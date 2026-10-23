"""Inspection respects durable stage intent and its own admitted operation."""

import asyncio
import json
import logging
import threading
from pathlib import Path

import pytest

from sage.adapters.content_store_postgres import PostgresContentStore
from sage.instrumentation.performance import scope
from sage.models.schemas import IngestRequest
from tests.helpers.pipeline_wait import drain_abstraction_queue


@pytest.mark.parametrize("inline", [False, True])
@pytest.mark.parametrize("race", [False, True])
async def test_inspection_uses_atomic_admission_and_preserves_indexing_checkpoint(
    ingestion_service, pg_pool, tmp_vault_dir, monkeypatch, inline, race
):
    service = ingestion_service
    graph = service._store
    store = PostgresContentStore(pg_pool)
    service._content_store = store
    source = tmp_vault_dir / "inspection-admission.md"
    source.write_text("# Inspection admission\n\nAuthored evidence remains exact.")
    admitted, observed, newer = [], [], []
    insert, publish = graph.insert_document, store.publish_processing

    async def insertion(doc, **kwargs):
        result = await insert(doc, **kwargs)
        row = await graph.get_document(doc.id)
        assert await graph.get_processing_stage(doc.id) == "indexing"
        admitted.append(row.processing_coverage.attempt)
        return result

    async def publication(document_id, attempt, dimensions, **kwargs):
        if "inspection" in dimensions:
            assert admitted == [attempt]
            assert await graph.get_processing_stage(document_id) == "indexing"
            if race:
                newer.append(
                    await graph.update_document(
                        document_id,
                        {"pipeline_status": "abstraction_in_progress"},
                        processing_stage="abstraction",
                    )
                )
            result = await publish(document_id, attempt, dimensions, **kwargs)
            observed.append(result)
            assert await graph.get_processing_stage(document_id) == (
                "abstraction" if race else "indexing"
            )
            return result
        return await publish(document_id, attempt, dimensions, **kwargs)

    monkeypatch.setattr(graph, "insert_document", insertion)
    monkeypatch.setattr(store, "publish_processing", publication)
    result = await service.ingest(IngestRequest(source=str(source)), wait_for_pipeline=inline)
    await drain_abstraction_queue(service)
    after = await graph.get_document(result.document.id)
    assert admitted == [1]
    assert observed == [not race]
    if race:
        assert len(newer) == 1
        assert after == newer[0]
        assert after.processing_coverage.inspection.scope == "unknown"
        assert await store.get_all_chunks(after.id) == []
    else:
        assert after.pipeline_status.value == "abstraction_complete"
        assert after.processing_coverage.inspection.generation == admitted[0]
        assert after.processing_coverage.projection.generation == admitted[0]
        assert after.processing_coverage.inspection.selection_summary.selected_inputs > 0
        assert await graph.get_processing_stage(after.id) == "abstraction"
    assert not service._inflight


async def test_committed_index_retry_keeps_inspection_without_new_source_work(
    ingestion_service, pg_pool, tmp_vault_dir, monkeypatch
):
    import sage.services.ingestion as ingestion

    service = ingestion_service
    graph = service._store
    store = PostgresContentStore(pg_pool)
    service._content_store = store
    service._config.abstraction.max_attempts = 2
    monkeypatch.setattr(service, "_compute_backoff", lambda attempt: 0)
    source = tmp_vault_dir / "inspection-retry.md"
    source.write_text("# Original inspected heading\n\nOriginal inspected authored evidence.")
    project, embed, generate = (
        ingestion._run_inspected_projection,
        service._embed_chunks,
        service._generate_abstract_text,
    )
    projected, embedded, generated, inspections = [], [], [], []

    async def projection(*args, **kwargs):
        result = await project(*args, **kwargs)
        projected.append(result.discovery_evidence.identity)
        return result

    async def embedding(chunks):
        embedded.append(True)
        return await embed(chunks)

    async def generation(text, *args, **kwargs):
        generated.append(text)
        rows = await graph.list_all_documents()
        inspections.append(rows[0].processing_coverage.inspection.model_dump(mode="json"))
        assert await graph.get_processing_stage(rows[0].id) == "abstraction"
        if len(generated) == 1:
            source.write_text(
                "# Changed source\n\nNew content must not be inspected by this retry."
            )
            raise RuntimeError("controlled Stage3 retry")
        return await generate(text, *args, **kwargs)

    monkeypatch.setattr(ingestion, "_run_inspected_projection", projection)
    monkeypatch.setattr(service, "_embed_chunks", embedding)
    monkeypatch.setattr(service, "_generate_abstract_text", generation)
    result = await service.ingest(IngestRequest(source=str(source)), wait_for_pipeline=False)
    await drain_abstraction_queue(service)
    after = await graph.get_document(result.document.id)
    assert after.pipeline_status.value == "abstraction_complete"
    assert len(projected) == len(embedded) == 1
    assert len(generated) == 2
    assert (
        inspections[0]
        == inspections[1]
        == after.processing_coverage.inspection.model_dump(mode="json")
    )
    assert after.processing_coverage.inspection.identity == projected[0].processing_identity()
    assert all("Original inspected" in text and "Changed source" not in text for text in generated)


async def test_abstract_checkpoint_recovery_preserves_inspection_without_source(
    ingestion_service, pg_pool, tmp_vault_dir, monkeypatch
):
    service = ingestion_service
    graph = service._store
    store = PostgresContentStore(pg_pool)
    service._content_store = store
    source = tmp_vault_dir / "inspection-recovery.md"
    source.write_text("# Retained inspected heading\n\nAlready inspected retained evidence.")
    result = await service.ingest(IngestRequest(source=str(source)))
    before = await graph.get_document(result.document.id)
    inspection = before.processing_coverage.inspection
    chunks = await store.get_all_chunks(before.id)
    assert inspection.selection_summary.selected_inputs > 0
    assert await graph.get_processing_stage(before.id) == "abstraction"
    retained_source = Path(service._config.vault.storage_root) / before.source_path
    assert retained_source.exists()
    retained_source.unlink()
    source.unlink()
    assert not retained_source.exists()
    admitted = await graph.update_document(
        before.id,
        {"pipeline_status": "abstraction_interrupted"},
        processing_stage="abstraction",
        advance_version=False,
    )
    assert admitted.processing_coverage.attempt > before.processing_coverage.attempt
    assert await service.recover_incomplete_documents() == 1
    await drain_abstraction_queue(service)
    after = await graph.get_document(before.id)
    assert after.pipeline_status.value == "abstraction_complete"
    assert after.processing_coverage.inspection == inspection
    assert after.processing_coverage.projection == before.processing_coverage.projection
    assert await store.get_all_chunks(before.id) == chunks
    assert not source.exists()
    assert not retained_source.exists()


async def test_cancelled_inspection_trace_waits_for_actual_worker_completion(
    tmp_path, monkeypatch, caplog
):
    import sage.services.ingestion as ingestion
    from sage.source_adapters.markdown_adapter import MarkdownAdapter

    source = tmp_path / "inspection-worker.md"
    source.write_text("# Inspection worker\n\nActual authored evidence.")
    entered, release, completed = threading.Event(), threading.Event(), threading.Event()
    selector = ingestion.select_discovery

    def controlled(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        try:
            return selector(*args, **kwargs)
        finally:
            completed.set()

    def records():
        return [
            json.loads(row.message)
            for row in caplog.records
            if row.name == "sage.ingestion.performance"
        ]

    monkeypatch.setattr(ingestion, "select_discovery", controlled)
    caplog.set_level(logging.INFO, logger="sage.ingestion.performance")

    async def caller():
        with scope("test_vault", "inspection-worker", "inspection"):
            await ingestion._run_inspected_projection(MarkdownAdapter(), source, None)

    task = asyncio.create_task(caller())
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not completed.is_set()
        assert not records(), "trace cannot finish while inspection thread still owns work"
    finally:
        release.set()
        assert await asyncio.to_thread(completed.wait, 2)
        # Drain the same bounded executor so its completion callbacks settle.
        await asyncio.get_running_loop().run_in_executor(
            ingestion._projection_executor(), lambda: None
        )
    [row] = records()
    assert row["outcome"] == "cancelled"
    assert row["document_id"] == "inspection-worker"
    assert row["stages"]["inspection.execute"]["count"] == 1
