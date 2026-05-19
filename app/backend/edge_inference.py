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

from sage.models.enums import EdgeType, RationaleKind
from sage.models.schemas import Edge, LinkRequest, StagingEdge
from sage.services.filename_parser import ParsedMetadata, normalize_version
from sage.services.graph_ops import GraphOpsService
from sage.storage.graph_store import GraphStore

logger = logging.getLogger(__name__)

# Doc types classified as workflow artifacts (EI-021)
WORKFLOW_DOC_TYPES = frozenset(
    {
        "checklist",
        "work_plan",
        "session_context",
        "template",
    }
)

# Rationale prefixes mark edges authored by an auto-inference rule.
# Promoted to a typed, indexed `rationale_kind` column on the edges table
# in T-0080; the prefix is still emitted in evidence/rationale text so
# that staging edges (no rationale_kind column) can be classified at
# promotion time.
VERSION_CHAIN_RATIONALE_PREFIX = "[version_chain]"
FILENAME_CODE_MATCH_RATIONALE_PREFIX = "[filename_code_match]"


def _is_version_chain_edge(edge: Edge) -> bool:
    """T-0080: provenance gate check reads the typed column, not the
    rationale-text prefix.
    """
    return edge.rationale_kind == RationaleKind.VERSION_CHAIN


# T-0080: map from the inference rule's `method` name (set on each
# PlannedEdge) to the typed RationaleKind for the produced edge.
_METHOD_TO_RATIONALE_KIND: dict[str, RationaleKind] = {
    "version_chain": RationaleKind.VERSION_CHAIN,
    "filename_code_match": RationaleKind.FILENAME_CODE_MATCH,
}


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
class PlannedEdgeRemoval:
    edge_id: str
    source_id: str
    target_id: str
    edge_type: EdgeType
    reason: str


@dataclass
class EdgePlan:
    edges: list[PlannedEdge] = field(default_factory=list)
    removals: list[PlannedEdgeRemoval] = field(default_factory=list)


