"""Document metadata endpoints (CAS-ADR-029 v4 plural-noun convention):
- POST /sage_vaults/{vault_id}/metadata -- update_metadata (N>=1 items).
"""

from fastapi import APIRouter, Depends

from sage.api.dependencies import get_metadata_service, get_vault_id
from sage.models.schemas import (
    BulkMetadataRequest,
    BulkMetadataResponse,
    ErrorResponse,
    VaultIdStr,
)
from sage.services.metadata import MetadataService

router = APIRouter(tags=["Document Metadata"])


@router.post(
    "/metadata",
    response_model=BulkMetadataResponse,
    description=(
        "Patch mutable metadata fields on one or more documents in a single "
        "call (CAS-ADR-029 v4 plural-noun convention). Accepts an `items` "
        "array of N>=1 per-item patch requests; length-1 is fully supported. "
        "Each item is processed under its own per-document lock and per-item "
        "database transaction. The batch is NOT atomic: a per-item SAGEError "
        "surfaces in the per-item error envelope while earlier-or-later "
        "successful items remain committed. The endpoint returns 200 even "
        "when some items fail; check `success_count` / `error_count` on the "
        "response. Request body accepts an optional `response_mode` "
        "(`light` | `full`): `light` drops the per-item `document` body "
        "from success entries to stay within the inline-output budget; "
        "failure entries always carry the full structured error envelope. "
        "When unset, batches with more than 5 items default to `light`, "
        "smaller batches default to `full`."
    ),
    responses={
        404: {
            "model": ErrorResponse,
            "description": "`vault_not_found`: no vault registered with that id.",
        },
        409: {
            "model": ErrorResponse,
            "description": (
                "`stale_read`: a per-item `expected_version` does not match "
                "the target document's current version (CAS-ADR-038 Primitive "
                "B). Detail carries `document_id`, `expected_version`, and "
                "`current_version`."
            ),
        },
    },
)
async def update_metadata(
    request: BulkMetadataRequest,
    vault_id: VaultIdStr = Depends(get_vault_id),
    metadata_service: MetadataService = Depends(get_metadata_service),
) -> BulkMetadataResponse:
    return await metadata_service.bulk_update_metadata(request, modified_by="system")
