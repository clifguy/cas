"""Batch ingestion orchestrator: the three-phase bulk-ingest pipeline.

Owns the three-phase pipeline:
  Phase 1: Pre-ingest edge plan construction
  Phase 2: Per-file sequential ingestion
  Phase 3: Post-ingest edge resolution and execution

This is substrate-level orchestration over the SAGE service bundle: it
composes ``IngestionService.ingest`` with the batch edge-inference
primitives in ``sage.services.batch_inference`` and is consumed by
every bulk-ingest caller -- the MCP tool surface, the in-process
FastAPI delivery path, and the upload+stream REST endpoint. Callers
provide delivery glue (JSON serialization or SSE streaming) via
optional progress callbacks; this module stays free of HTTP coupling.

The ``needs_review`` confirmation-queue policy is a caller input, not a
substrate constant: ``run`` defaults it to ``True`` so the CAS
bulk-ingest workflow (CAS-ADR-021) keeps surfacing inferred values for
human confirmation, but a caller that wants caller-authoritative
metadata may pass ``needs_review=False``.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from sage.models.enums import SourceType
from sage.models.schemas import IngestRequest
from sage.services.batch_inference import (
    EdgePlan,
    InferenceItem,
    plan_batch_edges,
    resolve_and_execute,
)
from sage.services.filename_parser import ParsedMetadata

if TYPE_CHECKING:
    from sage.mcp_init import SAGEServices

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------


@dataclass
class ParsedMetadataInput:
    """Neutral metadata representation accepted from any caller."""

    title: str
    date: str | None = None
    project: str | None = None
    codes: list[str] = field(default_factory=list)
    version: str | None = None
    doc_type: str | None = None


@dataclass
class FileDescriptor:
    """Neutral file descriptor accepted from any caller."""

    file_path: str
    source_type: str
    parsed_metadata: ParsedMetadataInput | None = None


@dataclass
class IngestSummary:
    """Result of a batch ingestion run."""

    docs_new: int = 0
    docs_version: int = 0
    metadata_pending: int = 0
    abstracts_generated: int = 0
    abstracts_deferred: int = 0
    edges_created: dict[str, int] = field(default_factory=dict)
    edges_staged: dict[str, int] = field(default_factory=dict)
    edges_removed: int = 0
    edges_dropped: int = 0
    edge_warnings: list[dict[str, str]] = field(default_factory=list)
    error_count: int = 0
    errors: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        """Produce the summary dict both callers need."""
        result: dict = {
            "documents_created": {
                "new": self.docs_new,
                "new_version": self.docs_version,
            },
            "metadata_pending": self.metadata_pending,
            "edges_created": self.edges_created,
            "edges_staged": self.edges_staged,
            "edges_removed": self.edges_removed,
            "edges_dropped": self.edges_dropped,
            "abstracts_generated": self.abstracts_generated,
            "abstracts_deferred": self.abstracts_deferred,
            "error_count": self.error_count,
            "errors": self.errors,
        }
        if self.edge_warnings:
            result["edge_warnings"] = self.edge_warnings
        return result


# Callback type aliases
OnFileStart = Callable[[int, int, str], Awaitable[None]]
OnFileDone = Callable[[int, int, str, str], Awaitable[None]]
OnFileError = Callable[[int, int, str, str], Awaitable[None]]


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


class BatchIngestService:
    """Shared three-phase batch ingestion pipeline."""

    async def run(
        self,
        files: list[FileDescriptor],
        vault_services: SAGEServices,
        infer_edges: bool = True,
        needs_review: bool = True,
        on_file_start: OnFileStart | None = None,
        on_file_done: OnFileDone | None = None,
        on_file_error: OnFileError | None = None,
    ) -> IngestSummary:
        """Execute the three-phase batch ingestion pipeline.

        ``needs_review`` (defaults to ``True``, CAS-ADR-021):
        Each per-file ``IngestRequest`` issued by this service sets
        ``needs_review`` to the caller-supplied value. The default of
        ``True`` lands every document with ``metadata_confirmed=False``
        in the metadata-review queue — the opposite of
        ``IngestionService.ingest``'s caller-authoritative default — so
        the CAS bulk-ingest workflow surfaces inferred values for human
        confirmation. A caller that wants caller-authoritative metadata
        passes ``needs_review=False``. See the inline comment at the
        ``IngestRequest`` construction site.

        Filename parsing always runs when ``needs_review=True``:
        Because ``needs_review`` opts the document into the
        confirmation queue, the vault's ``FilenameParser`` runs on every
        file regardless of the contents of
        ``FileDescriptor.parsed_metadata``. It may populate ``date``,
        ``project``, ``codes``, ``version``, and ``doc_type`` from the
        filename when the caller omits those keys — the exact fields the
        parser extracts are vault-config-defined under
        ``metadata_extraction.filename_extraction.segment_fields``.

        Per-file failure isolation (CAS-ADR-029):
        The batch is NOT atomic. Per-file exceptions are caught into
        ``IngestSummary.errors`` as ``{filename, message}`` entries
        (with ``error_count`` advancing in lockstep); the batch
        continues with the remaining files and Phase 3 edge
        execution still runs across whatever did insert. Earlier or
        later items are not rolled back. Mirrors the bulk-tool
        atomicity contract used by ``sage_bulk_*`` operations.

        Predecessor auto-transition on Tier-1 supersedes inference:
        When ``infer_edges=True`` and Phase 3 edge resolution
        creates a Tier-1 ``supersedes`` edge via version-chain
        inference, the target document transitions as part of edge
        execution — no explicit lifecycle-transition call is
        required. Both halves of that transition come from the
        vault's lifecycle table: the states it may be taken from,
        and the state it lands in (``active -> archived`` under the
        base lifecycle).

        A target already holding a state a supersession lands in
        gets the edge and no write — the chain-repair case, where an
        earlier supersession already moved it. A target in any other
        state is not superseded at all: the edge is not created,
        ``edges_dropped`` advances, and a
        ``supersede_target_not_transitionable`` warning names the
        observed state and the permitted ones (an absent or unreadable
        target refuses the edge the same way, under
        ``supersede_target_missing`` and
        ``supersede_target_read_failed``). Because the refusal is
        settled before anything is written, a chain repair whose
        replacement add is refused withholds its removals as well,
        under ``chain_repair_withheld``, rather than severing the
        chain it was repairing. Replacement adds are written before
        the removals they replace, and a replacement that fails on
        write (``edge_creation_failed``) withholds its group's
        removals the same way, so a repair never leaves the graph
        holding fewer supersedes edges than it found.

        A transition-carrying supersession commits its edge and its
        lifecycle write as one database transaction, under the same
        per-predecessor lock the explicit lifecycle and ingest
        surfaces take, against a fresh in-lock read — a failed commit
        leaves neither half behind, and a state change racing the
        check refuses the edge instead of forking the chain. Only the
        single-row write that converges a pre-existing edge's
        outstanding transition can still fail with the edge standing,
        reported as ``lifecycle_transition_failed``. A successful
        transition is followed by the chunk-store lifecycle sync the
        explicit lifecycle path performs, keeping chunk-level
        pre-filtering aligned with the document's new state; a sync
        failure warns as ``chunk_lifecycle_sync_failed``. All of these
        surface as warnings in ``IngestSummary.edge_warnings``; no case
        raises, and none appears in ``IngestSummary.errors``.

        Tier-1 provenance-gate downgrade:
        Tier-1 ``supersedes`` adds are gated on provenance: if any
        existing edge in a candidate version chain has a
        non-``version_chain`` rationale (e.g., a human-curated
        ``manual_review`` edge in the same chain), the entire
        group's Tier-1 adds are silently downgraded to Tier-2
        (deposited in the staging-edge table for review rather than
        landing as production edges; the predecessor auto-transition
        above does NOT fire on a downgraded group). The
        production-vs-staging outcome of a batch is therefore
        rule-dependent on the vault's prior edge graph, not
        deterministic from the input ``FileDescriptor`` list alone.

        Args:
            files: Neutral file descriptors to ingest.
            vault_services: SAGE service bundle for the target vault.
            infer_edges: When True, run two-phase edge inference.
            needs_review: When True (default), every per-file ingest
                opts into the metadata-review queue.
            on_file_start: Optional callback before each file.
            on_file_done: Optional callback after successful ingestion.
            on_file_error: Optional callback on per-file failure.
        """
        if not files:
            raise ValueError("No files selected for ingestion")

        summary = IngestSummary()
        total = len(files)

        # Phase 1: Pre-ingest edge plan
        edge_plan = None
        if infer_edges:
            edge_plan = await self._build_edge_plan(files, vault_services)

        # Phase 2: Per-file ingestion. Identifier_mention inference
        # now runs inside IngestionService.ingest itself (after Stage 2,
        # before Stage 3), so all ingest pathways honor the rule. This
        # service retains pre-ingest plan construction (Phase 1) and Tier-2
        # staging-edge orchestration (Phase 3) only.
        path_to_id: dict[str, str] = {}
        for i, fd in enumerate(files):
            filename = Path(fd.file_path).name

            if on_file_start is not None:
                await on_file_start(i, total, filename)

            try:
                metadata_dict = _metadata_dict_from_parsed(fd.parsed_metadata)
                request = IngestRequest(
                    source=fd.file_path,
                    source_type=SourceType(fd.source_type),
                    metadata=metadata_dict,
                    # CAS-ADR-021: SAGE's default is to commit caller-
                    # supplied metadata as authoritative. The CAS bulk-
                    # ingest workflow surfaces inferred values for human
                    # confirmation, so it opts the document into the
                    # metadata-review queue (needs_review defaults True).
                    needs_review=needs_review,
                )
                ingest_result = await vault_services.ingestion_service.ingest(
                    request,
                )
                path_to_id[fd.file_path] = ingest_result.document.id

                if ingest_result.is_new:
                    summary.docs_new += 1
                else:
                    summary.docs_version += 1

                if not ingest_result.document.metadata_confirmed:
                    summary.metadata_pending += 1

                if vault_services.config.abstraction.enabled:
                    summary.abstracts_generated += 1
                else:
                    summary.abstracts_deferred += 1

                if on_file_done is not None:
                    await on_file_done(i, total, filename, ingest_result.document.id)

            except Exception as exc:
                summary.error_count += 1
                summary.errors.append(
                    {
                        "filename": filename,
                        "message": str(exc),
                    }
                )
                if on_file_error is not None:
                    await on_file_error(i, total, filename, str(exc))

        # Phase 3: Post-ingest edge creation
        if edge_plan is not None:
            edge_result = await resolve_and_execute(
                edge_plan,
                path_to_id,
                vault_services.graph_store,
                vault_services.graph_ops_service,
                # The vault's own lifecycle service and lock manager, not
                # parallel constructions: the batch path validates against
                # the same transition table, builds its writes through the
                # same prepare step, and serializes on the same
                # per-predecessor locks as the explicit lifecycle and
                # ingest surfaces, so a supersession cannot behave
                # differently for arriving through this path.
                vault_services.lifecycle_service,
                vault_services.lock_manager,
                # For the chunk-store lifecycle sync that follows a
                # supersede's document write.
                content_store=vault_services.content_store,
            )
            summary.edges_created = edge_result.edges_created
            summary.edges_staged = edge_result.edges_staged
            summary.edges_removed = edge_result.edges_removed
            summary.edges_dropped = edge_result.edges_dropped
            summary.edge_warnings = edge_result.warnings

        return summary

    async def _build_edge_plan(
        self,
        files: list[FileDescriptor],
        vault_services: SAGEServices,
    ) -> EdgePlan:
        """Phase 1: build edge plan from file descriptors.

        The caller's job is the ``FileDescriptor`` -> ``InferenceItem``
        adapter; the vault-querying and rule application live in the
        SAGE substrate (``sage.services.batch_inference``).
        """
        scan_items: list[InferenceItem] = []
        for fd in files:
            pm = fd.parsed_metadata
            if pm:
                parsed = ParsedMetadata(
                    title=pm.title,
                    date=pm.date,
                    project=pm.project,
                    codes=pm.codes,
                    version=pm.version,
                    doc_type=pm.doc_type,
                )
            else:
                parsed = ParsedMetadata(title=Path(fd.file_path).stem)
            scan_items.append(
                InferenceItem(
                    ref=fd.file_path,
                    is_existing=False,
                    parsed=parsed,
                )
            )

        return await plan_batch_edges(scan_items=scan_items, vault_services=vault_services)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _metadata_dict_from_parsed(
    pm: ParsedMetadataInput | None,
) -> dict[str, str] | None:
    """Convert ParsedMetadataInput to flat string dict for IngestRequest.metadata."""
    if pm is None:
        return None
    d: dict[str, str] = {}
    if pm.title:
        d["title"] = pm.title
    if pm.date:
        d["date"] = pm.date
    if pm.project:
        d["project"] = pm.project
    if pm.codes:
        d["codes"] = ",".join(pm.codes)
    if pm.version:
        d["version_label"] = pm.version
    if pm.doc_type:
        d["doc_type"] = pm.doc_type
    return d or None
