"""Bulk reabstract sweep selected by pipeline status (out-of-band operator tool).

Recovery tool for the stranded-abstract scenario: an abstraction-provider
outage, a credit exhaustion, or a model misconfiguration fails a large batch
mid-ingest and leaves every affected document at a terminal ``failed`` pipeline
status. Content, chunks, and embeddings survive intact -- only the abstract is
missing -- but a failed-pipeline document is excluded from scoring retrieval, so
the records go silently absent while the vault keeps answering queries normally.

No existing lever covers the case. The deferred-abstract sweep enumerates
``abstraction_skipped`` only; startup recovery leaves skip and failure alone as
operator territory, and reaches ``abstraction_interrupted`` only at the next
process start. This module is that operator lever: a status-selected sweep that
re-dispatches abstraction per document and reports what happened to each one,
without waiting for a restart.

Safeguards:
- Dry-run is the default. The caller must pass ``apply=True`` for any dispatch,
  so a mistyped selector costs a worklist listing rather than provider spend.
- The status selector is validated against the pipeline-status vocabulary and an
  unrecognized token is named in the refusal alongside the accepted values.
- Enumeration opts out of the storage layer's default failed-pipeline exclusion.
  Without that opt-out the sweep would enumerate nothing and report a clean run.
- A per-document failure is recorded and the sweep continues. One unrecoverable
  document cannot strand the rest of the batch, which is the whole point of a
  bulk lever.
- ``limit`` caps a single run so a sweep larger than the execution window can be
  taken in chunks rather than killed part-way through with no record.
- Re-execution is selector-shaped: the worklist is recomputed from live pipeline
  status on every run, so a ``failed`` selector resumes an interrupted sweep
  (documents already recovered to ``abstraction_complete`` drop out) while
  ``all`` redoes every document. Platform-level retry tolerance for the sweep
  leans on this property; the destructive maintenance commands run with no
  retry at all (CAS-ADR-029).

Non-destructive: the sweep only regenerates abstracts on documents whose
projection chunks are still stored. It removes nothing.

This module is unreachable from the SAGE Core API and MCP server by
architectural invariant (import-topology test).
"""

from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timezone

from sage.adapters.interfaces import GraphStore
from sage.models.enums import (
    TERMINAL_PIPELINE_STATUS_VALUES,
    PipelineStatus,
    ReabstractOutcome,
)
from sage.models.schemas import Document, ReabstractReport, ReabstractReportEntry

# Selector token expanding to the whole pipeline-status vocabulary.
ALL_SELECTOR = "all"

# Statuses swept when the caller supplies no selector: the terminal states a
# document reaches with its abstract missing. The remaining terminal state
# already has an abstract, and a non-terminal one is mid-flight and owned by the
# running worker.
DEFAULT_STATUSES: frozenset[str] = frozenset(
    {
        PipelineStatus.FAILED.value,
        PipelineStatus.ABSTRACTION_SKIPPED.value,
        PipelineStatus.ABSTRACTION_INTERRUPTED.value,
    }
)

# Rows fetched per enumeration query. The predicate is pushed into the indexed
# pipeline_status column and paged, rather than pulling the whole vault into
# memory and filtering in Python.
_PAGE_SIZE = 500

# Poll interval for the post-dispatch wait-for-terminal loop. Matches the
# in-process service path: each abstraction takes seconds, so 50 ms keeps the
# polling overhead negligible without adding perceptible latency.
_POLL_INTERVAL_SECONDS = 0.05


# Sentinel returned when a document disappears between dispatch and settling.
_MISSING = "missing"


def parse_status_selector(raw: str | None) -> frozenset[str]:
    """Resolve a status-selector string to a set of pipeline-status values.

    An empty or absent selector yields :data:`DEFAULT_STATUSES`. The token
    ``all`` expands to the full vocabulary, read off the enum so a status added
    later is covered rather than silently dropped; because it is a superset,
    mixing it with named statuses is honored rather than refused. Tokens are
    comma-separated, whitespace-tolerant, and case-insensitive.

    Raises:
        ValueError: an unrecognized token, named alongside the accepted values.
    """
    vocabulary = frozenset(s.value for s in PipelineStatus)
    text = (raw or "").strip()
    if not text:
        return DEFAULT_STATUSES
    tokens = [t.strip().lower() for t in text.split(",") if t.strip()]
    if not tokens:
        return DEFAULT_STATUSES
    if ALL_SELECTOR in tokens:
        return vocabulary
    unknown = [t for t in tokens if t not in vocabulary]
    if unknown:
        raise ValueError(
            f"unrecognized pipeline status {', '.join(repr(t) for t in unknown)}; "
            f"accepted values are {', '.join(sorted(vocabulary))} "
            f"(or {ALL_SELECTOR!r} for every status)."
        )
    return frozenset(tokens)


async def collect_worklist(
    *,
    graph_store: GraphStore,
    statuses: frozenset[str],
    limit: int | None,
) -> tuple[list[Document], int]:
    """Enumerate documents in the selected statuses, capped at ``limit``.

    Returns the (possibly capped) worklist and the total number of matching
    documents across every selected status, so the caller can report how much a
    cap left behind. Statuses are visited in the vocabulary's declaration order
    for a stable worklist.

    Every query opts out of the default failed-pipeline exclusion: ``failed`` is
    the status this sweep exists to recover, and the default would filter it out.

    Because enumeration reads live ``pipeline_status``, a re-run with a
    ``failed`` selector resumes — recovered documents no longer match — while
    ``all`` re-enumerates everything.
    """
    worklist: list[Document] = []
    total = 0
    for status in (s.value for s in PipelineStatus):
        if status not in statuses:
            continue
        offset = 0
        while True:
            remaining = None if limit is None else limit - len(worklist)
            page_size = _PAGE_SIZE if remaining is None else max(min(_PAGE_SIZE, remaining), 1)
            page, matched = await graph_store.query_documents(
                filters={"pipeline_status": status},
                limit=page_size,
                offset=offset,
                default_exclude_failed=False,
            )
            if offset == 0:
                # Counted once per status, before any cap short-circuits the
                # paging, so the total reflects the whole selection.
                total += matched
            if remaining is not None and remaining <= 0:
                break
            take = page if remaining is None else page[:remaining]
            worklist.extend(take)
            offset += len(page)
            if not page or offset >= matched:
                break
    return worklist, total


