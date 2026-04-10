"""Graph operations service: link, check_preconditions, traverse.

Covers behavioral tests BH-021, BH-023, BH-031 through BH-037.
"""

import json
import uuid
from datetime import datetime, timezone

from sage.api.errors import (
    DocumentNotFoundError,
    PipelineIncompleteError,
    SelfReferentialEdgeError,
)
from sage.config import VaultConfig
from sage.models.enums import EdgeType, PipelineStatus, SourceType
from sage.models.schemas import (
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

            nodes.append(TraversalNode(
                document=doc_summary,
                edge=edge,
                depth=min_depth,
                edge_count=len(rows),
            ))

        return TraverseResponse(start_id=request.start_id, nodes=nodes)

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
