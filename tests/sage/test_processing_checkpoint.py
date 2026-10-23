"""Durable stage intent is independent of the volatile queue and served scope."""

import asyncio
import json
from datetime import datetime, timedelta, timezone

import pytest
from httpx import ASGITransport, AsyncClient

from sage.adapters.content_store_postgres import PostgresContentStore
from sage.app import create_app
from sage.mcp_server import build_partitioned_server
from sage.models.enums import PipelineStatus
from sage.models.schemas import BulkMetadataRequest, IngestRequest
from sage.services.ingestion import IngestionService
from sage.services.metadata import MetadataService
from tests.helpers.pipeline_wait import drain_abstraction_queue
from tests.sage.test_processing_coverage import _chunks, _doc, _evidence
from tests.sage.test_processing_coverage_wire import _decode
from tests.sage.test_validation_envelope_parity import vault_services as _shared_services

vault_services = _shared_services


@pytest.mark.parametrize("facts", ["legacy", "older_index"])
@pytest.mark.parametrize("window", ["none", "queued", "running", "metadata"])
async def test_stored_passage_intent_survives_restart(
    ingestion_service, pg_pool, postgres_graph_store, monkeypatch, facts, window
):
    service = ingestion_service
    store = PostgresContentStore(pg_pool)
    service._content_store = store
    doc = _doc(1)
    await postgres_graph_store.insert_document(doc)
    attempt = await store.begin_processing_attempt(doc.id)
    assert await store.publish_processing(
        doc.id, attempt, _evidence(), chunks=_chunks("LEGACY PASSAGES")
    )
    now = datetime.now(timezone.utc)
    await postgres_graph_store.update_document(
        doc.id,
        {
            "projected_at": None if facts == "legacy" else now.isoformat(),
            "indexed_at": None if facts == "legacy" else (now - timedelta(days=1)).isoformat(),
        },
    )
    # A real pre-extension row has no durable checkpoint; no stage is backfilled.
    async with pg_pool.connection() as conn:
        await conn.execute(
            "UPDATE documents SET processing_resume_stage = NULL WHERE id = %s", (doc.id,)
        )
    before = await postgres_graph_store.get_document(doc.id)
    served = await store.get_all_chunks(doc.id)
    entered = asyncio.Event()
    real_generate = service._generate_abstract_text

    async def hold(text, *args, **kwargs):
        assert text == "LEGACY PASSAGES"
        entered.set()
        await asyncio.Event().wait()

    if window == "queued":
        queue = asyncio.Queue()
        service._abstraction_queue = queue
        monkeypatch.setattr(service, "_ensure_worker_running", lambda: queue)
    elif window != "none":
        monkeypatch.setattr(service, "_generate_abstract_text", hold)
    try:
        await service.reabstract(doc.id)
        admitted = await postgres_graph_store.get_document(doc.id)
        assert await postgres_graph_store.get_processing_stage(doc.id) == "abstraction"
        assert admitted.processing_coverage.attempt > before.processing_coverage.attempt
        if window != "none":
            if window != "queued":
                await asyncio.wait_for(entered.wait(), 5)
            if window == "metadata":
                metadata = MetadataService(service._store, service._locks, service._config, store)
                await metadata.bulk_update_metadata(
                    BulkMetadataRequest(items=[{"document_id": doc.id, "title": "Updated"}])
                )
            served = await store.get_all_chunks(doc.id)
            await service.stop_worker()
            stopped = await postgres_graph_store.get_document(doc.id)
            assert stopped.pipeline_status.value == "abstraction_interrupted"
            assert await postgres_graph_store.get_processing_stage(doc.id) == "abstraction"
            recovered = IngestionService(
                service._store,
                service._locks,
                PostgresContentStore(pg_pool),
                service._embedding,
                service._abstraction,
                service._config,
                source_adapters=service._adapters,
            )
            generated = []

            async def observe(text, *args, **kwargs):
                generated.append(text)
                return await real_generate(text, *args, **kwargs)

            monkeypatch.setattr(recovered, "_generate_abstract_text", observe)
            try:
                assert await recovered.recover_incomplete_documents() == 1
                await drain_abstraction_queue(recovered)
            finally:
                await recovered.stop_worker()
            assert generated == ["LEGACY PASSAGES"]
        else:
            await drain_abstraction_queue(service)
        after = await postgres_graph_store.get_document(doc.id)
        assert after.pipeline_status.value == "abstraction_complete"
        assert await store.get_all_chunks(doc.id) == served
        assert [c.content for c in served] == ["LEGACY PASSAGES"]
        assert after.processing_coverage.projection == before.processing_coverage.projection
        assert after.projected_at == before.projected_at and after.indexed_at == before.indexed_at
        assert "processing_resume_stage" not in after.model_dump()
    finally:
        await service.stop_worker()


