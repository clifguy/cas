"""Utilities router: export_projection, eval_retrieval, refresh_views."""

from typing import Annotated

from fastapi import APIRouter, Depends, Path

from sage.api.dependencies import get_utilities_service, get_vault_id
from sage.models.schemas import (
    DocumentIdStr,
    ErrorResponse,
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
    responses={
        400: {
            "model": ErrorResponse,
            "description": (
                "`path_traversal_denied`: `output_path` resolves outside the "
                "vault's `storage_root`."
            ),
        },
        404: {
            "model": ErrorResponse,
            "description": (
                "`document_not_found`: no document with that id.\n\n"
                "`no_projection`: the document exists but has no stored "
                "projection (e.g. ingestion failed mid-pipeline).\n\n"
                "Or vault not found."
            ),
        },
    },
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
    responses={
        404: {
            "model": ErrorResponse,
            "description": (
                "`document_not_found`: no document with that id.\n\n"
                "`no_projection`: the document exists but has no stored "
                "projection (e.g. ingestion failed mid-pipeline or the "
                "document is awaiting reabstraction). Inspect "
                "`pipeline_status` via `GET /documents/{id}`.\n\n"
                "Or vault not found."
            ),
        },
    },
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
    responses={
        404: {
            "model": ErrorResponse,
            "description": (
                "`document_not_found`: no document with that id.\n\n"
                "`heading_not_found`: no chunk's `heading_path` matches the "
                "supplied prefix. The error detail includes "
                "`candidate_matches`, a list of stored paths that contain "
                "the query as a substring.\n\n"
                "Or vault not found."
            ),
        },
    },
)
async def read_section(
    document_id: _DocumentIdPath,
    heading_path: Annotated[str, Path(description="Heading path prefix")],
    vault_id: VaultIdStr = Depends(get_vault_id),
    service: UtilitiesService = Depends(get_utilities_service),
) -> ReadSectionResponse:
    return await service.read_section(document_id, heading_path)


@router.post(
    "/eval-retrieval",
    response_model=EvalRetrievalResult,
    responses={
        400: {
            "model": ErrorResponse,
            "description": (
                "`assertions_file_invalid`: the referenced YAML is malformed "
                "or has the wrong structure."
            ),
        },
        404: {
            "model": ErrorResponse,
            "description": (
                "`assertions_not_configured`: the vault config has no "
                "`retrieval_health.assertions_file` entry.\n\n"
                "`assertions_file_not_found`: the configured assertions "
                "file does not exist under the vault's `storage_root`.\n\n"
                "Or vault not found."
            ),
        },
    },
)
async def eval_retrieval(
    vault_id: VaultIdStr = Depends(get_vault_id),
    service: UtilitiesService = Depends(get_utilities_service),
) -> EvalRetrievalResult:
    return await service.eval_retrieval()


@router.post(
    "/refresh-views",
    response_model=RefreshViewsResponse,
    responses={
        404: {
            "model": ErrorResponse,
            "description": "Vault not found.",
        },
    },
)
async def refresh_views(
    vault_id: VaultIdStr = Depends(get_vault_id),
    service: UtilitiesService = Depends(get_utilities_service),
) -> RefreshViewsResponse:
    return await service.refresh_views()
