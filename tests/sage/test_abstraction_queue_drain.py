"""The vault-scoped abstraction-queue drain used at test teardown.

``drain_abstraction_queue`` replaces a fixed sleep at the end of a fixture: it
waits on the abstraction work itself, so teardown neither races work that is
still running nor pays a fixed delay when there is none. Each test here pins
one arm of that predicate against a drain that would pass it by accident.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from sage.adapters.stubs import StubAbstractionProvider, StubContentStore, StubEmbeddingProvider
from sage.config import VaultConfig
from sage.services.ingestion import _AbstractionJob
from tests.helpers import pipeline_wait
from tests.helpers.pipeline_wait import (
    TERMINAL_PIPELINE_STATES,
    drain_abstraction_queue,
    drain_vaults,
)
from tests.sage.conftest import initialize_services_for_test
from tests.sage.test_abstraction_queue import _GatedAbstractionProvider, _seed_indexed_doc


async def _still_pending(task: asyncio.Task, window: float = 0.05) -> bool:
    """Whether ``task`` is still running after a short bounded wait."""
    try:
        await asyncio.wait_for(asyncio.shield(task), window)
    except TimeoutError:
        return not task.done()
    return False


@pytest.fixture(autouse=True)
async def _stop_fixture_worker(ingestion_service):
    """Stop the fixture service's worker so no ``queue.get()`` task outlives the loop."""
    yield
    await ingestion_service.stop_worker(restamp=False)


async def _hold_a_job(service, tmp_vault_dir, name: str) -> tuple[str, _GatedAbstractionProvider]:
    """Put one abstraction job in flight and hold it inside the provider."""
    doc_id = await _seed_indexed_doc(service, tmp_vault_dir, name)
    gated = _GatedAbstractionProvider()
    service._abstraction = gated
    await service.reabstract(doc_id)
    await asyncio.wait_for(gated.entered.wait(), timeout=2.0)
    return doc_id, gated


async def test_drain_returns_immediately_when_no_queue_exists(ingestion_service):
    """A service that never dispatched is drained as it stands, and is not given a queue."""
    assert ingestion_service._abstraction_queue is None

    await drain_abstraction_queue(ingestion_service, timeout=0.5)

    assert ingestion_service._abstraction_queue is None
    assert ingestion_service._worker_task is None


async def test_drain_waits_for_a_queued_job_and_its_claim(
    ingestion_service, graph_store, tmp_vault_dir
):
    """The drain holds while a job is running, when ``qsize()`` already reads zero."""
    doc_id, gated = await _hold_a_job(ingestion_service, tmp_vault_dir, "samples/dr1.md")
    queue = ingestion_service._abstraction_queue
    assert queue.qsize() == 0, "control: the job has left the queue and is running"

    drain = asyncio.create_task(drain_abstraction_queue(ingestion_service, timeout=5.0))
    try:
        assert await _still_pending(drain), "the drain returned while a job was in flight"
    finally:
        gated.gate.set()
    await drain

    assert doc_id not in ingestion_service._inflight
    assert queue._unfinished_tasks == 0
    doc = await graph_store.get_document(doc_id)
    assert doc.pipeline_status.value in TERMINAL_PIPELINE_STATES


async def test_drain_waits_for_a_running_job_that_holds_no_claim(ingestion_service, tmp_vault_dir):
    """The join arm holds on its own: a job running with no claim still keeps the drain waiting.

    Every ordinary job runs under a claim, so a drain that polled the claim
    registry alone would pass the other tests here. A job enqueued without one
    separates the two arms.
    """
    doc_id = await _seed_indexed_doc(ingestion_service, tmp_vault_dir, "samples/dr8.md")
    gated = _GatedAbstractionProvider()
    ingestion_service._abstraction = gated
    ingestion_service._enqueue_abstraction_job(
        _AbstractionJob(document_id=doc_id, projection=None, doc_type="note")
    )
    await asyncio.wait_for(gated.entered.wait(), timeout=2.0)
    assert ingestion_service._inflight == {}, "control: the running job holds no claim"

    drain = asyncio.create_task(drain_abstraction_queue(ingestion_service, timeout=5.0))
    try:
        assert await _still_pending(drain), "the drain returned while an unclaimed job ran"
    finally:
        gated.gate.set()
    await drain

    assert ingestion_service._abstraction_queue._unfinished_tasks == 0


async def test_drain_waits_for_a_claim_with_no_queued_job(ingestion_service, tmp_vault_dir):
    """A claim held ahead of its enqueue keeps the drain waiting though the queue is idle."""
    doc_id = await _seed_indexed_doc(ingestion_service, tmp_vault_dir, "samples/dr2.md")
    assert ingestion_service._try_claim(doc_id, "test") is None

    drain = asyncio.create_task(drain_abstraction_queue(ingestion_service, timeout=5.0))
    try:
        assert await _still_pending(drain), "the drain returned while a claim was held"
    finally:
        ingestion_service._release_claim(doc_id)
    await drain

    assert ingestion_service._inflight == {}


