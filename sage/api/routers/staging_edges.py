"""Staging edge endpoints: list, confirm, dismiss.

GET /sage_vaults/{vault_id}/staging-edges -- list Tier 2 staging edges (BE-010)
POST /sage_vaults/{vault_id}/staging-edges/{edge_id}/confirm -- promote to production (BE-011)
POST /sage_vaults/{vault_id}/staging-edges/{edge_id}/dismiss -- delete (BE-012)
"""

import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends

from sage.api.dependencies import get_graph_store, get_vault_id
from sage.api.errors import StagingEdgeNotFoundError
from sage.models.schemas import Edge, StagingEdge
from sage.storage.graph_store import GraphStore

router = APIRouter(tags=["staging_edges"])


@router.get("/staging-edges", response_model=list[StagingEdge])
async def list_staging_edges(
    vault_id: str = Depends(get_vault_id),
    graph_store: GraphStore = Depends(get_graph_store),
) -> list[StagingEdge]:
    """Return all Tier 2 suggested edges awaiting review."""
    return await graph_store.list_staging_edges()


@router.post("/staging-edges/{edge_id}/confirm")
async def confirm_staging_edge(
    edge_id: str,
    vault_id: str = Depends(get_vault_id),
    graph_store: GraphStore = Depends(get_graph_store),
) -> dict:
    """Confirm a staging edge: move it to the production edge table."""
    staging = await graph_store.get_staging_edge(edge_id)
    if staging is None:
        raise StagingEdgeNotFoundError(edge_id)

    # Create production edge with new ID
    production_edge = Edge(
        id=str(uuid.uuid4()),
        source_id=staging.source_id,
        target_id=staging.target_id,
        edge_type=staging.edge_type,
        created_at=datetime.now(timezone.utc),
        notes=f"Confirmed from staging edge {edge_id}",
        rationale=staging.inference_evidence,
    )
    await graph_store.insert_edge(production_edge)

    # Remove from staging
    await graph_store.delete_staging_edge(edge_id)

    return {
        "confirmed": True,
        "staging_edge_id": edge_id,
        "production_edge_id": production_edge.id,
    }


@router.post("/staging-edges/{edge_id}/dismiss")
async def dismiss_staging_edge(
    edge_id: str,
    vault_id: str = Depends(get_vault_id),
    graph_store: GraphStore = Depends(get_graph_store),
) -> dict:
    """Dismiss a staging edge: delete it without creating a production edge."""
    staging = await graph_store.get_staging_edge(edge_id)
    if staging is None:
        raise StagingEdgeNotFoundError(edge_id)

    await graph_store.delete_staging_edge(edge_id)

    return {"dismissed": True, "staging_edge_id": edge_id}
