"""Utilities router: export_projection, eval_retrieval, refresh_views."""

from fastapi import APIRouter, Depends, Path

from sage.api.dependencies import get_utilities_service, get_vault_id
from sage.models.schemas import (
    EvalRetrievalResult,
    ExportProjectionRequest,
    ExportProjectionResponse,
    ReadProjectionResponse,
    RefreshViewsResponse,
)
from sage.services.utilities import UtilitiesService

router = APIRouter(tags=["Utilities"])


@router.post(
    "/documents/{document_id}/export",
    response_model=ExportProjectionResponse,
)
async def export_projection(
    request: ExportProjectionRequest,
    document_id: str = Path(..., description="Document identifier"),
    vault_id: str = Depends(get_vault_id),
    service: UtilitiesService = Depends(get_utilities_service),
) -> ExportProjectionResponse:
    return await service.export_projection(document_id, request.output_path)


@router.get(
    "/documents/{document_id}/projection",
    response_model=ReadProjectionResponse,
)
async def read_projection(
    document_id: str = Path(..., description="Document identifier"),
    vault_id: str = Depends(get_vault_id),
    service: UtilitiesService = Depends(get_utilities_service),
) -> ReadProjectionResponse:
    return await service.read_projection(document_id)


@router.post("/eval-retrieval", response_model=EvalRetrievalResult)
async def eval_retrieval(
    vault_id: str = Depends(get_vault_id),
    service: UtilitiesService = Depends(get_utilities_service),
) -> EvalRetrievalResult:
    return await service.eval_retrieval()


@router.post("/refresh-views", response_model=RefreshViewsResponse)
async def refresh_views(
    vault_id: str = Depends(get_vault_id),
    service: UtilitiesService = Depends(get_utilities_service),
) -> RefreshViewsResponse:
    return await service.refresh_views()
