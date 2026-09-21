"""Pending metadata endpoint.

GET /sage_vaults/{vault_id}/pending-metadata -- documents awaiting metadata
    confirmation (BE-014, BE-015).
"""

from fastapi import APIRouter, Depends, Query

from sage.api.dependencies import get_metadata_service, get_vault_id
from sage.api.response_docs import boundary_400, invalid_parameter_422
from sage.api.wire_route import WireRoute
from sage.models.enums import ResponseMode
from sage.models.schemas import ErrorResponse, PendingMetadataPage, VaultIdStr
from sage.services.metadata import (
    PENDING_METADATA_DEFAULT_LIMIT,
    PENDING_METADATA_MAX_LIMIT,
    MetadataService,
)

router = APIRouter(route_class=WireRoute, tags=["pending_metadata"])


@router.get(
    "/pending-metadata",
    response_model=PendingMetadataPage,
    responses={
        400: boundary_400(path=("invalid_vault_id",), request=("unknown_parameter",)),
        404: {
            "model": ErrorResponse,
            "description": (
                "`vault_not_found`: no vault registered with that id; `detail.available_vaults` "
                "lists the registered vaults."
            ),
        },
        422: invalid_parameter_422(),
    },
)
async def list_pending_metadata(
    limit: int = Query(default=PENDING_METADATA_DEFAULT_LIMIT, ge=0, le=PENDING_METADATA_MAX_LIMIT),
    offset: int = Query(default=0, ge=0),
    response_mode: ResponseMode | None = Query(default=None),
    vault_id: VaultIdStr = Depends(get_vault_id),
    service: MetadataService = Depends(get_metadata_service),
) -> PendingMetadataPage:
    """Return one page of the documents whose extracted metadata has not been confirmed."""
    return await service.list_pending_metadata(limit, offset, response_mode)
