"""POST /sage_vaults/{vault_id}/documents/{document_id}/lifecycle -- set_lifecycle."""

from fastapi import APIRouter, Depends

from sage.api.dependencies import get_lifecycle_service, get_vault_id
from sage.models.schemas import (
    DocumentIdStr,
    SetLifecycleRequest,
    SetLifecycleResponse,
    VaultIdStr,
)
from sage.services.lifecycle import LifecycleService

router = APIRouter(tags=["Document Lifecycle"])


@router.post(
    "/documents/{document_id}/lifecycle",
    response_model=SetLifecycleResponse,
)
async def set_lifecycle(
    document_id: DocumentIdStr,
    request: SetLifecycleRequest,
    vault_id: VaultIdStr = Depends(get_vault_id),
    lifecycle_service: LifecycleService = Depends(get_lifecycle_service),
) -> SetLifecycleResponse:
    return await lifecycle_service.set_lifecycle(document_id, request)