@dataclass
class EdgeResult:
    edges_created: dict[str, int] = field(default_factory=dict)
    edges_staged: dict[str, int] = field(default_factory=dict)
    edges_removed: int = 0
    edges_dropped: int = 0
    warnings: list[dict[str, str]] = field(default_factory=list)


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
        existing_supersedes_edges: list[Edge] | None = None,
    ) -> EdgePlan:
        """Phase 1: build edge plan from full manifest + existing vault state.

        Args:
            scan_items: New files from the scan (ref = file_path).
            existing_items: Existing vault documents that may participate in
                a chain. Includes both active heads and archived predecessors
                reachable via supersedes edges from any active head whose
                chain identity matches a new arrival.
            existing_supersedes_edges: Existing supersedes edges between
                members of any chain identity that has a new arrival. Used
                to diff existing vs desired chain and emit removals.
        """
        all_items = scan_items + existing_items
        plan = EdgePlan()

        chain_edges, chain_removals = self._chain_repair_plan(
            all_items, existing_supersedes_edges or []
        )
        plan.edges.extend(chain_edges)
        plan.removals.extend(chain_removals)
        plan.edges.extend(self._filename_code_match(all_items))

        return plan

    def _chain_repair_plan(
        self,
        items: list[InferenceItem],
        existing_edges: list[Edge],
    ) -> tuple[list[PlannedEdge], list[PlannedEdgeRemoval]]:
        """Version chain inference with chain repair (Tier 1, supersedes).

        Groups items by title, project, and doc_type. For each group with
        at least one new arrival and one versioned item, computes the
        desired linear supersedes chain (sorted by normalized version),
        diffs against existing supersedes edges between group members,
        and emits adds + removals.

        Provenance gate: if any edge in a group's removal set has a
        non-version_chain rationale, the entire group's repair is downgraded
        to Tier 2 staging and no removals are emitted -- a human reviews
        before any hand-curated edge is replaced.

        A versionless file in a group with versioned files is treated as
        the original (sorts before all versions). Items with different
        doc_types do not chain even if they share a title; items with null
        doc_type group only with other null-doc_type items.
        """
        # Group by (title, project, doc_type) -- chain identity
        groups: dict[tuple[str, str | None, str | None], list[InferenceItem]] = {}
        for item in items:
            key = (item.parsed.title.lower(), item.parsed.project, item.parsed.doc_type)
            groups.setdefault(key, []).append(item)

        # Index existing edges by (source_id, target_id) for fast diff
        existing_by_pair: dict[tuple[str, str], Edge] = {
            (e.source_id, e.target_id): e for e in existing_edges
        }

        added: list[PlannedEdge] = []
        removed: list[PlannedEdgeRemoval] = []

        for _key, group in groups.items():
            if not any(not it.is_existing for it in group):
                continue  # nothing new -- skip
            versioned = [it for it in group if it.parsed.version is not None]
            if not versioned:
                continue  # all versionless
            if len(group) < 2:
                continue

            sorted_group = sorted(
                group,
                key=lambda it: (
                    normalize_version(it.parsed.version)
                    if it.parsed.version is not None
                    else (0, 0, 0)
                ),
            )

            # Desired chain: each version supersedes its immediate predecessor.
            desired_pairs: set[tuple[str, str]] = set()
            desired_edge_specs: list[tuple[InferenceItem, InferenceItem]] = []
            for i in range(1, len(sorted_group)):
                newer = sorted_group[i]
                older = sorted_group[i - 1]
                desired_pairs.add((newer.ref, older.ref))
                desired_edge_specs.append((newer, older))

            # Existing edges restricted to this group's members
            group_member_ids = {it.ref for it in group if it.is_existing}
            group_existing_edges = [
                e
                for e in existing_edges
                if e.source_id in group_member_ids and e.target_id in group_member_ids
            ]

            # Diff
            group_removals: list[PlannedEdgeRemoval] = []
            for e in group_existing_edges:
                if (e.source_id, e.target_id) not in desired_pairs:
                    group_removals.append(
                        PlannedEdgeRemoval(
                            edge_id=e.id,
                            source_id=e.source_id,
                            target_id=e.target_id,
                            edge_type=e.edge_type,
                            reason=(
                                f"chain_repair: {e.source_id} -> {e.target_id} "
                                "no longer in desired chain"
                            ),
                        )
                    )

            group_adds: list[PlannedEdge] = []
            for newer, older in desired_edge_specs:
                if (newer.ref, older.ref) in existing_by_pair:
                    continue  # already exists in correct position
                if newer.is_existing and older.is_existing:
                    # Both existing but no edge between them -- emit add
                    pass
                newer_label = newer.parsed.version or "(original)"
                older_label = older.parsed.version or "(original)"
                group_adds.append(
                    PlannedEdge(
                        source_ref=newer.ref,
                        target_ref=older.ref,
                        edge_type=EdgeType.SUPERSEDES,
                        tier=1,
                        method="version_chain",
                        evidence=(
                            f"{VERSION_CHAIN_RATIONALE_PREFIX} "
                            f"{newer_label} supersedes "
                            f"{older_label} "
                            f"(title: {newer.parsed.title})"
                        ),
                    )
                )

            # Provenance gate: if any to-be-removed edge isn't version_chain,
            # downgrade the entire group's plan (adds + removals) to Tier 2.
            if group_removals and not all(
                _is_version_chain_edge(existing_by_pair[(r.source_id, r.target_id)])
                for r in group_removals
            ):
                for add in group_adds:
                    add.tier = 2
                # Drop removals -- staging only proposes the new edges; the
                # human reviewer decides what to do with the conflicting
                # existing edges.
                added.extend(group_adds)
            else:
                added.extend(group_adds)
                removed.extend(group_removals)

        return added, removed

    def _filename_code_match(self, items: list[InferenceItem]) -> list[PlannedEdge]:
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

        # For each workflow item, find content items sharing a code.
        # Only plan edges where at least one side is new.
        seen: set[tuple[str, str]] = set()
        for wf_item in workflow_items:
            for code in wf_item.parsed.codes:
                for ct_item in code_to_content.get(code.upper(), []):
                    if wf_item.is_existing and ct_item.is_existing:
                        continue
                    pair = (wf_item.ref, ct_item.ref)
                    if pair in seen:
                        continue
                    seen.add(pair)
                    edges.append(
                        PlannedEdge(
                            source_ref=wf_item.ref,
                            target_ref=ct_item.ref,
                            edge_type=EdgeType.COVERS,
                            tier=2,
                            method="filename_code_match",
                            evidence=(
                                f"{FILENAME_CODE_MATCH_RATIONALE_PREFIX} "
                                f"Workflow '{wf_item.parsed.title}' shares "
                                f"code {code} with '{ct_item.parsed.title}'"
                            ),
                        )
                    )

        return edges


