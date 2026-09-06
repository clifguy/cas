"""Batch edge inference service.

Owns the vault-declared inference rules that operate on multi-document
batch context. Per principle 5 ("SAGE owns vault-declared behavior"), the
inference rules themselves and their Phase 1/Phase 2 machinery belong in
SAGE; the app layer's ``BatchIngestService`` retains only the file-scan
and per-document ingest loop, then delegates to this module for the
inference pass.

Active inference methods:
  - version_chain: Tier 1, supersedes edges. Linear chain only.
  - filename_code_match: Tier 2, covers edges. Workflow -> content direction.

Public entry points:

* ``EdgeInferenceEngine.build_edge_plan`` -- pure planner; given pre-built
  ``InferenceItem`` lists for the scan batch and the existing vault, plus
  existing supersedes edges, returns an ``EdgePlan``. Side-effect free.
* ``plan_batch_edges`` -- batch-context entry point. Takes the scan-batch
  ``InferenceItem``s plus the ``SAGEServices`` container, performs the
  vault-side queries (active docs, chain-repair candidates, existing
  supersedes edges) needed to anchor inference, and returns the planner's
  ``EdgePlan``. This is what ``BatchIngestService._build_edge_plan``
  invokes during Phase 1.
* ``resolve_and_execute`` -- Phase 2 executor. Resolves file paths to
  document IDs and writes Tier 1 edges via ``GraphOpsService._create_edge``
  (or, for a supersession carrying a lifecycle transition,
  ``GraphStore.supersede_atomic`` under the per-predecessor lock) and
  Tier 2 edges via ``GraphStore.insert_staging_edge``.

The provenance gate inside ``_chain_repair_plan`` (CAS-ADR-019) is the
canonical implementation of that decision; relocations must preserve it
verbatim.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from sage.adapters.interfaces import ContentStore, GraphStore, NaturalKeyConflict
from sage.api.errors import SupersedeTargetNotActiveError
from sage.config import render_state_set
from sage.models.enums import EdgeType, RationaleKind
from sage.models.schemas import Edge, EdgeWarning, LinkRequest, StagingEdge
from sage.services.filename_parser import ParsedMetadata, normalize_version
from sage.services.graph_ops import GraphOpsService
from sage.storage.locks import DocumentLockManager

if TYPE_CHECKING:
    from sage.mcp_init import SAGEServices
    from sage.services.lifecycle import LifecycleService

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
# in; the prefix is still emitted in evidence/rationale text so
# that staging edges (no rationale_kind column) can be classified at
# promotion time.
VERSION_CHAIN_RATIONALE_PREFIX = "[version_chain]"
FILENAME_CODE_MATCH_RATIONALE_PREFIX = "[filename_code_match]"


def _is_version_chain_edge(edge: Edge) -> bool:
    """Provenance gate check reads the typed column, not the
    rationale-text prefix.
    """
    return edge.rationale_kind == RationaleKind.VERSION_CHAIN


# Map from the inference rule's `method` name (set on each
# PlannedEdge) to the typed RationaleKind for the produced edge.
_METHOD_TO_RATIONALE_KIND: dict[str, RationaleKind] = {
    "version_chain": RationaleKind.VERSION_CHAIN,
    "filename_code_match": RationaleKind.FILENAME_CODE_MATCH,
    "identifier_mention": RationaleKind.REFERENCES_MENTION,
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
    # Anchors for transitive_both edges (references) emitted by
    # identifier_mention. Resolved doc IDs; resolve_and_execute forwards
    # them into LinkRequest. None for edges whose policy is `none` (e.g.,
    # SUPERSEDES via version_chain).
    source_valid_from_version: str | None = None
    target_valid_from_version: str | None = None
    # Chain-repair unit this edge belongs to. A repair removes edges and
    # adds the ones that replace them; both halves carry the same key so
    # the executor can settle them together rather than committing the
    # removal before it knows whether the replacement can be created.
    # None for edges that replace nothing (code match, identifier mention,
    # and version-chain adds in a group that emitted no removals).
    repair_group: str | None = None


@dataclass
class PlannedEdgeRemoval:
    edge_id: str
    source_id: str
    target_id: str
    edge_type: EdgeType
    reason: str
    # See PlannedEdge.repair_group.
    repair_group: str | None = None


@dataclass
class EdgePlan:
    edges: list[PlannedEdge] = field(default_factory=list)
    removals: list[PlannedEdgeRemoval] = field(default_factory=list)


# Dispositions a planned edge can carry out of the settlement pass.
_PROCEED = "proceed"
_REFUSED = "refused"


@dataclass
class _SettledEdge:
    """A planned edge with its ids resolved and its disposition decided.

    Settlement is separated from execution because chain repair's removals
    must not commit while a replacement add is already known to be
    refused; deciding every edge first is what lets the removal pass
    consult that outcome. Settling an add does not make it creatable --
    the write can still fail -- so this ordering closes the refusal case
    only.
    """

    planned: PlannedEdge
    source_id: str
    target_id: str
    disposition: str
    # The state the target should land in, when the edge is a Tier-1
    # supersedes whose target's state permits the transition. None both for
    # edges that are not supersedes and for a target already holding a
    # landing state, neither of which needs a write.
    supersede_to_state: str | None = None
    # Emitted only if the edge is refused; carries the reason and detail.
    warning: EdgeWarning | None = None


@dataclass
class EdgeResult:
    edges_created: dict[str, int] = field(default_factory=dict)
    edges_staged: dict[str, int] = field(default_factory=dict)
    edges_removed: int = 0
    edges_dropped: int = 0
    warnings: list[EdgeWarning] = field(default_factory=list)


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
            # Stable identity for this chain's repair unit, carried on both
            # the removals and the adds that replace them.
            group_key = "|".join("" if part is None else str(part) for part in _key)
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
                            repair_group=group_key,
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
                        repair_group=group_key,
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


async def plan_batch_edges(
    *,
    scan_items: list[InferenceItem],
    vault_services: SAGEServices,
) -> EdgePlan:
    """Phase 1 batch-context entry point.

    Given pre-built ``InferenceItem``s for the scan batch and the vault's
    ``SAGEServices`` container, performs the vault-side queries needed to
    anchor inference and returns an ``EdgePlan``:

      - Pass A: active documents (always included regardless of chain
        identity). Bounded by active doc count.
      - Pass B: chain-repair candidates -- one targeted query per unique
        (project, doc_type) pair drawn from the scan batch's versioned
        items. Bounded by the cardinality of that pair.

    Existing supersedes edges between any chain-scope member are then
    fetched so the engine can diff existing vs desired chain.

    This function relocates the vault-querying body of
    ``BatchIngestService._build_edge_plan`` so the principle-5 boundary
    runs through the FileDescriptor -> InferenceItem conversion in the
    app layer and everything below it in SAGE.
    """
    engine = EdgeInferenceEngine()

    # Chain identities present in this batch (only versioned items
    # participate in chain repair).
    scan_chain_keys: set[tuple[str, str | None, str | None]] = {
        (it.parsed.title.lower(), it.parsed.project, it.parsed.doc_type)
        for it in scan_items
        if it.parsed.version is not None
    }

    # Existing-doc fetch: two SQL-pushed passes feed the
    # union of "always include" + "chain-repair candidates" instead
    # of pulling the entire vault into Python.
    #
    # The "active" here scopes which documents are inference candidates at
    # all, across every edge type -- it is a query filter, not a lifecycle
    # precondition, and it is deliberately not derived from the transition
    # table. Whether a particular supersession is legal is settled against
    # the table in `resolve_and_execute`, on the target, per planned edge.
    active_docs, _ = await vault_services.graph_store.query_documents(
        filters={"lifecycle_status": "active"},
        limit=10_000_000,
        offset=0,
        default_exclude_failed=False,
    )

    chain_dim_pairs: set[tuple[str | None, str | None]] = {
        (key[1], key[2]) for key in scan_chain_keys
    }
    candidate_docs: list = []
    seen_ids: set[str] = {d.id for d in active_docs}
    for project, doc_type in chain_dim_pairs:
        filters: dict[str, object] = {}
        if project is not None:
            filters["project"] = project
        if doc_type is not None:
            filters["doc_type"] = doc_type
        # When both are None, fall back to an unfiltered scan for
        # this slice. Rare (versioned arrival with neither project
        # nor doc_type) and matches the original worst case.
        docs, _ = await vault_services.graph_store.query_documents(
            filters=filters or None,
            limit=10_000_000,
            offset=0,
            default_exclude_failed=False,
        )
        for doc in docs:
            if doc.id in seen_ids:
                continue
            seen_ids.add(doc.id)
            candidate_docs.append(doc)

    existing_items: list[InferenceItem] = []
    existing_chain_doc_ids: list[str] = []
    for doc in list(active_docs) + candidate_docs:
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
    existing_supersedes_edges: list[Edge] = []
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


async def resolve_and_execute(
    edge_plan: EdgePlan,
    path_to_id: dict[str, str],
    graph_store: GraphStore,
    graph_ops_service: GraphOpsService,
    lifecycle_service: LifecycleService,
    lock_manager: DocumentLockManager,
    content_store: ContentStore | None = None,
    *,
    path_to_declared: dict[str, str] | None = None,
) -> EdgeResult:
    """Phase 2: resolve file paths to document IDs and execute edges.

    A Tier-1 ``supersedes`` add carries a lifecycle side effect on its
    target. Which effect applies is settled against the vault's transition
    table before anything is written, in three cases:

    * the target's state permits ``supersede`` -- the edge is created and
      the target moves to the state the table names, committed as one
      database transaction under the same per-predecessor lock every
      other supersede surface takes (CAS-ADR-038), against a fresh
      in-lock read of the target;
    * the target already holds a state a supersession lands in -- the edge
      is created and nothing is written, the chain-repair case where an
      earlier supersession already moved it;
    * neither -- no edge is created, ``edges_dropped`` advances, and a
      ``supersede_target_not_transitionable`` warning names the observed
      state and the permitted ones. A target that does not resolve, or
      whose read fails, is refused the same way under
      ``supersede_target_missing`` and ``supersede_target_read_failed``;
      the three are distinct because only the first is a statement about
      a document's lifecycle state.

    The atomic commit closes both halves of the first case at once: a
    failed commit leaves neither the edge nor the transition behind
    (reported as ``edge_creation_failed``), and a state change racing the
    settlement read is caught by the in-lock re-validation, which refuses
    the edge rather than forking a chain another successor just claimed.
    A commit that finds the edge already present -- the state a
    pre-transactional failure stranded, or a re-ingest of an existing
    pair -- converges the transition alone; only that single-row
    convergence write can still fail with the edge standing, reported as
    ``lifecycle_transition_failed``. A successful transition is followed
    by the same chunk-store lifecycle sync the explicit lifecycle path
    performs, so the target's chunks land in the same state the document
    does; the sync is best-effort, and a failure warns as
    ``chunk_lifecycle_sync_failed`` with the document and chunk stores left
    disagreeing until the next reprojection.

    Because the settlement runs first, it also constrains the removal pass.
    Chain repair emits removals and the adds that replace them; both carry
    the same ``repair_group``. If any Tier-1 ``supersedes`` add in a group
    is refused, that group's removals are withheld and its remaining adds
    are dropped -- committing the removal would sever a chain the refused
    add was meant to re-link, and creating the surviving adds alongside the
    edge that was kept would branch it. The adds are written before the
    removals they replace, and a group whose add fails at write time joins
    the withheld set, so a removal never commits ahead of a replacement
    that does not exist: an interrupted repair leaves extra edges, never a
    severed chain.

    The residuals, stated exactly: the chunk sync is best-effort; the
    already-landed case links against the advisory settlement read with
    no lock; the convergence write is a single unguarded call; and a
    partially landed repair group keeps both its old and its new edges
    until a rerun converges it.

    Args:
        edge_plan: The pre-ingest edge plan.
        path_to_id: Mapping from file_path to SAGE document_id for
            successfully ingested files.
        graph_store: For staging edge insertion and the atomic supersede
            commit.
        graph_ops_service: For Tier 1 link() and unlink() calls.
        lifecycle_service: The vault's lifecycle service. Supplies the
            transition table the settlement reads and builds each
            supersession's writes, so this path validates and commits
            through the same machinery as every other supersede surface.
        lock_manager: The vault's per-document lock manager -- the same
            instance the lifecycle and ingest surfaces lock predecessors
            through, so concurrent supersessions of one target serialize
            across all three.
        content_store: The vault's content store, for syncing a superseded
            target's chunk-level ``lifecycle_status`` after the document
            write. ``None`` skips the sync (legacy wiring).
        path_to_declared: Mapping from file_path to the path the caller
            named, for a caller that put something else in the descriptor's
            file_path -- a delivery that stages the bytes first substitutes
            the staging location. Sibling of ``path_to_id``: that one carries
            what a resolved reference becomes, this one what an unresolved
            one is called back at the caller. Only the warnings for a file
            that never resolved read it, since every other warning already
            names document ids. Omitted where the two spellings coincide,
            which is what a caller that names its own file wants.
    """
    result = EdgeResult()
    transition_table = lifecycle_service.transition_table

    def _declared(ref: str) -> str:
        """Spell a file reference as the caller does.

        A refusal must name the caller's spelling and never a resolved one;
        ``IngestionService.ingest`` is the single home for why. The staged
        path stays the resolution key -- it is what ``path_to_id`` is keyed
        by and what the unresolved-reference test below reads -- so the
        substitution happens at the point of report, not before it.
        """
        return path_to_declared.get(ref, ref) if path_to_declared else ref

    async def _sync_chunk_lifecycle(
        source_id: str, target_id: str, edge_type_value: str, to_state: str
    ) -> None:
        # Mirror the transition onto the target's chunks, as every other
        # lifecycle-writing surface does after its document write:
        # pre-filter pushdown reads lifecycle_status off the chunk row,
        # so without the sync a superseded document's chunks keep
        # answering active-filtered searches. Best-effort: a failure
        # warns and the batch continues.
        if content_store is None:
            return
        try:
            await content_store.update_chunk_metadata(target_id, {"lifecycle_status": to_state})
        except Exception as exc:
            logger.warning(
                "Target %s transitioned to '%s' but chunk-store sync failed: %s",
                target_id,
                to_state,
                exc,
            )
            result.warnings.append(
                EdgeWarning(
                    source=source_id,
                    target=target_id,
                    edge_type=edge_type_value,
                    reason="chunk_lifecycle_sync_failed",
                    detail=str(exc),
                )
            )

    # Loop-invariant: the table does not change across the batch, and the
    # landing-state set sits on the routine chain-repair branch below. A
    # target already in a landing state has nothing left to transition:
    # an earlier supersession put it there, and chain repair reaches that
    # case routinely when it re-points an edge at an already-archived
    # predecessor.
    allowed_states = transition_table.states_allowing("supersede")
    landing_states = transition_table.landing_states("supersede")

    # Pass 1 -- settle every edge without writing anything. A Tier-1
    # supersedes whose target cannot be superseded has to be known before
    # the removal pass runs, so that chain repair does not commit a removal
    # whose replacement has already been refused.
    settled: list[_SettledEdge] = []
    for planned in edge_plan.edges:
        source_id = path_to_id.get(planned.source_ref, planned.source_ref)
        target_id = path_to_id.get(planned.target_ref, planned.target_ref)

        # If either ref is still a file path (not resolved), it failed ingestion
        if source_id == planned.source_ref and "/" in planned.source_ref:
            settled.append(
                _SettledEdge(
                    planned,
                    source_id,
                    target_id,
                    _REFUSED,
                    warning=EdgeWarning(
                        source=_declared(planned.source_ref),
                        target=_declared(planned.target_ref),
                        edge_type=planned.edge_type.value,
                        reason="ingestion_failed",
                        detail=f"Source file failed ingestion: {_declared(planned.source_ref)}",
                    ),
                )
            )
            continue
        if target_id == planned.target_ref and "/" in planned.target_ref:
            settled.append(
                _SettledEdge(
                    planned,
                    source_id,
                    target_id,
                    _REFUSED,
                    warning=EdgeWarning(
                        source=_declared(planned.source_ref),
                        target=_declared(planned.target_ref),
                        edge_type=planned.edge_type.value,
                        reason="ingestion_failed",
                        detail=f"Target file failed ingestion: {_declared(planned.target_ref)}",
                    ),
                )
            )
            continue

        if not (planned.tier == 1 and planned.edge_type == EdgeType.SUPERSEDES):
            settled.append(_SettledEdge(planned, source_id, target_id, _PROCEED))
            continue

        try:
            target_doc = await graph_store.get_document(target_id)
        except Exception as exc:
            logger.exception(
                "Failed to read supersedes target %s; edge not created",
                target_id,
            )
            settled.append(
                _SettledEdge(
                    planned,
                    source_id,
                    target_id,
                    _REFUSED,
                    warning=EdgeWarning(
                        source=source_id,
                        target=target_id,
                        edge_type=planned.edge_type.value,
                        reason="supersede_target_read_failed",
                        detail=str(exc),
                    ),
                )
            )
            continue

        if target_doc is None:
            logger.warning(
                "Supersedes edge %s -> %s not created: target document not found",
                source_id,
                target_id,
            )
            settled.append(
                _SettledEdge(
                    planned,
                    source_id,
                    target_id,
                    _REFUSED,
                    warning=EdgeWarning(
                        source=source_id,
                        target=target_id,
                        edge_type=planned.edge_type.value,
                        reason="supersede_target_missing",
                        detail=f"Cannot supersede document {target_id}: no such document",
                    ),
                )
            )
            continue

        current_state = target_doc.lifecycle_status
        transition = transition_table.validate_transition(current_state, "supersede")
        if transition is not None:
            settled.append(
                _SettledEdge(
                    planned, source_id, target_id, _PROCEED, supersede_to_state=transition[0]
                )
            )
        elif current_state in landing_states:
            # Already where a supersession leaves a document. The edge is
            # sound and there is nothing to write -- the ordinary
            # chain-repair case, not an anomaly, so it is not warned.
            logger.debug(
                "Supersedes target %s already in state '%s'; no transition needed",
                target_id,
                current_state,
            )
            settled.append(_SettledEdge(planned, source_id, target_id, _PROCEED))
        else:
            logger.warning(
                "Supersedes edge %s -> %s not created: target state '%s' does not "
                "permit supersede (permitted from: %s)",
                source_id,
                target_id,
                current_state,
                render_state_set(allowed_states),
            )
            settled.append(
                _SettledEdge(
                    planned,
                    source_id,
                    target_id,
                    _REFUSED,
                    warning=EdgeWarning(
                        source=source_id,
                        target=target_id,
                        edge_type=planned.edge_type.value,
                        reason="supersede_target_not_transitionable",
                        # The error type every other supersede surface raises
                        # for this condition formats the detail, so the
                        # precondition reads identically wherever it is
                        # reported.
                        detail=SupersedeTargetNotActiveError(
                            target_id, current_state, allowed_states
                        ).message,
                    ),
                )
            )

    # Pass 2 -- a repair group with a refused Tier-1 supersedes add is
    # withheld whole. Only groups that actually carry removals: elsewhere a
    # refused add is simply dropped, which takes nothing away.
    groups_with_removals = {r.repair_group for r in edge_plan.removals if r.repair_group}
    withheld_groups = {
        s.planned.repair_group
        for s in settled
        if s.disposition is _REFUSED
        and s.planned.repair_group in groups_with_removals
        and s.planned.tier == 1
        and s.planned.edge_type == EdgeType.SUPERSEDES
    }

    # Pass 3 -- adds, written before the removals they replace. A repair
    # group whose replacement fails at write time joins the withheld set
    # below, so the removal pass never commits a removal whose
    # replacement does not exist; an interruption between the passes
    # leaves extra edges, never a severed chain.
    for entry in settled:
        planned = entry.planned
        source_id = entry.source_id
        target_id = entry.target_id

        if entry.disposition is _REFUSED:
            result.edges_dropped += 1
            if entry.warning is not None:
                result.warnings.append(entry.warning)
            continue

        if planned.repair_group in withheld_groups:
            # Sound on its own, but its repair group was withheld; creating
            # it alongside the edge that was kept would branch the chain.
            result.edges_dropped += 1
            continue

        # Every auto-inference method stamps its provenance prefix
        # on the evidence string above; derive the typed discriminator
        # from that prefix so the LinkRequest and StagingEdge land with
        # the correct rationale_kind without re-encoding the mapping.
        rationale_kind = _METHOD_TO_RATIONALE_KIND.get(planned.method, RationaleKind.MANUAL)

        if (
            planned.tier == 1
            and planned.edge_type == EdgeType.SUPERSEDES
            and entry.supersede_to_state is not None
        ):
            # A supersession with a transition to write commits both
            # halves in one transaction, under the per-predecessor lock
            # the lifecycle and ingest surfaces also take (CAS-ADR-038),
            # against a fresh read of the target. The settlement read
            # above is advisory -- it exists so the withheld-group
            # computation can run before anything is written -- and the
            # lock plus re-read is what makes the decision authoritative.
            async with lock_manager.lock(target_id):
                try:
                    fresh_target = await graph_store.get_document(target_id)
                except Exception as exc:
                    logger.exception(
                        "Failed to re-read supersedes target %s; edge not created",
                        target_id,
                    )
                    result.edges_dropped += 1
                    result.warnings.append(
                        EdgeWarning(
                            source=source_id,
                            target=target_id,
                            edge_type=planned.edge_type.value,
                            reason="supersede_target_read_failed",
                            detail=str(exc),
                        )
                    )
                    if planned.repair_group in groups_with_removals:
                        withheld_groups.add(planned.repair_group)
                    continue
                if fresh_target is None:
                    result.edges_dropped += 1
                    result.warnings.append(
                        EdgeWarning(
                            source=source_id,
                            target=target_id,
                            edge_type=planned.edge_type.value,
                            reason="supersede_target_missing",
                            detail=f"Cannot supersede document {target_id}: no such document",
                        )
                    )
                    if planned.repair_group in groups_with_removals:
                        withheld_groups.add(planned.repair_group)
                    continue
                try:
                    transition = lifecycle_service.prepare_supersede(
                        fresh_target,
                        source_id,
                        rationale=planned.evidence,
                        rationale_kind=rationale_kind,
                    )
                except SupersedeTargetNotActiveError as exc:
                    # The fresh state no longer permits the transition the
                    # settlement approved: a racer moved the target in
                    # between. That includes a move into a landing state --
                    # accepted at settlement time only because the *plan*
                    # put the target there; reached here it means another
                    # successor just claimed this predecessor, and creating
                    # the edge anyway would fork the chain.
                    logger.warning(
                        "Supersedes edge %s -> %s not created: %s",
                        source_id,
                        target_id,
                        exc.message,
                    )
                    result.edges_dropped += 1
                    result.warnings.append(
                        EdgeWarning(
                            source=source_id,
                            target=target_id,
                            edge_type=planned.edge_type.value,
                            reason="supersede_target_not_transitionable",
                            detail=exc.message,
                        )
                    )
                    if planned.repair_group in groups_with_removals:
                        withheld_groups.add(planned.repair_group)
                    continue

                to_state = transition.predecessor_updates["lifecycle_status"]
                try:
                    await graph_store.supersede_atomic(
                        target_id, transition.predecessor_updates, transition.edge
                    )
                except NaturalKeyConflict:
                    # The edge already exists but the target still needs
                    # the transition -- the state a pre-transactional
                    # failure stranded, or a re-ingest of a pair the
                    # planner could not resolve against existing edges.
                    # Converge the transition alone. No warning and no
                    # counter: the strand was reported when it was
                    # created, and the edge is not newly minted.
                    logger.warning(
                        "Supersedes edge %s -> %s already exists; converging the "
                        "target's outstanding transition to '%s'",
                        source_id,
                        target_id,
                        to_state,
                    )
                    try:
                        await graph_store.update_document(target_id, transition.predecessor_updates)
                    except Exception as exc:
                        logger.warning(
                            "Supersedes edge exists but converging target %s to '%s' failed: %s",
                            target_id,
                            to_state,
                            exc,
                        )
                        result.warnings.append(
                            EdgeWarning(
                                source=source_id,
                                target=target_id,
                                edge_type=planned.edge_type.value,
                                reason="lifecycle_transition_failed",
                                detail=str(exc),
                            )
                        )
                        continue
                    await _sync_chunk_lifecycle(
                        source_id, target_id, planned.edge_type.value, to_state
                    )
                    continue
                except Exception as exc:
                    logger.exception(
                        "Failed to settle supersedes edge %s -> %s",
                        source_id,
                        target_id,
                    )
                    result.edges_dropped += 1
                    result.warnings.append(
                        EdgeWarning(
                            source=source_id,
                            target=target_id,
                            edge_type=planned.edge_type.value,
                            reason="edge_creation_failed",
                            detail=str(exc),
                        )
                    )
                    if planned.repair_group in groups_with_removals:
                        withheld_groups.add(planned.repair_group)
                    continue

                key = planned.edge_type.value
                result.edges_created[key] = result.edges_created.get(key, 0) + 1
                await _sync_chunk_lifecycle(source_id, target_id, planned.edge_type.value, to_state)
            continue

        try:
            if planned.tier == 1:
                # Link_idempotent makes auto-inferred edges
                # idempotent under re-ingest. A duplicate natural-key
                # triple returns the existing edge with created=False;
                # we still count it as edges_created when newly minted,
                # otherwise as edges_kept so the IngestSummary surfaces
                # the no-op path.
                _edge, created = await graph_ops_service._create_edge(
                    LinkRequest(
                        source_id=source_id,
                        target_id=target_id,
                        edge_type=planned.edge_type,
                        source_valid_from_version=planned.source_valid_from_version,
                        target_valid_from_version=planned.target_valid_from_version,
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
                # Insert_staging_edge returns (edge, created); we
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
                EdgeWarning(
                    source=source_id,
                    target=target_id,
                    edge_type=planned.edge_type.value,
                    reason="edge_creation_failed",
                    detail=str(exc),
                )
            )
            if (
                planned.tier == 1
                and planned.edge_type == EdgeType.SUPERSEDES
                and planned.repair_group in groups_with_removals
            ):
                withheld_groups.add(planned.repair_group)
            continue

    # Pass 4 -- removals, after every replacement add has landed. Chain
    # repair drops superseded predecessor edges only once the edges that
    # replace them exist, except where the replacement was refused or
    # failed to land above.
    for removal in edge_plan.removals:
        if removal.repair_group in withheld_groups:
            logger.warning(
                "Chain-repair removal of edge %s (%s -> %s) withheld: a replacement "
                "supersedes edge in the same repair could not be created",
                removal.edge_id,
                removal.source_id,
                removal.target_id,
            )
            result.warnings.append(
                EdgeWarning(
                    source=removal.source_id,
                    target=removal.target_id,
                    edge_type=removal.edge_type.value,
                    reason="chain_repair_withheld",
                    detail=f"Existing edge {removal.source_id} -> {removal.target_id} kept: "
                    "a replacement supersedes edge in the same repair was refused "
                    "or failed to land, and removing this one would leave the "
                    "chain shorter than it was found",
                )
            )
            continue
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
                EdgeWarning(
                    source=removal.source_id,
                    target=removal.target_id,
                    edge_type=removal.edge_type.value,
                    reason="edge_removal_failed",
                    detail=str(exc),
                )
            )

    return result
