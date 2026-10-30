"""The deferred-abstract sweep reports documents with no readable text apart from failures.

A document ingested with an empty projection settles at ``abstraction_skipped``
with its abstraction evidence marked ``empty_readable_projection``. It has no
passages, so dispatching a reabstract for it can only be refused. The sweep
recognises it from that same coverage signal, reports it as
``no_readable_text`` without dispatching, and keeps it out of ``failed_count``
on every pass.
"""

from __future__ import annotations

from pathlib import Path

from sage.models.coverage import EMPTY_READABLE_PROJECTION, CoverageDimension
from sage.models.enums import PipelineStatus, ReabstractOutcome, SourceType
from sage.models.schemas import IngestRequest, ReabstractProgressEvent, ReabstractSummaryEvent
from sage.services.ingestion import IngestionService
from sage.source_adapters.markdown_adapter import MarkdownAdapter
from tests.sage.test_ingestion import _create_test_file, _EmptyTextStubAdapter
from tests.sage.test_lifecycle import _id
from tests.sage.test_reabstract_deferred_service import (
    _build_maintenance,
    _make_skipped_doc,
    _seed_doc_with_chunks,
)


class _EmptyPdfAdapter(_EmptyTextStubAdapter):
    """The empty-text stub, registered for PDF sources."""

    EXTENSIONS = [".pdf"]


def _empty_text_ingestion(
    *,
    graph_store,
    lock_manager,
    stub_content_store,
    stub_embedding_provider,
    minimal_config,
    lifecycle_service,
    abstraction_provider,
) -> IngestionService:
    """An ingestion service whose adapters project every source to empty text."""
    return IngestionService(
        graph_store=graph_store,
        lock_manager=lock_manager,
        content_store=stub_content_store,
        embedding_provider=stub_embedding_provider,
        abstraction_provider=abstraction_provider,
        config=minimal_config,
        source_adapters={
            SourceType.MARKDOWN: _EmptyTextStubAdapter(),
            SourceType.PDF: _EmptyPdfAdapter(),
        },
        lifecycle_service=lifecycle_service,
    )


async def _ingest_unreadable(
    service: IngestionService,
    tmp_vault_dir: Path,
    graph_store,
    relative_path: str,
    source_type: SourceType = SourceType.MARKDOWN,
) -> str:
    """Ingest a source whose projection is empty; return its document id."""
    _create_test_file(tmp_vault_dir, relative_path, content="# Scan\n\nNo extractable text.")
    result = await service.ingest(IngestRequest(source=relative_path, source_type=source_type))
    settled = await graph_store.get_document(result.document.id)
    assert settled.pipeline_status == PipelineStatus.ABSTRACTION_SKIPPED.value
    return result.document.id


async def mark_unreadable(graph_store, content_store, doc) -> None:
    """Insert ``doc`` and record the coverage an empty projection publishes.

    For boundary tests that exercise transport rather than ingestion; the
    writer itself is exercised through the real pipeline above.
    """
    await graph_store.insert_document(doc)
    attempt = await content_store.begin_processing_attempt(doc.id)
    assert await content_store.publish_processing(
        doc.id,
        attempt,
        {
            "abstraction_evidence": CoverageDimension(
                scope="not_applicable", limitations=[EMPTY_READABLE_PROJECTION]
            )
        },
    )


class _DispatchSpy:
    """Records which documents the sweep dispatched, delegating to the real call."""

    def __init__(self, ingestion: IngestionService) -> None:
        self.dispatched: list[str] = []
        self._real = ingestion.reabstract
        ingestion.reabstract = self  # type: ignore[method-assign]

    async def __call__(self, document_id: str) -> dict:
        self.dispatched.append(document_id)
        return await self._real(document_id)


