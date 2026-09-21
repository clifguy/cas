"""Utilities router: export_projection, verify_vault_retrieval, refresh_views."""

from typing import Annotated

from fastapi import APIRouter, Depends, Path

from sage.api.dependencies import get_utilities_service, get_vault_id
from sage.api.response_docs import boundary_400
from sage.api.wire_route import WireRoute
from sage.models.schemas import (
    DocumentIdStr,
    ErrorResponse,
    EvalRetrievalResult,
    ExportProjectionRequest,
    ExportProjectionResponse,
    ListHeadingsResponse,
    ReadProjectionResponse,
    ReadSectionResponse,
    RefreshViewsResponse,
    VaultIdStr,
)
from sage.services.utilities import UtilitiesService

router = APIRouter(route_class=WireRoute, tags=["Utilities"])

# ``Annotated[T, Path(...)]`` preserves the alias's ``AfterValidator``.
# The bare ``T = Path(...)`` form silently strips it.
_DocumentIdPath = Annotated[DocumentIdStr, Path(description="Document identifier")]


@router.post(
    "/documents/{document_id}/export",
    response_model=ExportProjectionResponse,
    responses={
        400: boundary_400(
            path=("invalid_document_id", "invalid_vault_id"),
            request=("unknown_parameter",),
            extra="`path_traversal_denied`: `output_path` resolves outside the "
            "vault's `storage_root`.",
        ),
        404: {
            "model": ErrorResponse,
            "description": (
                "`document_not_found`: no document with that id.\n\n"
                "`no_projection`: the document exists but has no stored projection (e.g. "
                "ingestion failed mid-pipeline).\n\n"
                "`vault_not_found`: no vault registered with that id; `detail.available_vaults` "
                "lists the registered vaults."
            ),
        },
        501: {
            "model": ErrorResponse,
            "description": (
                "`caller_filesystem_unavailable`: the export writes into the "
                "server's own vault tree, which a caller cannot read back under "
                "the cloud profile; the refusal comes before any read."
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
        400: boundary_400(
            path=("invalid_document_id", "invalid_vault_id"), request=("unknown_parameter",)
        ),
        404: {
            "model": ErrorResponse,
            "description": (
                "`document_not_found`: no document with that id.\n\n"
                "`no_projection`: the document exists but has no stored projection (e.g. "
                "ingestion failed mid-pipeline or the document is awaiting reabstraction). "
                "Inspect `pipeline_status` via `GET /documents/{id}`.\n\n"
                "`vault_not_found`: no vault registered with that id; `detail.available_vaults` "
                "lists the registered vaults."
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
        400: boundary_400(
            path=("invalid_document_id", "invalid_vault_id"), request=("unknown_parameter",)
        ),
        404: {
            "model": ErrorResponse,
            "description": (
                "`document_not_found`: no document with that id.\n\n"
                "`heading_not_found`: no chunk's `heading_path` matches the supplied prefix. "
                "The error detail includes `candidate_matches`, a list of stored paths that "
                "contain the query as a substring.\n\n"
                "`vault_not_found`: no vault registered with that id; `detail.available_vaults` "
                "lists the registered vaults."
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


@router.get(
    "/documents/{document_id}/headings",
    response_model=ListHeadingsResponse,
    responses={
        400: boundary_400(
            path=("invalid_document_id", "invalid_vault_id"), request=("unknown_parameter",)
        ),
        404: {
            "model": ErrorResponse,
            "description": (
                "`document_not_found`: no document with that id.\n\n"
                "`no_projection`: the document exists but has no stored projection (e.g. "
                "ingestion failed mid-pipeline or the document is awaiting reabstraction).\n\n"
                "`vault_not_found`: no vault registered with that id; `detail.available_vaults` "
                "lists the registered vaults."
            ),
        },
    },
)
async def list_headings(
    document_id: _DocumentIdPath,
    vault_id: VaultIdStr = Depends(get_vault_id),
    service: UtilitiesService = Depends(get_utilities_service),
) -> ListHeadingsResponse:
    return await service.list_headings(document_id)


@router.post(
    "/eval-retrieval",
    response_model=EvalRetrievalResult,
    responses={
        400: boundary_400(
            path=("invalid_vault_id",),
            request=("unknown_parameter",),
            extra="`assertions_file_invalid`: the referenced YAML is malformed "
            "or has the wrong structure, or its path leaves the vault's `storage_root`.\n\n"
            "`assertions_not_configured`: the vault config has no "
            "`retrieval_health.assertions_file` entry.",
        ),
        404: {
            "model": ErrorResponse,
            "description": (
                "`assertions_file_not_found`: the configured assertions file does not exist "
                "at its path in the vault-source store -- under `storage_root` on the "
                "filesystem binding, or in the vault's document-store folder on the "
                "document-store binding.\n\n"
                "`vault_not_found`: no vault registered with that id; `detail.available_vaults` "
                "lists the registered vaults."
            ),
        },
        502: {
            "model": ErrorResponse,
            "description": (
                "`vault_source_store_refused`: the vault-source store declined the "
                "operation on its merits -- quota, a permission it withdrew, a reply "
                "that could not be used. Resolve it at the store before retrying; "
                "`detail.store_status` carries the status it declined with."
            ),
        },
        503: {
            "model": ErrorResponse,
            "description": (
                "`vault_source_store_unavailable`: the vault-source store declined to "
                "serve the operation just now -- throttling, or a transient backend "
                "signal. The same request may succeed on a later attempt."
            ),
        },
    },
)
async def verify_vault_retrieval(
    vault_id: VaultIdStr = Depends(get_vault_id),
    service: UtilitiesService = Depends(get_utilities_service),
) -> EvalRetrievalResult:
    return await service.eval_retrieval()


@router.post(
    "/refresh-views",
    response_model=RefreshViewsResponse,
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
async def refresh_views(
    vault_id: VaultIdStr = Depends(get_vault_id),
    service: UtilitiesService = Depends(get_utilities_service),
) -> RefreshViewsResponse:
    return await service.refresh_views()
