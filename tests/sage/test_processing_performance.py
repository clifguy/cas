"""Processing publication keeps its atomic boundary observable."""

import json
import logging

import pytest

from sage.adapters.content_store_postgres import PostgresContentStore
from sage.instrumentation.performance import scope
from sage.models.schemas import IngestRequest
from tests.helpers.pipeline_wait import drain_abstraction_queue
from tests.sage.test_processing_coverage import _chunks, _doc, _evidence


def _records(caplog):
    return [json.loads(r.message) for r in caplog.records if r.name == "sage.ingestion.performance"]


@pytest.mark.parametrize("rollback", [False, True])
async def test_publication_timing_observes_the_atomic_checkpoint_transaction(
    pg_pool, postgres_graph_store, caplog, rollback
):
    from psycopg.errors import RaiseException

    graph = postgres_graph_store
    store = PostgresContentStore(pg_pool)
    doc = _doc(1)
    await graph.insert_document(doc, processing_stage="indexing")
    old = await store.begin_processing_attempt(doc.id)
    assert await store.publish_processing(doc.id, old, _evidence(), chunks=_chunks("OLD BODY"))
    before = await graph.get_document(doc.id)
    caplog.set_level(logging.INFO, logger="sage.ingestion.performance")
    with scope("vault", doc.id, "replacement"):
        admitted = await graph.update_document(
            doc.id, {"pipeline_status": "indexing_in_progress"}, processing_stage="indexing"
        )
        if rollback:
            async with pg_pool.connection() as conn:
                await conn.execute(
                    "CREATE FUNCTION refuse_timed_checkpoint() RETURNS trigger LANGUAGE plpgsql "
                    "AS $$ BEGIN RAISE EXCEPTION 'timed checkpoint refused'; END $$"
                )
                await conn.execute(
                    "CREATE TRIGGER refuse_timed_checkpoint BEFORE UPDATE ON documents "
                    "FOR EACH ROW EXECUTE FUNCTION refuse_timed_checkpoint()"
                )
        try:
            if rollback:
                with pytest.raises(RaiseException, match="timed checkpoint refused"):
                    await store.publish_processing(
                        doc.id,
                        admitted.processing_coverage.attempt,
                        _evidence(),
                        chunks=_chunks("CURRENT BODY"),
                    )
            else:
                assert await store.publish_processing(
                    doc.id,
                    admitted.processing_coverage.attempt,
                    _evidence(),
                    chunks=_chunks("CURRENT BODY"),
                )
        finally:
            if rollback:
                async with pg_pool.connection() as conn:
                    await conn.execute("DROP TRIGGER refuse_timed_checkpoint ON documents")
                    await conn.execute("DROP FUNCTION refuse_timed_checkpoint()")
    [row] = _records(caplog)
    stages = row["stages"]
    assert stages["graph.commit"]["count"] == 1
    assert stages["database.write_chunks"]["count"] == 1
    assert stages["database.pool"]["count"] == 1
    end = "database.rollback" if rollback else "database.commit"
    assert stages[end]["count"] == 1
    assert stages[end]["first_start_ns"] >= stages["database.write_chunks"]["last_end_ns"]
    assert ("database.commit" in stages) != rollback
    after = await graph.get_document(doc.id)
    chunks = await store.get_all_chunks(doc.id)
    if rollback:
        assert chunks[0].content == "OLD BODY"
        assert after.processing_coverage.projection == before.processing_coverage.projection
        assert after.indexed_at == before.indexed_at
        assert await graph.get_processing_stage(doc.id) == "indexing"
    else:
        assert chunks[0].content == "CURRENT BODY"
        assert (
            after.processing_coverage.projection.generation == admitted.processing_coverage.attempt
        )
        assert after.indexed_at > before.indexed_at
        assert await graph.get_processing_stage(doc.id) == "abstraction"


@pytest.mark.parametrize("inline", [False, True])
async def test_ingest_trace_keeps_publication_and_owned_queue_work_correlated(
    ingestion_service, pg_pool, tmp_vault_dir, caplog, inline
):
    service = ingestion_service
    service._content_store = PostgresContentStore(pg_pool)
    source = tmp_vault_dir / "trace.md"
    source.write_text("CURRENT CORRELATED body.")
    caplog.set_level(logging.INFO, logger="sage.ingestion.performance")
    try:
        result = await service.ingest(IngestRequest(source=str(source)), wait_for_pipeline=inline)
        await drain_abstraction_queue(service)
        rows = _records(caplog)
        assert len(rows) == (1 if inline else 2)
        assert {r["document_id"] for r in rows} == {result.document.id}
        assert len({r["operation_id"] for r in rows}) == len(rows)
        pipeline = next(r for r in rows if r["operation"] == ("ingest" if inline else "pipeline"))
        assert pipeline["stages"]["database.write_chunks"]["count"] == 1
        assert pipeline["stages"]["database.commit"]["count"] >= 2
        if not inline:
            assert pipeline["stages"]["pipeline.queue"]["count"] == 1
            assert pipeline["counts"]["pipeline.attempts"] == 1
        doc = await service._store.get_document(result.document.id)
        assert doc.pipeline_status.value == "abstraction_complete"
        assert await service._store.get_processing_stage(doc.id) == "abstraction"
        assert any(
            "CURRENT CORRELATED" in c.content
            for c in await service._content_store.get_all_chunks(doc.id)
        )
    finally:
        await service.stop_worker()
