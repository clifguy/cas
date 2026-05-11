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

from app.backend.edge_inference import (
    EdgeInferenceEngine,
    EdgePlan,
    InferenceItem,
    resolve_and_execute,
)
from sage.models.enums import SourceType
from sage.models.schemas import IngestRequest
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
    adapter: str
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

        # Phase 2: Per-file ingestion
        path_to_id: dict[str, str] = {}
        for i, fd in enumerate(files):
            filename = Path(fd.file_path).name

            if on_file_start is not None:
                await on_file_start(i, total, filename)

            try:
                metadata_dict = _metadata_dict_from_parsed(fd.parsed_metadata)
                request = IngestRequest(
                    source=fd.file_path,
                    adapter=SourceType(fd.adapter),
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
        """Phase 1: build edge plan from file descriptors + existing vault docs.

        Includes archived predecessors of any chain whose identity matches a
        new arrival, so chain-repair can diff the full historical chain
        against the desired chain.
        """
        engine = EdgeInferenceEngine()

        # Build scan items from file descriptors
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

        # Chain identities present in this batch (only versioned items
        # participate in chain repair).
        scan_chain_keys: set[tuple[str, str | None, str | None]] = {
            (it.parsed.title.lower(), it.parsed.project, it.parsed.doc_type)
            for it in scan_items
            if it.parsed.version is not None
        }

        # Fetch all docs once. Active docs always participate. Archived docs
        # participate only when they share a chain identity with a new
        # arrival (so chain-repair can see the full historical chain).
        existing_items: list[InferenceItem] = []
        existing_chain_doc_ids: list[str] = []
        all_docs = await vault_services.graph_store.list_all_documents()
        for doc in all_docs:
            doc_chain_key = (
                doc.title.lower() if doc.title else "",
                doc.project,
                doc.doc_type,
            )
            in_repair_scope = doc_chain_key in scan_chain_keys
            if doc.lifecycle_status == "active" or in_repair_scope:
                existing_items.append(
                    InferenceItem(
                        ref=doc.id,
                        is_existing=True,
                        parsed=ParsedMetadata(
                            title=doc.title,
                            project=doc.project,
                            codes=doc.tags,
                            version=doc.version_label,
                            doc_type=doc.doc_type,
                        ),
                    )
                )
                if in_repair_scope:
                    existing_chain_doc_ids.append(doc.id)

        # Fetch existing supersedes edges between chain-scope members so the
        # engine can diff existing vs desired chain.
        existing_supersedes_edges = []
        seen_edge_ids: set[str] = set()
        chain_id_set = set(existing_chain_doc_ids)
        for doc_id in existing_chain_doc_ids:
            edges = await vault_services.graph_store.get_edges_by_source(doc_id, "supersedes")
            for e in edges:
                if e.id in seen_edge_ids:
                    continue
                if e.target_id in chain_id_set:
                    existing_supersedes_edges.append(e)
                    seen_edge_ids.add(e.id)

        return engine.build_edge_plan(
            scan_items,
            existing_items,
            existing_supersedes_edges=existing_supersedes_edges,
        )


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
