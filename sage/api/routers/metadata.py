"""PATCH /sage_vaults/{vault_id}/documents/{document_id}/metadata -- update_metadata."""

from fastapi import APIRouter, Depends

from sage.api.dependencies import get_metadata_service, get_vault_id
from sage.models.schemas import Document, DocumentIdStr, UpdateMetadataRequest, VaultIdStr
from sage.services.metadata import MetadataService

router = APIRouter(tags=["Document Metadata"])


@router.patch(
    "/documents/{document_id}/metadata",
    response_model=Document,
)
async def update_metadata(
    document_id: DocumentIdStr,
    request: UpdateMetadataRequest,
    vault_id: VaultIdStr = Depends(get_vault_id),
    metadata_service: MetadataService = Depends(get_metadata_service),
) -> Document:
    # In Phase 1, use vault owner as modifier. In production, extract from auth.
    return await metadata_service.update_metadata(document_id, request, modified_by="system")
