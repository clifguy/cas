"""Utilities router: export_projection and eval_retrieval endpoints."""

from fastapi import APIRouter, Depends, Path

from sage.api.dependencies import get_utilities_service, get_vault_id
from sage.models.schemas import (
    EvalRetrievalResult,
    ExportProjectionRequest,
    ExportProjectionResponse,
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


@router.post("/eval-retrieval", response_model=EvalRetrievalResult)
async def eval_retrieval(
    vault_id: str = Depends(get_vault_id),
    service: UtilitiesService = Depends(get_utilities_service),
) -> EvalRetrievalResult:
    return await service.eval_retrieval(vault_id)
