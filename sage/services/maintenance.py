"""Vault-scoped maintenance/admin operations (CAS-ADR-029).

Pilot operation: schema migration for a single vault in the running
session. Subsequent admin operations on this surface slot into the same
three-layer service + router + MCP-tool shape.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncGenerator
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from sage.adapters.interfaces import ContentStore, GraphStore
from sage.api.errors import ReabstractAlreadyInFlightError
from sage.config import VaultConfig
from sage.models.enums import (
    SUCCESSFUL_TERMINAL_PIPELINE_STATUSES,
    EdgeType,
    PipelineStatus,
    ReabstractOutcome,
    StalenessBasis,
)
from sage.models.schemas import (
    Document,
    DriftEntry,
    DriftReport,
    MigrationReport,
    OptimizeContentStoreReport,
    ReabstractProgressEvent,
    ReabstractReport,
    ReabstractReportEntry,
    ReabstractSummaryEvent,
    SourceFileIntegrityEntry,
    SourceFileIntegrityReport,
    Tier3UniquenessActivation,
    Tier3UniquenessCollision,
    canonicalize_sha256,
)
from sage.services.maintenance_log import MAINTENANCE_LOG_FILENAME
from sage.storage.tier3_uniqueness import Tier3UniqueIndexBlockedError
from sage.vault_management import config_path_for_vault

# Union type for events yielded by reabstract_deferred_events.
ReabstractEvent = ReabstractProgressEvent | ReabstractSummaryEvent

if TYPE_CHECKING:
    from sage.services.ingestion import IngestionService
    from sage.services.vault_registry import VaultRegistryService
    from sage.vault_source_binding import VaultSourceStore


# Poll interval for the post-dispatch wait-for-terminal loop. Hardcoded
# at 50 ms: fast enough to keep request latency dominated by the
# abstraction call itself (each reabstract takes seconds), slow enough
# that the polling overhead is negligible. The standalone-script path
# defaults to 1.0 s because it ran with a TTY in the loop; the
# in-process service path has no such concern.
_POLL_INTERVAL_SECONDS = 0.05


# Name reported in MigrationReport.backfills_applied when the migration
# repaired documents whose pipeline_error outlived the failure it described.
BACKFILL_STALE_PIPELINE_ERROR = "clear_pipeline_error_on_successful_terminal_status"


def _canonical_or_none(content_hash: str | None) -> str | None:
    """Canonicalize a content hash, preserving null.

    Drift comparison reads hashes straight from edge and document rows, which
    never crossed the `Sha256Str` alias and so may carry a spelling predating
    it. Null stays null so an absent hash keeps comparing unequal to a present
    one rather than collapsing into a canonical string.
    """
    return canonicalize_sha256(content_hash) if content_hash is not None else None


def _expected_stored_hash(doc: Document) -> str:
    """The digest a re-read of this document's retained source must reproduce.

    ``stored_content_hash`` when the document carries one: a binding may rewrite
    its copy at rest (CAS-ADR-043), and it is that copy the audit re-reads, so
    the provenance hash -- the digest of what the caller delivered -- is the
    wrong comparator and would report every such document as corrupt.

    ``source_content_hash`` otherwise. A null means the document was ingested
    before the two digests were recorded separately, and back then the recorded
    provenance hash *was* the as-stored digest -- so it remains the correct
    comparator for those records, and corruption detection is unaffected by
    their age. What such a record cannot support is the other direction: its
    delivered-byte digest is not recoverable, so a caller re-delivering the
    original bytes will not match it.
    """
    return doc.stored_content_hash or doc.source_content_hash


class MaintenanceService:
    """Pilot of the maintenance/admin API surface (CAS-ADR-029)."""

    def __init__(
        self,
        vault_id: str,
        graph_store: GraphStore,
        config: VaultConfig,
        registry_service: "VaultRegistryService | None",
        content_store: ContentStore,
        ingestion_service: "IngestionService | None" = None,
        vault_dir: Path | None = None,
    ) -> None:
        self._vault_id = vault_id
        self._graph_store = graph_store
        self._config = config
        self._registry_service = registry_service
        self._content_store = content_store
        self._ingestion = ingestion_service
        # vault_dir resolves where the audit log lives. Production
        # invocations through mcp_init don't pass it -- the directory is
        # derived on demand from vault_management.config_path_for_vault,
        # which resolves against the root this process is bound to
        # (CAS-ADR-043). Tests with ephemeral vaults outside that root
        # pass the path explicitly.
        self._vault_dir = vault_dir
        # Per-vault single-flight lock for reabstract_deferred.
        # Non-blocking check: a second caller raises rather than queueing
        # (reabstract passes can run for minutes against the in-process
        # Qwen3; silently queuing would mask client-side coordination
        # bugs). _reabstract_started_at is set inside the lock so a
        # rejected concurrent caller can read it without racing.
        self._reabstract_lock = asyncio.Lock()
        self._reabstract_started_at: datetime | None = None

    async def migrate_vault(self) -> MigrationReport:
        """Run the schema-migration surface's backfill and tier3-uniqueness scan.

        Postgres provisions each vault's schema externally (CAS-ADR-042), so
        there is no pending-column work for this method to detect or apply and
        ``columns_added`` is always empty.

        One data backfill runs. A document that failed abstraction and was
        later repaired predates the rule that a successful terminal
        ``pipeline_status`` clears ``pipeline_error``, so it still carries the
        message describing a failure that no longer holds. The backfill nulls
        ``pipeline_error`` on every document already at a successful terminal
        status and names itself in ``backfills_applied`` only when it changed
        rows, so a clean vault still reports an empty list and a re-call after
        a repair reports nothing further.

        Scan every ``unique_keys`` declaration in vault config. For each
        declared (doc_type, field), build the chain-head-grouped value map and
        report any collisions; for each clean declaration, ensure the
        underlying partial UNIQUE index exists. The substrate refuses to
        activate a declaration while collisions remain (CAS-ADR-031 §5);
        existing index state for a colliding declaration is preserved
        (no implicit DROP) so a previously-clean activation is not silently
        torn down. The returned MigrationReport carries both
        ``tier3_uniqueness_activations`` (successful installs) and
        ``tier3_uniqueness_collisions`` (refused activations).
        """
        cleared = await self._graph_store.clear_pipeline_error_for_statuses(
            sorted(status.value for status in SUCCESSFUL_TERMINAL_PIPELINE_STATUSES)
        )
        backfills_applied = [BACKFILL_STALE_PIPELINE_ERROR] if cleared else []

        activations, collisions = await self._activate_tier3_uniqueness()

        return MigrationReport(
            vault_id=self._vault_id,
            columns_added=[],
            backfills_applied=backfills_applied,
            tier3_uniqueness_activations=activations,
            tier3_uniqueness_collisions=collisions,
        )

    async def detect_drift(self) -> DriftReport:
        """Walk every active sync_target / derived_from edge; classify drift.

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

    async def verify_vault_source_files(
        self, check_hashes: bool = False
    ) -> SourceFileIntegrityReport:
        """Audit that every document's backing source file is present.

        Walks every document in the vault (all lifecycle states) and
        checks that its ``source_path`` resolves to an existing file
        under the vault storage root. When ``check_hashes`` is true, each
        present file's SHA-256 is recomputed and compared against the
        digest recorded for the retained copy -- ``stored_content_hash``,
        or ``source_content_hash`` when that is null (see
        :func:`_expected_stored_hash`). Read-only; mutates nothing.

        Audits the vault-local source files (the ``imports/`` copies that
        ``get_document`` delivers), distinct from the content store
        reclaimed by ``optimize_content_store``.

        Returns a SourceFileIntegrityReport with per-document entries for
        missing or hash-mismatched files and aggregate counts; documents
        with an intact source file are absent from ``entries``.
        """
        all_docs = await self._graph_store.list_all_documents()
        storage_root = Path(self._config.vault.storage_root).expanduser().resolve()

        # Audit through the active profile's vault-source store so the integrity
        # check is binding-agnostic (CAS-ADR-043).
        from sage.mcp_init import get_stack_config, resolve_stack_vault_source_store

        store = resolve_stack_vault_source_store(get_stack_config())

        entries: list[SourceFileIntegrityEntry] = []
        for doc in all_docs:
            entry = self._check_document_source_file(doc, storage_root, check_hashes, store)
            if entry is not None:
                entries.append(entry)

        summary = {
            "healthy": len(all_docs) - len(entries),
            "missing": sum(1 for e in entries if e.integrity_status == "missing"),
            "hash_mismatch": sum(1 for e in entries if e.integrity_status == "hash_mismatch"),
        }

        return SourceFileIntegrityReport(
            vault_id=self._vault_id,
            total_documents_checked=len(all_docs),
            check_hashes=check_hashes,
            summary=summary,
            entries=entries,
        )

    def _check_document_source_file(
        self,
        doc: Document,
        storage_root: Path,
        check_hashes: bool,
        store: VaultSourceStore,
    ) -> SourceFileIntegrityEntry | None:
        """Return an integrity entry for ``doc`` if its source file is
        missing or hash-mismatched, else None.

        Existence and hashing are resolved through the vault-source store
        (CAS-ADR-043), the same store ``get_document`` delivers through, so
        the audit observes exactly what delivery would. When ``check_hashes``
        is set, a present source is additionally hashed and compared against
        the digest recorded for the *stored* copy (see
        :func:`_expected_stored_hash`). A missing source is always
        classified ``missing`` regardless of ``check_hashes`` (it is never
        a hash error).
        """
        if not store.source_exists(self._vault_id, storage_root, doc.source_path):
            return self._integrity_entry(doc, "missing", observed=None)

        if check_hashes:
            observed = store.hash_source(self._vault_id, storage_root, doc.source_path)
            if observed != _expected_stored_hash(doc):
                return self._integrity_entry(doc, "hash_mismatch", observed=observed)

        return None

    @staticmethod
    def _integrity_entry(
        doc: Document,
        integrity_status: str,
        *,
        observed: str | None,
    ) -> SourceFileIntegrityEntry:
        return SourceFileIntegrityEntry(
            document_id=doc.id,
            title=doc.title,
            source_path=doc.source_path,
            lifecycle_status=doc.lifecycle_status,
            version_label=doc.version_label,
            integrity_status=integrity_status,
            expected_content_hash=_expected_stored_hash(doc),
            observed_content_hash=observed,
        )

    async def optimize_content_store(
        self,
        cleanup_older_than_days: int = 7,
    ) -> OptimizeContentStoreReport:
        """Reclaim disk in the content store and audit the call.

        Captures pre/post substrate observations (bytes, retained version
        count, fragment count) around the ContentStore.optimize() call so the
        caller sees what was reclaimed. Appends a JSONL line to
        ``<vault_dir>/.maintenance_log.jsonl`` following the per-document
        purge precedent.

        cleanup_older_than_days must be a non-negative integer. The Postgres
        binding's optimize() has no age-threshold analog (VACUUM reclaims
        every eligible dead tuple); the parameter is preserved for the port
        contract and passed through unconditionally.
        """
        if cleanup_older_than_days < 0:
            raise ValueError("cleanup_older_than_days must be >= 0")

        started_at = datetime.now(timezone.utc)
        snapshot = await self._content_store.optimize(
            cleanup_older_than=timedelta(days=cleanup_older_than_days)
        )
        finished_at = datetime.now(timezone.utc)

        report = OptimizeContentStoreReport(
            vault_id=self._vault_id,
            cleanup_older_than_days=cleanup_older_than_days,
            started_at=started_at,
            finished_at=finished_at,
            bytes_reclaimed=max(0, snapshot["pre_bytes"] - snapshot["post_bytes"]),
            **snapshot,
        )

        self._append_optimize_audit_record(report)
        return report

    def _append_optimize_audit_record(self, report: OptimizeContentStoreReport) -> None:
        """Append one JSONL line capturing this optimize call to the vault's
        ``.maintenance_log.jsonl``.

        The record carries the optimize report's own fields plus a
        timestamp and operation tag; maintenance operations share the
        one log file, discriminated by that tag.
        """
        vault_dir = self._vault_dir or config_path_for_vault(self._vault_id).parent
        audit_path = vault_dir / MAINTENANCE_LOG_FILENAME
        audit_path.parent.mkdir(parents=True, exist_ok=True)
        record = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "operation": "optimize_vault_content_store",
            "vault_id": report.vault_id,
            "cleanup_older_than_days": report.cleanup_older_than_days,
            "started_at": report.started_at.isoformat(),
            "finished_at": report.finished_at.isoformat(),
            "pre_bytes": report.pre_bytes,
            "post_bytes": report.post_bytes,
            "bytes_reclaimed": report.bytes_reclaimed,
            "pre_versions": report.pre_versions,
            "post_versions": report.post_versions,
            "pre_fragments": report.pre_fragments,
            "post_fragments": report.post_fragments,
            "pre_small_fragments": report.pre_small_fragments,
            "post_small_fragments": report.post_small_fragments,
        }
        with open(audit_path, "a") as f:
            f.write(json.dumps(record) + "\n")

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

        # Both sides are compared canonically. `recorded_hash` is read from a
        # raw edge row rather than through a model, so it never crossed the
        # `Sha256Str` alias and may carry a non-canonical spelling predating
        # it. Comparing raw would report drift for a hash that differs only in
        # spelling -- and because `DriftEntry` canonicalizes both fields when
        # it is built, the resulting entry would render two identical hashes
        # as evidence of a difference.
        if recorded_hash is not None:
            # Hash-authoritative path.
            if _canonical_or_none(recorded_hash) != _canonical_or_none(head_hash):
                basis = StalenessBasis.CONTENT_DRIFT
            elif recorded_version is not None and recorded_version != head_id:
                basis = StalenessBasis.CHAIN_ADVANCED_NO_CONTENT_CHANGE
            else:
                return None  # current — recorded matches head
        else:
            # Only version recorded; dereference its hash to compare.
            if recorded_doc_hash is None or _canonical_or_none(
                recorded_doc_hash
            ) != _canonical_or_none(head_hash):
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
        """Enumerate cross-chain collisions on `(doc_type, field)`.

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
                    # clean, but the store's CREATE UNIQUE INDEX still
                    # rejected. Surface as a synthetic collision entry so
                    # the operator sees the substrate's view rather than
                    # losing the diagnostic to a swallowed exception.
                    collisions.append(
                        Tier3UniquenessCollision(
                            doc_type=exc.doc_type,
                            field=exc.field,
                            value="<reported by the store, value not recovered>",
                            document_ids=[],
                        )
                    )
                    continue
                activations.append(Tier3UniquenessActivation(doc_type=dt.value, field=field))
        return activations, collisions

    def _reject_if_in_flight(self) -> None:
        """Raise ReabstractAlreadyInFlightError synchronously if a reabstract
        is already running on this vault.

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
        """Backfill semantic abstracts for the deferred-abstract worklist.

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
        worklist.

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
                "explicitly."
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
                # pre-behavior in which PDF entries lead the
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
