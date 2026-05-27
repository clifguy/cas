"""IngestionService background-task tracking tests.

Locks in the invariant that Stage 2-3 dispatch and reabstract dispatch hold
strong references to their asyncio.Task in a per-service set until the task's
done callback fires. The set closes the asyncio "task disappears mid-execution"
window documented at https://docs.python.org/3/library/asyncio-task.html
("Save a reference to the result of this function, to avoid a task
disappearing mid-execution. The event loop only keeps weak references to
tasks.").
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


async def test_background_dispatch_retains_task_reference_until_done(
    tmp_vault_dir, ingestion_service, graph_store
):
    """While the Stage 2 background task is in flight, ``_background_tasks``
    must hold a strong reference to it. After completion, the done callback
    must drain the set.
    """
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

    # Yield twice so the background task starts and reaches gate.wait() inside
    # the embed step. One yield schedules it; the second lets it advance past
    # the synchronous prelude in _stage2_indexing.
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    # Invariant during in-flight: tracking set is populated.
    assert hasattr(ingestion_service, "_background_tasks"), (
        "IngestionService must declare _background_tasks for background-dispatch tracking."
    )
    assert len(ingestion_service._background_tasks) == 1

    gate.set()
    await _await_terminal(graph_store, doc_id)

    # Done callback drains the set. Yield once to let add_done_callback fire.
    await asyncio.sleep(0.05)
    assert ingestion_service._background_tasks == set()


async def test_background_dispatch_survives_dropped_caller_reference(
    tmp_vault_dir, ingestion_service, graph_store, stub_content_store
):
    """The IngestionService must hold a strong reference to the Stage 2
    background task so the task cannot be GC'd while in flight even when the
    caller drops every reference it held. Use weakref + gc.collect() to assert
    the strong-reference invariant deterministically.
    """
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

    # Capture a weakref to the in-flight task via the tracking set.
    assert len(ingestion_service._background_tasks) == 1
    (in_flight_task,) = tuple(ingestion_service._background_tasks)
    task_ref = weakref.ref(in_flight_task)
    del in_flight_task

    # Drop our local references and force a GC pass. The asyncio event loop
    # only keeps a weak reference to scheduled tasks; the tracking set is what
    # keeps this one alive.
    gc.collect()
    gc.collect()

    # Strong reference survives because IngestionService holds it.
    assert task_ref() is not None, (
        "background task was GC'd despite being in flight — tracking set missing"
    )
    assert task_ref() in ingestion_service._background_tasks

    # Release the gate and confirm Stage 2 completes successfully (chunks land
    # in the content store, terminal status reached). This is the user-visible
    # outcome the fix protects.
    gate.set()
    terminal = await _await_terminal(graph_store, doc_id)
    assert terminal in {PipelineStatus.ABSTRACTION_COMPLETE, PipelineStatus.ABSTRACTION_SKIPPED}
    chunks = await stub_content_store.get_all_chunks(doc_id)
    assert chunks, (
        "Stage 2 must have indexed chunks; the no-chunks state is the silent-loss "
        "failure mode this tracking set closes."
    )

    # After completion the done callback drained the set; the weakref's
    # referent is no longer kept alive by the service.
    await asyncio.sleep(0.05)
    assert ingestion_service._background_tasks == set()


async def test_reabstract_background_also_tracked(tmp_vault_dir, ingestion_service, graph_store):
    """The reabstract dispatch site shares the same ``_background_tasks`` set.
    Without symmetric coverage at the reabstract call site, that path remains
    vulnerable to the same GC-driven silent loss.
    """
    _create_test_file(tmp_vault_dir, "samples/a3.md", "# A3\n\nContent.")

    # Run the initial ingest fully so Stage 2 chunks land and the document
    # is ready to reabstract.
    request = IngestRequest(source="samples/a3.md", source_type=SourceType.MARKDOWN)
    result = await ingestion_service.ingest(request, wait_for_pipeline=True)
    doc_id = result.document.id

    # Gate the abstraction provider so the reabstract background task hangs
    # inside generate_abstract, holding the tracking-set reservation.
    entered = asyncio.Event()
    gate = asyncio.Event()

    async def gated_abstract(text: str, max_tokens: int, doc_type):
        entered.set()
        await gate.wait()
        return "gated stub abstract"

    ingestion_service._abstraction.generate_abstract = gated_abstract

    await ingestion_service.reabstract(doc_id)

    # Wait until the background task is actually inside generate_abstract.
    await asyncio.wait_for(entered.wait(), timeout=2.0)

    try:
        # The reabstract task must be in the same tracking set as ingest's
        # Stage 2-3 task. Single set, both call sites.
        assert len(ingestion_service._background_tasks) == 1
        (in_flight_task,) = tuple(ingestion_service._background_tasks)
        assert not in_flight_task.done()
    finally:
        gate.set()

    # Release the gate, let reabstract complete, and confirm the set drains.
    await asyncio.sleep(0.2)
    for _ in range(200):
        if not ingestion_service._background_tasks:
            break
        await asyncio.sleep(0.01)
    assert ingestion_service._background_tasks == set()


# Each test above pins a distinct production invariant. Pre-fix
# (``asyncio.create_task(...)`` with the return value discarded), the
# tracking set never existed and the asyncio event loop's weak reference
# was the only thing keeping a dispatched task alive between awaits. The
# weakref + ``gc.collect()`` harness above demonstrates the
# strong-reference contract deterministically; the symmetry test ensures
# both the ingest and the reabstract dispatch sites participate in the
# same set.
