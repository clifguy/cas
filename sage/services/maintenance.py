"""Vault-scoped maintenance/admin operations (CAS-ADR-029).

Pilot operation: schema migration for a single vault in the running
session. Subsequent ``sage_admin_*`` operations slot into the same
three-layer service + router + MCP-tool shape.
"""

from __future__ import annotations

import asyncio
import shutil
import sqlite3
import tempfile
from collections.abc import AsyncGenerator
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from sage.api.errors import ReabstractAlreadyInFlightError
from sage.config import VaultConfig
from sage.models.enums import EdgeType, PipelineStatus, ReabstractOutcome, StalenessBasis
from sage.models.schemas import (
    DriftEntry,
    DriftReport,
    MigrationReport,
    MigrationReportEntry,
    ReabstractProgressEvent,
    ReabstractReport,
    ReabstractReportEntry,
    ReabstractSummaryEvent,
    Tier3UniquenessActivation,
    Tier3UniquenessCollision,
)
from sage.storage.graph_store import GraphStore
from sage.storage.migrations import (
    BACKFILL_PLAN,
    MIGRATION_PLAN,
    TABLES,
    Backfill,
    Migration,
    Tier3UniqueIndexBlockedError,
    pending_backfills,
    pending_migrations,
)

# Union type for events yielded by reabstract_deferred_events.
ReabstractEvent = ReabstractProgressEvent | ReabstractSummaryEvent

if TYPE_CHECKING:
    from sage.services.ingestion import IngestionService
    from sage.services.vault_registry import VaultRegistryService


# Poll interval for the post-dispatch wait-for-terminal loop. Hardcoded
# at 50 ms: fast enough to keep request latency dominated by the
# abstraction call itself (each reabstract takes seconds), slow enough
# that the polling overhead is negligible. The standalone-script path
# defaults to 1.0 s because it ran with a TTY in the loop; the
# in-process service path has no such concern.
_POLL_INTERVAL_SECONDS = 0.05


def _detect_pending_work(db_path: Path) -> tuple[list[Migration], list[Backfill]]:
    """Detect what GraphStore.initialize(migrate=True) would apply.

    Returns ``(pending_alters, pending_bfs)`` as the graph_store would
    see them after applying the pending ALTER TABLE migrations: backfill
    detection runs against a temp copy of the db whose schema has the
    post-migration columns. This mirrors the re-detect step in
    ``GraphStore._initialize_sync`` so backfills that depend on newly-
    added columns (e.g., the T-0080 rationale_kind backfill, whose
    detector queries the ``rationale_kind`` column that the migration
    itself adds) are surfaced rather than silently skipped.

    The live db file is not touched; the temp copy is discarded.
    """
    live = sqlite3.connect(str(db_path))
    try:
        pending_alters = pending_migrations(live, MIGRATION_PLAN)
    finally:
        live.close()

    with tempfile.NamedTemporaryFile(
        suffix=".db", delete=False, prefix="sage_migrate_detect_"
    ) as tmp:
        tmp_path = Path(tmp.name)
    try:
        shutil.copy2(db_path, tmp_path)
        sim = sqlite3.connect(str(tmp_path))
        try:
            # Mirror GraphStore._initialize_sync ordering: CREATE TABLE
            # IF NOT EXISTS for every table (so a backfill detector that
            # queries a not-yet-extant table like ``document_tags`` does
            # not raise OperationalError and get silently swallowed),
            # then apply pending ALTER TABLE migrations so backfills
            # whose detectors query newly-added columns see them.
            for ddl in TABLES:
                sim.execute(ddl)
            for m in pending_alters:
                sim.execute(m.ddl)
            pending_bfs = pending_backfills(sim, BACKFILL_PLAN)
        finally:
            sim.close()
    finally:
        tmp_path.unlink(missing_ok=True)

    return pending_alters, pending_bfs