@pytest.mark.parametrize("binding", ["postgres", "memory"])
async def test_checkpoint_admission_and_publication_bindings(
    binding, postgres_graph_store, pg_pool
):
    from sage.adapters.stubs import StubContentStore, StubGraphStore

    graph = postgres_graph_store if binding == "postgres" else StubGraphStore()
    store = PostgresContentStore(pg_pool) if binding == "postgres" else StubContentStore()
    store.bind_processing_store(graph)
    doc = _doc(1)
    await graph.insert_document(doc, processing_stage="indexing")
    assert await graph.get_processing_stage(doc.id) == "indexing"
    admitted = await graph.update_document(
        doc.id,
        {"pipeline_status": "abstraction_in_progress"},
        advance_version=False,
        processing_stage="abstraction",
    )
    attempt = admitted.processing_coverage.attempt
    assert attempt > 0
    assert await graph.get_processing_stage(doc.id) == "abstraction"
    assert (
        await graph.update_document(
            doc.id,
            {"title": "obsolete"},
            processing_stage="indexing",
            expected_processing_attempt=attempt - 1,
        )
        is None
    )
    assert (await graph.get_document(doc.id)).title == doc.title
    assert not await store.update_processing_status(
        doc.id,
        attempt - 1,
        {"pipeline_status": "indexing_in_progress"},
        processing_stage="indexing",
    )
    assert await graph.get_processing_stage(doc.id) == "abstraction"
    assert await store.update_processing_status(
        doc.id, attempt, {"pipeline_status": "indexing_in_progress"}, processing_stage="indexing"
    )
    assert await graph.get_processing_stage(doc.id) == "indexing"
    assert await store.publish_processing(doc.id, attempt, _evidence(), chunks=_chunks("new"))
    assert await graph.get_processing_stage(doc.id) == "abstraction"
    assert (await store.get_all_chunks(doc.id))[0].content == "new"


@pytest.mark.parametrize("operation", ["insert", "update"])
async def test_admission_failure_rolls_back_metadata_intent_and_attempt(
    pg_pool, postgres_graph_store, operation
):
    from psycopg.errors import RaiseException

    doc = _doc(1)
    if operation == "update":
        await postgres_graph_store.insert_document(doc, processing_stage="indexing")
        before = await postgres_graph_store.get_document(doc.id)
    async with pg_pool.connection() as conn:
        await conn.execute(
            "CREATE FUNCTION refuse_admission_tags() RETURNS trigger LANGUAGE plpgsql AS $$ "
            "BEGIN RAISE EXCEPTION 'admission refused'; END $$"
        )
        await conn.execute(
            "CREATE TRIGGER refuse_admission_tags BEFORE INSERT ON document_tags "
            "FOR EACH ROW EXECUTE FUNCTION refuse_admission_tags()"
        )
    try:
        with pytest.raises(RaiseException, match="admission refused"):
            if operation == "insert":
                await postgres_graph_store.insert_document(doc, processing_stage="indexing")
            else:
                await postgres_graph_store.update_document(
                    doc.id,
                    {"tags": ["new"], "pipeline_status": "abstraction_in_progress"},
                    processing_stage="abstraction",
                )
    finally:
        async with pg_pool.connection() as conn:
            await conn.execute("DROP TRIGGER refuse_admission_tags ON document_tags")
            await conn.execute("DROP FUNCTION refuse_admission_tags()")
    after = await postgres_graph_store.get_document(doc.id)
    if operation == "insert":
        assert after is None and await postgres_graph_store.get_processing_stage(doc.id) is None
    else:
        assert after == before
        assert await postgres_graph_store.get_processing_stage(doc.id) == "indexing"


