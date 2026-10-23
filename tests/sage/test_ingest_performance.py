"""Causal checks for bounded stage accounting across asynchronous workers."""

import asyncio
import json
import logging
from concurrent.futures import ThreadPoolExecutor

import pytest

from sage.instrumentation.performance import current_trace, offload, scope, span


def records(caplog):
    return [json.loads(r.message) for r in caplog.records if r.name == "sage.ingestion.performance"]


def test_scope_aggregates_and_keeps_only_numeric_observations(caplog):
    caplog.set_level(logging.INFO, logger="sage.ingestion.performance")
    with scope("vault-a", "doc-a", "ingest") as trace:
        for _ in range(100):
            with span("inference"):
                trace.count("submitted_texts", 1)
    [row] = records(caplog)
    assert row["outcome"] == "success"
    assert row["stages"]["inference"]["count"] == 100
    assert row["counts"]["submitted_texts"] == 100
    assert row["document_id"] == "doc-a"
    assert len(row["stages"]) == 1
    assert row["finished_at"] >= row["started_at"]
    assert current_trace() is None


def test_disabled_scope_is_inert(caplog):
    caplog.set_level(logging.INFO, logger="sage.ingestion.performance")
    with scope("vault", "doc", "ingest", enabled=False) as trace:
        assert trace is None
        with span("inference"):
            pass
    assert not records(caplog)


def test_failure_records_class_without_exception_text(caplog):
    caplog.set_level(logging.INFO, logger="sage.ingestion.performance")
    with pytest.raises(ValueError):
        with scope("vault", "doc", "ingest"):
            with span("inference"):
                raise ValueError("sensitive source content")
    [row] = records(caplog)
    assert row["outcome"] == "failed"
    assert row["stages"]["inference"]["failed"] == 1
    assert "sensitive" not in json.dumps(row)


async def test_offload_separates_queue_from_execution_and_keeps_context(caplog, monkeypatch):
    from concurrent.futures import Future

    clock = [0]
    monkeypatch.setattr("sage.instrumentation.performance.time.perf_counter_ns", lambda: clock[0])

    class DeferredExecutor:
        def submit(self, function, *args):
            self.future = Future()
            self.function = lambda: function(*args)
            return self.future

        def run(self):
            try:
                self.future.set_result(self.function())
            except BaseException as exc:
                self.future.set_exception(exc)

    executor = DeferredExecutor()
    caplog.set_level(logging.INFO, logger="sage.ingestion.performance")
    with scope("vault", "doc", "ingest"):
        task = asyncio.create_task(
            offload(executor, "projection", lambda: current_trace().document_id)
        )
        await asyncio.sleep(0)
        assert not task.done()
        clock[0] = 100_000_000
        await asyncio.get_running_loop().run_in_executor(None, executor.run)
        assert await task == "doc"
    [row] = records(caplog)
    assert row["stages"]["projection.queue"]["total_ms"] == 100
    assert row["stages"]["projection.execute"]["total_ms"] == 0