async def test_unreadable_document_is_reported_apart_from_failures_on_every_sweep(
    tmp_vault_dir,
    graph_store,
    lock_manager,
    stub_content_store,
    stub_embedding_provider,
    minimal_config,
    lifecycle_service,
    ingestion_service,
    stub_abstraction_provider,
):
    """One abstractable deferred document and one with an empty readable projection.

    The first is abstracted; the second is reported ``no_readable_text``,
    never dispatched and never counted as a failure, and a repeat sweep
    reports it the same way.
    """
    readable = _make_skipped_doc(_id("readable"))
    await _seed_doc_with_chunks(graph_store, stub_content_store, readable)
    unreadable_id = await _ingest_unreadable(
        _empty_text_ingestion(
            graph_store=graph_store,
            lock_manager=lock_manager,
            stub_content_store=stub_content_store,
            stub_embedding_provider=stub_embedding_provider,
            minimal_config=minimal_config,
            lifecycle_service=lifecycle_service,
            abstraction_provider=stub_abstraction_provider,
        ),
        tmp_vault_dir,
        graph_store,
        "scans/unreadable.md",
    )
    spy = _DispatchSpy(ingestion_service)
    maintenance = _build_maintenance(
        graph_store=graph_store,
        config=minimal_config,
        content_store=stub_content_store,
        ingestion_service=ingestion_service,
    )

    first = await maintenance.reabstract_deferred()

    outcomes = {entry.document_id: entry.outcome for entry in first.entries}
    assert outcomes == {
        readable.id: ReabstractOutcome.SUCCESS,
        unreadable_id: ReabstractOutcome.NO_READABLE_TEXT,
    }
    assert first.reabstracted_count == 1
    assert first.no_readable_text_count == 1
    assert first.failed_count == 0
    assert spy.dispatched == [readable.id]
    unreadable_entry = next(e for e in first.entries if e.document_id == unreadable_id)
    assert unreadable_entry.error_message is None
    assert unreadable_entry.elapsed_seconds is None

    second = await maintenance.reabstract_deferred()

    assert [(e.document_id, e.outcome) for e in second.entries] == [
        (unreadable_id, ReabstractOutcome.NO_READABLE_TEXT)
    ]
    assert second.no_readable_text_count == 1
    assert second.failed_count == 0
    assert spy.dispatched == [readable.id]
    still = await graph_store.get_document(unreadable_id)
    assert still.pipeline_status == PipelineStatus.ABSTRACTION_SKIPPED.value


async def test_missing_passages_without_the_coverage_signal_remain_a_dispatch_failure(
    graph_store,
    stub_content_store,
    ingestion_service,
    minimal_config,
):
    """Absent passages alone do not establish unreadable text.

    A skipped document with no passages whose coverage carries no
    empty-readable-projection mark is still dispatched and still reported
    ``dispatch_failed``: the sweep decides from the recorded signal, not
    from a second heuristic.
    """
    unmarked = _make_skipped_doc(_id("unmarked"))
    await graph_store.insert_document(unmarked)
    maintenance = _build_maintenance(
        graph_store=graph_store,
        config=minimal_config,
        content_store=stub_content_store,
        ingestion_service=ingestion_service,
    )

    report = await maintenance.reabstract_deferred()

    assert [(e.document_id, e.outcome) for e in report.entries] == [
        (unmarked.id, ReabstractOutcome.DISPATCH_FAILED)
    ]
    assert report.failed_count == 1
    assert report.no_readable_text_count == 0


async def test_unreadable_document_streams_one_skipped_event_and_a_counted_summary(
    tmp_vault_dir,
    graph_store,
    lock_manager,
    stub_content_store,
    stub_embedding_provider,
    minimal_config,
    lifecycle_service,
    ingestion_service,
    stub_abstraction_provider,
):
    """The streaming shape: a single ``skipped`` event naming the outcome, then the count."""
    unreadable_id = await _ingest_unreadable(
        _empty_text_ingestion(
            graph_store=graph_store,
            lock_manager=lock_manager,
            stub_content_store=stub_content_store,
            stub_embedding_provider=stub_embedding_provider,
            minimal_config=minimal_config,
            lifecycle_service=lifecycle_service,
            abstraction_provider=stub_abstraction_provider,
        ),
        tmp_vault_dir,
        graph_store,
        "scans/streamed.md",
    )
    maintenance = _build_maintenance(
        graph_store=graph_store,
        config=minimal_config,
        content_store=stub_content_store,
        ingestion_service=ingestion_service,
    )

    events = [event async for event in maintenance.reabstract_deferred_events()]

    progress = [e for e in events if isinstance(e, ReabstractProgressEvent)]
    assert len(progress) == 1
    event = progress[0]
    assert event.current_document_id == unreadable_id
    assert event.status == "skipped"
    assert event.outcome == ReabstractOutcome.NO_READABLE_TEXT
    assert event.error is None
    assert event.elapsed_seconds is None
    assert (event.processed, event.total) == (1, 1)
    summary = events[-1]
    assert isinstance(summary, ReabstractSummaryEvent)
    assert summary.no_readable_text_count == 1
    assert summary.failed_count == 0


