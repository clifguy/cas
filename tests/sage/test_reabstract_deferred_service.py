"""Unit tests for MaintenanceService.reabstract_deferred (T-0089, CAS-ADR-029).

Graduation of scripts/reabstract_deferred.py into the maintenance API
surface. The new service method enumerates documents whose
pipeline_status is 'abstraction_skipped', delegates each to the
in-process IngestionService.reabstract (reusing the already-loaded
AbstractionProvider per F-8), polls until each reaches a terminal
status, and assembles a ReabstractReport.

Test surface pins the seven contracts from T-0089:

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
from sage.models.schemas import Document, ReabstractReport
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
    """Stub provider that fails on the first call and succeeds afterward.

    Used by the per-document failure test (#5) to exercise the loop's
    isolation contract: one document's failure must not abort the
    sibling reabstract attempts.
    """

    def __init__(self) -> None:
        self.call_count = 0

    async def generate_abstract(self, text: str, max_tokens: int, doc_type: str | None) -> str:
        self.call_count += 1
        if self.call_count == 1:
            raise RuntimeError("simulated LLM failure for first call")
        return f"stub abstract after {self.call_count} calls"


def _build_maintenance(
    *,
    graph_store,
    config,
    ingestion_service: IngestionService | None,
) -> MaintenanceService:
    """Construct a MaintenanceService with the T-0089 ingestion dependency.

    db_path and registry_service are not exercised by reabstract_deferred
    (only migrate_vault touches them); we still pass concrete values so
    the constructor signature is satisfied. registry_service is None,
    matching test paths that do not exercise the migration reload.
    """
    from pathlib import Path

    return MaintenanceService(
        vault_id=config.vault.id,
        db_path=Path(config.vault.brain_root) / "graph.db",
        graph_store=graph_store,
        config=config,
        registry_service=None,
        ingestion_service=ingestion_service,
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


async def test_reabstract_deferred_empty_returns_zero_report(
    graph_store,
    ingestion_service,
    minimal_config,
):
    """Vault with no documents in abstraction_skipped returns an empty
    report; no work is dispatched."""
    maintenance = _build_maintenance(
        graph_store=graph_store,
        config=minimal_config,
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
    await _seed_doc_with_chunks(graph_store, stub_content_store, fail_doc)
    await _seed_doc_with_chunks(graph_store, stub_content_store, ok_doc)

    maintenance = _build_maintenance(
        graph_store=graph_store,
        config=minimal_config,
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
        ingestion_service=None,
    )

    with pytest.raises(RuntimeError) as exc_info:
        await maintenance.reabstract_deferred()
    assert "ingestion" in str(exc_info.value).lower()
