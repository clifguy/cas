"""Document metadata endpoints:
- PATCH /sage_vaults/{vault_id}/documents/{document_id}/metadata -- update_metadata.
- POST /sage_vaults/{vault_id}/metadata/bulk -- bulk_update_metadata.
"""

from fastapi import APIRouter, Depends

from sage.api.dependencies import get_metadata_service, get_vault_id
from sage.models.schemas import (
    BulkMetadataRequest,
    BulkMetadataResponse,
    DocumentIdStr,
    ErrorResponse,
    UpdateMetadataRequest,
    UpdateMetadataResponse,
    VaultIdStr,
)
from sage.services.metadata import MetadataService

router = APIRouter(tags=["Document Metadata"])


@router.patch(
    "/documents/{document_id}/metadata",
    response_model=UpdateMetadataResponse,
    responses={
        400: {
            "model": ErrorResponse,
            "description": ("Invalid field name or value (e.g., unknown doc_type for this vault)."),
        },
        403: {
            "model": ErrorResponse,
            "description": "Caller is not a registered editor for this document.",
        },
        404: {
            "model": ErrorResponse,
            "description": "`document_not_found`: no document with that id; or vault not found.",
        },
    },
)
async def update_metadata(
    document_id: DocumentIdStr,
    request: UpdateMetadataRequest,
    vault_id: VaultIdStr = Depends(get_vault_id),
    metadata_service: MetadataService = Depends(get_metadata_service),
) -> UpdateMetadataResponse:
    # In Phase 1, use vault owner as modifier. In production, extract from auth.
    return await metadata_service.update_metadata(document_id, request, modified_by="system")


@router.post(
    "/metadata/bulk",
    response_model=BulkMetadataResponse,
    description=(
        "Apply one metadata patch per item; per-item per-document lock "
        "and per-item SQLite transaction. The batch is NOT atomic "
        "(CAS-ADR-029): a per-item SAGEError surfaces in the response's "
        "per-item error envelope while earlier-or-later successful items "
        "remain committed. The endpoint returns 200 even when some items "
        "fail; check ``success_count`` / ``error_count`` on the response. "
        "Request body accepts an optional ``response_mode`` (``light`` | "
        "``full``) per T-0153: ``light`` drops the per-item ``document`` "
        "body from success entries to stay within the inline-output "
        "budget; failure entries always carry the full structured error "
        "envelope. When unset, batches with more than 5 items default to "
        "``light``, smaller batches default to ``full``."
    ),
    responses={
        404: {
            "model": ErrorResponse,
            "description": "`vault_not_found`: no vault registered with that id.",
        },
    },
)
async def bulk_update_metadata(
    request: BulkMetadataRequest,
    vault_id: VaultIdStr = Depends(get_vault_id),
    metadata_service: MetadataService = Depends(get_metadata_service),
) -> BulkMetadataResponse:
    return await metadata_service.bulk_update_metadata(request, modified_by="system")
