"""Two-phase edge inference engine (EI-013 through EI-030).

Phase 1 (pre-ingest): Builds an edge plan from parsed filename metadata
and existing vault documents.

Phase 2 (post-ingest): Resolves file paths to document IDs and executes
the edge plan (Tier 1 via link(), Tier 2 via staging insertion).

Active inference methods (Phase 1):
  - version_chain: Tier 1, supersedes edges. Linear chain only.
  - filename_code_match: Tier 2, covers edges. Workflow -> content direction.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone

from app.backend.filename_parser import ParsedMetadata, normalize_version
from sage.models.enums import EdgeType
from sage.models.schemas import LinkRequest, StagingEdge
from sage.services.graph_ops import GraphOpsService
from sage.storage.graph_store import GraphStore

logger = logging.getLogger(__name__)

# Doc types classified as workflow artifacts (EI-021)
WORKFLOW_DOC_TYPES = frozenset({
    "checklist", "work_plan", "session_context", "template",
})


@dataclass
class InferenceItem:
    """Unified view of a file (new or existing) for inference."""
    ref: str  # file_path for new files, document_id for existing
    is_existing: bool
    parsed: ParsedMetadata


@dataclass
class PlannedEdge:
    source_ref: str  # file_path or document_id
    target_ref: str
    edge_type: EdgeType
    tier: int
    method: str
    evidence: str


@dataclass
class EdgePlan:
    edges: list[PlannedEdge] = field(default_factory=list)


@dataclass
class EdgeResult:
    edges_created: dict[str, int] = field(default_factory=dict)
    edges_staged: dict[str, int] = field(default_factory=dict)
    edges_dropped: int = 0


def _is_workflow(doc_type: str | None) -> bool:
    return doc_type is not None and doc_type in WORKFLOW_DOC_TYPES


def _is_content(doc_type: str | None) -> bool:
    return doc_type is not None and doc_type not in WORKFLOW_DOC_TYPES


class EdgeInferenceEngine:
    """Builds and executes edge plans from parsed filename metadata."""

    def build_edge_plan(
        self,
        scan_items: list[InferenceItem],
        existing_items: list[InferenceItem],
    ) -> EdgePlan:
        """Phase 1: build edge plan from full manifest + existing vault docs.

        Args:
            scan_items: New files from the scan (ref = file_path).
            existing_items: Existing vault documents (ref = document_id).
        """
        all_items = scan_items + existing_items
        plan = EdgePlan()

        plan.edges.extend(self._version_chain(all_items))
        plan.edges.extend(self._filename_code_match(all_items))

        return plan

    def _version_chain(self, items: list[InferenceItem]) -> list[PlannedEdge]:
        """Version chain inference (Tier 1, supersedes).

        Groups items by title identity, sorts by normalized version,
        creates a linear chain: each version supersedes its immediate
        predecessor. A versionless file in a group with versioned files
        is treated as the original (sorts before all versions).
        """
        edges: list[PlannedEdge] = []

        # Group by (title, project) -- version chain identity
        groups: dict[tuple[str, str | None], list[InferenceItem]] = {}
        for item in items:
            key = (item.parsed.title.lower(), item.parsed.project)
            groups.setdefault(key, []).append(item)

        for _key, group in groups.items():
            # Need at least one versioned item to form a chain
            versioned = [it for it in group if it.parsed.version is not None]
            if not versioned:
                continue  # all versionless, nothing to chain

            if len(group) < 2:
                continue  # EI-017: need at least 2 items

            # Sort by normalized version ascending.
            # None version -> (0,0,0), sorts before any real version.
            sorted_group = sorted(
                group,
                key=lambda it: normalize_version(it.parsed.version)
                if it.parsed.version is not None
                else (0, 0, 0),
            )

            # Linear chain: each version supersedes its immediate predecessor
            for i in range(1, len(sorted_group)):
                newer = sorted_group[i]
                older = sorted_group[i - 1]
                newer_label = newer.parsed.version or "(original)"
                older_label = older.parsed.version or "(original)"
                edges.append(PlannedEdge(
                    source_ref=newer.ref,
                    target_ref=older.ref,
                    edge_type=EdgeType.SUPERSEDES,
                    tier=1,
                    method="version_chain",
                    evidence=(
                        f"{newer_label} supersedes "
                        f"{older_label} "
                        f"(title: {newer.parsed.title})"
                    ),
                ))

        return edges

    def _filename_code_match(
        self, items: list[InferenceItem]
    ) -> list[PlannedEdge]:
        """Filename code match inference (Tier 2, covers).

        Fires between workflow artifacts and content artifacts sharing
        at least one code. Direction: workflow -> content. Workflow-to-
        workflow and content-to-content pairs produce no edges.
        """
        edges: list[PlannedEdge] = []

        workflow_items = [it for it in items if _is_workflow(it.parsed.doc_type)]
        content_items = [it for it in items if _is_content(it.parsed.doc_type)]

        # Build code -> content items index
        code_to_content: dict[str, list[InferenceItem]] = {}
        for item in content_items:
            for code in item.parsed.codes:
                code_to_content.setdefault(code.upper(), []).append(item)

        # For each workflow item, find content items sharing a code
        seen: set[tuple[str, str]] = set()
        for wf_item in workflow_items:
            for code in wf_item.parsed.codes:
                for ct_item in code_to_content.get(code.upper(), []):
                    pair = (wf_item.ref, ct_item.ref)
                    if pair in seen:
                        continue
                    seen.add(pair)
                    edges.append(PlannedEdge(
                        source_ref=wf_item.ref,
                        target_ref=ct_item.ref,
                        edge_type=EdgeType.COVERS,
                        tier=2,
                        method="filename_code_match",
                        evidence=(
                            f"Workflow '{wf_item.parsed.title}' shares code "
                            f"{code} with '{ct_item.parsed.title}'"
                        ),
                    ))

        return edges


async def resolve_and_execute(
    edge_plan: EdgePlan,
    path_to_id: dict[str, str],
    graph_store: GraphStore,
    graph_ops_service: GraphOpsService,
) -> EdgeResult:
    """Phase 2: resolve file paths to document IDs and execute edges.

    Args:
        edge_plan: The pre-ingest edge plan.
        path_to_id: Mapping from file_path to SAGE document_id for
            successfully ingested files.
        graph_store: For staging edge insertion.
        graph_ops_service: For Tier 1 link() calls.
    """
    result = EdgeResult()

    for planned in edge_plan.edges:
        source_id = path_to_id.get(planned.source_ref, planned.source_ref)
        target_id = path_to_id.get(planned.target_ref, planned.target_ref)

        # If either ref is still a file path (not resolved), it failed ingestion
        if source_id == planned.source_ref and "/" in planned.source_ref:
            result.edges_dropped += 1
            continue
        if target_id == planned.target_ref and "/" in planned.target_ref:
            result.edges_dropped += 1
            continue

        try:
            if planned.tier == 1:
                await graph_ops_service.link(LinkRequest(
                    source_id=source_id,
                    target_id=target_id,
                    edge_type=planned.edge_type,
                    rationale=planned.evidence,
                ))
                key = planned.edge_type.value
                result.edges_created[key] = result.edges_created.get(key, 0) + 1
            else:
                staging = StagingEdge(
                    id=str(uuid.uuid4()),
                    source_id=source_id,
                    target_id=target_id,
                    edge_type=planned.edge_type,
                    inference_evidence=planned.evidence,
                    confidence_tier=planned.tier,
                    created_at=datetime.now(timezone.utc),
                )
                await graph_store.insert_staging_edge(staging)
                key = planned.edge_type.value
                result.edges_staged[key] = result.edges_staged.get(key, 0) + 1
        except Exception:
            logger.exception(
                "Failed to create edge %s -> %s (%s)",
                source_id, target_id, planned.edge_type.value,
            )
            result.edges_dropped += 1
            continue

        # Lifecycle side effect: transition target to "archived"
        if planned.tier == 1 and planned.edge_type == EdgeType.SUPERSEDES:
            try:
                target_doc = await graph_store.get_document(target_id)
                if target_doc and target_doc.lifecycle_status == "active":
                    now = datetime.now(timezone.utc).isoformat()
                    await graph_store.update_document(
                        target_id,
                        {"lifecycle_status": "archived", "updated_at": now},
                    )
            except Exception:
                logger.warning(
                    "Supersedes edge created but failed to transition "
                    "target %s to archived",
                    target_id,
                )

    return result
