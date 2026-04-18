"""Graph operations service: link, check_preconditions, traverse.

Covers behavioral tests BH-021, BH-023, BH-031 through BH-037.
"""

import json
import logging
import uuid
from datetime import datetime, timezone

from sage.api.errors import (
    DocumentNotFoundError,
    EdgeAnchorPolicyViolationError,
    EdgeNotFoundError,
    MergedFromValidationError,
    PipelineIncompleteError,
    RetractTargetNotEdgeError,
    SelfReferentialEdgeError,
    TBDPolicyEdgeError,
)
from sage.config import VaultConfig
from sage.models.edge_registry import EdgeTypeRegistry
from sage.models.enums import (
    EdgeType,
    PipelineStatus,
    ResolutionPolicy,
    SourceType,
    TraversalDirection,
)
from sage.models.schemas import (
    ChainEntry,
    ChainRequest,
    ChainResponse,
    DocumentSummary,
    Edge,
    LinkRequest,
    PreconditionCheck,
    PreconditionResult,
    TraversalNode,
    TraverseRequest,
    TraverseResponse,
)
from sage.storage.graph_store import GraphStore

logger = logging.getLogger(__name__)


class _LineageCache:
    """Per-request cache of supersedes-lineage lookups (CAS-ADR-017, Chunk 4).

    Encapsulated so the ADR-deferred process-level cache can slot in later.
    Tracks hit/miss counts for test instrumentation (CR-022).
    """

    def __init__(self, graph_store: GraphStore) -> None:
        self._store = graph_store
        self._cache: dict[str, frozenset[str]] = {}
        self.fetches = 0

    async def get(self, doc_id: str) -> frozenset[str]:
        if doc_id not in self._cache:
            lineage = await self._store.get_supersedes_lineage(doc_id)
            self._cache[doc_id] = frozenset(lineage)
            self.fetches += 1
        return self._cache[doc_id]

# Lifecycle states that satisfy depends_on preconditions (BH-033, BH-034).
# Domain-specific states like 'filed' are excluded (BH-036).
_SATISFYING_STATUSES = frozenset({"active", "completed"})


