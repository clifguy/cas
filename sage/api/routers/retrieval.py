"""Retrieval router: discover endpoint (semantic + deterministic modes)."""

from fastapi import APIRouter, Depends

from sage.api.dependencies import get_retrieval_service, get_vault_id
from sage.models.schemas import DiscoverRequest, DiscoverResponse
from sage.services.retrieval import RetrievalService

router = APIRouter(tags=["Retrieval"])


@router.post("/discover", response_model=DiscoverResponse)
async def discover(
    request: DiscoverRequest,
    vault_id: str = Depends(get_vault_id),
    service: RetrievalService = Depends(get_retrieval_service),
) -> DiscoverResponse:
    return await service.discover(request)