@pytest.mark.parametrize("outcome", ["publish", "stale", "rollback"])
async def test_checkpoint_advances_only_with_fenced_atomic_stage2_publication(
    pg_pool, postgres_graph_store, outcome
):
    from psycopg.errors import RaiseException

    doc = _doc(1)
    await postgres_graph_store.insert_document(doc)
    store = PostgresContentStore(pg_pool)
    old = await store.begin_processing_attempt(doc.id)
    assert await store.publish_processing(doc.id, old, _evidence(), chunks=_chunks("old"))
    admitted = await postgres_graph_store.update_document(
        doc.id, {"pipeline_status": "indexing_in_progress"}, processing_stage="indexing"
    )
    attempt = admitted.processing_coverage.attempt
    if outcome == "stale":
        await store.begin_processing_attempt(doc.id)
    if outcome == "rollback":
        async with pg_pool.connection() as conn:
            await conn.execute(
                "CREATE FUNCTION refuse_checkpoint() RETURNS trigger LANGUAGE plpgsql AS $$ "
                "BEGIN RAISE EXCEPTION 'checkpoint refused'; END $$"
            )
            await conn.execute(
                "CREATE TRIGGER refuse_checkpoint BEFORE UPDATE ON documents "
                "FOR EACH ROW EXECUTE FUNCTION refuse_checkpoint()"
            )
    try:
        if outcome == "rollback":
            with pytest.raises(RaiseException, match="checkpoint refused"):
                await store.publish_processing(doc.id, attempt, _evidence(), chunks=_chunks("new"))
        else:
            assert await store.publish_processing(
                doc.id, attempt, _evidence(), chunks=_chunks("new")
            ) == (outcome == "publish")
    finally:
        if outcome == "rollback":
            async with pg_pool.connection() as conn:
                await conn.execute("DROP TRIGGER refuse_checkpoint ON documents")
                await conn.execute("DROP FUNCTION refuse_checkpoint()")
    after = await postgres_graph_store.get_document(doc.id)
    if outcome == "publish":
        assert await postgres_graph_store.get_processing_stage(doc.id) == "abstraction"
        assert (await store.get_all_chunks(doc.id))[0].content == "new"
        assert after.indexed_at > admitted.indexed_at
        assert after.processing_coverage.projection.generation == attempt
    else:
        assert await postgres_graph_store.get_processing_stage(doc.id) == "indexing"
        assert (await store.get_all_chunks(doc.id))[0].content == "old"
        assert after.indexed_at == admitted.indexed_at
        assert after.processing_coverage.projection == admitted.processing_coverage.projection


async def test_checkpoint_schema_upgrade_preserves_unknown_legacy_state(
    pg_pool, pg_schema, postgres_graph_store
):
    from sage.storage.postgres.schema import bootstrap_schema

    doc = _doc(1)
    await postgres_graph_store.insert_document(doc)
    async with pg_pool.connection() as conn:
        await conn.execute("ALTER TABLE documents DROP COLUMN processing_resume_stage")
    for _ in range(2):
        async with pg_pool.connection() as conn:
            await bootstrap_schema(conn, schema=pg_schema)
        read = await postgres_graph_store.get_document(doc.id)
        assert (
            read.processing_coverage.attempt == 0
            and read.processing_coverage.projection.scope == "unknown"
        )
        assert await postgres_graph_store.get_processing_stage(doc.id) is None
        assert read.pipeline_status == doc.pipeline_status


