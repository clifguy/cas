"""Graph operations service: link, check_preconditions, traverse.

Covers behavioral tests BH-021, BH-023, BH-031 through BH-037.
"""

import json
import uuid
from datetime import datetime, timezone

from sage.api.errors import (
    DocumentNotFoundError,
    EdgeNotFoundError,
    PipelineIncompleteError,
    SelfReferentialEdgeError,
)
from sage.config import VaultConfig
from sage.models.enums import EdgeType, PipelineStatus, SourceType
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

# Lifecycle states that satisfy depends_on preconditions (BH-033, BH-034).
# Domain-specific states like 'filed' are excluded (BH-036).
_SATISFYING_STATUSES = frozenset({"active", "completed"})


class GraphOpsService:
    def __init__(self, graph_store: GraphStore, config: VaultConfig) -> None:
        self._store = graph_store
        self._config = config

    # ------------------------------------------------------------------
    # Link (BH-031, BH-032)
    # ------------------------------------------------------------------

    async def link(self, request: LinkRequest) -> Edge:
        """Create a typed edge between two documents."""
        if request.source_id == request.target_id:
            raise SelfReferentialEdgeError(request.source_id)

        source = await self._store.get_document(request.source_id)
        if source is None:
            raise DocumentNotFoundError(request.source_id)

        target = await self._store.get_document(request.target_id)
        if target is None:
            raise DocumentNotFoundError(request.target_id)

        edge = Edge(
            id=str(uuid.uuid4()),
            source_id=request.source_id,
            target_id=request.target_id,
            edge_type=request.edge_type,
            created_at=datetime.now(timezone.utc),
            notes=request.notes,
            rationale=request.rationale,
        )
        await self._store.insert_edge(edge)
        return edge

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
        """Graph walk with deduplication by document."""
        start_doc = await self._store.get_document(request.start_id)
        if start_doc is None:
            raise DocumentNotFoundError(request.start_id)

        raw = await self._store.traverse(
            start_id=request.start_id,
            edge_type=request.edge_type.value if request.edge_type else None,
            direction=request.direction.value,
            depth=request.depth,
        )

        # Deduplicate: group by doc_id, pick most recent edge, min depth
        grouped: dict[str, list[dict]] = {}
        for row in raw:
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

        return ChainResponse(
            chain=chain_entries,
            head_id=head_id,
            tail_id=tail_id,
            query_position=query_position,
            length=len(chain_entries),
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
