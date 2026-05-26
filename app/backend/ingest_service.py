"""Shared batch ingestion service (BIS-001 through BIS-019).

Owns the three-phase pipeline:
  Phase 1: Pre-ingest edge plan construction
  Phase 2: Per-file sequential ingestion
  Phase 3: Post-ingest edge resolution and execution

Callers (MCP tool, FastAPI router) provide delivery glue -- JSON
serialization or SSE streaming -- via optional progress callbacks.
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
        on_file_start: OnFileStart | None = None,
        on_file_done: OnFileDone | None = None,
        on_file_error: OnFileError | None = None,
    ) -> IngestSummary:
        """Execute the three-phase batch ingestion pipeline.

        Hard-coded ``needs_review=True`` (CAS-ADR-021):
        Every per-file ``IngestRequest`` issued by this service sets
        ``needs_review=True`` unconditionally, regardless of caller
        intent. Every document this service ingests therefore lands
        with ``metadata_confirmed=False`` and is added to the
        metadata-review queue — the opposite of
        ``IngestionService.ingest``'s caller-authoritative default.
        This is intentional under CAS-ADR-021: the CAS bulk-ingest
        workflow surfaces inferred values for human confirmation, so
        it explicitly opts every document into the confirmation
        queue. See the inline comment at the ``IngestRequest``
        construction site for the line where the flip is stamped.

        Filename parsing always runs (consequence of the above):
        Because ``needs_review=True`` is hard-coded, the vault's
        ``FilenameParser`` runs on every file regardless of the
        contents of ``FileDescriptor.parsed_metadata``. It may
        populate ``date``, ``project``, ``codes``, ``version``, and
        ``doc_type`` from the filename when the caller omits those
        keys — the exact fields the parser extracts are
        vault-config-defined under
        ``metadata_extraction.filename_extraction.segment_fields``.
        Callers wanting a no-filename-parser ingest path must call
        ``IngestionService.ingest`` directly with
        ``needs_review=False``.

        Per-file failure isolation (CAS-ADR-029):
        The batch is NOT atomic. Per-file exceptions are caught into
        ``IngestSummary.errors`` as ``{filename, message}`` entries
        (with ``error_count`` advancing in lockstep); the batch
        continues with the remaining files and Phase 3 edge
        execution still runs across whatever did insert. Earlier or
        later items are not rolled back. Mirrors the bulk-tool
        atomicity contract used by ``sage_bulk_*`` operations.

        Predecessor auto-archive on Tier-1 supersedes inference:
        When ``infer_edges=True`` and Phase 3 edge resolution
        creates a Tier-1 ``supersedes`` edge via version-chain
        inference, the target document silently transitions from
        ``active`` to ``archived`` as part of edge execution — no
        explicit lifecycle-transition call is required and none
        surfaces in the summary. Lifecycle transition failures
        during this phase are collected as warnings in
        ``IngestSummary.edge_warnings`` only; they do not raise and
        do not appear in ``IngestSummary.errors``.

        Tier-1 provenance-gate downgrade:
        Tier-1 ``supersedes`` adds are gated on provenance: if any
        existing edge in a candidate version chain has a
        non-``version_chain`` rationale (e.g., a human-curated
        ``manual_review`` edge in the same chain), the entire
        group's Tier-1 adds are silently downgraded to Tier-2
        (deposited in the staging-edge table for review rather than
        landing as production edges; the predecessor auto-archive
        above does NOT fire on a downgraded group). The
        production-vs-staging outcome of a batch is therefore
        rule-dependent on the vault's prior edge graph, not
        deterministic from the input ``FileDescriptor`` list alone.

        Args:
            files: Neutral file descriptors to ingest.
            vault_services: SAGE service bundle for the target vault.
            infer_edges: When True, run two-phase edge inference.
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
                    # confirmation, so it explicitly opts the document
                    # into the metadata-review queue.
                    needs_review=True,
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

        The app layer's job is the ``FileDescriptor`` -> ``InferenceItem``
        adapter; the vault-querying and rule application moved to the
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
