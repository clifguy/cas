"""Staging edge endpoints: list, confirm, dismiss.

GET /sage_vaults/{vault_id}/staging-edges -- list Tier 2 staging edges (BE-010)
POST /sage_vaults/{vault_id}/staging-edges/{edge_id}/confirm -- promote to production (BE-011)
POST /sage_vaults/{vault_id}/staging-edges/{edge_id}/dismiss -- delete (BE-012)
"""

from fastapi import APIRouter, Body, Depends

from sage.api.dependencies import get_staging_edges_service, get_vault_id
from sage.api.response_docs import boundary_400, invalid_parameter_422
from sage.api.wire_route import WireRoute
from sage.models.schemas import (
    EdgeIdStr,
    ErrorResponse,
    StagingEdgeConfirmRequest,
    StagingEdgeConfirmResponse,
    StagingEdgeDismissResponse,
    StagingEdgeListResponse,
    VaultIdStr,
)
from sage.services.staging_edges import StagingEdgesService

router = APIRouter(route_class=WireRoute, tags=["staging_edges"])


@router.get(
    "/staging-edges",
    response_model=StagingEdgeListResponse,
    responses={
        400: boundary_400(path=("invalid_vault_id",), request=("unknown_parameter",)),
        404: {
            "model": ErrorResponse,
            "description": (
                "`vault_not_found`: no vault registered with that id; `detail.available_vaults` "
                "lists the registered vaults."
            ),
        },
    },
)
async def list_staging_edges(
    vault_id: VaultIdStr = Depends(get_vault_id),
    service: StagingEdgesService = Depends(get_staging_edges_service),
) -> StagingEdgeListResponse:
    """Return all Tier 2 suggested edges awaiting review."""
    return await service.list_staging_edges()


@router.post(
    "/staging-edges/{edge_id}/confirm",
    response_model=StagingEdgeConfirmResponse,
    responses={
        400: boundary_400(
            path=("invalid_edge_id", "invalid_vault_id"), request=("unknown_parameter",)
        ),
        404: {
            "model": ErrorResponse,
            "description": (
                "`staging_edge_not_found`: the id is unknown (already confirmed, already "
                "dismissed, or never existed).\n\n"
                "`vault_not_found`: no vault registered with that id; `detail.available_vaults` "
                "lists the registered vaults."
            ),
        },
        422: invalid_parameter_422(),
    },
)
async def confirm_staging_edge(
    edge_id: EdgeIdStr,
    body: StagingEdgeConfirmRequest = Body(default_factory=StagingEdgeConfirmRequest),
    vault_id: VaultIdStr = Depends(get_vault_id),
    service: StagingEdgesService = Depends(get_staging_edges_service),
) -> StagingEdgeConfirmResponse:
    """Confirm a staging edge: move it to the production edge table.

    The body is optional; a confirm that sends none records the agent the
    request's User-Agent names.
    """
    return await service.confirm_staging_edge(edge_id, agent=body.agent)


@router.post(
    "/staging-edges/{edge_id}/dismiss",
    response_model=StagingEdgeDismissResponse,
    responses={
        400: boundary_400(
            path=("invalid_edge_id", "invalid_vault_id"), request=("unknown_parameter",)
        ),
        404: {
            "model": ErrorResponse,
            "description": (
                "`staging_edge_not_found`: the id is unknown (already confirmed, already "
                "dismissed, or never existed).\n\n"
                "`vault_not_found`: no vault registered with that id; `detail.available_vaults` "
                "lists the registered vaults."
            ),
        },
    },
)
async def dismiss_staging_edge(
    edge_id: EdgeIdStr,
    vault_id: VaultIdStr = Depends(get_vault_id),
    service: StagingEdgesService = Depends(get_staging_edges_service),
) -> StagingEdgeDismissResponse:
    """Dismiss a staging edge: delete it without creating a production edge."""
    return await service.dismiss_staging_edge(edge_id)
