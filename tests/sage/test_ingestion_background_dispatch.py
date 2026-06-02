"""IngestionService off-loop worker reference-safety tests.

Pins the structural guarantee that closes the asyncio "task disappears
mid-execution" window (https://docs.python.org/3/library/asyncio-task.html:
"The event loop only keeps weak references to tasks"). Previously, each
wait_for_pipeline=False ingest spawned its own fire-and-forget task and relied
on a per-dispatch tracking set to hold a strong reference. With the persistent
abstraction queue the model is simpler and stronger:

* Queued work is plain data on an ``asyncio.Queue`` the service holds — not a
  task, so it cannot be GC'd mid-flight.
* A single long-lived worker task drains the queue; the service holds a strong
  reference to it in ``_worker_task``, so it survives a GC pass while in flight.
* Both the ingest and the reabstract entry points feed that one worker — there
  is no per-dispatch task proliferation.

The user-visible outcome the original silent-loss guard protected — chunks
land and the document reaches a terminal status — is asserted directly.
"""

import asyncio
import gc
import weakref

from sage.models.enums import PipelineStatus, SourceType
from sage.models.schemas import IngestRequest

# Reuse the helper that creates a markdown file under the temp vault's
# sources/ tree. Keeps fixture wiring identical to the rest of the
# ingestion test suite.
from tests.sage.test_ingestion import _create_test_file

_TERMINAL_STATES = {
    PipelineStatus.ABSTRACTION_COMPLETE,
    PipelineStatus.ABSTRACTION_SKIPPED,
    PipelineStatus.FAILED,
}


async def _await_terminal(graph_store, doc_id: str, *, attempts: int = 400) -> PipelineStatus:
    """Poll the document until it reaches a terminal pipeline_status."""
    for _ in range(attempts):
        doc = await graph_store.get_document(doc_id)
        if doc is not None and doc.pipeline_status in _TERMINAL_STATES:
            return doc.pipeline_status
        await asyncio.sleep(0.01)
    raise AssertionError(f"document {doc_id} did not reach terminal status in time")


async def test_enqueued_work_drains_to_terminal_with_chunks(
    tmp_vault_dir, ingestion_service, graph_store, stub_content_store
):
    """A wait_for_pipeline=False ingest enqueues Stage 2-3 work that the single
    worker drains to a terminal status with chunks indexed. While the worker is
    gated mid-job, the service holds a live ``_worker_task``."""
    _create_test_file(tmp_vault_dir, "samples/a1.md", "# A1\n\nContent.")

    gate = asyncio.Event()
    original_embed = ingestion_service._embedding.embed

    async def gated_embed(texts):
        await gate.wait()
        return await original_embed(texts)

    ingestion_service._embedding.embed = gated_embed

    request = IngestRequest(source="samples/a1.md", source_type=SourceType.MARKDOWN)
    result = await ingestion_service.ingest(request, wait_for_pipeline=False)
    doc_id = result.document.id

    # Yield so the worker starts and reaches gate.wait() inside the embed step.
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    # Invariant during in-flight: a single live worker task is held.
    assert ingestion_service._worker_task is not None
    assert not ingestion_service._worker_task.done()

    gate.set()
    terminal = await _await_terminal(graph_store, doc_id)
    assert terminal in {PipelineStatus.ABSTRACTION_COMPLETE, PipelineStatus.ABSTRACTION_SKIPPED}
    chunks = await stub_content_store.get_all_chunks(doc_id)
    assert chunks, "Stage 2 must have indexed chunks; no-chunks is the silent-loss failure mode."

    await ingestion_service.stop_worker()


async def test_worker_task_survives_dropped_caller_reference(
    tmp_vault_dir, ingestion_service, graph_store, stub_content_store
):
    """The IngestionService holds a strong reference to the single worker task,
    so it cannot be GC'd while draining even when the caller drops every local
    reference. weakref + gc.collect() makes the strong-reference invariant
    deterministic."""
    _create_test_file(tmp_vault_dir, "samples/a2.md", "# A2\n\nContent.")

    gate = asyncio.Event()
    original_embed = ingestion_service._embedding.embed

    async def gated_embed(texts):
        await gate.wait()
        return await original_embed(texts)

    ingestion_service._embedding.embed = gated_embed

    request = IngestRequest(source="samples/a2.md", source_type=SourceType.MARKDOWN)
    result = await ingestion_service.ingest(request, wait_for_pipeline=False)
    doc_id = result.document.id

    await asyncio.sleep(0)
    await asyncio.sleep(0)

    worker_task = ingestion_service._worker_task
    assert worker_task is not None
    task_ref = weakref.ref(worker_task)
    del worker_task

    gc.collect()
    gc.collect()

    assert task_ref() is not None, "worker task was GC'd despite being held by the service"
    assert task_ref() is ingestion_service._worker_task

    gate.set()
    terminal = await _await_terminal(graph_store, doc_id)
    assert terminal in {PipelineStatus.ABSTRACTION_COMPLETE, PipelineStatus.ABSTRACTION_SKIPPED}
    chunks = await stub_content_store.get_all_chunks(doc_id)
    assert chunks, "Stage 2 must have indexed chunks; the no-chunks state is the silent-loss mode."

    await ingestion_service.stop_worker()


async def test_ingest_and_reabstract_share_one_worker(
    tmp_vault_dir, ingestion_service, graph_store
):
    """Both the ingest and the reabstract entry points feed the SAME single
    worker draining one queue — not a per-dispatch task each. A gated job holds
    the worker while a second entry point enqueues behind it; exactly one
    worker task exists."""
    # Seed one fully-indexed document so reabstract has chunks to work from.
    _create_test_file(tmp_vault_dir, "samples/a3.md", "# A3\n\nContent.")
    seed = await ingestion_service.ingest(
        IngestRequest(source="samples/a3.md", source_type=SourceType.MARKDOWN),
        wait_for_pipeline=True,
    )
    seeded_id = seed.document.id

    entered = asyncio.Event()
    gate = asyncio.Event()

    async def gated_abstract(text: str, max_tokens: int, doc_type) -> str:
        entered.set()
        await gate.wait()
        return "gated stub abstract"

    ingestion_service._abstraction.generate_abstract = gated_abstract

    # Reabstract enqueues a Stage-3 job; the worker enters generate_abstract
    # and blocks on the gate.
    await ingestion_service.reabstract(seeded_id)
    await asyncio.wait_for(entered.wait(), timeout=2.0)

    # A concurrent wait=False ingest enqueues behind the gated job — same queue,
    # same single worker.
    _create_test_file(tmp_vault_dir, "samples/a4.md", "# A4\n\nDifferent content.")
    second = await ingestion_service.ingest(
        IngestRequest(source="samples/a4.md", source_type=SourceType.MARKDOWN),
        wait_for_pipeline=False,
    )

    try:
        assert ingestion_service._worker_task is not None
        assert not ingestion_service._worker_task.done()
        assert ingestion_service._abstraction_queue is not None
    finally:
        gate.set()

    # Both reach terminal once the gate releases — drained by the one worker.
    assert await _await_terminal(graph_store, seeded_id) == PipelineStatus.ABSTRACTION_COMPLETE
    second_terminal = await _await_terminal(graph_store, second.document.id)
    assert second_terminal == PipelineStatus.ABSTRACTION_COMPLETE

    await ingestion_service.stop_worker()
