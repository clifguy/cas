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
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from sage.api.errors import ReabstractAlreadyInFlightError
from sage.config import VaultConfig
from sage.models.enums import PipelineStatus, ReabstractOutcome
from sage.models.schemas import (
    MigrationReport,
    MigrationReportEntry,
    ReabstractReport,
    ReabstractReportEntry,
)
from sage.storage.graph_store import GraphStore
from sage.storage.migrations import (
    BACKFILL_PLAN,
    MIGRATION_PLAN,
    TABLES,
    Backfill,
    Migration,
    pending_backfills,
    pending_migrations,
)

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
        """
        pending_alters, pending_bfs = _detect_pending_work(self._db_path)

        columns_added = [
            MigrationReportEntry(table=m.table, column=m.column) for m in pending_alters
        ]
        backfills_applied = [b.name for b in pending_bfs]

        if not columns_added and not backfills_applied:
            return MigrationReport(
                vault_id=self._vault_id,
                columns_added=[],
                backfills_applied=[],
            )

        await self._graph_store.close()
        fresh = GraphStore(self._db_path)
        await fresh.initialize(migrate=True)
        await fresh.close()

        await self._registry_service.reload(self._vault_id, self._config)

        return MigrationReport(
            vault_id=self._vault_id,
            columns_added=columns_added,
            backfills_applied=backfills_applied,
        )

    async def reabstract_deferred(self, include_pdf: bool = False) -> ReabstractReport:
        """Backfill semantic abstracts for the deferred-abstract worklist (T-0089).

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
        if self._ingestion is None:
            raise RuntimeError(
                f"reabstract_deferred requires an IngestionService "
                f"dependency; vault {self._vault_id!r} MaintenanceService "
                "was constructed without one. The production "
                "initialize_services path wires it in; tests that exercise "
                "the maintenance surface must pass ingestion_service "
                "explicitly (T-0089, F-8 guard)."
            )

        if self._reabstract_lock.locked():
            # Non-blocking rejection. _reabstract_started_at is set
            # inside the lock by the in-flight caller before any await
            # that could yield to this branch, so reading it here is
            # race-free.
            raise ReabstractAlreadyInFlightError(
                vault_id=self._vault_id,
                start_time=self._reabstract_started_at or datetime.now(timezone.utc),
            )

        # Local capture narrows the type from `IngestionService | None`
        # to `IngestionService` for the helper call site; the gate above
        # has already raised if it was None.
        ingestion = self._ingestion
        async with self._reabstract_lock:
            self._reabstract_started_at = datetime.now(timezone.utc)
            try:
                return await self._run_reabstract_deferred(
                    ingestion=ingestion, include_pdf=include_pdf
                )
            finally:
                self._reabstract_started_at = None

    async def _run_reabstract_deferred(
        self, *, ingestion: "IngestionService", include_pdf: bool
    ) -> ReabstractReport:
        """Lock-held body of reabstract_deferred. Separated so the lock
        management stays readable; ``ingestion`` is passed in narrowed
        from the public-method gate.
        """
        all_docs = await self._graph_store.list_all_documents()
        skipped = [
            d for d in all_docs if d.pipeline_status == PipelineStatus.ABSTRACTION_SKIPPED.value
        ]
        pdf_skipped = [d for d in skipped if not include_pdf and d.source_type == "pdf"]
        worklist = [d for d in skipped if include_pdf or d.source_type != "pdf"]

        entries: list[ReabstractReportEntry] = []
        reabstracted = 0
        failed = 0

        for doc in pdf_skipped:
            entries.append(
                ReabstractReportEntry(
                    document_id=doc.id,
                    outcome=ReabstractOutcome.SKIPPED_PDF,
                )
            )

        terminal = {
            PipelineStatus.ABSTRACTION_COMPLETE.value,
            PipelineStatus.FAILED.value,
        }
        for doc in worklist:
            doc_started = datetime.now(timezone.utc)
            try:
                await ingestion.reabstract(doc.id)
            except Exception as exc:
                elapsed = (datetime.now(timezone.utc) - doc_started).total_seconds()
                entries.append(
                    ReabstractReportEntry(
                        document_id=doc.id,
                        outcome=ReabstractOutcome.LLM_FAILURE,
                        error_message=f"dispatch failed: {exc!r}",
                        elapsed_seconds=elapsed,
                    )
                )
                failed += 1
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
            else:
                entries.append(
                    ReabstractReportEntry(
                        document_id=doc.id,
                        outcome=ReabstractOutcome.LLM_FAILURE,
                        error_message=f"terminal pipeline_status: {status}",
                        elapsed_seconds=elapsed,
                    )
                )
                failed += 1

        return ReabstractReport(
            vault_id=self._vault_id,
            reabstracted_count=reabstracted,
            skipped_pdf_count=len(pdf_skipped),
            failed_count=failed,
            entries=entries,
        )

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
