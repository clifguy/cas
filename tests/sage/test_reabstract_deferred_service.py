"""Unit tests for MaintenanceService.reabstract_deferred (CAS-ADR-029).

Graduation of scripts/reabstract_deferred.py into the maintenance API
surface. The new service method enumerates documents whose
pipeline_status is 'abstraction_skipped', delegates each to the
in-process IngestionService.reabstract (reusing the already-loaded
AbstractionProvider per F-8), polls until each reaches a terminal
status, and assembles a ReabstractReport.

Test surface pins the seven contracts from

    1. Empty worklist -> empty report (idempotent no-op).
    2. Skipped document is promoted to abstraction_complete and the
       semantic_abstract is populated.
    3. PDFs are skipped by default and recorded as skipped_pdf entries.
    4. PDFs are reabstracted when include_pdf=True.
    5. Per-document failures are isolated; the report records the
       failure and the loop continues.
    6. Single-flight per vault: a second concurrent call raises
       ReabstractAlreadyInFlightError with the start_time of the first.
    7. Hard-error path: a MaintenanceService constructed without an
       IngestionService raises RuntimeError rather than silently
       initializing a second AbstractionProvider (F-8 guard).
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest

from sage.adapters.interfaces import AbstractionProvider, Chunk
from sage.api.errors import ReabstractAlreadyInFlightError
from sage.models.enums import PipelineStatus, ReabstractOutcome, SourceType
from sage.models.schemas import (
    Document,
    ReabstractProgressEvent,
    ReabstractReport,
    ReabstractSummaryEvent,
)
from sage.services.ingestion import IngestionService
from sage.services.maintenance import MaintenanceService
from sage.source_adapters.markdown_adapter import MarkdownAdapter
from tests.sage.test_lifecycle import _id, _make_doc

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_skipped_doc(
    doc_id: str,
    *,
    source_type: SourceType = SourceType.MARKDOWN,
) -> Document:
    """Variant of _make_doc that lands in pipeline_status=abstraction_skipped.

    _make_doc defaults pipeline_status to ABSTRACTION_COMPLETE so test
    fixtures land in a normal post-pipeline state; the deferred-abstract
    worklist needs the inverse.
    """
    doc = _make_doc(doc_id, pipeline_status=PipelineStatus.ABSTRACTION_SKIPPED)
    if source_type != SourceType.MARKDOWN:
        # _make_doc hardcodes MARKDOWN; rebuild the source_type-dependent
        # fields when overriding.
        doc = Document(
            id=doc.id,
            title=doc.title,
            source_type=source_type,
            source_path=f"test/{doc_id}.{source_type.value}",
            source_content_hash=doc.source_content_hash,
            adapter_version=doc.adapter_version,
            created_by=doc.created_by,
            created_at=doc.created_at,
            last_modified_by=doc.last_modified_by,
            updated_at=doc.updated_at,
            projected_at=doc.projected_at,
            pipeline_status=PipelineStatus.ABSTRACTION_SKIPPED,
        )
    return doc


async def _seed_doc_with_chunks(
    graph_store,
    content_store,
    doc: Document,
    *,
    body_text: str = "Body content for projection.",
) -> None:
    """Insert a document and index a single body chunk for it.

    IngestionService.reabstract enforces has_chunks(document_id) -- a
    document with no projection chunks raises NoProjectionError before
    the background task is dispatched. Seeding one chunk satisfies that
    precondition without depending on the full ingest pipeline.
    """
    await graph_store.insert_document(doc)
    chunk = Chunk(
        document_id=doc.id,
        heading_path="Body",
        content=body_text,
        chunk_index=0,
    )
    await content_store.index_chunks(doc.id, [chunk])


def _build_ingestion_service(
    *,
    graph_store,
    lock_manager,
    content_store,
    embedding_provider,
    abstraction_provider: AbstractionProvider,
    config,
    lifecycle_service,
) -> IngestionService:
    """Construct an IngestionService with a caller-chosen abstraction provider.

    The conftest ``ingestion_service`` fixture binds StubAbstractionProvider
    at construction; tests that need to vary the provider build their own
    instance via this helper.
    """
    return IngestionService(
        graph_store=graph_store,
        lock_manager=lock_manager,
        content_store=content_store,
        embedding_provider=embedding_provider,
        abstraction_provider=abstraction_provider,
        config=config,
        source_adapters={SourceType.MARKDOWN: MarkdownAdapter()},
        lifecycle_service=lifecycle_service,
    )


class _GatedAbstractionProvider(AbstractionProvider):
    """Stub provider that blocks on an asyncio.Event before returning.

    Used by the single-flight lock test (#6) to keep the first
    reabstract_deferred call mid-flight while a second call attempts to
    enter. ``entered`` signals "the background generate_abstract is
    running and waiting"; ``gate`` is released by the test to let the
    first call complete.
    """

    def __init__(self) -> None:
        self.entered = asyncio.Event()
        self.gate = asyncio.Event()

    async def generate_abstract(self, text: str, max_tokens: int, doc_type: str | None) -> str:
        self.entered.set()
        await self.gate.wait()
        return "gated stub abstract"


class _SelectivelyFailingProvider(AbstractionProvider):
    """Stub provider that fails whenever the input text carries ``_FAIL_MARKER``
    and succeeds otherwise.

    Used by the per-document failure tests to exercise the loop's isolation
    contract: one document's failure must not abort the sibling reabstract
    attempts. Keying on text rather than call order keeps the failure
    deterministic even though the abstraction queue worker retries a failing
    document several times before it reaches a terminal FAILED state.
    """

    _FAIL_MARKER = "FAILME"

    def __init__(self) -> None:
        self.call_count = 0

    async def generate_abstract(self, text: str, max_tokens: int, doc_type: str | None) -> str:
        self.call_count += 1
        if self._FAIL_MARKER in text:
            raise RuntimeError("simulated LLM failure for marked document")
        return f"stub abstract after {self.call_count} calls"


def _build_maintenance(
    *,
    graph_store,
    config,
    content_store,
    ingestion_service: IngestionService | None,
) -> MaintenanceService:
    """Construct a MaintenanceService with the ingestion dependency.

    db_path, registry_service, and content_store are not exercised by
    reabstract_deferred (migrate_vault, optimize_content_store, etc.
    touch those); we still pass concrete values so the constructor
    signature is satisfied. registry_service is None, matching test
    paths that do not exercise the migration reload.
    """
    from pathlib import Path

    return MaintenanceService(
        vault_id=config.vault.id,
        db_path=Path(config.vault.brain_root) / "graph.db",
        graph_store=graph_store,
        config=config,
        registry_service=None,
        content_store=content_store,
        ingestion_service=ingestion_service,
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


async def test_reabstract_deferred_empty_returns_zero_report(
    graph_store,
    ingestion_service,
    minimal_config,
    stub_content_store,
):
    """Vault with no documents in abstraction_skipped returns an empty
    report; no work is dispatched."""
    maintenance = _build_maintenance(
        graph_store=graph_store,
        config=minimal_config,
        content_store=stub_content_store,
        ingestion_service=ingestion_service,
    )

    report = await maintenance.reabstract_deferred()

    assert isinstance(report, ReabstractReport)
    assert report.vault_id == minimal_config.vault.id
    assert report.reabstracted_count == 0
    assert report.skipped_pdf_count == 0
    assert report.failed_count == 0
    assert report.entries == []


async def test_reabstract_deferred_promotes_one_skipped_document(
    graph_store,
    stub_content_store,
    ingestion_service,
    minimal_config,
):
    """A single abstraction_skipped markdown doc is reabstracted to
    abstraction_complete and the semantic_abstract is populated.

    Anti-coincidental-pass: the assertion re-reads the document via
    graph_store rather than reusing the pre-call Document object, so the
    test cannot pass on a stale projection cache.
    """
    doc = _make_skipped_doc(_id("deferred_md_a"))
    await _seed_doc_with_chunks(graph_store, stub_content_store, doc)

    maintenance = _build_maintenance(
        graph_store=graph_store,
        config=minimal_config,
        content_store=stub_content_store,
        ingestion_service=ingestion_service,
    )

    report = await maintenance.reabstract_deferred()

    assert report.reabstracted_count == 1
    assert report.skipped_pdf_count == 0
    assert report.failed_count == 0
    assert len(report.entries) == 1
    assert report.entries[0].document_id == doc.id
    assert report.entries[0].outcome == ReabstractOutcome.SUCCESS
    assert report.entries[0].error_message is None

    refreshed = await graph_store.get_document(doc.id)
    assert refreshed.pipeline_status == PipelineStatus.ABSTRACTION_COMPLETE.value
    assert refreshed.semantic_abstract is not None
    assert refreshed.semantic_abstract != ""


async def test_reabstract_deferred_skips_pdfs_by_default(
    graph_store,
    stub_content_store,
    ingestion_service,
    minimal_config,
):
    """Default include_pdf=False: PDFs are recorded as skipped_pdf entries
    and their pipeline_status remains abstraction_skipped."""
    md_doc = _make_skipped_doc(_id("deferred_md_b"))
    pdf_doc = _make_skipped_doc(_id("deferred_pdf_b"), source_type=SourceType.PDF)
    await _seed_doc_with_chunks(graph_store, stub_content_store, md_doc)
    await _seed_doc_with_chunks(graph_store, stub_content_store, pdf_doc)

    maintenance = _build_maintenance(
        graph_store=graph_store,
        config=minimal_config,
        content_store=stub_content_store,
        ingestion_service=ingestion_service,
    )

    report = await maintenance.reabstract_deferred()

    assert report.reabstracted_count == 1
    assert report.skipped_pdf_count == 1
    assert report.failed_count == 0

    outcomes_by_id = {entry.document_id: entry.outcome for entry in report.entries}
    assert outcomes_by_id[md_doc.id] == ReabstractOutcome.SUCCESS
    assert outcomes_by_id[pdf_doc.id] == ReabstractOutcome.SKIPPED_PDF

    refreshed_pdf = await graph_store.get_document(pdf_doc.id)
    assert refreshed_pdf.pipeline_status == PipelineStatus.ABSTRACTION_SKIPPED.value
    assert refreshed_pdf.semantic_abstract is None


async def test_reabstract_deferred_includes_pdfs_when_flag_set(
    graph_store,
    stub_content_store,
    ingestion_service,
    minimal_config,
):
    """include_pdf=True: PDFs join the worklist and are reabstracted to
    abstraction_complete alongside markdown docs."""
    md_doc = _make_skipped_doc(_id("deferred_md_c"))
    pdf_doc = _make_skipped_doc(_id("deferred_pdf_c"), source_type=SourceType.PDF)
    await _seed_doc_with_chunks(graph_store, stub_content_store, md_doc)
    await _seed_doc_with_chunks(graph_store, stub_content_store, pdf_doc)

    maintenance = _build_maintenance(
        graph_store=graph_store,
        config=minimal_config,
        content_store=stub_content_store,
        ingestion_service=ingestion_service,
    )

    report = await maintenance.reabstract_deferred(include_pdf=True)

    assert report.reabstracted_count == 2
    assert report.skipped_pdf_count == 0
    assert report.failed_count == 0
    assert {e.outcome for e in report.entries} == {ReabstractOutcome.SUCCESS}

    for doc in (md_doc, pdf_doc):
        refreshed = await graph_store.get_document(doc.id)
        assert refreshed.pipeline_status == PipelineStatus.ABSTRACTION_COMPLETE.value
        assert refreshed.semantic_abstract is not None


async def test_reabstract_deferred_records_per_document_failure(
    graph_store,
    lock_manager,
    stub_content_store,
    stub_embedding_provider,
    minimal_config,
    lifecycle_service,
):
    """A failing first document does not abort the second.

    Uses _SelectivelyFailingProvider so the first generate_abstract call
    raises and the second succeeds. After the run, the first document
    has pipeline_status='failed' (terminal) and the report records an
    llm_failure outcome; the second has pipeline_status='abstraction_complete'.
    """
    provider = _SelectivelyFailingProvider()
    ingestion = _build_ingestion_service(
        graph_store=graph_store,
        lock_manager=lock_manager,
        content_store=stub_content_store,
        embedding_provider=stub_embedding_provider,
        abstraction_provider=provider,
        config=minimal_config,
        lifecycle_service=lifecycle_service,
    )

    # Use ids that sort to give us deterministic iteration order;
    # graph_store.list_all_documents returns in insertion order.
    fail_doc = _make_skipped_doc(_id("deferred_fail_a"))
    ok_doc = _make_skipped_doc(_id("deferred_ok_b"))
    await _seed_doc_with_chunks(
        graph_store, stub_content_store, fail_doc, body_text="FAILME body content."
    )
    await _seed_doc_with_chunks(graph_store, stub_content_store, ok_doc)

    maintenance = _build_maintenance(
        graph_store=graph_store,
        config=minimal_config,
        content_store=stub_content_store,
        ingestion_service=ingestion,
    )

    report = await maintenance.reabstract_deferred()

    assert report.reabstracted_count == 1
    assert report.failed_count == 1
    assert report.skipped_pdf_count == 0

    outcomes_by_id = {entry.document_id: entry.outcome for entry in report.entries}
    assert outcomes_by_id[fail_doc.id] == ReabstractOutcome.LLM_FAILURE
    assert outcomes_by_id[ok_doc.id] == ReabstractOutcome.SUCCESS

    refreshed_fail = await graph_store.get_document(fail_doc.id)
    refreshed_ok = await graph_store.get_document(ok_doc.id)
    assert refreshed_fail.pipeline_status == PipelineStatus.FAILED.value
    assert refreshed_ok.pipeline_status == PipelineStatus.ABSTRACTION_COMPLETE.value


async def test_reabstract_deferred_second_concurrent_call_raises_in_flight_error(
    graph_store,
    lock_manager,
    stub_content_store,
    stub_embedding_provider,
    minimal_config,
    lifecycle_service,
):
    """Single-flight per vault: a second call while the first is still
    running raises ReabstractAlreadyInFlightError with start_time set.
    After the first releases, a third call succeeds.

    Anti-coincidental-pass: the lock acquisition MUST be non-blocking
    (lock.locked() + raise) -- if implementation queues on
    ``await lock.acquire()``, the second call hangs and this test
    deadlocks instead of raising.
    """
    provider = _GatedAbstractionProvider()
    ingestion = _build_ingestion_service(
        graph_store=graph_store,
        lock_manager=lock_manager,
        content_store=stub_content_store,
        embedding_provider=stub_embedding_provider,
        abstraction_provider=provider,
        config=minimal_config,
        lifecycle_service=lifecycle_service,
    )

    doc = _make_skipped_doc(_id("deferred_lock_a"))
    await _seed_doc_with_chunks(graph_store, stub_content_store, doc)

    maintenance = _build_maintenance(
        graph_store=graph_store,
        config=minimal_config,
        content_store=stub_content_store,
        ingestion_service=ingestion,
    )

    # Capture a wall-clock window around the first call's lock
    # acquisition so we can assert start_time falls inside it.
    before = datetime.now(timezone.utc)
    task_a = asyncio.create_task(maintenance.reabstract_deferred())
    # Wait for the gated provider to hit its gate; by then the
    # background task is awaiting, the polling loop is active, and the
    # lock is held.
    await asyncio.wait_for(provider.entered.wait(), timeout=5.0)
    after = datetime.now(timezone.utc)

    with pytest.raises(ReabstractAlreadyInFlightError) as exc_info:
        await maintenance.reabstract_deferred()

    err = exc_info.value
    assert err.code == "reabstract_already_in_flight"
    assert err.status_code == 409
    assert err.detail is not None
    assert err.detail["vault_id"] == minimal_config.vault.id
    start_time = datetime.fromisoformat(err.detail["start_time"])
    assert before <= start_time <= after

    # Release the gate; the first call's polling loop sees terminal
    # status, the lock is released, the task completes.
    provider.gate.set()
    report_a = await asyncio.wait_for(task_a, timeout=5.0)
    assert report_a.reabstracted_count == 1

    # Third call against the now-empty worklist must succeed: confirms
    # the lock is released on completion.
    report_c = await maintenance.reabstract_deferred()
    assert report_c.reabstracted_count == 0


async def test_reabstract_deferred_hard_errors_when_no_ingestion_service(
    graph_store,
    minimal_config,
    stub_content_store,
):
    """MaintenanceService constructed with ingestion_service=None must
    raise RuntimeError naming the missing dependency rather than
    silently constructing a fallback provider.

    Anti-coincidental-pass: if a future implementer adds a
    "load my own provider" fallback path, this test catches it before
    that path can re-create the F-8 dual-Qwen3 hazard.
    """
    maintenance = _build_maintenance(
        graph_store=graph_store,
        config=minimal_config,
        content_store=stub_content_store,
        ingestion_service=None,
    )

    with pytest.raises(RuntimeError) as exc_info:
        await maintenance.reabstract_deferred()
    assert "ingestion" in str(exc_info.value).lower()


# ---------------------------------------------------------------------------
# Streaming-generator tests
#
# Cover the async-generator surface of MaintenanceService that the new
# SSE-emitting HTTP route consumes. The aggregator tests above pin the
# MCP-facing contract; these tests pin the per-event wire shape and the
# error-before-stream-open contract.
# ---------------------------------------------------------------------------


async def test_reabstract_deferred_events_yields_started_then_completed_per_document(
    graph_store,
    stub_content_store,
    ingestion_service,
    minimal_config,
):
    """Two abstraction_skipped docs produce ordered events:
    started(A), completed(A), started(B), completed(B), summary.

    Pins the per-doc started+completed pair shape (the maintenance-panel
    UX affordance: 'X is processing now' followed by 'X finished'),
    monotonic ``processed`` counter (0 on first started, incrementing to
    N on the Nth terminal event), constant ``total``, and the summary
    event landing last.

    Anti-coincidental-pass: a summary-only stream that drops progress
    events fails the ``assert len(progress) == 4`` check; an ordering
    bug fails the per-doc started/completed pairing check.
    """
    doc_a = _make_skipped_doc(_id("stream_md_a"))
    doc_b = _make_skipped_doc(_id("stream_md_b"))
    await _seed_doc_with_chunks(graph_store, stub_content_store, doc_a)
    await _seed_doc_with_chunks(graph_store, stub_content_store, doc_b)

    maintenance = _build_maintenance(
        graph_store=graph_store,
        config=minimal_config,
        content_store=stub_content_store,
        ingestion_service=ingestion_service,
    )

    events: list = []
    async for ev in maintenance.reabstract_deferred_events(include_pdf=False):
        events.append(ev)

    progress = [e for e in events if isinstance(e, ReabstractProgressEvent)]
    summaries = [e for e in events if isinstance(e, ReabstractSummaryEvent)]

    # 2 docs × (started + completed) = 4 progress events, then 1 summary.
    assert len(progress) == 4, f"expected 4 progress events, got {len(progress)}: {events!r}"
    assert len(summaries) == 1
    assert events[-1] is summaries[0], "summary event must be last"

    # First doc: started(processed=0) -> completed(processed=1).
    assert progress[0].status == "started"
    assert progress[0].current_document_id == doc_a.id
    assert progress[0].processed == 0
    assert progress[0].total == 2

    assert progress[1].status == "completed"
    assert progress[1].current_document_id == doc_a.id
    assert progress[1].outcome == ReabstractOutcome.SUCCESS
    assert progress[1].processed == 1
    assert progress[1].total == 2

    # Second doc: started(processed=1) -> completed(processed=2).
    assert progress[2].status == "started"
    assert progress[2].current_document_id == doc_b.id
    assert progress[2].processed == 1
    assert progress[2].total == 2

    assert progress[3].status == "completed"
    assert progress[3].current_document_id == doc_b.id
    assert progress[3].outcome == ReabstractOutcome.SUCCESS
    assert progress[3].processed == 2
    assert progress[3].total == 2

    # Summary event mirrors the aggregator's ReabstractReport.
    summary = summaries[0]
    assert summary.vault_id == minimal_config.vault.id
    assert summary.reabstracted_count == 2
    assert summary.skipped_pdf_count == 0
    assert summary.failed_count == 0
    assert len(summary.entries) == 2


async def test_reabstract_deferred_events_emits_skipped_for_pdf_when_include_pdf_false(
    graph_store,
    stub_content_store,
    ingestion_service,
    minimal_config,
):
    """include_pdf=False: a PDF in the worklist surfaces as a single
    ``status=skipped`` progress event (no started/completed pair --
    skipped_pdf is not dispatched). Markdown doc still gets the normal
    pair.

    Anti-coincidental-pass: if the PDF branch silently drops events,
    the user sees nothing happen for skipped entries in the maintenance
    panel. The assertion ``len(progress) == 3`` (markdown started +
    markdown completed + pdf skipped) catches that omission directly.
    """
    md_doc = _make_skipped_doc(_id("stream_md_pdf"))
    pdf_doc = _make_skipped_doc(_id("stream_pdf_skip"), source_type=SourceType.PDF)
    await _seed_doc_with_chunks(graph_store, stub_content_store, md_doc)
    await _seed_doc_with_chunks(graph_store, stub_content_store, pdf_doc)

    maintenance = _build_maintenance(
        graph_store=graph_store,
        config=minimal_config,
        content_store=stub_content_store,
        ingestion_service=ingestion_service,
    )

    events: list = []
    async for ev in maintenance.reabstract_deferred_events(include_pdf=False):
        events.append(ev)

    progress = [e for e in events if isinstance(e, ReabstractProgressEvent)]
    skipped = [p for p in progress if p.status == "skipped"]

    assert len(progress) == 3, f"expected 3 progress events, got {len(progress)}: {events!r}"
    assert len(skipped) == 1
    assert skipped[0].current_document_id == pdf_doc.id
    assert skipped[0].outcome == ReabstractOutcome.SKIPPED_PDF

    # Markdown doc had its normal started/completed pair.
    md_events = [p for p in progress if p.current_document_id == md_doc.id]
    assert {p.status for p in md_events} == {"started", "completed"}

    # Summary reflects the split.
    summary = events[-1]
    assert isinstance(summary, ReabstractSummaryEvent)
    assert summary.reabstracted_count == 1
    assert summary.skipped_pdf_count == 1
    assert summary.failed_count == 0


async def test_reabstract_deferred_events_emits_failed_then_continues(
    graph_store,
    lock_manager,
    stub_content_store,
    stub_embedding_provider,
    minimal_config,
    lifecycle_service,
):
    """A mid-batch LLM failure surfaces as a status=failed progress event
    and the generator continues to the next document. Mirrors the
    aggregator's per-doc isolation test (#5) at the streaming layer.

    Anti-coincidental-pass: if the failure path raises out of the
    generator instead of yielding a ``failed`` event, ``async for``
    propagates the exception and the second doc never gets a started
    event. The ``len(progress) == 4`` assertion plus the started/failed
    + started/completed pairing checks catch that.
    """
    provider = _SelectivelyFailingProvider()
    ingestion = _build_ingestion_service(
        graph_store=graph_store,
        lock_manager=lock_manager,
        content_store=stub_content_store,
        embedding_provider=stub_embedding_provider,
        abstraction_provider=provider,
        config=minimal_config,
        lifecycle_service=lifecycle_service,
    )

    fail_doc = _make_skipped_doc(_id("stream_fail_a"))
    ok_doc = _make_skipped_doc(_id("stream_ok_b"))
    await _seed_doc_with_chunks(
        graph_store, stub_content_store, fail_doc, body_text="FAILME body content."
    )
    await _seed_doc_with_chunks(graph_store, stub_content_store, ok_doc)

    maintenance = _build_maintenance(
        graph_store=graph_store,
        config=minimal_config,
        content_store=stub_content_store,
        ingestion_service=ingestion,
    )

    events: list = []
    async for ev in maintenance.reabstract_deferred_events(include_pdf=False):
        events.append(ev)

    progress = [e for e in events if isinstance(e, ReabstractProgressEvent)]
    assert len(progress) == 4, f"expected 4 progress events, got {len(progress)}: {events!r}"

    # First doc -> started + failed.
    assert progress[0].status == "started"
    assert progress[0].current_document_id == fail_doc.id

    assert progress[1].status == "failed"
    assert progress[1].current_document_id == fail_doc.id
    assert progress[1].outcome == ReabstractOutcome.LLM_FAILURE
    assert progress[1].error is not None and progress[1].error != ""

    # Second doc -> started + completed (loop continued past the failure).
    assert progress[2].status == "started"
    assert progress[2].current_document_id == ok_doc.id

    assert progress[3].status == "completed"
    assert progress[3].current_document_id == ok_doc.id
    assert progress[3].outcome == ReabstractOutcome.SUCCESS

    summary = events[-1]
    assert isinstance(summary, ReabstractSummaryEvent)
    assert summary.reabstracted_count == 1
    assert summary.failed_count == 1


async def test_reabstract_deferred_events_summary_payload_is_reabstract_report_shaped(
    graph_store,
    stub_content_store,
    ingestion_service,
    minimal_config,
):
    """The summary event's payload (sans event_type discriminator) round-trips
    through ReabstractReport.model_validate. Pins the structural-equivalence
    contract that lets the MCP tool aggregator return a report dict
    derived from the streaming generator.

    Anti-coincidental-pass: a future change that adds a field to
    ReabstractSummaryEvent without backing it into ReabstractReport (or
    vice versa) breaks the model_validate call. The aggregator can no
    longer trivially derive its return from the summary event.
    """
    md_doc = _make_skipped_doc(_id("stream_shape_md"))
    pdf_doc = _make_skipped_doc(_id("stream_shape_pdf"), source_type=SourceType.PDF)
    await _seed_doc_with_chunks(graph_store, stub_content_store, md_doc)
    await _seed_doc_with_chunks(graph_store, stub_content_store, pdf_doc)

    maintenance = _build_maintenance(
        graph_store=graph_store,
        config=minimal_config,
        content_store=stub_content_store,
        ingestion_service=ingestion_service,
    )

    events: list = []
    async for ev in maintenance.reabstract_deferred_events(include_pdf=False):
        events.append(ev)

    summary = events[-1]
    assert isinstance(summary, ReabstractSummaryEvent)

    # The summary payload (minus the SSE event-type discriminator) is a
    # ReabstractReport in disguise. If the two shapes ever diverge, this
    # model_validate call fails closed.
    payload = summary.model_dump(exclude={"event_type"})
    report = ReabstractReport.model_validate(payload)
    assert report.vault_id == minimal_config.vault.id
    assert report.reabstracted_count == 1
    assert report.skipped_pdf_count == 1
    assert report.failed_count == 0
    assert len(report.entries) == 2


async def test_reabstract_deferred_events_raises_in_flight_before_first_yield(
    graph_store,
    lock_manager,
    stub_content_store,
    stub_embedding_provider,
    minimal_config,
    lifecycle_service,
):
    """The 409 in-flight check fires synchronously on the
    ``reabstract_deferred_events(...)`` call itself, BEFORE iteration
    starts.

    Why this matters: FastAPI's StreamingResponse opens the HTTP
    response (status 200, text/event-stream) as soon as it is
    constructed; the route handler must therefore see the
    ReabstractAlreadyInFlightError SYNCHRONOUSLY -- i.e., from the
    constructor call, not from the first ``__anext__()`` -- so it can
    raise instead of returning a StreamingResponse. The conventional
    ``async def`` generator does not execute its body until the first
    iteration, so the implementation must use the
    ``def`` -> ``self._impl_async_def`` wrapper pattern (precedent:
    ``IngestStreamingService.stream`` raises EmptyFileListError before
    constructing its StreamingResponse).

    Anti-coincidental-pass: if the in-flight check is deferred into the
    generator body, calling ``maintenance.reabstract_deferred_events(...)``
    succeeds without raising; ``pytest.raises`` then fails the test
    immediately.
    """
    provider = _GatedAbstractionProvider()
    ingestion = _build_ingestion_service(
        graph_store=graph_store,
        lock_manager=lock_manager,
        content_store=stub_content_store,
        embedding_provider=stub_embedding_provider,
        abstraction_provider=provider,
        config=minimal_config,
        lifecycle_service=lifecycle_service,
    )

    doc = _make_skipped_doc(_id("stream_lock_a"))
    await _seed_doc_with_chunks(graph_store, stub_content_store, doc)

    maintenance = _build_maintenance(
        graph_store=graph_store,
        config=minimal_config,
        content_store=stub_content_store,
        ingestion_service=ingestion,
    )

    async def _consume_first(gen):
        async for ev in gen:
            # Drain to completion once the gate releases.
            del ev

    # Start the first stream consuming, which acquires the lock.
    gen_a = maintenance.reabstract_deferred_events(include_pdf=False)
    task_a = asyncio.create_task(_consume_first(gen_a))

    # Wait for the gated provider to be hit -- by then the lock is held.
    await asyncio.wait_for(provider.entered.wait(), timeout=5.0)

    # SYNCHRONOUS check: calling reabstract_deferred_events MUST raise
    # before any iteration. No `await`, no `async for`.
    with pytest.raises(ReabstractAlreadyInFlightError) as exc_info:
        maintenance.reabstract_deferred_events(include_pdf=False)
    after = datetime.now(timezone.utc)

    err = exc_info.value
    assert err.code == "reabstract_already_in_flight"
    assert err.status_code == 409
    assert err.detail is not None
    assert err.detail["vault_id"] == minimal_config.vault.id
    start_time = datetime.fromisoformat(err.detail["start_time"])
    # start_time was set by the in-flight caller inside the lock, so it
    # predates this point in wall-clock time.
    assert start_time <= after

    # Release the gate; let the first stream complete and clean up.
    provider.gate.set()
    await asyncio.wait_for(task_a, timeout=5.0)


async def test_reabstract_deferred_aggregator_consumes_event_stream(
    graph_store,
    ingestion_service,
    minimal_config,
    monkeypatch,
    stub_content_store,
):
    """reabstract_deferred() builds its ReabstractReport by consuming the
    reabstract_deferred_events() generator. Monkeypatching the generator
    to yield a fabricated sequence proves the aggregator does not
    re-implement the per-document iteration.

    Anti-coincidental-pass: if the aggregator carries its own duplicate
    iteration loop (i.e., does not consume the generator), the
    monkeypatched events have no effect and the aggregator returns the
    real worklist's report (empty in this seeded vault). The
    assertions then fail on the fabricated counts.
    """
    maintenance = _build_maintenance(
        graph_store=graph_store,
        config=minimal_config,
        content_store=stub_content_store,
        ingestion_service=ingestion_service,
    )

    # Build fabricated events that DO NOT correspond to any real
    # vault state; they should still flow through the aggregator's
    # return value if the aggregator consumes the generator.
    fake_doc_id_ok = _id("fake_aggregator_ok")
    fake_doc_id_pdf = _id("fake_aggregator_pdf")

    def _fake_events_factory(include_pdf: bool = False):  # noqa: ARG001
        async def _gen():
            yield ReabstractProgressEvent(
                event_type="progress",
                processed=0,
                total=2,
                current_document_id=fake_doc_id_ok,
                current_title="Fake OK",
                status="started",
            )
            yield ReabstractProgressEvent(
                event_type="progress",
                processed=1,
                total=2,
                current_document_id=fake_doc_id_ok,
                current_title="Fake OK",
                status="completed",
                outcome=ReabstractOutcome.SUCCESS,
                elapsed_seconds=0.01,
            )
            yield ReabstractProgressEvent(
                event_type="progress",
                processed=1,
                total=2,
                current_document_id=fake_doc_id_pdf,
                current_title="Fake PDF",
                status="skipped",
                outcome=ReabstractOutcome.SKIPPED_PDF,
            )
            yield ReabstractSummaryEvent(
                event_type="summary",
                vault_id=minimal_config.vault.id,
                reabstracted_count=1,
                skipped_pdf_count=1,
                failed_count=0,
                entries=[],
            )

        return _gen()

    monkeypatch.setattr(maintenance, "reabstract_deferred_events", _fake_events_factory)

    report = await maintenance.reabstract_deferred()

    # If the aggregator runs its own loop instead of consuming the
    # generator, these fail (the real worklist is empty).
    assert report.reabstracted_count == 1
    assert report.skipped_pdf_count == 1
    assert report.failed_count == 0