async def test_recovery_admission_cannot_steal_a_newer_live_owner(
    ingestion_service, pg_pool, postgres_graph_store, monkeypatch
):
    old = ingestion_service
    old._content_store = PostgresContentStore(pg_pool)
    doc = _doc(1)
    await postgres_graph_store.insert_document(doc)
    await old._content_store.index_chunks(doc.id, _chunks("LEGACY PASSAGES"))
    newer = IngestionService(
        old._store,
        old._locks,
        PostgresContentStore(pg_pool),
        old._embedding,
        old._abstraction,
        old._config,
        source_adapters=old._adapters,
    )
    entered, release = asyncio.Event(), asyncio.Event()
    generate = newer._generate_abstract_text

    async def hold(text, *args, **kwargs):
        assert text == "LEGACY PASSAGES"
        entered.set()
        await release.wait()
        return await generate(text, *args, **kwargs)

    monkeypatch.setattr(newer, "_generate_abstract_text", hold)
    get_stage = postgres_graph_store.get_processing_stage
    before = []
    raced = False

    async def race(document_id):
        nonlocal raced
        if not raced:
            raced = True
            await newer.reabstract(document_id)
            await asyncio.wait_for(entered.wait(), 5)
            before.append(await postgres_graph_store.get_document(document_id))
        return await get_stage(document_id)

    monkeypatch.setattr(postgres_graph_store, "get_processing_stage", race)
    try:
        assert await old.recover_incomplete_documents() == 0
        assert before and doc.id in newer._inflight
        await old.stop_worker()
        assert await postgres_graph_store.get_document(doc.id) == before[0]
        assert await get_stage(doc.id) == "abstraction"
        release.set()
        await drain_abstraction_queue(newer)
        assert (
            await postgres_graph_store.get_document(doc.id)
        ).pipeline_status.value == "abstraction_complete"
    finally:
        release.set()
        await newer.stop_worker()
        await old.stop_worker()


async def test_checkpoint_alone_never_schedules_terminal_work(
    ingestion_service, postgres_graph_store
):
    doc = _doc(1).model_copy(update={"pipeline_status": PipelineStatus.ABSTRACTION_COMPLETE})
    await postgres_graph_store.insert_document(doc, processing_stage="indexing")
    assert await postgres_graph_store.get_processing_stage(doc.id) == "indexing"
    assert await ingestion_service.recover_incomplete_documents() == 0
    assert not ingestion_service._inflight
    assert (await postgres_graph_store.get_document(doc.id)).processing_coverage.attempt == 1


