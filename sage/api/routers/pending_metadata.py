"""Pending metadata endpoint.

GET /sage_vaults/{vault_id}/pending-metadata -- documents awaiting metadata
    confirmation (BE-014, BE-015).
"""

from fastapi import APIRouter, Depends

from sage.api.dependencies import get_metadata_service, get_vault_id
from sage.models.schemas import PendingMetadataItem, VaultIdStr
from sage.services.metadata import MetadataService

router = APIRouter(tags=["pending_metadata"])


@router.get("/pending-metadata", response_model=list[PendingMetadataItem])
async def list_pending_metadata(
    vault_id: VaultIdStr = Depends(get_vault_id),
    service: MetadataService = Depends(get_metadata_service),
) -> list[PendingMetadataItem]:
    """Return documents whose extracted metadata has not been confirmed."""
    return await service.list_pending_metadata()
