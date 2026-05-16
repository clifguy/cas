"""PATCH /sage_vaults/{vault_id}/documents/{document_id}/metadata -- update_metadata."""

from fastapi import APIRouter, Depends

from sage.api.dependencies import get_metadata_service, get_vault_id
from sage.models.schemas import (
    Document,
    DocumentIdStr,
    ErrorResponse,
    UpdateMetadataRequest,
    VaultIdStr,
)
from sage.services.metadata import MetadataService

router = APIRouter(tags=["Document Metadata"])


@router.patch(
    "/documents/{document_id}/metadata",
    response_model=Document,
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
) -> Document:
    # In Phase 1, use vault owner as modifier. In production, extract from auth.
    return await metadata_service.update_metadata(document_id, request, modified_by="system")