@pytest.mark.parametrize("failure", ["success", "retryable", "nonretryable"])
async def test_recovered_abstract_intent_preserves_provider_retry_and_failure_semantics(
    ingestion_service, pg_pool, postgres_graph_store, monkeypatch, failure
):
    from sage.adapters.interfaces import NonRetryableAbstractionError

    service = ingestion_service
    service._content_store = PostgresContentStore(pg_pool)
    doc = _doc(1)
    await postgres_graph_store.insert_document(doc)
    await service._content_store.index_chunks(doc.id, _chunks("LEGACY PASSAGES"))
    queue = asyncio.Queue()
    service._abstraction_queue = queue
    monkeypatch.setattr(service, "_ensure_worker_running", lambda: queue)
    await service.reabstract(doc.id)
    await service.stop_worker()
    recovered = IngestionService(
        service._store,
        service._locks,
        PostgresContentStore(pg_pool),
        service._embedding,
        service._abstraction,
        service._config,
        source_adapters=service._adapters,
    )
    recovered._config.abstraction.max_attempts = 2
    calls = []
    generate = recovered._generate_abstract_text

    async def controlled(text, *args, **kwargs):
        calls.append(text)
        if failure == "nonretryable":
            raise NonRetryableAbstractionError("controlled input refusal")
        if failure == "retryable" and len(calls) == 1:
            raise RuntimeError("controlled temporary error")
        return await generate(text, *args, **kwargs)

    monkeypatch.setattr(recovered, "_generate_abstract_text", controlled)
    monkeypatch.setattr(recovered, "_compute_backoff", lambda attempt: 0)
    try:
        assert await recovered.recover_incomplete_documents() == 1
        await drain_abstraction_queue(recovered)
        after = await postgres_graph_store.get_document(doc.id)
        assert calls == ["LEGACY PASSAGES"] * (2 if failure == "retryable" else 1)
        assert await postgres_graph_store.get_processing_stage(doc.id) == "abstraction"
        assert after.pipeline_status.value == (
            "failed" if failure == "nonretryable" else "abstraction_complete"
        )
        assert after.indexed_at is None and after.projected_at is None
        assert (await recovered._content_store.get_all_chunks(doc.id))[
            0
        ].content == "LEGACY PASSAGES"
        if failure == "nonretryable":
            assert "controlled input refusal" in after.pipeline_error
            assert await recovered.recover_incomplete_documents() == 0
        else:
            assert after.pipeline_error is None
    finally:
        await recovered.stop_worker()


@pytest.mark.parametrize("kind", ["ingest", "metadata", "bulk"])
async def test_internal_checkpoint_cannot_cross_rest_or_mcp_writes(vault_services, kind):
    services = vault_services
    await services.graph_store.insert_document(_doc(1))
    original = await services.graph_store.get_processing_stage(_doc(1).id)
    app = create_app(config=services.config)
    app.state.vault_registry = {"test_vault": services}
    ordinary = build_partitioned_server("sage")
    forged = "indexing"
    if kind == "ingest":
        route, body, tool = (
            "documents",
            {"source": "missing.md", "processing_resume_stage": forged},
            "ingest_document",
        )
    elif kind == "metadata":
        route, body, tool = (
            "metadata",
            {"items": [{"document_id": _doc(1).id, "processing_resume_stage": forged}]},
            "update_metadata",
        )
    else:
        route, body, tool = (
            "documents:batch",
            {"files": [{"file_path": "missing.md", "processing_resume_stage": forged}]},
            "bulk_ingest_document",
        )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        if kind == "bulk":
            response = await client.post(
                f"/sage_vaults/test_vault/{route}",
                files={"files": ("source.md", b"hello", "text/markdown")},
                data={"metadata": json.dumps({"files": [{"processing_resume_stage": forged}]})},
            )
        else:
            response = await client.post(f"/sage_vaults/test_vault/{route}", json=body)
        assert response.status_code in (400, 422), response.text
        assert "processing_resume_stage" in response.text
    result = _decode(await ordinary.call_tool(tool, {"vault_id": "test_vault", **body}))
    assert result.get("error") or any(
        item.get("status") == "error" for item in result.get("results", [])
    ), result
    assert "processing_resume_stage" in json.dumps(result)
    assert await services.graph_store.get_processing_stage(_doc(1).id) == original