async def test_excluded_pdf_stays_skipped_pdf_and_an_included_one_is_no_readable_text(
    tmp_vault_dir,
    graph_store,
    lock_manager,
    stub_content_store,
    stub_embedding_provider,
    minimal_config,
    lifecycle_service,
    ingestion_service,
    stub_abstraction_provider,
):
    """The PDF exclusion is decided first; an included empty PDF is unreadable, not failing."""
    pdf_id = await _ingest_unreadable(
        _empty_text_ingestion(
            graph_store=graph_store,
            lock_manager=lock_manager,
            stub_content_store=stub_content_store,
            stub_embedding_provider=stub_embedding_provider,
            minimal_config=minimal_config,
            lifecycle_service=lifecycle_service,
            abstraction_provider=stub_abstraction_provider,
        ),
        tmp_vault_dir,
        graph_store,
        "scans/image-only.pdf",
        SourceType.PDF,
    )
    maintenance = _build_maintenance(
        graph_store=graph_store,
        config=minimal_config,
        content_store=stub_content_store,
        ingestion_service=ingestion_service,
    )

    excluded = await maintenance.reabstract_deferred()
    included = await maintenance.reabstract_deferred(include_pdf=True)

    assert [(e.document_id, e.outcome) for e in excluded.entries] == [
        (pdf_id, ReabstractOutcome.SKIPPED_PDF)
    ]
    assert (excluded.skipped_pdf_count, excluded.no_readable_text_count) == (1, 0)
    assert [(e.document_id, e.outcome) for e in included.entries] == [
        (pdf_id, ReabstractOutcome.NO_READABLE_TEXT)
    ]
    assert (included.skipped_pdf_count, included.no_readable_text_count) == (0, 1)
    assert included.failed_count == 0


async def test_ingestion_records_the_signal_the_sweep_reads(
    tmp_vault_dir,
    graph_store,
    lock_manager,
    stub_content_store,
    stub_embedding_provider,
    minimal_config,
    lifecycle_service,
    stub_abstraction_provider,
):
    """The writer and the sweep share one predicate over recorded coverage.

    An empty projection published by the real pipeline satisfies it; an
    ordinary readable document does not.
    """
    from sage.models.coverage import has_empty_readable_projection

    unreadable_id = await _ingest_unreadable(
        _empty_text_ingestion(
            graph_store=graph_store,
            lock_manager=lock_manager,
            stub_content_store=stub_content_store,
            stub_embedding_provider=stub_embedding_provider,
            minimal_config=minimal_config,
            lifecycle_service=lifecycle_service,
            abstraction_provider=stub_abstraction_provider,
        ),
        tmp_vault_dir,
        graph_store,
        "scans/signal.md",
    )
    readable_service = IngestionService(
        graph_store=graph_store,
        lock_manager=lock_manager,
        content_store=stub_content_store,
        embedding_provider=stub_embedding_provider,
        abstraction_provider=stub_abstraction_provider,
        config=minimal_config,
        source_adapters={SourceType.MARKDOWN: MarkdownAdapter()},
        lifecycle_service=lifecycle_service,
    )
    _create_test_file(tmp_vault_dir, "notes/readable.md", content="# Note\n\nReadable body.")
    readable = await readable_service.ingest(
        IngestRequest(source="notes/readable.md", source_type=SourceType.MARKDOWN)
    )

    unreadable_coverage, _ = await stub_content_store.read_processing_snapshot(unreadable_id)
    readable_coverage, _ = await stub_content_store.read_processing_snapshot(readable.document.id)

    assert has_empty_readable_projection(unreadable_coverage)
    assert not has_empty_readable_projection(readable_coverage)
