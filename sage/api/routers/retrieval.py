"""Retrieval router: discover endpoint (semantic + deterministic modes)."""

from fastapi import APIRouter, Depends

from sage.api.dependencies import get_retrieval_service, get_vault_id
from sage.models.schemas import DiscoverRequest, DiscoverResponse, ErrorResponse, VaultIdStr
from sage.services.retrieval import RetrievalService

router = APIRouter(tags=["Retrieval"])


@router.post(
    "/discover",
    response_model=DiscoverResponse,
    responses={
        400: {
            "model": ErrorResponse,
            "description": ("Invalid parameters (unknown mode, scope, or filter field)."),
        },
        404: {
            "model": ErrorResponse,
            "description": (
                "Vault not found, or document not found (for specific/deterministic scope)."
            ),
        },
    },
)
async def discover(
    request: DiscoverRequest,
    vault_id: VaultIdStr = Depends(get_vault_id),
    service: RetrievalService = Depends(get_retrieval_service),
) -> DiscoverResponse:
    return await service.discover(request)