@pytest.mark.parametrize("stage", [2, 3])
async def test_retry_resumes_the_unpublished_stage_without_repeating_published_indexing(
    ingestion_service, pg_pool, tmp_vault_dir, monkeypatch, stage
):
    service = ingestion_service
    service._content_store = PostgresContentStore(pg_pool)
    source = tmp_vault_dir / "retry.md"
    source.write_text("CURRENT RETRY SOURCE body.")
    service._config.abstraction.max_attempts = 2
    monkeypatch.setattr(service, "_compute_backoff", lambda attempt: 0)
    embed, generate = service._embed_chunks, service._generate_abstract_text
    embedded, generated, generations = [], [], []

    async def controlled_embed(chunks):
        embedded.append([c.content for c in chunks])
        assert any("CURRENT RETRY SOURCE" in c.content for c in chunks)
        if stage == 2 and len(embedded) == 1:
            raise RuntimeError("controlled indexing retry")
        return await embed(chunks)

    async def controlled_generate(text, *args, **kwargs):
        generated.append(text)
        assert "CURRENT RETRY SOURCE" in text
        row = await service._store.get_document(document_id)
        generations.append(row.processing_coverage.projection.generation)
        if stage == 3 and len(generated) == 1:
            raise RuntimeError("controlled abstraction retry")
        return await generate(text, *args, **kwargs)

    monkeypatch.setattr(service, "_embed_chunks", controlled_embed)
    monkeypatch.setattr(service, "_generate_abstract_text", controlled_generate)
    try:
        response = await service.ingest(IngestRequest(source=str(source)), wait_for_pipeline=False)
        document_id = response.document.id
        await drain_abstraction_queue(service)
        row = await service._store.get_document(document_id)
        assert row.pipeline_status.value == "abstraction_complete"
        assert len(embedded) == (2 if stage == 2 else 1)
        assert len(generated) == (2 if stage == 3 else 1)
        assert len(set(generations)) == 1
        assert row.processing_coverage.projection.generation == generations[-1]
        assert row.indexed_at is not None
        assert await service._store.get_processing_stage(document_id) == "abstraction"
        assert any(
            "CURRENT RETRY SOURCE" in c.content
            for c in await service._content_store.get_all_chunks(document_id)
        )
    finally:
        await service.stop_worker()


@pytest.mark.parametrize(
    "entry,boundary", [("initial", "admission"), ("initial", "retention"), ("force", "admission")]
)
@pytest.mark.parametrize("inline", [False, True])
async def test_ingest_cannot_readmit_over_an_owner_after_atomic_admission(
    ingestion_service, pg_pool, tmp_vault_dir, monkeypatch, entry, inline, boundary
):
    from sage.services.ingestion import _SupersededProcessing

    service = ingestion_service
    graph = service._store
    store = PostgresContentStore(pg_pool)
    service._content_store = store
    source = tmp_vault_dir / "admission.md"
    source.write_text("OWNED SOURCE body.")
    if entry == "force":
        await service.ingest(IngestRequest(source=str(source)))
    insert, update = graph.insert_document, graph.update_document
    publish = store.publish_processing
    newer = []

    async def admit_newer(document_id):
        row = await update(
            document_id,
            {"pipeline_status": "abstraction_in_progress"},
            processing_stage="abstraction",
        )
        newer.append(row)

    async def insert_then_race(doc, **kwargs):
        result = await insert(doc, **kwargs)
        if boundary == "admission":
            await admit_newer(doc.id)
        return result

    async def update_then_race(document_id, updates, **kwargs):
        result = await update(document_id, updates, **kwargs)
        if boundary == "admission" and kwargs.get("processing_stage") == "indexing":
            await admit_newer(document_id)
        return result

    async def publish_then_race(document_id, attempt, dimensions, **kwargs):
        result = await publish(document_id, attempt, dimensions, **kwargs)
        if boundary == "retention" and "source_retention" in dimensions:
            await admit_newer(document_id)
        return result

    monkeypatch.setattr(store, "publish_processing", publish_then_race)
    monkeypatch.setattr(graph, "insert_document", insert_then_race)
    monkeypatch.setattr(graph, "update_document", update_then_race)
    try:
        request = IngestRequest(source=str(source), force=entry == "force")
        if boundary == "admission" and entry == "initial":
            with pytest.raises(_SupersededProcessing, match="stale source retention"):
                await service.ingest(request, wait_for_pipeline=inline)
        else:
            await service.ingest(request, wait_for_pipeline=inline)
            await drain_abstraction_queue(service)
        assert len(newer) == 1
        assert await graph.get_document(newer[0].id) == newer[0]
        assert await graph.get_processing_stage(newer[0].id) == "abstraction"
        assert not service._inflight
    finally:
        await service.stop_worker()