async def resolve_and_execute(
    edge_plan: EdgePlan,
    path_to_id: dict[str, str],
    graph_store: GraphStore,
    graph_ops_service: GraphOpsService,
) -> EdgeResult:
    """Phase 2: resolve file paths to document IDs and execute edges.

    Removals run first so chain-repair leaves no transient invalid state
    visible to the lifecycle side effects fired during the add pass.

    Args:
        edge_plan: The pre-ingest edge plan.
        path_to_id: Mapping from file_path to SAGE document_id for
            successfully ingested files.
        graph_store: For staging edge insertion.
        graph_ops_service: For Tier 1 link() and unlink() calls.
    """
    result = EdgeResult()

    # Removals first (chain-repair: drop incorrect predecessor edges before
    # adding correct ones).
    for removal in edge_plan.removals:
        try:
            await graph_ops_service.unlink(removal.edge_id)
            result.edges_removed += 1
        except Exception as exc:
            logger.exception(
                "Failed to remove edge %s (%s -> %s)",
                removal.edge_id,
                removal.source_id,
                removal.target_id,
            )
            result.warnings.append(
                {
                    "source": removal.source_id,
                    "target": removal.target_id,
                    "edge_type": removal.edge_type.value,
                    "reason": "edge_removal_failed",
                    "detail": str(exc),
                }
            )

    for planned in edge_plan.edges:
        source_id = path_to_id.get(planned.source_ref, planned.source_ref)
        target_id = path_to_id.get(planned.target_ref, planned.target_ref)

        # If either ref is still a file path (not resolved), it failed ingestion
        if source_id == planned.source_ref and "/" in planned.source_ref:
            result.edges_dropped += 1
            result.warnings.append(
                {
                    "source": planned.source_ref,
                    "target": planned.target_ref,
                    "edge_type": planned.edge_type.value,
                    "reason": "ingestion_failed",
                    "detail": f"Source file failed ingestion: {planned.source_ref}",
                }
            )
            continue
        if target_id == planned.target_ref and "/" in planned.target_ref:
            result.edges_dropped += 1
            result.warnings.append(
                {
                    "source": planned.source_ref,
                    "target": planned.target_ref,
                    "edge_type": planned.edge_type.value,
                    "reason": "ingestion_failed",
                    "detail": f"Target file failed ingestion: {planned.target_ref}",
                }
            )
            continue

        # T-0080: every auto-inference method stamps its provenance prefix
        # on the evidence string above; derive the typed discriminator
        # from that prefix so the LinkRequest and StagingEdge land with
        # the correct rationale_kind without re-encoding the mapping.
        rationale_kind = _METHOD_TO_RATIONALE_KIND.get(planned.method, RationaleKind.MANUAL)
        try:
            if planned.tier == 1:
                # T-0079: link_idempotent makes auto-inferred edges
                # idempotent under re-ingest. A duplicate natural-key
                # triple returns the existing edge with created=False;
                # we still count it as edges_created when newly minted,
                # otherwise as edges_kept so the IngestSummary surfaces
                # the no-op path.
                _edge, created = await graph_ops_service.link_idempotent(
                    LinkRequest(
                        source_id=source_id,
                        target_id=target_id,
                        edge_type=planned.edge_type,
                        rationale=planned.evidence,
                        rationale_kind=rationale_kind,
                    )
                )
                key = planned.edge_type.value
                if created:
                    result.edges_created[key] = result.edges_created.get(key, 0) + 1
                else:
                    logger.debug(
                        "Edge %s -> %s (%s) already exists; auto-inference no-op",
                        source_id,
                        target_id,
                        planned.edge_type.value,
                    )
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
                # T-0079: insert_staging_edge returns (edge, created); we
                # discard the result here since the planning layer
                # already accounts for staging edges by their planned
                # status, not by post-write created/skipped state.
                await graph_store.insert_staging_edge(staging, on_conflict="noop")
                key = planned.edge_type.value
                result.edges_staged[key] = result.edges_staged.get(key, 0) + 1
        except Exception as exc:
            logger.exception(
                "Failed to create edge %s -> %s (%s)",
                source_id,
                target_id,
                planned.edge_type.value,
            )
            result.edges_dropped += 1
            result.warnings.append(
                {
                    "source": source_id,
                    "target": target_id,
                    "edge_type": planned.edge_type.value,
                    "reason": "edge_creation_failed",
                    "detail": str(exc),
                }
            )
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
            except Exception as exc:
                logger.warning(
                    "Supersedes edge created but failed to transition target %s to archived",
                    target_id,
                )
                result.warnings.append(
                    {
                        "source": source_id,
                        "target": target_id,
                        "edge_type": planned.edge_type.value,
                        "reason": "lifecycle_transition_failed",
                        "detail": str(exc),
                    }
                )

    return result