class GraphOpsService:
    def __init__(
        self,
        graph_store: GraphStore,
        config: VaultConfig,
        edge_type_registry: EdgeTypeRegistry | None = None,
    ) -> None:
        self._store = graph_store
        self._config = config
        self._edge_type_registry = edge_type_registry or EdgeTypeRegistry.default()

    # ------------------------------------------------------------------
    # Link (BH-031, BH-032, CAS-ADR-017)
    # ------------------------------------------------------------------

    async def link(self, request: LinkRequest) -> Edge:
        """Create a typed edge between two documents.

        Applies the CAS-ADR-017 write-time invariant: the effective
        resolution_policy (from the edge-type registry) is frozen onto
        the row, the anchor / retracted_edge_id field shape must match
        the policy, and any chain-scoped anchor must sit in the
        supersedes lineage of its endpoint document.
        """
        # Document existence and self-reference are logically prior to
        # anchor-shape validation: we reject unknown documents and
        # self-loops first so callers get the most fundamental error.
        source = await self._store.get_document(request.source_id)
        if source is None:
            raise DocumentNotFoundError(request.source_id)

        if request.edge_type != EdgeType.RETRACTS:
            if request.target_id is not None and request.source_id == request.target_id:
                raise SelfReferentialEdgeError(request.source_id)
            if request.target_id is not None:
                target = await self._store.get_document(request.target_id)
                if target is None:
                    raise DocumentNotFoundError(request.target_id)

        policy = self._edge_type_registry.policy_for(request.edge_type)

        if policy == ResolutionPolicy.TBD:
            raise TBDPolicyEdgeError(request.edge_type.value)

        self._validate_link_request_shape(request, policy)

        if request.edge_type == EdgeType.RETRACTS:
            existing = await self._store.get_edge(request.retracted_edge_id)
            if existing is None:
                raise RetractTargetNotEdgeError(request.retracted_edge_id)

        if request.edge_type == EdgeType.MERGED_FROM:
            await self._validate_merged_from_chain_positions(request)

        await self._validate_anchor_in_lineage(request, policy)

        # `transitive_source`: target anchor is frozen at derivation,
        # copied from target_id (the derivation-time target).
        if policy == ResolutionPolicy.TRANSITIVE_SOURCE:
            target_anchor = request.target_id
        else:
            target_anchor = request.target_valid_from_version

        edge = Edge(
            id=str(uuid.uuid4()),
            source_id=request.source_id,
            target_id=request.target_id,
            edge_type=request.edge_type,
            resolution_policy=policy,
            source_valid_from_version=request.source_valid_from_version,
            target_valid_from_version=target_anchor,
            valid_until_version=None,
            retracted_edge_id=request.retracted_edge_id,
            created_at=datetime.now(timezone.utc),
            notes=request.notes,
            rationale=request.rationale,
        )

        if request.edge_type == EdgeType.MERGED_FROM:
            # Atomic: insert merged_from AND tombstone predecessor-chain
            # non-policy-none edges with valid_until_version = terminal.
            lineage = await self._store.get_supersedes_lineage(request.target_id)
            tombstone_ids = await self._store.find_tombstone_candidates(list(lineage))
            await self._store.merge_atomic(
                edge, tombstone_ids, request.target_id
            )
            return edge

        await self._store.insert_edge(edge)
        return edge

    async def _validate_merged_from_chain_positions(
        self, request: LinkRequest
    ) -> None:
        """Enforce CR-029..CR-031: chain-first successor, chain-head predecessor.

        source_id (successor) must have no outbound supersedes edge
        (nothing older on its chain); target_id (predecessor) must have
        no inbound supersedes edge (nothing newer supersedes it).
        """
        if await self._store.has_supersedes_predecessor(request.source_id):
            raise MergedFromValidationError(
                "source is not the first version of its chain (has an "
                "outbound supersedes edge)",
                source_id=request.source_id,
                target_id=request.target_id,
            )
        if await self._store.has_supersedes_successor(request.target_id):
            raise MergedFromValidationError(
                "target is not a chain head (has a newer superseding version)",
                source_id=request.source_id,
                target_id=request.target_id,
            )

    def _validate_link_request_shape(
        self, request: LinkRequest, policy: ResolutionPolicy
    ) -> None:
        """Enforce the policy-keyed field-shape invariant.

        Does not verify anchor-in-lineage membership; that check lands
        in Chunk 4 alongside the lineage accessor.
        """
        edge_type = request.edge_type
        offending: list[str] = []

        # retracts: target_id null, retracted_edge_id required,
        # source-side anchor required, target-side anchor null, no until.
        if edge_type == EdgeType.RETRACTS:
            if request.target_id is not None:
                offending.append("target_id")
            if request.retracted_edge_id is None:
                raise EdgeAnchorPolicyViolationError(
                    edge_type.value,
                    policy.value,
                    "retracts edges require retracted_edge_id",
                    ["retracted_edge_id"],
                )
            if request.source_valid_from_version is None:
                raise EdgeAnchorPolicyViolationError(
                    edge_type.value,
                    policy.value,
                    "retracts edges require source_valid_from_version",
                    ["source_valid_from_version"],
                )
            if request.target_valid_from_version is not None:
                offending.append("target_valid_from_version")
            if offending:
                raise EdgeAnchorPolicyViolationError(
                    edge_type.value,
                    policy.value,
                    f"fields must be null on retracts edges: {', '.join(offending)}",
                    offending,
                )
            return

        # Non-retracts edges: target_id required; retracted_edge_id must be null.
        if request.target_id is None:
            raise EdgeAnchorPolicyViolationError(
                edge_type.value,
                policy.value,
                "target_id is required for this edge type",
                ["target_id"],
            )
        if request.retracted_edge_id is not None:
            raise EdgeAnchorPolicyViolationError(
                edge_type.value,
                policy.value,
                "retracted_edge_id may only be set on retracts edges",
                ["retracted_edge_id"],
            )

        if policy == ResolutionPolicy.NONE:
            # supersedes, merged_from: both anchor fields must be null.
            if request.source_valid_from_version is not None:
                offending.append("source_valid_from_version")
            if request.target_valid_from_version is not None:
                offending.append("target_valid_from_version")
            if offending:
                raise EdgeAnchorPolicyViolationError(
                    edge_type.value,
                    policy.value,
                    f"anchor fields must be null for policy 'none': {', '.join(offending)}",
                    offending,
                )
            return

        if policy == ResolutionPolicy.TRANSITIVE_SOURCE:
            if request.source_valid_from_version is None:
                raise EdgeAnchorPolicyViolationError(
                    edge_type.value,
                    policy.value,
                    "source_valid_from_version is required for policy 'transitive_source'",
                    ["source_valid_from_version"],
                )
            if request.target_valid_from_version is not None:
                raise EdgeAnchorPolicyViolationError(
                    edge_type.value,
                    policy.value,
                    (
                        "target_valid_from_version must be null for policy "
                        "'transitive_source' (target anchor is frozen at derivation)"
                    ),
                    ["target_valid_from_version"],
                )
            return

        if policy == ResolutionPolicy.TRANSITIVE_BOTH:
            if request.source_valid_from_version is None:
                offending.append("source_valid_from_version")
            if request.target_valid_from_version is None:
                offending.append("target_valid_from_version")
            if offending:
                raise EdgeAnchorPolicyViolationError(
                    edge_type.value,
                    policy.value,
                    (
                        "both source_valid_from_version and target_valid_from_version "
                        "are required for policy 'transitive_both'"
                    ),
                    offending,
                )
            return

    async def _validate_anchor_in_lineage(
        self, request: LinkRequest, policy: ResolutionPolicy
    ) -> None:
        """Verify chain-scoped anchor(s) are in their endpoint's supersedes lineage.

        Shape has already been validated. No-op for policy `none` and for
        `retracts` (anchor is on a single chain, endpoint is source_id,
        checked here). Missing anchor documents surface as
        EdgeAnchorPolicyViolationError (the anchor must refer to a real
        chain member for lineage semantics to be well-defined).
        """
        if policy == ResolutionPolicy.NONE:
            # supersedes, merged_from, retracts all land here.
            # retracts still carries a source-side anchor: check it on
            # the retracting chain (source_id's lineage).
            if request.edge_type == EdgeType.RETRACTS and (
                request.source_valid_from_version is not None
            ):
                await self._require_anchor_in_lineage(
                    request.edge_type.value,
                    policy.value,
                    "source_valid_from_version",
                    request.source_valid_from_version,
                    request.source_id,
                )
            return

        if request.source_valid_from_version is not None:
            await self._require_anchor_in_lineage(
                request.edge_type.value,
                policy.value,
                "source_valid_from_version",
                request.source_valid_from_version,
                request.source_id,
            )

        if (
            policy == ResolutionPolicy.TRANSITIVE_BOTH
            and request.target_valid_from_version is not None
            and request.target_id is not None
        ):
            await self._require_anchor_in_lineage(
                request.edge_type.value,
                policy.value,
                "target_valid_from_version",
                request.target_valid_from_version,
                request.target_id,
            )

    async def _require_anchor_in_lineage(
        self,
        edge_type: str,
        policy: str,
        field: str,
        anchor_id: str,
        endpoint_id: str,
    ) -> None:
        anchor_doc = await self._store.get_document(anchor_id)
        if anchor_doc is None:
            raise EdgeAnchorPolicyViolationError(
                edge_type,
                policy,
                f"{field}={anchor_id!r} does not reference a known document",
                [field],
            )
        lineage = await self._store.get_supersedes_lineage(endpoint_id)
        if anchor_id not in lineage:
            raise EdgeAnchorPolicyViolationError(
                edge_type,
                policy,
                (
                    f"{field}={anchor_id!r} is not in the supersedes lineage "
                    f"of {endpoint_id!r}"
                ),
                [field],
            )

    # ------------------------------------------------------------------
    # Unlink (delete production edge)
    # ------------------------------------------------------------------

    async def unlink(self, edge_id: str) -> dict:
        """Delete a production edge by ID."""
        edge = await self._store.get_edge(edge_id)
        if edge is None:
            raise EdgeNotFoundError(edge_id)
        await self._store.delete_edge(edge_id)
        return {"deleted": True, "edge_id": edge_id}

    # ------------------------------------------------------------------
    # Check preconditions (BH-023, BH-033 through BH-036)
    # ------------------------------------------------------------------

    async def check_preconditions(self, function_id: str) -> PreconditionResult:
        """Validate all depends_on targets for a function document."""
        function_doc = await self._store.get_document(function_id)
        if function_doc is None:
            raise DocumentNotFoundError(function_id)

        depends_on_edges = await self._store.get_edges_by_source(
            function_id, EdgeType.DEPENDS_ON.value
        )

        checks: list[PreconditionCheck] = []
        for edge in depends_on_edges:
            target = await self._store.get_document(edge.target_id)
            if target is None:
                checks.append(PreconditionCheck(
                    target_id=edge.target_id,
                    required="active or completed",
                    actual="not found",
                    satisfied=False,
                ))
                continue

            # Pipeline failure overrides lifecycle check (BH-023)
            if target.pipeline_status == PipelineStatus.FAILED:
                checks.append(PreconditionCheck(
                    target_id=edge.target_id,
                    required="active or completed",
                    actual="failed (pipeline_incomplete)",
                    satisfied=False,
                ))
                continue

            satisfied = target.lifecycle_status in _SATISFYING_STATUSES
            checks.append(PreconditionCheck(
                target_id=edge.target_id,
                required="active or completed",
                actual=target.lifecycle_status,
                satisfied=satisfied,
            ))

        return PreconditionResult(
            function_id=function_id,
            satisfied=all(c.satisfied for c in checks),
            checks=checks,
        )

    # ------------------------------------------------------------------
    # Traverse (BH-037)
    # ------------------------------------------------------------------

    async def traverse(self, request: TraverseRequest) -> TraverseResponse:
        """Graph walk with deduplication by document.

        Applies CAS-ADR-017 chain-resolution semantics: for edge types
        whose `resolution_policy` is chain-scoped (`transitive_source`,
        `transitive_both`), the seed set expands to include the start
        document's supersedes lineage on chain-scoped sides, and each
        candidate edge is filtered by its anchor's membership in the
        appropriate lineage. Policy `none` and `retracts`/`merged_from`
        resolution behavior land in Chunks 5 and 6.
        """
        start_doc = await self._store.get_document(request.start_id)
        if start_doc is None:
            raise DocumentNotFoundError(request.start_id)

        cache = _LineageCache(self._store)

        raw = await self._collect_raw_with_seeds(request, cache)

        # Per-edge anchor filter (honors stored resolution_policy per CAS-ADR-017).
        filtered: list[dict] = []
        for row in raw:
            if await self._edge_passes_anchor_filter(row, cache):
                filtered.append(row)

        # CAS-ADR-017 Chunk 5: retracts short-circuit. Only chain-resolved
        # edges (transitive_source, transitive_both) are suppressible; the
        # retracts primitive does not veto policy=none edges indiscriminately.
        filtered = await self._apply_retracts(
            filtered, request.start_id, cache
        )

        # CAS-ADR-017 Chunk 6: tombstone suppression. Edges whose
        # `valid_until_version` sits strictly as an ancestor of the query
        # start_id are dropped. Equal-to-start is kept (CR-034: historical
        # query at the merge point still surfaces the edge).
        filtered = await self._apply_tombstones(
            filtered, request.start_id, cache
        )

        # Deduplicate: group by doc_id, pick most recent edge, min depth
        grouped: dict[str, list[dict]] = {}
        for row in filtered:
            grouped.setdefault(row["doc_id"], []).append(row)

        nodes: list[TraversalNode] = []
        for doc_id, rows in grouped.items():
            # Most recent edge by created_at
            best = max(rows, key=lambda r: r["edge_created_at"])
            min_depth = min(r["depth"] for r in rows)

            doc_summary = DocumentSummary(
                id=doc_id,
                title=best["d_title"],
                lifecycle_status=best["d_lifecycle_status"],
                source_type=SourceType(best["d_source_type"]),
                source_path=best.get("d_source_path"),
                version_label=best["d_version_label"],
                project=best["d_project"],
                doc_type=best["d_doc_type"],
                tags=json.loads(best["d_tags"]) if best["d_tags"] else [],
                document_date=(
                    datetime.strptime(best["d_document_date"], "%Y-%m-%d").replace(
                        tzinfo=timezone.utc
                    )
                    if best.get("d_document_date")
                    else None
                ),
                source_modified_at=(
                    datetime.fromisoformat(best["d_source_modified_at"])
                    if best.get("d_source_modified_at")
                    else None
                ),
            )

            edge = Edge(
                id=best["edge_id"],
                source_id=best["source_id"],
                target_id=best["target_id"],
                edge_type=EdgeType(best["edge_type"]),
                created_at=datetime.fromisoformat(best["edge_created_at"]),
                notes=best["notes"],
                rationale=best["rationale"],
            )

            # Per-type edge counts (deduplicated by edge ID to avoid
            # inflation from multi-path traversal at different depths)
            seen_edges: dict[str, set[str]] = {}
            for r in rows:
                et = r["edge_type"]
                seen_edges.setdefault(et, set()).add(r["edge_id"])
            counts = {et: len(ids) for et, ids in seen_edges.items()}

            nodes.append(TraversalNode(
                document=doc_summary,
                edge=edge,
                depth=min_depth,
                edge_counts=counts,
            ))

        return TraverseResponse(start_id=request.start_id, nodes=nodes)

    async def _apply_retracts(
        self,
        rows: list[dict],
        query_start_id: str,
        cache: _LineageCache,
    ) -> list[dict]:
        """Drop rows whose candidate edge has an in-lineage retraction.

        Only rows for chain-resolved policies (transitive_source,
        transitive_both) are candidates for retracts suppression. A
        retracts edge suppresses its target iff the retracts
        `source_valid_from_version` is in the supersedes lineage of
        `query_start_id`. This is one-sided (retracting-chain only):
        queries from the counterpart chain naturally do not share
        lineage with the retracts anchor so are unaffected.
        """
        if not rows:
            return rows

        candidate_ids: list[str] = []
        for row in rows:
            policy = self._effective_policy(row)
            if policy in (
                ResolutionPolicy.TRANSITIVE_SOURCE,
                ResolutionPolicy.TRANSITIVE_BOTH,
            ):
                candidate_ids.append(row["edge_id"])

        if not candidate_ids:
            return rows

        retracts_map = await self._store.get_retracts_for_edges(candidate_ids)
        if not retracts_map:
            return rows

        start_lineage = await cache.get(query_start_id)

        kept: list[dict] = []
        for row in rows:
            retracts = retracts_map.get(row["edge_id"])
            if not retracts:
                kept.append(row)
                continue
            policy = self._effective_policy(row)
            if policy not in (
                ResolutionPolicy.TRANSITIVE_SOURCE,
                ResolutionPolicy.TRANSITIVE_BOTH,
            ):
                kept.append(row)
                continue
            suppressed = any(
                r.source_valid_from_version is not None
                and r.source_valid_from_version in start_lineage
                for r in retracts
            )
            if not suppressed:
                kept.append(row)
        return kept

    async def _apply_tombstones(
        self,
        rows: list[dict],
        query_start_id: str,
        cache: _LineageCache,
    ) -> list[dict]:
        """Drop rows tombstoned by an upstream merged_from.

        An edge is suppressed iff `valid_until_version` is a strict
        ancestor of `query_start_id` on the supersedes chain. The
        equal-to-start case is kept (CR-034): querying at the termination
        version still surfaces the edge, since the tombstone marks the
        BOUNDARY, not itself.
        """
        if not rows:
            return rows

        # Fast path: no row has a tombstone set.
        has_tombstone = any(row.get("valid_until_version") for row in rows)
        if not has_tombstone:
            return rows

        start_lineage = await cache.get(query_start_id)

        kept: list[dict] = []
        for row in rows:
            tombstone = row.get("valid_until_version")
            if not tombstone:
                kept.append(row)
                continue
            if tombstone == query_start_id:
                # Historical query at the merge boundary: not suppressed.
                kept.append(row)
                continue
            if tombstone in start_lineage:
                # Tombstone is a strict ancestor of the query start:
                # query is downstream of the termination -> suppress.
                continue
            kept.append(row)
        return kept

    def _effective_policy(self, row: dict) -> ResolutionPolicy:
        """Return the authoritative policy for an edge row.

        Prefer the stored column (frozen at creation per CR-012); fall
        back to the registry for legacy rows without it.
        """
        stored = row.get("resolution_policy")
        if stored:
            return ResolutionPolicy(stored)
        try:
            edge_type_enum = EdgeType(row["edge_type"])
        except ValueError:
            return ResolutionPolicy.NONE
        return self._edge_type_registry.policy_for(edge_type_enum)

    async def _collect_raw_with_seeds(
        self, request: TraverseRequest, cache: _LineageCache
    ) -> list[dict]:
        """Run direction-split, policy-driven multi-seed traversal.

        For direction=both we split into outbound and inbound phases so
        each side can use its own seed set (relevant for
        `transitive_source` inbound, where the target is frozen and the
        seed must not expand). Rows are deduped across seeds by
        edge_id, keeping the smallest observed depth.
        """
        if request.direction == TraversalDirection.BOTH:
            phases: list[TraversalDirection] = [
                TraversalDirection.OUTBOUND,
                TraversalDirection.INBOUND,
            ]
        else:
            phases = [request.direction]

        edge_type_filter = (
            request.edge_type.value if request.edge_type else None
        )

        raw_by_edge: dict[str, dict] = {}
        for phase in phases:
            seeds = await self._determine_seeds(
                request.start_id, request.edge_type, phase, cache
            )
            for seed in seeds:
                rows = await self._store.traverse(
                    start_id=seed,
                    edge_type=edge_type_filter,
                    direction=phase.value,
                    depth=request.depth,
                )
                for row in rows:
                    eid = row["edge_id"]
                    existing = raw_by_edge.get(eid)
                    if existing is None or row["depth"] < existing["depth"]:
                        raw_by_edge[eid] = row

        return list(raw_by_edge.values())

    async def _determine_seeds(
        self,
        start_id: str,
        edge_type: EdgeType | None,
        direction: TraversalDirection,
        cache: _LineageCache,
    ) -> list[str]:
        """Pick seed doc_ids based on registry policy and direction.

        Chain-scoped near-side: expand to supersedes lineage of start_id.
        Frozen near-side (transitive_source inbound): seeds = [start_id].
        policy=none: seeds = [start_id] (no chain expansion needed).
        No edge_type filter: expand to lineage (most permissive; the
          per-edge anchor filter prunes false positives). Acceptable for
          mixed-type traverses; full per-policy split is not required
          until downstream chunks drive the need.
        """
        if edge_type is None:
            lineage = await cache.get(start_id)
            return list(lineage) if lineage else [start_id]

        policy = self._edge_type_registry.policy_for(edge_type)

        if policy == ResolutionPolicy.NONE:
            return [start_id]

        if policy == ResolutionPolicy.TBD:
            raise TBDPolicyEdgeError(edge_type.value)

        if policy == ResolutionPolicy.TRANSITIVE_SOURCE:
            if direction == TraversalDirection.INBOUND:
                # Target is frozen; exact-match only.
                return [start_id]
            lineage = await cache.get(start_id)
            return list(lineage) if lineage else [start_id]

        if policy == ResolutionPolicy.TRANSITIVE_BOTH:
            lineage = await cache.get(start_id)
            return list(lineage) if lineage else [start_id]

        return [start_id]

    async def _edge_passes_anchor_filter(
        self, row: dict, cache: _LineageCache
    ) -> bool:
        """True iff the edge's anchors sit in the appropriate lineages.

        The stored `resolution_policy` column is authoritative (frozen at
        creation). For legacy rows without a stored policy, the registry
        is consulted. Missing anchor documents yield WARN + suppress
        (conservative behavior, CAS-ADR-017 Chunk 4 open-question
        resolution).
        """
        edge_type_val = row["edge_type"]
        try:
            edge_type_enum = EdgeType(edge_type_val)
        except ValueError:
            logger.warning(
                "traverse: edge %s has unknown edge_type=%r; suppressing",
                row.get("edge_id"),
                edge_type_val,
            )
            return False

        stored_policy = row.get("resolution_policy")
        if stored_policy:
            policy = ResolutionPolicy(stored_policy)
        else:
            # Pre-backfill or legacy row: consult registry.
            policy = self._edge_type_registry.policy_for(edge_type_enum)

        if policy == ResolutionPolicy.NONE:
            return True

        if policy == ResolutionPolicy.TBD:
            logger.warning(
                "traverse: edge %s has TBD resolution_policy; suppressing",
                row.get("edge_id"),
            )
            return False

        source_anchor = row.get("source_valid_from_version")
        target_anchor = row.get("target_valid_from_version")
        source_id = row["source_id"]
        target_id = row.get("target_id")

        if policy == ResolutionPolicy.TRANSITIVE_SOURCE:
            if source_anchor is None:
                return False
            return await self._anchor_in_lineage(source_anchor, source_id, cache)

        if policy == ResolutionPolicy.TRANSITIVE_BOTH:
            if source_anchor is None or target_anchor is None or target_id is None:
                return False
            if not await self._anchor_in_lineage(source_anchor, source_id, cache):
                return False
            if not await self._anchor_in_lineage(target_anchor, target_id, cache):
                return False
            return True

        return True

    async def _anchor_in_lineage(
        self, anchor_id: str, endpoint_id: str, cache: _LineageCache
    ) -> bool:
        lineage = await cache.get(endpoint_id)
        if anchor_id in lineage:
            return True
        if not lineage:
            logger.warning(
                "traverse: endpoint %r has empty lineage (document missing); "
                "suppressing anchor check",
                endpoint_id,
            )
            return False
        # Anchor is not in lineage. If the anchor doc itself is missing,
        # surface a specific WARN per CR-021.
        anchor_doc = await self._store.get_document(anchor_id)
        if anchor_doc is None:
            logger.warning(
                "traverse: anchor document %r is missing (referenced as "
                "anchor for endpoint %r); suppressing edge",
                anchor_id,
                endpoint_id,
            )
        return False

    # ------------------------------------------------------------------
    # Chain walk (BH-089 through BH-096)
    # ------------------------------------------------------------------

    async def chain(self, request: ChainRequest) -> ChainResponse:
        """Walk an edge chain to both ends from any starting document."""
        start_doc = await self._store.get_document(request.document_id)
        if start_doc is None:
            raise DocumentNotFoundError(request.document_id)

        raw = await self._store.chain_walk(
            start_id=request.document_id,
            edge_type=request.edge_type.value,
        )

        documents = raw["documents"]
        edges = raw["edges"]
        doc_map = {d["doc_id"]: d for d in documents}

        # Build adjacency: source_id -> set of target_ids
        # For supersedes: source supersedes target, so source is newer.
        successors: dict[str, set[str]] = {d["doc_id"]: set() for d in documents}
        predecessors: dict[str, set[str]] = {d["doc_id"]: set() for d in documents}
        for e in edges:
            successors.setdefault(e["source_id"], set()).add(e["target_id"])
            predecessors.setdefault(e["target_id"], set()).add(e["source_id"])

        # Detect linearity: every node has at most 1 predecessor and 1 successor
        is_linear = all(
            len(successors[d]) <= 1 and len(predecessors[d]) <= 1
            for d in doc_map
        )

        # Topological sort: find roots (no inbound edges = no predecessors)
        # and walk forward through successors, then reverse so position 0
        # is the end of the chain (oldest/original) and position N is the
        # root (newest/head).
        #
        # For supersedes: source supersedes target, so edge direction is
        # newer->older.  Roots (no predecessors) are the newest documents.
        # Walking successors goes backward in time.  Reversing gives
        # oldest-first ordering.
        roots = [d for d in doc_map if not predecessors.get(d)]

        if is_linear and len(roots) == 1:
            # Simple linear chain: walk from root to end, then reverse
            walk: list[str] = []
            current = roots[0]
            visited: set[str] = set()
            while current and current not in visited:
                visited.add(current)
                walk.append(current)
                nexts = successors.get(current, set())
                current = next(iter(nexts), None) if nexts else None
            ordered = list(reversed(walk))
        else:
            # Non-linear (fork) or multiple roots: BFS from all roots,
            # then reverse
            walk = []
            visited = set()
            queue = list(roots) if roots else [request.document_id]
            for node in queue:
                if node in visited:
                    continue
                visited.add(node)
                walk.append(node)
                for succ in sorted(successors.get(node, set())):
                    if succ not in visited:
                        queue.append(succ)
            ordered = list(reversed(walk))

        # Build chain entries with positions
        chain_entries: list[ChainEntry] = []
        query_position = 0
        for i, doc_id in enumerate(ordered):
            d = doc_map[doc_id]
            if doc_id == request.document_id:
                query_position = i
            chain_entries.append(ChainEntry(
                id=doc_id,
                title=d["title"],
                version_label=d["version_label"],
                lifecycle_status=d["lifecycle_status"],
                document_date=d["document_date"],
                position=i,
            ))

        # tail = position 0 (oldest), head = position N (newest)
        tail_id = ordered[0] if ordered else request.document_id
        head_id = ordered[-1] if ordered else request.document_id

        # When chain has only the query document (no matching edges),
        # report what edge types DO exist so the caller can adjust.
        available_edge_types: list[str] | None = None
        if len(chain_entries) == 1:
            outbound = await self._store.get_edges_by_source(request.document_id)
            inbound = await self._store.get_edges_by_target(request.document_id)
            all_types = sorted({e.edge_type.value for e in outbound + inbound})
            available_edge_types = all_types

        # Apply slice if limit is specified
        total_length = len(chain_entries)
        if request.limit is not None:
            chain_entries = chain_entries[request.offset:request.offset + request.limit]
        elif request.offset > 0:
            chain_entries = chain_entries[request.offset:]

        return ChainResponse(
            chain=chain_entries,
            head_id=head_id,
            tail_id=tail_id,
            query_position=query_position,
            length=len(chain_entries),
            total_length=total_length,
            is_linear=is_linear,
            available_edge_types=available_edge_types,
        )

    # ------------------------------------------------------------------
    # Minimal discover stub for BH-021
    # ------------------------------------------------------------------

    async def check_pipeline_for_retrieval(self, document_id: str) -> None:
        """Raise PipelineIncompleteError if document has failed pipeline.

        Minimal stub for deterministic retrieval gating. Full discover
        implementation is in Slice 3.
        """
        doc = await self._store.get_document(document_id)
        if doc is None:
            raise DocumentNotFoundError(document_id)
        if doc.pipeline_status == PipelineStatus.FAILED:
            raise PipelineIncompleteError(document_id)
