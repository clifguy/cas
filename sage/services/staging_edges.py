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
from sage.models.schemas import Edge, StagingEdge
from sage.storage.graph_store import GraphStore


class StagingEdgesService:
    def __init__(self, graph_store: GraphStore) -> None:
        self._store = graph_store

    async def list_staging_edges(self) -> list[StagingEdge]:
        """Return all Tier 2 suggested edges awaiting review."""
        return await self._store.list_staging_edges()

    async def confirm_staging_edge(self, edge_id: str) -> dict:
        """Promote a staging edge to the production edge table.

        Mints a new production-edge UUID, copies source/target/edge_type
        from the staging record, sequences insert + delete.
        """
        staging = await self._store.get_staging_edge(edge_id)
        if staging is None:
            raise StagingEdgeNotFoundError(edge_id)

        production = Edge(
            id=str(uuid.uuid4()),
            source_id=staging.source_id,
            target_id=staging.target_id,
            edge_type=staging.edge_type,
            created_at=datetime.now(timezone.utc),
            notes=f"Confirmed from staging edge {edge_id}",
            rationale=staging.inference_evidence,
        )
        await self._store.insert_edge(production)
        await self._store.delete_staging_edge(edge_id)

        return {
            "confirmed": True,
            "staging_edge_id": edge_id,
            "production_edge_id": production.id,
        }

    async def dismiss_staging_edge(self, edge_id: str) -> dict:
        """Delete a staging edge without creating a production edge."""
        staging = await self._store.get_staging_edge(edge_id)
        if staging is None:
            raise StagingEdgeNotFoundError(edge_id)
        await self._store.delete_staging_edge(edge_id)
        return {"dismissed": True, "staging_edge_id": edge_id}
