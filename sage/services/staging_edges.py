"""Staging edge operations: list, confirm, dismiss.

Owns the work behind three endpoints:
- GET /staging-edges -- list all Tier 2 staging edges (BE-010).
- POST /staging-edges/{edge_id}/confirm -- promote a staging edge to
  production (BE-011): mint UUID, construct Edge, insert + delete sequence.
- POST /staging-edges/{edge_id}/dismiss -- delete a staging edge without
  promotion (BE-012).
"""

import uuid
from datetime import datetime, timezone

from sage.api.errors import StagingEdgeNotFoundError
from sage.models.schemas import (
    Edge,
    StagingEdge,
    StagingEdgeConfirmResponse,
    StagingEdgeDismissResponse,
)
from sage.storage.graph_store import GraphStore


class StagingEdgesService:
    def __init__(self, graph_store: GraphStore) -> None:
        self._store = graph_store

    async def list_staging_edges(self) -> list[StagingEdge]:
        """Return all Tier 2 suggested edges awaiting review."""
        return await self._store.list_staging_edges()

    async def confirm_staging_edge(self, edge_id: str) -> StagingEdgeConfirmResponse:
        """Promote a staging edge to the production edge table.

        Mints a new production-edge UUID, copies source/target/edge_type
        from the staging record, sequences insert + delete.

        Confirm idempotency on natural-key collision (T-0079):
        If the staging edge's natural-key triple
        ``(source_id, target_id, edge_type)`` already exists in the
        production edges table -- for example, because a parallel
        ``GraphOpsService.link`` call or an earlier auto-inference path
        already created the production edge -- the underlying
        ``insert_edge`` invocation passes ``on_conflict="noop"``, which
        returns the existing production edge's id rather than raising
        ``IntegrityError``. The staging row is then consumed in either
        case. Callers cannot distinguish "this confirm caused the
        production edge to be created" from "the production edge
        pre-existed; this confirm only consumed the staging row" by
        inspecting the response.

        Insert-then-delete atomicity gap:
        ``insert_edge`` and ``delete_staging_edge`` are sequenced without
        a wrapping transaction. If the delete fails after the insert
        succeeds, the staging row persists alongside the production edge;
        the natural-key triple then exists in both tables until a
        subsequent confirm consumes the orphaned staging row (which is
        itself a T-0079 silent-idempotent no-op per the rule above).
        Callers should treat confirm as "at-least-once" for the
        production-edge insert and rely on the natural-key UNIQUE
        constraint plus T-0079 idempotency to absorb retries.
        """
        staging = await self._store.get_staging_edge(edge_id)
        if staging is None:
            raise StagingEdgeNotFoundError(edge_id)

        candidate = Edge(
            id=str(uuid.uuid4()),
            source_id=staging.source_id,
            target_id=staging.target_id,
            edge_type=staging.edge_type,
            created_at=datetime.now(timezone.utc),
            notes=f"Confirmed from staging edge {edge_id}",
            rationale=staging.inference_evidence,
        )
        # T-0079: if the natural-key triple already exists in production
        # (e.g., a parallel sage_link or earlier auto-inference path
        # already created the edge), confirm-staging is idempotent:
        # consume the staging row and surface the existing production
        # edge's id rather than raising IntegrityError to the caller.
        stored, _created = await self._store.insert_edge(candidate, on_conflict="noop")
        await self._store.delete_staging_edge(edge_id)

        return StagingEdgeConfirmResponse(
            confirmed=True,
            staging_edge_id=edge_id,
            production_edge_id=stored.id,
        )

    async def dismiss_staging_edge(self, edge_id: str) -> StagingEdgeDismissResponse:
        """Delete a staging edge without creating a production edge."""
        staging = await self._store.get_staging_edge(edge_id)
        if staging is None:
            raise StagingEdgeNotFoundError(edge_id)
        await self._store.delete_staging_edge(edge_id)
        return StagingEdgeDismissResponse(dismissed=True, staging_edge_id=edge_id)