async def test_cancelled_offload_defers_summary_until_worker_finishes(caplog):
    import threading

    caplog.set_level(logging.INFO, logger="sage.ingestion.performance")
    entered = threading.Event()
    release = threading.Event()
    pool = ThreadPoolExecutor(max_workers=1)
    try:

        async def caller():
            with scope("vault", "doc", "ingest"):
                await offload(pool, "projection", lambda: (entered.set(), release.wait(5)))

        task = asyncio.create_task(caller())
        assert await asyncio.to_thread(entered.wait, 2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not records(caplog)
        release.set()
        await asyncio.to_thread(pool.shutdown)
        [row] = records(caplog)
        assert row["outcome"] == "cancelled"
        assert row["stages"]["projection.execute"]["count"] == 1
    finally:
        release.set()
        pool.shutdown()


async def test_concurrent_scopes_do_not_mix_vaults(caplog):
    caplog.set_level(logging.INFO, logger="sage.ingestion.performance")

    async def caller(vault):
        with scope(vault, vault + "-doc", "ingest") as trace:
            await asyncio.sleep(0)
            trace.count("submitted_texts", 1)

    await asyncio.gather(caller("one"), caller("two"))
    rows = records(caplog)
    assert {(r["vault_id"], r["document_id"]) for r in rows} == {
        ("one", "one-doc"),
        ("two", "two-doc"),
    }
    assert all(r["counts"] == {"submitted_texts": 1} for r in rows)


def test_nested_scope_keeps_parent_identity(caplog):
    caplog.set_level(logging.INFO, logger="sage.ingestion.performance")
    with scope("vault", "doc", "ingest") as parent:
        with scope("vault", "doc", "embedding") as child:
            assert parent is child
    assert len(records(caplog)) == 1


def test_cardinality_is_bounded(caplog):
    caplog.set_level(logging.INFO, logger="sage.ingestion.performance")
    with scope("vault", "doc", "ingest") as trace:
        for i in range(1000):
            trace.count(str(i), 1)
            with span(str(i)):
                pass
    [row] = records(caplog)
    assert len(row["stages"]) <= 33
    assert len(row["counts"]) <= 33


@pytest.mark.parametrize("hook_name", ["tokenize", "preprocess"])
async def test_real_embedding_boundary_records_counts_and_model_tokenization(caplog, hook_name):
    import numpy as np

    from sage.adapters.embedding_nomic import NomicEmbeddingProvider

    class Model:
        def tokenize(self, texts):
            return {"attention_mask": np.ones((len(texts), 5), dtype=int)}

        def encode(self, texts, **kwargs):
            self.tokenize(texts)
            return np.ones((len(texts), 3))

    if hook_name == "preprocess":
        Model.preprocess = Model.tokenize
        Model.encode = lambda self, texts, **kwargs: (
            self.preprocess(texts),
            np.ones((len(texts), 3)),
        )[1]
    provider = object.__new__(NomicEmbeddingProvider)
    provider._executor = None
    provider._model = Model()
    try:
        caplog.set_level(logging.INFO, logger="sage.ingestion.performance")
        with scope("vault", "doc", "index"):
            vectors = await provider.embed(["first", "second"])
        [row] = records(caplog)
        assert vectors == [[1.0, 1.0, 1.0], [1.0, 1.0, 1.0]]
        assert row["counts"]["embedding.texts"] == 2
        assert row["counts"]["embedding.tokens"] == 10
        assert row["stages"]["embedding.admission"]["count"] == 2
        assert row["stages"]["embedding.executor"]["count"] == 2
        assert row["stages"]["embedding.tokenize"]["count"] == 2
        assert row["stages"]["embedding.encode"]["count"] == 2
        assert hook_name not in provider._model.__dict__
        assert row["ranges"]["embedding.token_length"] == {"minimum": 5, "maximum": 5}
    finally:
        if provider._executor:
            provider._executor.shutdown()


async def test_transaction_success_is_measured_after_actual_exit(caplog, monkeypatch):
    from contextlib import asynccontextmanager

    from sage.instrumentation.performance import observed_context

    clock = [0]
    monkeypatch.setattr("sage.instrumentation.performance.time.perf_counter_ns", lambda: clock[0])
    entered = asyncio.Event()
    release = asyncio.Event()

    @asynccontextmanager
    async def transaction():
        yield "connection"
        entered.set()
        await release.wait()

    async def caller():
        with scope("vault", "doc", "write"):
            async with observed_context(
                transaction(), "db.begin", "db.commit", "db.rollback"
            ) as conn:
                assert conn == "connection"

    caplog.set_level(logging.INFO, logger="sage.ingestion.performance")
    task = asyncio.create_task(caller())
    await entered.wait()
    assert not records(caplog)
    clock[0] = 100_000_000
    release.set()
    await task
    [row] = records(caplog)
    assert row["stages"]["db.commit"]["total_ms"] == 100
    assert row["stages"]["db.commit"]["count"] == 1
    assert "db.rollback" not in row["stages"]


async def test_commit_failure_is_not_reported_as_success(caplog):
    from contextlib import asynccontextmanager

    from sage.instrumentation.performance import observed_context

    @asynccontextmanager
    async def transaction():
        yield
        raise RuntimeError("commit failed")

    caplog.set_level(logging.INFO, logger="sage.ingestion.performance")
    with pytest.raises(RuntimeError):
        with scope("vault", "doc", "write"):
            async with observed_context(transaction(), "db.begin", "db.commit", "db.rollback"):
                pass
    [row] = records(caplog)
    assert row["outcome"] == "failed"
    assert row["stages"]["db.commit"]["failed"] == 1


async def test_actual_postgres_write_and_commit_are_attributed(pg_pool, caplog):
    from sage.adapters.content_store_postgres import PostgresContentStore
    from sage.adapters.interfaces import Chunk

    store = PostgresContentStore(pg_pool)
    vector = [1.0] + [0.0] * 767
    chunk = Chunk(
        document_id="perf-document",
        heading_path="Heading",
        content="Retained content",
        embedding=vector,
        chunk_index=0,
    )
    caplog.set_level(logging.INFO, logger="sage.ingestion.performance")
    with scope("vault", "perf-document", "index"):
        await store.index_chunks("perf-document", [chunk])
    [row] = records(caplog)
    assert row["stages"]["database.write_chunks"]["count"] == 1
    assert row["stages"]["database.commit"]["failed"] == 0
    assert (
        row["stages"]["database.commit"]["first_start_ns"]
        >= row["stages"]["database.write_chunks"]["last_end_ns"]
    )
    assert (await store.get_all_chunks("perf-document"))[0].content == "Retained content"


async def test_standalone_reembedding_establishes_document_scope(ingestion_service, caplog):
    from sage.adapters.interfaces import Chunk

    chunk = Chunk(
        document_id="maintenance-document", heading_path="Heading", content="Content", chunk_index=0
    )
    caplog.set_level(logging.INFO, logger="sage.ingestion.performance")
    await ingestion_service._embed_chunks([chunk])
    [row] = records(caplog)
    assert row["document_id"] == "maintenance-document"
    assert row["operation"] == "embedding.chunks"
    assert len(chunk.embedding) > 0


@pytest.mark.parametrize("enabled", [True, False])
async def test_cancelled_queued_work_does_not_execute_or_leak_trace(caplog, enabled):
    import threading

    caplog.set_level(logging.INFO, logger="sage.ingestion.performance")
    entered = threading.Event()
    release = threading.Event()
    executed = threading.Event()
    pool = ThreadPoolExecutor(max_workers=1)
    try:
        pool.submit(lambda: (entered.set(), release.wait(5)))
        assert await asyncio.to_thread(entered.wait, 2)

        async def caller():
            with scope("vault", "doc", "ingest", enabled=enabled):
                await offload(pool, "projection", executed.set)

        task = asyncio.create_task(caller())
        await asyncio.sleep(0)  # offload submits before its first suspension
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        release.set()
        await asyncio.to_thread(pool.shutdown)
        assert not executed.is_set()
        rows = records(caplog)
        if enabled:
            [row] = rows
            assert row["outcome"] == "cancelled"
            assert row["stages"]["projection.queue"]["cancelled"] == 1
            assert "projection.execute" not in row["stages"]
        else:
            assert not rows
    finally:
        release.set()
        pool.shutdown()


async def test_projection_rebuild_keeps_document_object_identity(
    ingestion_service, caplog, tmp_path
):
    from sage.models.schemas import IngestRequest

    source = tmp_path / "rebuild.md"
    source.write_text("# Rebuild\nA retained source for maintenance correlation.")
    result = await ingestion_service.ingest(IngestRequest(source=str(source)))
    doc = result.document
    caplog.clear()
    caplog.set_level(logging.INFO, logger="sage.ingestion.performance")
    # The real rebuild entrypoint can skip an unavailable source; it must still correlate.
    await ingestion_service._bring_passages_current(doc, None)
    [row] = records(caplog)
    assert row["operation"] == "projection.rebuild"
    assert row["document_id"] == doc.id