async def test_drain_times_out_loudly_naming_vault_and_pending_work(
    ingestion_service, minimal_config, tmp_vault_dir
):
    """A drain that cannot finish fails within its bound and says what it was waiting on."""
    doc_id, gated = await _hold_a_job(ingestion_service, tmp_vault_dir, "samples/dr3.md")
    loop = asyncio.get_running_loop()
    started = loop.time()
    try:
        with pytest.raises(AssertionError) as excinfo:
            await drain_abstraction_queue(ingestion_service, timeout=0.2)
    finally:
        gated.gate.set()

    assert loop.time() - started < 2.0
    message = str(excinfo.value)
    assert minimal_config.vault.id in message
    assert doc_id in message
    assert "1 unfinished abstraction job" in message


async def test_drain_fails_fast_when_worker_is_dead_with_jobs_pending(
    ingestion_service, tmp_vault_dir
):
    """Unfinished jobs with no worker to run them are reported at once, not at the bound."""
    doc_id, gated = await _hold_a_job(ingestion_service, tmp_vault_dir, "samples/dr4.md")
    worker = ingestion_service._worker_task
    worker.cancel()
    with pytest.raises(asyncio.CancelledError):
        await worker
    gated.gate.set()
    # The interrupted job's own ``finally`` marked it done; queue another one
    # that nothing will run, and leave it unfinished.
    ingestion_service._abstraction_queue.put_nowait(SimpleNamespace(document_id=doc_id))

    loop = asyncio.get_running_loop()
    started = loop.time()
    with pytest.raises(AssertionError, match="worker not running"):
        await drain_abstraction_queue(ingestion_service, timeout=5.0)
    assert loop.time() - started < 1.0

    ingestion_service._drain_abandoned_jobs()


async def test_drain_vaults_drains_each_named_registry_entry(ingestion_service, tmp_vault_dir):
    """Every named vault is drained, and one outside the named set is left alone."""
    doc_id = await _seed_indexed_doc(ingestion_service, tmp_vault_dir, "samples/dr5.md")
    idle = SimpleNamespace(_abstraction_queue=None, _worker_task=None, _inflight={}, _config=None)
    registry = {
        "busy_vault": SimpleNamespace(ingestion_service=ingestion_service),
        "idle_vault": SimpleNamespace(ingestion_service=idle),
    }
    assert ingestion_service._try_claim(doc_id, "test") is None
    try:
        with pytest.raises(AssertionError, match=doc_id):
            await drain_vaults(registry, timeout=0.2)
        await drain_vaults(registry, ["idle_vault", "gone_vault"], timeout=0.2)
    finally:
        ingestion_service._release_claim(doc_id)


async def test_initialize_services_for_test_drains_before_closing_storage(
    minimal_vault_config_dict, tmp_vault_dir, monkeypatch
):
    """The context manager's exit waits out in-flight work before storage closes."""
    config = VaultConfig.model_validate(minimal_vault_config_dict)
    observed: dict[str, object] = {}

    async with initialize_services_for_test(
        config,
        content_store=StubContentStore(),
        embedding_provider=StubEmbeddingProvider(),
        abstraction_provider=StubAbstractionProvider(),
    ) as services:
        service = services.ingestion_service
        real_close = services.close_storage

        async def _recording_close() -> None:
            queue = service._abstraction_queue
            observed["inflight"] = dict(service._inflight)
            observed["unfinished"] = None if queue is None else queue._unfinished_tasks
            await real_close()

        monkeypatch.setattr(services, "close_storage", _recording_close)
        _, gated = await _hold_a_job(service, tmp_vault_dir, "samples/dr6.md")

        async def _release_later() -> None:
            await asyncio.sleep(0.05)
            gated.gate.set()

        releaser = asyncio.create_task(_release_later())

    await releaser
    await service.stop_worker(restamp=False)
    assert observed == {"inflight": {}, "unfinished": 0}


async def test_initialize_services_for_test_closes_storage_even_if_drain_fails(
    minimal_vault_config_dict, tmp_vault_dir, monkeypatch
):
    """A drain that times out still lets the context manager release its storage."""
    config = VaultConfig.model_validate(minimal_vault_config_dict)
    closed: list[str] = []
    monkeypatch.setattr(pipeline_wait, "DEFAULT_DRAIN_TIMEOUT", 0.2)

    with pytest.raises(AssertionError, match="did not drain"):
        async with initialize_services_for_test(
            config,
            content_store=StubContentStore(),
            embedding_provider=StubEmbeddingProvider(),
            abstraction_provider=StubAbstractionProvider(),
        ) as services:
            real_close_storage = services.close_storage
            real_close_timing = services.close_timing

            async def _close_storage() -> None:
                closed.append("storage")
                await real_close_storage()

            def _close_timing() -> None:
                closed.append("timing")
                real_close_timing()

            monkeypatch.setattr(services, "close_storage", _close_storage)
            monkeypatch.setattr(services, "close_timing", _close_timing)
            doc_id = await _seed_indexed_doc(
                services.ingestion_service, tmp_vault_dir, "samples/dr7.md"
            )
            assert services.ingestion_service._try_claim(doc_id, "test") is None

    assert closed == ["timing", "storage"]
