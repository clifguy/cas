"""Utilities router: export_projection, eval_retrieval, refresh_views."""

from typing import Annotated

from fastapi import APIRouter, Depends, Path

from sage.api.dependencies import get_utilities_service, get_vault_id
from sage.models.schemas import (
    DocumentIdStr,
    EvalRetrievalResult,
    ExportProjectionRequest,
    ExportProjectionResponse,
    ReadProjectionResponse,
    ReadSectionResponse,
    RefreshViewsResponse,
    VaultIdStr,
)
from sage.services.utilities import UtilitiesService

router = APIRouter(tags=["Utilities"])

# ``Annotated[T, Path(...)]`` preserves the alias's ``AfterValidator``.
# The bare ``T = Path(...)`` form silently strips it.
_DocumentIdPath = Annotated[DocumentIdStr, Path(description="Document identifier")]


@router.post(
    "/documents/{document_id}/export",
    response_model=ExportProjectionResponse,
)
async def export_projection(
    request: ExportProjectionRequest,
    document_id: _DocumentIdPath,
    vault_id: VaultIdStr = Depends(get_vault_id),
    service: UtilitiesService = Depends(get_utilities_service),
) -> ExportProjectionResponse:
    return await service.export_projection(document_id, request.output_path)


@router.get(
    "/documents/{document_id}/projection",
    response_model=ReadProjectionResponse,
)
async def read_projection(
    document_id: _DocumentIdPath,
    vault_id: VaultIdStr = Depends(get_vault_id),
    service: UtilitiesService = Depends(get_utilities_service),
) -> ReadProjectionResponse:
    return await service.read_projection(document_id)


@router.get(
    "/documents/{document_id}/section/{heading_path:path}",
    response_model=ReadSectionResponse,
)
async def read_section(
    document_id: _DocumentIdPath,
    heading_path: Annotated[str, Path(description="Heading path prefix")],
    vault_id: VaultIdStr = Depends(get_vault_id),
    service: UtilitiesService = Depends(get_utilities_service),
) -> ReadSectionResponse:
    return await service.read_section(document_id, heading_path)


@router.post("/eval-retrieval", response_model=EvalRetrievalResult)
async def eval_retrieval(
    vault_id: VaultIdStr = Depends(get_vault_id),
    service: UtilitiesService = Depends(get_utilities_service),
) -> EvalRetrievalResult:
    return await service.eval_retrieval()


@router.post("/refresh-views", response_model=RefreshViewsResponse)
async def refresh_views(
    vault_id: VaultIdStr = Depends(get_vault_id),
    service: UtilitiesService = Depends(get_utilities_service),
) -> RefreshViewsResponse:
    return await service.refresh_views()
