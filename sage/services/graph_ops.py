"""Graph operations service: link, check_preconditions, traverse.

Covers behavioral tests BH-021, BH-023, BH-031 through BH-037.
"""

import asyncio
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
    ResolutionPathEntry,
    TraversalNode,
    TraverseRequest,
    TraverseResponse,
)
from sage.storage.graph_store import GraphStore, LinkReadContext

logger = logging.getLogger(__name__)


def _parse_doc_date(date_str: str) -> datetime | None:
    """Parse a stored document_date string into a UTC datetime, or None.

    Tolerant of the contract YYYY-MM-DD shape and any other ISO-8601 form
    datetime.fromisoformat understands (including a trailing Z); naive
    results are treated as UTC. Returns None on unparseable input rather
    than raising, since the read path should not crash on out-of-spec
    data already persisted by upstream paths.
    """
    try:
        parsed = datetime.fromisoformat(date_str)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


class _ResolutionPathRecorder:
    """Per-request collector for CAS-ADR-017 resolution_path debug events.

    Allocated only when `TraverseRequest.debug=True`. Emission sites guard
    with `if recorder is not None`, so the debug-off path has a single
    branch-and-skip and no payload construction.
    """

    def __init__(self) -> None:
        self.entries: list[ResolutionPathEntry] = []

    def anchor_hit(
        self, edge_id: str, anchor_field: str, anchor_version: str
    ) -> None:
        self.entries.append(ResolutionPathEntry(
            event_type="anchor_hit",
            edge_id=edge_id,
            anchor_field=anchor_field,
            anchor_version=anchor_version,
        ))

    def anchor_miss(
        self, edge_id: str, anchor_field: str, anchor_version: str | None
    ) -> None:
        self.entries.append(ResolutionPathEntry(
            event_type="anchor_miss",
            edge_id=edge_id,
            anchor_field=anchor_field,
            anchor_version=anchor_version,
        ))

    def retracts_applied(
        self, edge_id: str, retracting_edge_id: str
    ) -> None:
        self.entries.append(ResolutionPathEntry(
            event_type="retracts_applied",
            edge_id=edge_id,
            retracted_edge_id=retracting_edge_id,
        ))

    def tombstone_applied(
        self, edge_id: str, tombstone_version: str
    ) -> None:
        self.entries.append(ResolutionPathEntry(
            event_type="tombstone_applied",
            edge_id=edge_id,
            tombstone_version=tombstone_version,
        ))


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
        # Serializes link() across concurrent callers. Rationale: SQLite
        # writes serialize at the DB layer anyway, and per-call fan-out
        # into the graph-store executor compounded under parallel load,
        # filling the pool with orphaned work when MCP clients cancelled.
        # Queueing at the asyncio layer is cheap and bounds the pool's
        # backlog to one in-flight link's worth of submissions.
        self._link_lock = asyncio.Lock()

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
        policy = self._edge_type_registry.policy_for(request.edge_type)

        async with self._link_lock:
            ctx = await self._store.read_link_context(request, policy)

            # Error order preserves pre-batching behavior: document
            # existence → self-ref → target existence → TBD → shape →
            # retracted-edge existence → merged_from chain positions →
            # anchor lineage.
            if not ctx.source_exists:
                raise DocumentNotFoundError(request.source_id)

            if request.edge_type != EdgeType.RETRACTS:
                if (
                    request.target_id is not None
                    and request.source_id == request.target_id
                ):
                    raise SelfReferentialEdgeError(request.source_id)
                if request.target_id is not None and not ctx.target_exists:
                    raise DocumentNotFoundError(request.target_id)

            if policy == ResolutionPolicy.TBD:
                raise TBDPolicyEdgeError(request.edge_type.value)

            self._validate_link_request_shape(request, policy)

            if request.edge_type == EdgeType.RETRACTS:
                if ctx.retracted_edge is None:
                    raise RetractTargetNotEdgeError(request.retracted_edge_id)

            if request.edge_type == EdgeType.MERGED_FROM:
                if ctx.has_sup_predecessor:
                    raise MergedFromValidationError(
                        "source is not the first version of its chain (has an "
                        "outbound supersedes edge)",
                        source_id=request.source_id,
                        target_id=request.target_id,
                    )
                if ctx.has_sup_successor:
                    raise MergedFromValidationError(
                        "target is not a chain head (has a newer superseding version)",
                        source_id=request.source_id,
                        target_id=request.target_id,
                    )

            self._validate_anchors_from_context(request, policy, ctx)

            edge = Edge(
                id=str(uuid.uuid4()),
                source_id=request.source_id,
                target_id=request.target_id,
                edge_type=request.edge_type,
                resolution_policy=policy,
                source_valid_from_version=request.source_valid_from_version,
                target_valid_from_version=request.target_valid_from_version,
                valid_until_version=None,
                retracted_edge_id=request.retracted_edge_id,
                created_at=datetime.now(timezone.utc),
                notes=request.notes,
                rationale=request.rationale,
            )

            if request.edge_type == EdgeType.MERGED_FROM:
                await self._store.merge_atomic(
                    edge,
                    list(ctx.tombstone_candidates),
                    request.target_id,
                )
                return edge

            await self._store.insert_edge(edge)
            return edge

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

        if policy == ResolutionPolicy.TRANSITIVE_TARGET:
            if request.target_valid_from_version is None:
                raise EdgeAnchorPolicyViolationError(
                    edge_type.value,
                    policy.value,
                    "target_valid_from_version is required for policy 'transitive_target'",
                    ["target_valid_from_version"],
                )
            if request.source_valid_from_version is not None:
                raise EdgeAnchorPolicyViolationError(
                    edge_type.value,
                    policy.value,
                    (
                        "source_valid_from_version must be null for policy "
                        "'transitive_target' (source anchor is frozen at derivation)"
                    ),
                    ["source_valid_from_version"],
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

    def _validate_anchors_from_context(
        self,
        request: LinkRequest,
        policy: ResolutionPolicy,
        ctx: LinkReadContext,
    ) -> None:
        """Verify chain-scoped anchor(s) sit in the pre-fetched lineages.

        Pure Python — all DB state was gathered by `read_link_context`.
        No-op for policy `none` except for `retracts`, which carries a
        source-side anchor that must be in the retracting chain's lineage.
        """
        if policy == ResolutionPolicy.NONE:
            if (
                request.edge_type == EdgeType.RETRACTS
                and request.source_valid_from_version is not None
            ):
                self._check_anchor_in_lineage(
                    request.edge_type.value,
                    policy.value,
                    "source_valid_from_version",
                    request.source_valid_from_version,
                    request.source_id,
                    ctx.source_anchor_exists,
                    ctx.source_lineage,
                )
            return

        if request.source_valid_from_version is not None:
            self._check_anchor_in_lineage(
                request.edge_type.value,
                policy.value,
                "source_valid_from_version",
                request.source_valid_from_version,
                request.source_id,
                ctx.source_anchor_exists,
                ctx.source_lineage,
            )

        if (
            policy in (
                ResolutionPolicy.TRANSITIVE_TARGET,
                ResolutionPolicy.TRANSITIVE_BOTH,
            )
            and request.target_valid_from_version is not None
            and request.target_id is not None
        ):
            self._check_anchor_in_lineage(
                request.edge_type.value,
                policy.value,
                "target_valid_from_version",
                request.target_valid_from_version,
                request.target_id,
                ctx.target_anchor_exists,
                ctx.target_lineage,
            )

    @staticmethod
    def _check_anchor_in_lineage(
        edge_type: str,
        policy: str,
        field: str,
        anchor_id: str,
        endpoint_id: str,
        anchor_exists: bool,
        lineage: frozenset[str],
    ) -> None:
        if not anchor_exists:
            raise EdgeAnchorPolicyViolationError(
                edge_type,
                policy,
                f"{field}={anchor_id!r} does not reference a known document",
                [field],
            )
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
        recorder = _ResolutionPathRecorder() if request.debug else None

        raw = await self._collect_raw_with_seeds(request, cache)

        # Per-edge anchor filter (honors stored resolution_policy per CAS-ADR-017).
        filtered: list[dict] = []
        for row in raw:
            if await self._edge_passes_anchor_filter(row, cache, recorder):
                filtered.append(row)

        # CAS-ADR-017 Chunk 5: retracts short-circuit. Only chain-resolved
        # edges (transitive_source, transitive_both) are suppressible; the
        # retracts primitive does not veto policy=none edges indiscriminately.
        filtered = await self._apply_retracts(
            filtered, request.start_id, cache, recorder
        )

        # CAS-ADR-017 Chunk 6: tombstone suppression. Edges whose
        # `valid_until_version` sits strictly as an ancestor of the query
        # start_id are dropped. Equal-to-start is kept (CR-034: historical
        # query at the merge point still surfaces the edge).
        filtered = await self._apply_tombstones(
            filtered, request.start_id, cache, recorder
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
                    _parse_doc_date(best["d_document_date"])
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

        return TraverseResponse(
            start_id=request.start_id,
            nodes=nodes,
            resolution_path=recorder.entries if recorder is not None else None,
        )

    async def _apply_retracts(
        self,
        rows: list[dict],
        query_start_id: str,
        cache: _LineageCache,
        recorder: _ResolutionPathRecorder | None = None,
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
                ResolutionPolicy.TRANSITIVE_TARGET,
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
                ResolutionPolicy.TRANSITIVE_TARGET,
                ResolutionPolicy.TRANSITIVE_BOTH,
            ):
                kept.append(row)
                continue
            suppressing = next(
                (
                    r for r in retracts
                    if r.source_valid_from_version is not None
                    and r.source_valid_from_version in start_lineage
                ),
                None,
            )
            if suppressing is None:
                kept.append(row)
            elif recorder is not None:
                recorder.retracts_applied(row["edge_id"], suppressing.id)
        return kept

    async def _apply_tombstones(
        self,
        rows: list[dict],
        query_start_id: str,
        cache: _LineageCache,
        recorder: _ResolutionPathRecorder | None = None,
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
                if recorder is not None:
                    recorder.tombstone_applied(row["edge_id"], tombstone)
                continue
            kept.append(row)
        return kept

    def _effective_policy(self, row: dict) -> ResolutionPolicy:
        """Return the authoritative policy for an edge row.

        Prefer the stored column (frozen at creation per CR-012). For
        legacy rows without a stored policy (pre-ADR-017 writes) fall
        back to policy=none so the edge stays visible and retracts /
        tombstones do not apply. Promoting NULL to the registry's
        declared policy would require anchors that the legacy writer
        had no way to stamp; the result would be silent suppression
        of every legacy references / covers / derived_from edge. A
        migration that backfills resolution_policy + anchors will
        move legacy rows to full ADR-017 semantics.
        """
        stored = row.get("resolution_policy")
        if stored:
            return ResolutionPolicy(stored)
        return ResolutionPolicy.NONE

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

        if policy == ResolutionPolicy.TRANSITIVE_TARGET:
            # Mirror of transitive_source: source is frozen, target chain
            # advances. Outbound from a source-side start is exact-match
            # (source endpoint is frozen); inbound from a target-side
            # start expands to target lineage.
            if direction == TraversalDirection.OUTBOUND:
                return [start_id]
            lineage = await cache.get(start_id)
            return list(lineage) if lineage else [start_id]

        if policy == ResolutionPolicy.TRANSITIVE_BOTH:
            lineage = await cache.get(start_id)
            return list(lineage) if lineage else [start_id]

        return [start_id]

    async def _edge_passes_anchor_filter(
        self,
        row: dict,
        cache: _LineageCache,
        recorder: _ResolutionPathRecorder | None = None,
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
            EdgeType(edge_type_val)
        except ValueError:
            logger.warning(
                "traverse: edge %s has unknown edge_type=%r; suppressing",
                row.get("edge_id"),
                edge_type_val,
            )
            return False

        policy = self._effective_policy(row)

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
        edge_id = row.get("edge_id")

        if policy == ResolutionPolicy.TRANSITIVE_SOURCE:
            if source_anchor is None:
                if recorder is not None:
                    recorder.anchor_miss(
                        edge_id, "source_valid_from_version", None
                    )
                return False
            hit = await self._anchor_in_lineage(source_anchor, source_id, cache)
            if recorder is not None:
                if hit:
                    recorder.anchor_hit(
                        edge_id, "source_valid_from_version", source_anchor
                    )
                else:
                    recorder.anchor_miss(
                        edge_id, "source_valid_from_version", source_anchor
                    )
            return hit

        if policy == ResolutionPolicy.TRANSITIVE_TARGET:
            if target_anchor is None or target_id is None:
                if recorder is not None:
                    recorder.anchor_miss(
                        edge_id, "target_valid_from_version", target_anchor
                    )
                return False
            hit = await self._anchor_in_lineage(target_anchor, target_id, cache)
            if recorder is not None:
                if hit:
                    recorder.anchor_hit(
                        edge_id, "target_valid_from_version", target_anchor
                    )
                else:
                    recorder.anchor_miss(
                        edge_id, "target_valid_from_version", target_anchor
                    )
            return hit

        if policy == ResolutionPolicy.TRANSITIVE_BOTH:
            if source_anchor is None or target_anchor is None or target_id is None:
                if recorder is not None:
                    missing_field = (
                        "source_valid_from_version"
                        if source_anchor is None
                        else "target_valid_from_version"
                    )
                    missing_value = (
                        source_anchor
                        if missing_field == "source_valid_from_version"
                        else target_anchor
                    )
                    recorder.anchor_miss(edge_id, missing_field, missing_value)
                return False
            source_hit = await self._anchor_in_lineage(
                source_anchor, source_id, cache
            )
            if not source_hit:
                if recorder is not None:
                    recorder.anchor_miss(
                        edge_id, "source_valid_from_version", source_anchor
                    )
                return False
            target_hit = await self._anchor_in_lineage(
                target_anchor, target_id, cache
            )
            if not target_hit:
                if recorder is not None:
                    # Source check passed; record both outcomes so the
                    # trace shows which side dropped the edge.
                    recorder.anchor_hit(
                        edge_id, "source_valid_from_version", source_anchor
                    )
                    recorder.anchor_miss(
                        edge_id, "target_valid_from_version", target_anchor
                    )
                return False
            if recorder is not None:
                recorder.anchor_hit(
                    edge_id, "source_valid_from_version", source_anchor
                )
                recorder.anchor_hit(
                    edge_id, "target_valid_from_version", target_anchor
                )
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
