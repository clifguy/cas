"""Document metadata endpoints (CAS-ADR-029 v4 plural-noun convention):
- POST /sage_vaults/{vault_id}/metadata -- update_metadata (N>=1 items).
"""

from fastapi import APIRouter, Depends

from sage.api.dependencies import get_metadata_service, get_vault_id
from sage.api.response_docs import boundary_400, invalid_parameter_422
from sage.api.wire_route import WireRoute
from sage.models.schemas import (
    BulkMetadataRequest,
    BulkMetadataResponse,
    ErrorResponse,
    VaultIdStr,
)
from sage.services.metadata import MetadataService

router = APIRouter(route_class=WireRoute, tags=["Document Metadata"])


@router.post(
    "/metadata",
    response_model=BulkMetadataResponse,
    responses={
        400: boundary_400(
            path=("invalid_vault_id",),
            request=(
                "invalid_document_date",
                "invalid_document_id",
                "unknown_parameter",
                "undeclared_key",
            ),
        ),
        404: {
            "model": ErrorResponse,
            "description": (
                "`vault_not_found`: no vault registered with that id; "
                "`detail.available_vaults` lists the registered vaults."
            ),
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
        422: invalid_parameter_422(),
    },
)
async def update_metadata(
    request: BulkMetadataRequest,
    vault_id: VaultIdStr = Depends(get_vault_id),
    metadata_service: MetadataService = Depends(get_metadata_service),
) -> BulkMetadataResponse:
    return await metadata_service.bulk_update_metadata(request, modified_by="system")
