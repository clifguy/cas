"""Vault-scoped maintenance/admin operations (CAS-ADR-029).

Pilot operation: schema migration for a single vault in the running
session. Subsequent admin operations on this surface slot into the same
three-layer service + router + MCP-tool shape.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncGenerator
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, NamedTuple

from sage.adapters.interfaces import ContentStore, GraphStore
from sage.api.errors import (
    DocumentNotFoundError,
    ReabstractAlreadyInFlightError,
    RestoreProvenanceMismatchError,
    RestoreSourceNotAbsoluteError,
    RestoreTargetUnresolvedError,
    SourceFileNotFoundError,
    VaultSourcePathRefusedError,
)
from sage.config import VaultConfig
from sage.models.enums import (
    SUCCESSFUL_TERMINAL_PIPELINE_STATUSES,
    TERMINAL_PIPELINE_STATUS_VALUES,
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
    SourceFileRestoreReport,
    SourcePathNormalization,
    Tier3UniquenessActivation,
    Tier3UniquenessCollision,
    canonicalize_sha256,
)
from sage.services.document_surface import compose_document_surface
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

# Ceiling on how long the post-dispatch wait blocks on a single document
# before abandoning it. The wait polls pipeline_status, and a document can
# stop advancing toward a terminal one: a generation that runs long enough
# outlasts any waiter, and the process holding it can go away mid-flight.
# Stopping the worker is no longer such a case -- it settles the work it
# drops at `abstraction_interrupted`, which is terminal -- but a ceiling is
# still what bounds the wait, because without one the waiter polls a slow
# document forever, holding the SSE response open with no further events
# and giving the report-and-return MCP caller nothing to time out against.
#
# Sized to clear the slowest legitimate document rather than the typical
# one, because overshooting costs a stranded sweep an hour it would have
# spent stuck anyway, while undershooting abandons work that was still
# progressing: a full-context generation runs roughly 25 minutes, and the
# worker spends up to `abstraction.max_attempts` of them (3 by default)
# before it stamps a terminal status of its own.
_WAIT_TIMEOUT_SECONDS = 7200.0

# Returned by the wait when the document disappears between dispatch and
# settling, and when the ceiling above is reached. Neither is a
# pipeline_status value, so neither can collide with a real one.
_WAIT_MISSING = "missing"
_WAIT_TIMEOUT = "timeout"


class _FailureReport(NamedTuple):
    """How one non-success document is reported: outcome, event status, message.

    Named rather than positional because the two string slots -- a status
    drawn from a closed four-value vocabulary and a free-text message --
    transpose without a type error, and each new arm is another chance to
    swap them.
    """

    outcome: ReabstractOutcome
    event_status: str
    message: str


def _reabstract_failure_report(status: str) -> _FailureReport:
    """Classify a non-success settled status.

    ``status`` is whatever the post-dispatch wait returned other than
    ``abstraction_complete``: a terminal pipeline_status, or one of the
    waiter's own sentinels. The event status stays inside the progress
    stream's existing vocabulary -- a still-skipped document rides
    ``skipped`` alongside the excluded PDFs, everything else rides
    ``failed`` -- so widening the outcomes does not widen the transport.

    ``llm_failure`` is the residual arm rather than the default one. It
    claims the provider raised, so each status that reaches a terminal
    state some other way is named before the fallthrough: a document that
    declined abstraction, one the waiter abandoned, one whose work was
    dropped by a stopped queue, and one that no longer exists. Only a
    stored ``failed`` should arrive at the last return.
    """
    if status == PipelineStatus.ABSTRACTION_INTERRUPTED.value:
        return _FailureReport(
            ReabstractOutcome.INTERRUPTED,
            "failed",
            "abstraction work was dropped before it completed: the queue "
            "draining it was stopped. No provider was reached; the next "
            "server start re-runs it",
        )
    if status == PipelineStatus.ABSTRACTION_SKIPPED.value:
        return _FailureReport(
            ReabstractOutcome.STILL_SKIPPED,
            "skipped",
            "reabstract settled back at abstraction_skipped; the document "
            "declined abstraction rather than failing at it",
        )
    if status == _WAIT_TIMEOUT:
        return _FailureReport(
            ReabstractOutcome.TIMEOUT,
            "failed",
            f"document did not reach a terminal pipeline_status within "
            f"{_WAIT_TIMEOUT_SECONDS:.0f}s; abstraction may still be running",
        )
    if status == _WAIT_MISSING:
        # Reported as a failure because the document did not gain an
        # abstract, but described for what it was: a concurrent delete
        # between dispatch and settling, not a provider error. The former
        # message rendered this sentinel as "terminal pipeline_status:
        # missing", naming a status that does not exist.
        return _FailureReport(
            ReabstractOutcome.LLM_FAILURE,
            "failed",
            "document no longer exists; it was deleted between dispatch and settling",
        )
    return _FailureReport(
        ReabstractOutcome.LLM_FAILURE,
        "failed",
        f"terminal pipeline_status: {status}",
    )


# Name reported in MigrationReport.backfills_applied when the migration
# repaired documents whose pipeline_error outlived the failure it described.
BACKFILL_STALE_PIPELINE_ERROR = "clear_pipeline_error_on_successful_terminal_status"

# Name reported in MigrationReport.backfills_applied when the migration reduced
# stored source paths to the single spelling ingest records.
BACKFILL_NON_CANONICAL_SOURCE_PATH = "normalize_non_canonical_source_paths"

# Name reported in MigrationReport.backfills_applied when the migration moved a
# vault's document-level text off the passage surface onto its own.
BACKFILL_DOCUMENT_SURFACE = "relocate_document_level_text_to_document_surface"

# Name reported in MigrationReport.backfills_applied when the migration dropped
# the document title from the front of a document's passage heading paths.
BACKFILL_HEADING_PATH_TITLE_ROOT = "strip_document_title_from_passage_heading_paths"


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


@dataclass(frozen=True)
class _RetainedCopyObservation:
    """What one look at a document's retained source copy established.

    The source-file integrity audit and the source-file restore both turn on
    the same question -- does the retained copy still hash to what the record
    expects? -- and the restore exists to repair what the audit reports. Both
    read their answer from this one observation so that "intact" cannot mean
    one thing to the audit and another to the repair: an operator is never sent
    to fix a copy the restore then declares fine, and the restore never rewrites
    a copy the audit calls healthy.

    ``observed_hash`` is null when the copy is absent, and also when the caller
    asked only about presence; :attr:`intact` is false in both cases, and it is
    the caller's business to tell them apart through ``present``.

    ``symlinked`` says the recorded path is a link rather than the copy itself.
    It is carried here rather than asked separately at each surface, for the
    same reason the digest is: the write side refuses to write at such a path,
    so an audit that called it healthy would send an operator to a repair that
    then refuses. Being a link is not a state a copy can be intact in, so it
    suppresses :attr:`intact` outright -- however the bytes behind the link
    happen to hash. It defaults to False, the state of every ordinary retained
    file.

    ``symlinked`` and ``present`` are independent, and both stay meaningful
    together: a link whose target is gone is absent, while one resolving to a
    real file is present, and the two call for different repairs -- re-deliver
    the content, or merely put a real copy back at the recorded path. Reading
    ``present`` as "the copy is there" is only sound once ``symlinked`` is
    false; where it is true, presence describes what the link resolves to
    rather than the copy the record names.

    ``out_of_root`` says the recorded path names somewhere outside the vault's
    source tree -- the other question the write side asks, carried here for the
    same reason as the first: the store refuses to write at such a path, so an
    audit that called it healthy would send an operator to a repair that then
    refuses. Being unwritable is not a state a copy can be intact in, so it
    suppresses :attr:`intact` outright, however the bytes at the end of the path
    happen to hash.

    It stays a fact of its own rather than folding into ``symlinked`` because
    the two call for different remedies: a link at the recorded path is removed,
    while a path leaving the tree is re-pointed or the vault reconfigured.
    Neither implies the other -- a plain file under an ancestor pointing outside
    the tree is not a link, and a link resolving back inside the tree does not
    leave it. Independent of ``present`` too, for the reason linkedness is: the
    refusal does not depend on what, if anything, resolves at the far end.
    """

    present: bool
    observed_hash: str | None
    expected_hash: str
    symlinked: bool = False
    out_of_root: bool = False

    @property
    def intact(self) -> bool:
        """The retained copy is present, is not a link, names a path inside the
        source tree, and hashes to what the record expects."""
        if self.symlinked or self.out_of_root:
            return False
        return self.observed_hash is not None and self.observed_hash == self.expected_hash


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

        Two data backfills run, each naming itself in ``backfills_applied``
        only when it changed rows, so a clean vault reports an empty list and a
        re-call after a repair reports nothing further.

        The first: a document that failed abstraction and was later repaired
        predates the rule that a successful terminal ``pipeline_status`` clears
        ``pipeline_error``, so it still carries the message describing a failure
        that no longer holds. The backfill nulls ``pipeline_error`` on every
        document already at a successful terminal status.

        The second: a document ingested before the recorded source path was
        reduced to one spelling can still hold another -- a ``.`` segment, a
        doubled separator, or a trailing one. The read side resolves such a
        path while the write-time guard refuses it, so the record names a
        location its own bytes cannot be written back to, and re-projecting it
        raises the cross-document path-mismatch guard rather than repairing it.
        The backfill rewrites the stored value to the plain form ingest would
        compute, and reports each rewrite in ``source_paths_normalized``. A path
        that walks out of the source tree has no plain form inside it, so it is
        left exactly as recorded; ``verify_vault_source_files`` is where that
        condition is reported.

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

        normalized = await self._normalize_source_paths()
        if normalized:
            backfills_applied.append(BACKFILL_NON_CANONICAL_SOURCE_PATH)

        relocated, stripped = await self._migrate_to_document_surface()
        if relocated:
            backfills_applied.append(BACKFILL_DOCUMENT_SURFACE)
        if stripped:
            backfills_applied.append(BACKFILL_HEADING_PATH_TITLE_ROOT)

        activations, collisions = await self._activate_tier3_uniqueness()

        return MigrationReport(
            vault_id=self._vault_id,
            columns_added=[],
            backfills_applied=backfills_applied,
            source_paths_normalized=normalized,
            tier3_uniqueness_activations=activations,
            tier3_uniqueness_collisions=collisions,
        )

    async def _migrate_to_document_surface(self) -> tuple[int, int]:
        """Move document-level text onto its own surface (CAS-ADR-049).

        A vault provisioned before that decision holds two pieces of legacy
        state: a synthetic header row per document on the passage surface, and
        -- for a source format whose title is also its top-level heading --
        passage heading paths rooted at the document title. Both are repaired
        here.

        The pass is driven by the legacy header rows rather than by the
        document catalog, because their presence is exactly the condition
        being repaired. Each document's row is recomposed from its stored
        record rather than parsed out of the header's composed text: the record
        is the authority for title, tags, abstract and source path, and
        recomposing keeps a migrated vault identical to a freshly ingested one.
        The legacy row's embedding is carried forward, so a corpus is not
        re-embedded to change where its text is stored.

        The title is stripped from a heading path only where the path's first
        element is exactly the document's title. The title is not a prefix any
        adapter adds -- for markdown it coincides with the document's own
        top-level heading, and for other formats it is absent -- so an
        unconditional strip would take a real section or sheet name off every
        document that never carried the title there.

        Ordering is deliberate: relocate, then strip, then delete the legacy
        rows last. A pass interrupted part-way leaves the legacy rows in place,
        so the next run repeats the whole repair rather than resuming into a
        half-migrated vault. Re-running a completed migration finds no legacy
        rows and does nothing, which is what makes it idempotent.

        Returns:
            ``(documents relocated, documents whose heading paths changed)``.
            Both are zero on a vault that has nothing to repair, so neither
            backfill names itself in the report.
        """
        legacy = await self._content_store.legacy_document_header_rows()
        if not legacy:
            return (0, 0)

        relocated = 0
        stripped = 0
        for document_id, embedding in legacy:
            doc = await self._graph_store.get_document(document_id)
            if doc is None:
                # A header row whose document is gone has nothing to compose
                # from; the delete below reclaims it.
                continue
            await self._content_store.upsert_document_surface(
                compose_document_surface(document_id, doc, embedding)
            )
            relocated += 1
            if doc.title and await self._content_store.strip_heading_path_root(
                document_id, doc.title
            ):
                stripped += 1

        await self._content_store.delete_legacy_document_header_rows()
        return (relocated, stripped)

    async def _normalize_source_paths(self) -> list[SourcePathNormalization]:
        """Rewrite each stored source path that is not already in plain form.

        The plain form is computed with the same function ingest records
        through, not a second reduction that happens to agree today: the whole
        point of the rewrite is that the path a re-projection computes and the
        path the record holds stop differing, and two implementations of "plain
        form" would leave that difference in place under some spelling.

        A path the normalizer refuses is skipped rather than propagated as a
        failure. There is nothing to rewrite it to, and letting the refusal
        escape would turn one unrepairable legacy record into a failure of the
        whole migration, taking the other backfill and the uniqueness scan with
        it. What arrives here to be refused is an absolute path that also
        carries a reducible segment: reducing it is a real change, so the record
        is a candidate, but the result still names somewhere outside the vault.
        A path walking *up* out of the tree never arrives -- it is preserved
        rather than resolved, so it is already its own plain form and the store
        does not offer it -- and both conditions are reported by
        ``verify_vault_source_files`` rather than here.

        Every rewrite is planned before any of them is applied, so the pass can
        say which records end up sharing a path. That question has a stable
        answer only against the vault as it stood before the pass began: asked
        while rewriting, it would depend on how far the pass had got, and each
        of two converging records would give a different answer about the same
        convergence. This is also the last moment it can be asked at all -- the
        differing spellings are what keep such records apart, and once they are
        reduced the pass has spent the only evidence that they were ever
        distinguishable. It is reported rather than acted on: several documents
        on one source path is a state the substrate allows, resolving it means
        deciding which record to keep, and that judgment is the operator's.
        """
        from sage.vault_source_binding import VaultRootEscapeError, normalize_vault_relative

        # doc_id -> (stored spelling, plain form), in id order.
        planned: dict[str, tuple[str, str]] = {}
        for doc_id, stored in sorted(
            (await self._graph_store.list_non_canonical_source_paths()).items()
        ):
            try:
                plain = normalize_vault_relative(stored)
            except VaultRootEscapeError:
                continue
            if plain == stored:
                continue
            planned[doc_id] = (stored, plain)

        if not planned:
            # A vault with nothing to repair asks the store once and stops; the
            # holder lookup below is work only a real repair has earned.
            return []

        prior_holders = await self._graph_store.find_document_ids_by_source_paths(
            sorted({plain for _, plain in planned.values()})
        )

        normalized: list[SourcePathNormalization] = []
        for doc_id, (stored, plain) in planned.items():
            shared = (
                set(prior_holders.get(plain, ()))
                | {other for other, (_, p) in planned.items() if p == plain}
            ) - {doc_id}
            # Only the path is written. The record's meaning is unchanged --
            # this is a respelling, not a modification -- so stamping
            # ``updated_at`` here would make every record in a legacy vault
            # look freshly changed to the drift and staleness comparisons.
            await self._graph_store.update_document(doc_id, {"source_path": plain})
            normalized.append(
                SourcePathNormalization(
                    document_id=doc_id,
                    previous_source_path=stored,
                    normalized_source_path=plain,
                    path_shared_with=sorted(shared),
                )
            )
        return normalized

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

        A recorded path that is a *link* rather than the retained copy is
        reported as ``symlinked`` in both modes, and is not read through:
        every other read resolves a link, so such a path would otherwise
        read as an intact copy while its bytes live wherever the link's
        owner points -- and the store refuses to write there, so the
        repair the audit sends an operator to would itself be refused.

        A recorded path that resolves *outside* the vault's source tree --
        under an ancestor pointing elsewhere, say -- is reported as
        ``out_of_root`` in both modes, and likewise not read through. The
        store refuses to write there too, whether or not anything resolves
        at the far end, so it outranks ``missing``: an absent copy at such
        a path is not repaired by re-delivering the content.

        Returns a SourceFileIntegrityReport with per-document entries for
        missing, symlinked, out-of-root, or hash-mismatched files and
        aggregate counts; documents with an intact source file are absent
        from ``entries``.
        """
        all_docs = await self._graph_store.list_all_documents()
        storage_root = self._storage_root()
        store = self._vault_source_store()

        entries: list[SourceFileIntegrityEntry] = []
        for doc in all_docs:
            # A refusal aborts the walk rather than becoming a per-document
            # status: the audit and the repair read the store through one
            # observation helper, and a caller is owed the same answer from it
            # whichever operation asked. The error names the document the store
            # declined on, that document's path being what the refused call was
            # made with, rather than the walk as a whole.
            entry = self._check_document_source_file(doc, storage_root, check_hashes, store)
            if entry is not None:
                entries.append(entry)

        summary = {
            "healthy": len(all_docs) - len(entries),
            "missing": sum(1 for e in entries if e.integrity_status == "missing"),
            "hash_mismatch": sum(1 for e in entries if e.integrity_status == "hash_mismatch"),
            "symlinked": sum(1 for e in entries if e.integrity_status == "symlinked"),
            "out_of_root": sum(1 for e in entries if e.integrity_status == "out_of_root"),
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
        symlinked, out of root, missing, or hash-mismatched, else None.

        Classifies the shared :class:`_RetainedCopyObservation` -- the same
        one the restore judges "already intact" by -- so the audit and the
        repair agree by construction. When ``check_hashes`` is set, a present
        source is additionally hashed and compared against the digest recorded
        for the *stored* copy (see :func:`_expected_stored_hash`). A missing
        source is always classified ``missing`` regardless of ``check_hashes``
        (it is never a hash error).

        The two statuses that describe the *path* rather than the copy outrank
        both. ``symlinked`` comes first: where the recorded path is itself a
        link, removing it is the whole repair, and saying so is more use than
        naming wherever it happens to point. ``out_of_root`` comes next, ahead
        of ``missing``, because the store refuses to write there whether or not
        anything resolves at the far end -- reporting a document absent would
        send an operator to re-deliver content into a path that will decline it,
        when the fix is to bring the path back inside the tree.
        """
        observation = self._observe_retained_copy(doc, storage_root, store, hash_copy=check_hashes)
        if observation.symlinked:
            # Reported in both modes: establishing this is an lstat, not a
            # content read, so the existence-only mode carries no new cost and
            # has no reason to withhold the finding. The observed digest stays
            # null -- the audit reports the link rather than reading through it.
            return self._integrity_entry(doc, "symlinked", observed=None)

        if observation.out_of_root:
            # Reported in both modes for the same reason: establishing it is a
            # path resolution, not a content read. The observed digest stays
            # null -- what the bytes at the end of an unwritable path hash to is
            # not a fact about the copy this record can hold.
            return self._integrity_entry(doc, "out_of_root", observed=None)

        if not observation.present:
            return self._integrity_entry(doc, "missing", observed=None)

        if check_hashes and not observation.intact:
            return self._integrity_entry(doc, "hash_mismatch", observed=observation.observed_hash)

        return None

    def _observe_retained_copy(
        self,
        doc: Document,
        storage_root: Path,
        store: VaultSourceStore,
        *,
        hash_copy: bool = True,
    ) -> _RetainedCopyObservation:
        """Look once at ``doc``'s retained source copy.

        Existence and hashing are resolved through the vault-source store
        (CAS-ADR-043), the same store ``get_document`` delivers through, so
        the observation is exactly what delivery would see. With ``hash_copy``
        false only presence is established and no content is read; the
        audit's existence-only mode relies on that.
        """
        expected = _expected_stored_hash(doc)
        # Presence, linkedness and containment are independent facts about the
        # recorded path, and all three are cheap -- two stats and a path
        # resolution -- so all three are established before any is used. What the
        # audit orders is the *classification* -- a linked path is reported as the
        # link it is rather than as missing -- not these calls.
        present = store.source_exists(self._vault_id, storage_root, doc.source_path)
        out_of_root = store.source_is_out_of_root(self._vault_id, storage_root, doc.source_path)
        if store.source_is_symlink(self._vault_id, storage_root, doc.source_path):
            # A link is not the copy the record names, so no digest is taken
            # through it. Whether bytes resolve behind it is kept rather than
            # collapsed to absent: those are two different repairs. A dangling
            # link means the content is gone and has to be re-delivered; a
            # resolving one means it is sitting behind the link and only the
            # copy has to be put back at the path the record holds.
            return _RetainedCopyObservation(
                present=present,
                observed_hash=None,
                expected_hash=expected,
                symlinked=True,
                out_of_root=out_of_root,
            )
        if out_of_root:
            # No digest is taken either: the path names somewhere the store will
            # not write, so what the bytes at the far end hash to says nothing
            # about the copy this record can hold. Presence is kept for the same
            # reason it is kept behind a link -- once the path is brought back
            # inside the tree, whether the content is still there decides whether
            # a re-delivery is needed on top of that.
            return _RetainedCopyObservation(
                present=present,
                observed_hash=None,
                expected_hash=expected,
                out_of_root=True,
            )
        if not present:
            return _RetainedCopyObservation(
                present=False, observed_hash=None, expected_hash=expected
            )
        observed = (
            store.hash_source(self._vault_id, storage_root, doc.source_path) if hash_copy else None
        )
        return _RetainedCopyObservation(
            present=True, observed_hash=observed, expected_hash=expected
        )

    def _vault_source_store(self) -> VaultSourceStore:
        """The active profile's vault-source store.

        Resolved through the stack config at call time so the audit and the
        restore are binding-agnostic (CAS-ADR-043) and follow whichever
        binding the running profile selected.
        """
        from sage.mcp_init import get_stack_config, resolve_stack_vault_source_store

        return resolve_stack_vault_source_store(get_stack_config())

    def _storage_root(self) -> Path:
        """The vault's resolved storage root, as the source-store port expects it."""
        return Path(self._config.vault.storage_root).expanduser().resolve()

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

    async def restore_vault_source_file(
        self, source: str, document_id: str | None = None
    ) -> SourceFileRestoreReport:
        """Write delivered bytes back over a document's retained source file.

        The repair counterpart of :meth:`verify_vault_source_files`. That audit
        reports a retained copy that changed outside SAGE but cannot fix one: a
        re-ingest offers bytes to ``retain_source``, which sees only that they
        differ from what sits at its target -- indistinguishable from a name
        collision -- and homes the document at a second path rather than
        restoring the first. This writes to the path the record already names,
        through the port's ``write_source``, so the document does not move.

        SAGE keeps no pristine second copy of a source, so the caller supplies
        the bytes. The target is resolved from their digest against recorded
        provenance, which is what makes ``document_id`` unnecessary in the
        ordinary case: the bytes identify the document that was made from them.
        ``document_id`` pins the target when that resolution is ambiguous (two
        documents share a provenance hash) or unavailable (a record predating
        the delivered/stored digest split, whose provenance hash describes the
        stored copy rather than the delivered bytes).

        Writes nothing when the retained copy already hashes to its recorded
        digest: an unconditional rewrite would re-stamp the copy under a binding
        that rewrites at rest, churning the recorded digest for no repair. A
        recorded path that is a *link* is never treated that way, however the
        bytes behind it hash -- it is not the copy the record names -- and the
        write the fall-through attempts is refused by the store rather than
        landing wherever the link points. Nor is a path that resolves outside
        the vault's source tree: the store declines it on the same
        fall-through, so the refusal reaches the caller instead of a report
        calling a document fine that cannot be repaired where it sits.

        Where a write does happen, the store reports the digest of the copy it
        now holds -- reading it back only under a binding that may have
        rewritten the bytes -- and the record's ``stored_content_hash`` follows
        it only where the store demonstrably rewrote them. That is narrower
        than "an actual write happened": a store returning what it was handed
        licenses no update, so bytes that are not this document's leave the
        recorded mismatch reported rather than adopted. ``record_refreshed`` on
        the report says which
        happened. It matters under a binding that
        rewrites at rest, where writing the original bytes back produces a
        stored copy that is correct but freshly stamped, and so hashes to
        neither the delivered digest nor the previous stored one. The
        provenance hash is never touched -- the bytes the document was made
        from have not changed.
        """
        delivered = Path(source)
        if not delivered.is_absolute():
            raise RestoreSourceNotAbsoluteError(source)
        if not delivered.is_file():
            raise SourceFileNotFoundError(source)

        from sage.vault_source_binding import VaultRootEscapeError, hash_file

        # A streamed digest: the target has to be resolved from it before
        # anything is written, and the write streams the file from its path
        # again, so at no point does the delivered file need to fit in memory.
        delivered_hash = hash_file(delivered)

        doc, provenance_verified = await self._resolve_restore_target(delivered_hash, document_id)
        storage_root = self._storage_root()
        store = self._vault_source_store()

        # A restore reads the retained copy before it decides whether to write
        # at all, and a refusal on that read is the same upstream fact to the
        # caller as a refusal on the write: which of the two the store declined
        # is a detail of how the repair is sequenced, and a caller cannot act
        # differently on it. Both are typed at the binding, so neither depends
        # on the repair remembering to cover it.
        # The same observation the integrity audit classifies, so the copy the
        # audit reports drifted is the copy this repairs, and no other.
        observation = self._observe_retained_copy(doc, storage_root, store)
        expected = observation.expected_hash
        observed = observation.observed_hash

        if observation.intact:
            return SourceFileRestoreReport(
                vault_id=self._vault_id,
                document_id=doc.id,
                source_path=doc.source_path,
                status="already_intact",
                provenance_verified=provenance_verified,
                record_refreshed=False,
                expected_content_hash=expected,
                observed_content_hash=observed,
                stored_content_hash=observed,
            )

        try:
            restored_hash = store.write_source(
                self._vault_id, storage_root, doc.source_path, delivered
            )
        except VaultRootEscapeError as exc:
            # The binding refuses a destination it cannot write at the named
            # path. Translated here rather than left to propagate: it is a
            # ``ValueError``, not a ``SAGEError``, so the HTTP surface would
            # return a bare 500 with no error code against a spec that declares
            # none. The binding's own message travels with it -- it has several
            # distinct causes and only it knows which one fired.
            raise VaultSourcePathRefusedError(doc.source_path, str(exc)) from exc
        record_refreshed = restored_hash != expected and restored_hash != delivered_hash
        if record_refreshed:
            # Refreshed only when the *store* changed the bytes -- the sole
            # reason the recorded digest may legitimately move. A store that
            # returns what it was handed yields ``restored_hash ==
            # delivered_hash``; if that still differs from what the record
            # expected, the delivered bytes were not this document's, and
            # adopting their digest would take the integrity audit green over a
            # copy that is now simply wrong. Leaving the record alone keeps the
            # mismatch reported, which is the outcome to want: the operator sees
            # the repair did not take rather than losing the evidence.
            #
            # The rule cannot separate restamped-*right* from restamped-*wrong*:
            # once a store rewrites what it was handed, the resulting digest
            # differs from both comparators either way. That residue is reachable
            # only through an unverifiable pin, which is why the report carries
            # ``provenance_verified`` rather than leaving the caller to assume a
            # check that did not happen.
            await self._graph_store.update_document(doc.id, {"stored_content_hash": restored_hash})

        return SourceFileRestoreReport(
            vault_id=self._vault_id,
            document_id=doc.id,
            source_path=doc.source_path,
            status="restored",
            provenance_verified=provenance_verified,
            record_refreshed=record_refreshed,
            expected_content_hash=expected,
            observed_content_hash=observed,
            stored_content_hash=restored_hash,
        )

    async def _resolve_restore_target(
        self, delivered_hash: str, document_id: str | None
    ) -> tuple[Document, bool]:
        """The target document, and whether the delivered bytes were verified as its.

        The second value is false only on the pre-split pin below, where nothing
        on the record can confirm the delivered file is the right one. It travels
        to the caller so a restore never reports more assurance than it had.

        A pin is a primary-key read. Without one the search is enumerated rather
        than looked up by hash: the hash lookup collapses several documents
        sharing a provenance digest to one arbitrary row, and a restore silently
        picking among them could overwrite an intact document's copy. Scanning is
        affordable there -- this is an operator-invoked repair, and the audit
        that sends callers to it already walks every document.

        A pin says *which* copy to write over. It does not license writing
        arbitrary bytes there, so the delivered digest is still checked against
        the pinned document's provenance: without that, delivering the wrong file
        under a pin overwrites the retained copy and the caller's own refresh
        then re-describes the record to match, taking the integrity audit green
        over a document whose stored bytes are now something else.

        The check is skipped for a record carrying no stored digest. Such a
        record predates the delivered/stored split, so its provenance hash
        describes the stored copy and a caller re-delivering the original cannot
        match it -- the case the pin exists to serve, and the only genuinely
        unverifiable one. What keeps that exemption from laundering is the
        caller's refresh rule, which moves the recorded digest only when the
        store demonstrably rewrote the bytes.
        """
        if document_id is not None:
            pinned = await self._graph_store.get_document(document_id)
            if pinned is None:
                raise DocumentNotFoundError(document_id)
            if (
                pinned.stored_content_hash is not None
                and delivered_hash != pinned.source_content_hash
            ):
                raise RestoreProvenanceMismatchError(
                    document_id, delivered_hash, pinned.source_content_hash
                )
            return pinned, pinned.stored_content_hash is not None

        all_docs = await self._graph_store.list_all_documents()
        matches = [d for d in all_docs if d.source_content_hash == delivered_hash]
        if len(matches) != 1:
            raise RestoreTargetUnresolvedError(delivered_hash, [d.id for d in matches])
        return matches[0], True

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

                    status = await self._wait_for_terminal(doc.id)
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
                        # Every non-success settles into failed_count, but the
                        # ways of getting there are different findings and are
                        # reported apart. A document back at
                        # abstraction_skipped declined abstraction rather than
                        # failing at it; a timed-out one was abandoned by the
                        # waiter and may yet finish; an interrupted one had its
                        # work dropped by a stopped queue. Reporting any of them
                        # as an llm_failure sends an operator looking for a
                        # provider error that was never raised.
                        outcome, event_status, error_message = _reabstract_failure_report(status)
                        entries.append(
                            ReabstractReportEntry(
                                document_id=doc.id,
                                outcome=outcome,
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
                            status=event_status,
                            outcome=outcome,
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

    async def _wait_for_terminal(self, document_id: str) -> str:
        """Poll the document's pipeline_status until terminal, then return it.

        Returns :data:`_WAIT_MISSING` if the document disappears mid-flight,
        and :data:`_WAIT_TIMEOUT` if it has not settled within
        :data:`_WAIT_TIMEOUT_SECONDS`. The ceiling is what makes this
        bounded: a generation slow enough outlasts any waiter, and an
        unbounded poll against one never returns.

        Both the terminal set and the ceiling are read from module scope
        rather than taken as arguments. A parameter that one production
        caller always passes the same value to is a seam only tests use,
        and a second way to set the ceiling is a second thing that can
        disagree with the message reporting it.

        A timeout is a statement about the waiter, not about the document:
        the generation may still be running and may still complete. But it
        is not self-healing. This sweep enumerates ``abstraction_skipped``
        only, and an abandoned document sits at ``abstraction_in_progress``,
        so a later sweep reaches it only once something else advances it --
        the generation finishing, startup recovery
        (``recover_incomplete_documents``), or the bulk CLI with a selector
        naming that status. A document the worker dropped is a separate case
        and no longer one of these: stopping the worker settles it at
        ``abstraction_interrupted``, which is terminal, so this wait returns
        it rather than abandoning it.
        """
        deadline = asyncio.get_running_loop().time() + _WAIT_TIMEOUT_SECONDS
        while True:
            doc = await self._graph_store.get_document(document_id)
            if doc is None:
                return _WAIT_MISSING
            status = doc.pipeline_status
            if status in TERMINAL_PIPELINE_STATUS_VALUES:
                return status
            # Checked after the status read so a document that settled
            # exactly at the deadline is reported as settled rather than
            # abandoned one poll short of its own terminal status.
            if asyncio.get_running_loop().time() >= deadline:
                return _WAIT_TIMEOUT
            await asyncio.sleep(_POLL_INTERVAL_SECONDS)
