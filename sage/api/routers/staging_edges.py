"""Staging edge endpoints: list, confirm, dismiss.

GET /sage_vaults/{vault_id}/staging-edges -- list Tier 2 staging edges (BE-010)
POST /sage_vaults/{vault_id}/staging-edges/{edge_id}/confirm -- promote to production (BE-011)
POST /sage_vaults/{vault_id}/staging-edges/{edge_id}/dismiss -- delete (BE-012)
"""

from fastapi import APIRouter, Depends

from sage.api.dependencies import get_staging_edges_service, get_vault_id
from sage.models.schemas import (
    EdgeIdStr,
    StagingEdge,
    StagingEdgeConfirmResponse,
    StagingEdgeDismissResponse,
    VaultIdStr,
)
from sage.services.staging_edges import StagingEdgesService

router = APIRouter(tags=["staging_edges"])


@router.get("/staging-edges", response_model=list[StagingEdge])
async def list_staging_edges(
    vault_id: VaultIdStr = Depends(get_vault_id),
    service: StagingEdgesService = Depends(get_staging_edges_service),
) -> list[StagingEdge]:
    """Return all Tier 2 suggested edges awaiting review."""
    return await service.list_staging_edges()


@router.post("/staging-edges/{edge_id}/confirm", response_model=StagingEdgeConfirmResponse)
async def confirm_staging_edge(
    edge_id: EdgeIdStr,
    vault_id: VaultIdStr = Depends(get_vault_id),
    service: StagingEdgesService = Depends(get_staging_edges_service),
) -> StagingEdgeConfirmResponse:
    """Confirm a staging edge: move it to the production edge table."""
    return await service.confirm_staging_edge(edge_id)


@router.post("/staging-edges/{edge_id}/dismiss", response_model=StagingEdgeDismissResponse)
async def dismiss_staging_edge(
    edge_id: EdgeIdStr,
    vault_id: VaultIdStr = Depends(get_vault_id),
    service: StagingEdgesService = Depends(get_staging_edges_service),
) -> StagingEdgeDismissResponse:
    """Dismiss a staging edge: delete it without creating a production edge."""
    return await service.dismiss_staging_edge(edge_id)