class MaintenanceService:
    """Pilot of the maintenance/admin API surface (CAS-ADR-029)."""

    def __init__(
        self,
        vault_id: str,
        db_path: Path,
        graph_store: GraphStore,
        config: VaultConfig,
        registry_service: "VaultRegistryService | None",
        ingestion_service: "IngestionService | None" = None,
    ) -> None:
        self._vault_id = vault_id
        self._db_path = db_path
        self._graph_store = graph_store
        self._config = config
        self._registry_service = registry_service
        self._ingestion = ingestion_service
        # Per-vault single-flight lock for reabstract_deferred (T-0089).
        # Non-blocking check: a second caller raises rather than queueing
        # (reabstract passes can run for minutes against the in-process
        # Qwen3; silently queuing would mask client-side coordination
        # bugs). _reabstract_started_at is set inside the lock so a
        # rejected concurrent caller can read it without racing.
        self._reabstract_lock = asyncio.Lock()
        self._reabstract_started_at: datetime | None = None

    async def migrate_vault(self) -> MigrationReport:
        """Apply pending schema migrations and reload the vault in-session.

        Read-only pre-detect: enumerate which MIGRATION_PLAN columns and
        BACKFILL_PLAN entries are pending, simulating the post-migration
        schema on a temp copy so backfills that depend on newly-added
        columns are surfaced. If nothing is pending, return an empty
        report without touching the running graph_store -- the
        idempotent no-op path.

        Otherwise: close the running graph_store, open a fresh one
        solely for migration, run ``initialize(migrate=True)``, close
        it, then ask the VaultRegistryService to reload the vault so the
        registry holds a freshly-initialized SAGEServices bundle whose
        graph_store carries the post-migration schema.

        T-0115: after the schema migration step settles, scan every
        ``unique_keys`` declaration in vault config. For each declared
        (doc_type, field), build the chain-head-grouped value map and
        report any collisions; for each clean declaration, ensure the
        underlying partial UNIQUE index exists. The substrate refuses to
        activate a declaration while collisions remain (CAS-ADR-031 §5);
        existing index state for a colliding declaration is preserved
        (no implicit DROP) so a previously-clean activation is not
        silently torn down. The returned MigrationReport carries both
        ``tier3_uniqueness_activations`` (successful installs) and
        ``tier3_uniqueness_collisions`` (refused activations) -- callers
        must inspect both even on a no-op migration path, because the
        tier3 scan runs every call regardless of whether columns_added
        or backfills_applied are non-empty.

        Migration is NOT atomic with the post-migration reload:
        On the migrate-needed branch the sequence is
        ``self._graph_store.close()`` -> ``fresh = GraphStore(...)`` ->
        ``fresh.initialize(migrate=True)`` -> ``fresh.close()`` ->
        ``self._registry_service.reload(...)``. If
        ``fresh.initialize(migrate=True)`` raises (a faulty
        MIGRATION_PLAN ALTER TABLE, a BACKFILL_PLAN failure, or a
        Tier3UniqueIndexBlockedError surfaced via initialize-time
        index creation), the original graph_store is already closed
        and the registry has not yet been reloaded -- in-memory state
        is corrupted, with the registry slot pointing at closed
        services. Recovery options: (a) re-issue migrate_vault after
        fixing the underlying cause (races the same window), or
        (b) a process restart. T-0183 closed the atomicity hazard on
        the inner ``_registry_service.reload`` step itself, but the
        outer pre-reload migration sequence remains non-atomic; the
        structural fix is tracked as T-0201. See ``sage_reload_vault``
        for the in-place reload atomicity disclosure that this
        sequence inherits.
        """
        pending_alters, pending_bfs = _detect_pending_work(self._db_path)

        columns_added = [
            MigrationReportEntry(table=m.table, column=m.column) for m in pending_alters
        ]
        backfills_applied = [b.name for b in pending_bfs]

        if columns_added or backfills_applied:
            await self._graph_store.close()
            fresh = GraphStore(self._db_path)
            await fresh.initialize(migrate=True)
            await fresh.close()

            await self._registry_service.reload(self._vault_id, self._config)

        activations, collisions = await self._activate_tier3_uniqueness()

        return MigrationReport(
            vault_id=self._vault_id,
            columns_added=columns_added,
            backfills_applied=backfills_applied,
            tier3_uniqueness_activations=activations,
            tier3_uniqueness_collisions=collisions,
        )

    async def detect_drift(self) -> DriftReport:
        """Walk every active sync_target / derived_from edge; classify drift (T-0111).

        For each edge whose target's supersedes-chain head has advanced
        past the recorded ``synced_from_*`` provenance, emit a
        ``DriftEntry``. Hash is the authoritative comparator; the
        version doc-id is a display key. Edges whose recorded state
        still matches the head are absent from the report. See
        ``StalenessBasis`` for the four bucket semantics.
        """
        edges = await self._graph_store.list_provenance_edges(
            [EdgeType.SYNC_TARGET.value, EdgeType.DERIVED_FROM.value]
        )

        entries: list[DriftEntry] = []
        for edge in edges:
            entry = await self._classify_edge_for_drift(edge)
            if entry is not None:
                entries.append(entry)

        summary: dict[str, int] = {basis.value: 0 for basis in StalenessBasis}
        for entry in entries:
            summary[entry.staleness_basis.value] += 1

        return DriftReport(
            vault_id=self._vault_id,
            total_edges_walked=len(edges),
            summary=summary,
            entries=entries,
        )

    async def _classify_edge_for_drift(self, edge: dict) -> DriftEntry | None:
        """Build a DriftEntry for one edge, or None if the edge is current.

        See ``detect_drift`` for the four-bucket semantics. ``edge`` is
        a raw dict produced by ``list_provenance_edges``; this method
        does its own auxiliary reads (chain head, recorded-version
        dereference) and returns a fully-populated DriftEntry or None
        for the "current" case.
        """
        recorded_version = edge["synced_from_version"]
        recorded_hash = edge["synced_from_content_hash"]
        edge_type = EdgeType(edge["edge_type"])

        # Step 1: resolve target chain head.
        head_info = await self._graph_store.head_with_hash_for_chain(
            edge["target_id"], edge_type="supersedes"
        )

        # Step 2: chain nonlinear → data-quality flag, regardless of recorded state.
        if not head_info["is_linear"]:
            return DriftEntry(
                edge_id=edge["id"],
                edge_type=edge_type,
                source_id=edge["source_id"],
                target_id=edge["target_id"],
                recorded_version_id=recorded_version,
                recorded_version_label=None,
                recorded_content_hash=recorded_hash,
                current_head_id=None,
                current_head_version_label=None,
                current_head_content_hash=None,
                competing_head_count=head_info["heads_count"],
                staleness_basis=StalenessBasis.CHAIN_NONLINEAR,
            )

        head_id = head_info["head_id"]
        head_hash = head_info["head_content_hash"]
        head_label = head_info["head_version_label"]

        # Step 3: neither field recorded → legacy/unknown.
        if recorded_version is None and recorded_hash is None:
            return DriftEntry(
                edge_id=edge["id"],
                edge_type=edge_type,
                source_id=edge["source_id"],
                target_id=edge["target_id"],
                recorded_version_id=None,
                recorded_version_label=None,
                recorded_content_hash=None,
                current_head_id=head_id,
                current_head_version_label=head_label,
                current_head_content_hash=head_hash,
                competing_head_count=None,
                staleness_basis=StalenessBasis.RECORDED_NULL,
            )

        # Step 4: compute drift.
        recorded_version_label: str | None = None
        if recorded_version is not None:
            recorded_doc = await self._graph_store.get_document(recorded_version)
            if recorded_doc is not None:
                recorded_version_label = recorded_doc.version_label
            recorded_doc_hash = (
                recorded_doc.source_content_hash if recorded_doc is not None else None
            )
        else:
            recorded_doc_hash = None

        if recorded_hash is not None:
            # Hash-authoritative path.
            if recorded_hash != head_hash:
                basis = StalenessBasis.CONTENT_DRIFT
            elif recorded_version is not None and recorded_version != head_id:
                basis = StalenessBasis.CHAIN_ADVANCED_NO_CONTENT_CHANGE
            else:
                return None  # current — recorded matches head
        else:
            # Only version recorded; dereference its hash to compare.
            if recorded_doc_hash is None or recorded_doc_hash != head_hash:
                basis = StalenessBasis.CONTENT_DRIFT
            elif recorded_version != head_id:
                basis = StalenessBasis.CHAIN_ADVANCED_NO_CONTENT_CHANGE
            else:
                return None  # current — recorded version is head, hash matches

        return DriftEntry(
            edge_id=edge["id"],
            edge_type=edge_type,
            source_id=edge["source_id"],
            target_id=edge["target_id"],
            recorded_version_id=recorded_version,
            recorded_version_label=recorded_version_label,
            recorded_content_hash=recorded_hash,
            current_head_id=head_id,
            current_head_version_label=head_label,
            current_head_content_hash=head_hash,
            competing_head_count=None,
            staleness_basis=basis,
        )

    async def scan_tier3_uniqueness_collisions(
        self, doc_type: str, field: str
    ) -> list[Tier3UniquenessCollision]:
        """Enumerate cross-chain collisions on `(doc_type, field)` (T-0115).

        Groups chain heads of `doc_type` by their `tier3_metadata.<field>`
        value. Any value held by more than one chain head is a collision:
        each chain is one logical artifact per the supersession-lineage
        exception in CAS-ADR-031 §3, so a value spanning multiple chains
        means the identifier has been double-allocated.

        Read-only; callable independently of `migrate_vault` so an
        operator can inspect a vault before declaring `unique_keys`. The
        returned list is empty when the portfolio is clean.
        """
        groups = await self._graph_store.find_chain_heads_with_tier3_value(doc_type, field)
        return [
            Tier3UniquenessCollision(
                doc_type=doc_type,
                field=field,
                value=value,
                document_ids=sorted(doc_ids),
            )
            for value, doc_ids in groups
            if len(doc_ids) > 1
        ]

    async def _activate_tier3_uniqueness(
        self,
    ) -> tuple[list[Tier3UniquenessActivation], list[Tier3UniquenessCollision]]:
        """Walk the vault's `unique_keys` declarations.

        For each (doc_type, field) declared, scan for collisions. If clean,
        create (or confirm) the partial UNIQUE index. If colliding, record
        the collision and skip index creation so the substrate refuses to
        activate the constraint (CAS-ADR-031 §5).
        """
        activations: list[Tier3UniquenessActivation] = []
        collisions: list[Tier3UniquenessCollision] = []
        for dt in self._config.document_types.doc_types:
            if not dt.unique_keys:
                continue
            for field in dt.unique_keys:
                dt_collisions = await self.scan_tier3_uniqueness_collisions(dt.value, field)
                if dt_collisions:
                    collisions.extend(dt_collisions)
                    continue
                try:
                    await self._graph_store.ensure_tier3_unique_index(dt.value, field)
                except Tier3UniqueIndexBlockedError as exc:
                    # Defensive: a chain-head SELECT-based scan returned
                    # clean, but the SQLite CREATE UNIQUE INDEX still
                    # rejected. Surface as a synthetic collision entry so
                    # the operator sees the substrate's view rather than
                    # losing the diagnostic to a swallowed exception.
                    collisions.append(
                        Tier3UniquenessCollision(
                            doc_type=exc.doc_type,
                            field=exc.field,
                            value="<reported by SQLite, value not recovered>",
                            document_ids=[],
                        )
                    )
                    continue
                activations.append(Tier3UniquenessActivation(doc_type=dt.value, field=field))
        return activations, collisions

    def _reject_if_in_flight(self) -> None:
        """Raise ReabstractAlreadyInFlightError synchronously if a reabstract
        is already running on this vault (T-0089, T-0134).

        Synchronous helper -- not ``async`` -- so callers can fail fast
        BEFORE constructing a StreamingResponse. The in-flight check
        must surface as a real 409 (application/json ErrorResponse),
        not as an in-stream SSE error event after a 200 text/event-stream
        response has already been opened.

        Non-blocking rejection: ``self._reabstract_lock.locked()`` peeks
        at the lock state without awaiting. ``_reabstract_started_at``
        is set inside the lock by the in-flight caller before any await
        that could yield to this branch, so reading it here is race-free.
        """
        if self._reabstract_lock.locked():
            raise ReabstractAlreadyInFlightError(
                vault_id=self._vault_id,
                start_time=self._reabstract_started_at or datetime.now(timezone.utc),
            )

    async def reabstract_deferred(self, include_pdf: bool = False) -> ReabstractReport:
        """Backfill semantic abstracts for the deferred-abstract worklist (T-0089).

        Consumes the ``reabstract_deferred_events`` streaming generator
        and returns the final summary event re-shaped as a
        ``ReabstractReport``. The streaming generator is the single
        source of truth for per-document iteration logic; this method
        is a thin aggregator used by the MCP tool path where the
        caller wants one synchronous report rather than an event stream.

        Enumerates documents whose ``pipeline_status`` is
        ``abstraction_skipped``, dispatches
        ``IngestionService.reabstract`` per document, and polls until
        each reaches a terminal pipeline_status
        (``abstraction_complete`` or ``failed``). Per-document
        exceptions are caught and recorded as ``llm_failure`` entries;
        the loop does not abort on a single failure.

        Reuses the in-process IngestionService that the running SAGE
        process initialized at startup -- and therefore its already-
        loaded ``AbstractionProvider``. Does NOT initialize a second
        provider; the F-8 unified-memory cautionary tale (dual Qwen3
        MLX load triggers Apple Silicon OOM or kernel panic) is the
        binding constraint behind that rule. A ``MaintenanceService``
        constructed without an ``ingestion_service`` raises
        ``RuntimeError`` rather than fall back to a self-initialized
        provider; the standalone-script path lives in
        ``scripts/reabstract_deferred.py`` and runs in a separate OS
        process where the dual-provider hazard cannot apply.

        Single-flight per vault: a concurrent call while a reabstract
        is in flight raises ``ReabstractAlreadyInFlightError`` (409,
        structured payload includes ``start_time``) rather than
        queueing. Reabstract passes can run for minutes; silently
        queuing a second caller would mask client-side coordination
        bugs.

        Args:
            include_pdf: When ``False`` (default), documents whose
                ``source_type`` is ``pdf`` are skipped and recorded as
                ``skipped_pdf`` entries. Scanned PDFs typically have no
                extractable text and reabstract yields a degenerate
                abstract; the script default carries the same logic.
                Set to ``True`` to include PDFs in the worklist.

        Returns:
            ReabstractReport with aggregate counts and per-document
            outcome entries.

        Raises:
            RuntimeError: ingestion_service was not wired in at
                construction (defensive guard against the F-8 hazard).
            ReabstractAlreadyInFlightError: another reabstract is
                already running on this vault.
        """
        summary: ReabstractSummaryEvent | None = None
        # reabstract_deferred_events does the in-flight check + None-
        # ingestion guard synchronously before returning the generator;
        # exceptions from those checks propagate up unchanged.
        async for event in self.reabstract_deferred_events(include_pdf=include_pdf):
            if isinstance(event, ReabstractSummaryEvent):
                summary = event

        # The generator always emits exactly one summary event as its
        # final yield; if we somehow get here without one, the streaming
        # contract has been violated.
        if summary is None:
            raise RuntimeError(
                "reabstract_deferred_events did not emit a summary event; "
                "streaming-aggregator contract violated."
            )
        return ReabstractReport.model_validate(summary.model_dump(exclude={"event_type"}))

    def reabstract_deferred_events(
        self, include_pdf: bool = False
    ) -> AsyncGenerator[ReabstractEvent, None]:
        """Stream per-document progress events for the deferred-abstract
        worklist (T-0134).

        Returns an async generator that yields a ``ReabstractProgressEvent``
        per per-document state transition (one ``started`` and one
        ``completed``/``failed`` for each non-PDF entry; one ``skipped``
        for each PDF entry when ``include_pdf=False``), then a final
        ``ReabstractSummaryEvent`` carrying the aggregate
        ``ReabstractReport``-shaped payload.

        IMPORTANT -- synchronous pre-check before first yield. This
        method is a regular ``def`` (not ``async def``) that performs
        the in-flight check and None-ingestion guard synchronously,
        then returns the underlying async generator. The conventional
        ``async def`` generator does not execute its body until the
        first ``__anext__()``, which would mean a 409 would not raise
        until iteration starts -- by which point a FastAPI route has
        already opened a 200 text/event-stream response. Mirrors the
        precedent at ``IngestStreamingService.stream`` which raises
        ``EmptyFileListError`` synchronously before constructing its
        ``StreamingResponse`` (see app/backend/ingest_streaming_service.py).

        Args:
            include_pdf: When ``False`` (default), PDF docs surface as
                a single ``skipped`` progress event each. When ``True``,
                PDFs run through dispatch like every other doc.

        Returns:
            Async generator of ``ReabstractProgressEvent`` then a final
            ``ReabstractSummaryEvent``.

        Raises:
            RuntimeError: ingestion_service was not wired in at
                construction (defensive guard against the F-8 hazard).
            ReabstractAlreadyInFlightError: another reabstract is
                already running on this vault. Raised SYNCHRONOUSLY
                from this method (before iteration), so HTTP callers
                can return 409 instead of opening a stream.
        """
        if self._ingestion is None:
            raise RuntimeError(
                f"reabstract_deferred requires an IngestionService "
                f"dependency; vault {self._vault_id!r} MaintenanceService "
                "was constructed without one. The production "
                "initialize_services path wires it in; tests that exercise "
                "the maintenance surface must pass ingestion_service "
                "explicitly (T-0089, F-8 guard)."
            )

        self._reject_if_in_flight()
        # Local capture narrows `IngestionService | None` to
        # `IngestionService` for the inner-generator call site; the
        # gate above already raised if it was None.
        ingestion = self._ingestion
        return self._reabstract_deferred_events_impl(ingestion=ingestion, include_pdf=include_pdf)

    async def _reabstract_deferred_events_impl(
        self, *, ingestion: "IngestionService", include_pdf: bool
    ) -> AsyncGenerator[ReabstractEvent, None]:
        """Lock-held body of reabstract_deferred_events. Separated from
        the public method so the synchronous pre-checks run before the
        generator body (Python's async generators defer body execution
        until the first ``__anext__()``).
        """
        async with self._reabstract_lock:
            self._reabstract_started_at = datetime.now(timezone.utc)
            try:
                all_docs = await self._graph_store.list_all_documents()
                skipped = [
                    d
                    for d in all_docs
                    if d.pipeline_status == PipelineStatus.ABSTRACTION_SKIPPED.value
                ]
                pdf_skipped = [d for d in skipped if not include_pdf and d.source_type == "pdf"]
                worklist = [d for d in skipped if include_pdf or d.source_type != "pdf"]

                # Total document count for the progress counter: PDFs to
                # be skipped plus the dispatchable worklist. Constant
                # across all events in this stream.
                total = len(pdf_skipped) + len(worklist)
                processed = 0
                entries: list[ReabstractReportEntry] = []
                reabstracted = 0
                failed = 0

                # PDFs first: each surfaces as a single ``skipped`` event
                # (no dispatch, no wait). Aggregator order preserves the
                # pre-T-0134 behavior in which PDF entries lead the
                # report.
                for doc in pdf_skipped:
                    entry = ReabstractReportEntry(
                        document_id=doc.id,
                        outcome=ReabstractOutcome.SKIPPED_PDF,
                    )
                    entries.append(entry)
                    processed += 1
                    yield ReabstractProgressEvent(
                        event_type="progress",
                        processed=processed,
                        total=total,
                        current_document_id=doc.id,
                        current_title=doc.title,
                        status="skipped",
                        outcome=ReabstractOutcome.SKIPPED_PDF,
                    )

                terminal = {
                    PipelineStatus.ABSTRACTION_COMPLETE.value,
                    PipelineStatus.FAILED.value,
                }
                for doc in worklist:
                    # ``started`` event: processed counts terminal events
                    # only, so a started event leaves it unchanged.
                    yield ReabstractProgressEvent(
                        event_type="progress",
                        processed=processed,
                        total=total,
                        current_document_id=doc.id,
                        current_title=doc.title,
                        status="started",
                    )

                    doc_started = datetime.now(timezone.utc)
                    try:
                        await ingestion.reabstract(doc.id)
                    except Exception as exc:
                        elapsed = (datetime.now(timezone.utc) - doc_started).total_seconds()
                        error_message = f"dispatch failed: {exc!r}"
                        entries.append(
                            ReabstractReportEntry(
                                document_id=doc.id,
                                outcome=ReabstractOutcome.LLM_FAILURE,
                                error_message=error_message,
                                elapsed_seconds=elapsed,
                            )
                        )
                        failed += 1
                        processed += 1
                        yield ReabstractProgressEvent(
                            event_type="progress",
                            processed=processed,
                            total=total,
                            current_document_id=doc.id,
                            current_title=doc.title,
                            status="failed",
                            outcome=ReabstractOutcome.LLM_FAILURE,
                            error=error_message,
                            elapsed_seconds=elapsed,
                        )
                        continue

                    status = await self._wait_for_terminal(doc.id, terminal)
                    elapsed = (datetime.now(timezone.utc) - doc_started).total_seconds()
                    if status == PipelineStatus.ABSTRACTION_COMPLETE.value:
                        entries.append(
                            ReabstractReportEntry(
                                document_id=doc.id,
                                outcome=ReabstractOutcome.SUCCESS,
                                elapsed_seconds=elapsed,
                            )
                        )
                        reabstracted += 1
                        processed += 1
                        yield ReabstractProgressEvent(
                            event_type="progress",
                            processed=processed,
                            total=total,
                            current_document_id=doc.id,
                            current_title=doc.title,
                            status="completed",
                            outcome=ReabstractOutcome.SUCCESS,
                            elapsed_seconds=elapsed,
                        )
                    else:
                        error_message = f"terminal pipeline_status: {status}"
                        entries.append(
                            ReabstractReportEntry(
                                document_id=doc.id,
                                outcome=ReabstractOutcome.LLM_FAILURE,
                                error_message=error_message,
                                elapsed_seconds=elapsed,
                            )
                        )
                        failed += 1
                        processed += 1
                        yield ReabstractProgressEvent(
                            event_type="progress",
                            processed=processed,
                            total=total,
                            current_document_id=doc.id,
                            current_title=doc.title,
                            status="failed",
                            outcome=ReabstractOutcome.LLM_FAILURE,
                            error=error_message,
                            elapsed_seconds=elapsed,
                        )

                yield ReabstractSummaryEvent(
                    event_type="summary",
                    vault_id=self._vault_id,
                    reabstracted_count=reabstracted,
                    skipped_pdf_count=len(pdf_skipped),
                    failed_count=failed,
                    entries=entries,
                )
            finally:
                self._reabstract_started_at = None

    async def _wait_for_terminal(self, document_id: str, terminal: set[str]) -> str:
        """Poll the document's pipeline_status until it reaches a terminal
        value, then return it. Returns the sentinel string ``"missing"``
        if the document disappears mid-flight.
        """
        while True:
            doc = await self._graph_store.get_document(document_id)
            if doc is None:
                return "missing"
            status = doc.pipeline_status
            if status in terminal:
                return status
            await asyncio.sleep(_POLL_INTERVAL_SECONDS)