async def _wait_for_terminal(
    graph_store: GraphStore, document_id: str, poll_interval: float
) -> str:
    """Poll a document's pipeline status until terminal, then return it.

    Returns :data:`_MISSING` when the document disappears mid-flight.
    """
    while True:
        doc = await graph_store.get_document(document_id)
        if doc is None:
            return _MISSING
        status = str(doc.pipeline_status)
        if status in TERMINAL_PIPELINE_STATUS_VALUES:
            return status
        await asyncio.sleep(poll_interval)


async def run_sweep(
    *,
    graph_store: GraphStore,
    ingestion_service,
    vault_id: str,
    worklist: list[Document],
    poll_interval: float = _POLL_INTERVAL_SECONDS,
) -> ReabstractReport:
    """Dispatch reabstraction for every document in ``worklist`` and report.

    Each document is dispatched, then polled to a terminal status. A dispatch
    that raises and a document that settles anywhere other than
    ``abstraction_complete`` are both recorded as failures and the sweep
    continues to the next document.

    ``skipped_pdf_count`` is always zero: a failed PDF is exactly as worth
    recovering as any other document, so this sweep applies no source-type
    filter. The field is carried to keep the report shape uniform with the
    deferred-abstract report.
    """
    entries: list[ReabstractReportEntry] = []
    reabstracted = 0
    failed = 0
    total = len(worklist)

    for index, doc in enumerate(worklist, start=1):
        started = datetime.now(timezone.utc)
        try:
            await ingestion_service.reabstract(doc.id)
        except Exception as exc:
            elapsed = (datetime.now(timezone.utc) - started).total_seconds()
            message = f"dispatch failed: {exc!r}"
            entries.append(
                ReabstractReportEntry(
                    document_id=doc.id,
                    outcome=ReabstractOutcome.LLM_FAILURE,
                    error_message=message,
                    elapsed_seconds=elapsed,
                )
            )
            failed += 1
            print(f"[{index:5d}/{total}]  x  {doc.id}  {message}", flush=True)
            continue

        status = await _wait_for_terminal(graph_store, doc.id, poll_interval)
        elapsed = (datetime.now(timezone.utc) - started).total_seconds()
        if status == PipelineStatus.ABSTRACTION_COMPLETE.value:
            entries.append(
                ReabstractReportEntry(
                    document_id=doc.id,
                    outcome=ReabstractOutcome.SUCCESS,
                    elapsed_seconds=elapsed,
                )
            )
            reabstracted += 1
            print(f"[{index:5d}/{total}]  ok {doc.id}  {elapsed:6.1f}s", flush=True)
        else:
            message = f"terminal pipeline_status: {status}"
            entries.append(
                ReabstractReportEntry(
                    document_id=doc.id,
                    outcome=ReabstractOutcome.LLM_FAILURE,
                    error_message=message,
                    elapsed_seconds=elapsed,
                )
            )
            failed += 1
            print(f"[{index:5d}/{total}]  x  {doc.id}  {message}", flush=True)

    return ReabstractReport(
        vault_id=vault_id,
        reabstracted_count=reabstracted,
        skipped_pdf_count=0,
        failed_count=failed,
        entries=entries,
    )


async def reabstract_bulk(
    *,
    graph_store: GraphStore,
    ingestion_service,
    vault_id: str,
    statuses: frozenset[str],
    limit: int | None,
    reason: str,
    apply: bool,
    poll_interval: float = _POLL_INTERVAL_SECONDS,
) -> int:
    """Enumerate, report the plan, and (when ``apply``) run the sweep.

    Returns a process exit code: 0 for a dry run, an empty worklist, or a sweep
    in which every document reached ``abstraction_complete``; 1 when any
    document failed.
    """
    worklist, total = await collect_worklist(
        graph_store=graph_store, statuses=statuses, limit=limit
    )

    print(f"Vault:      {vault_id}")
    print(f"Statuses:   {', '.join(sorted(statuses))}")
    print(f"Reason:     {reason}")
    print(f"Matching:   {total}")
    print(f"Worklist:   {len(worklist)}")
    if limit is not None and total > len(worklist):
        print(f"  capped at {limit}; {total - len(worklist)} document(s) left for a later run")
    print()

    if not worklist:
        print("(no documents in the selected statuses; nothing to do)")
        return 0

    if not apply:
        for doc in worklist:
            print(f"  {doc.id}  [{doc.pipeline_status}]  {doc.title}")
        print()
        print("(dry-run; set the apply flag to dispatch)")
        return 0

    report = await run_sweep(
        graph_store=graph_store,
        ingestion_service=ingestion_service,
        vault_id=vault_id,
        worklist=worklist,
        poll_interval=poll_interval,
    )

    print()
    print(
        f"reabstract complete: {report.reabstracted_count} succeeded, "
        f"{report.failed_count} failed, of {len(report.entries)} attempted."
    )
    if report.failed_count:
        print("Failures:", file=sys.stderr)
        for entry in report.entries:
            if entry.outcome == ReabstractOutcome.LLM_FAILURE:
                print(f"  {entry.document_id}: {entry.error_message}", file=sys.stderr)
        return 1
    return 0
