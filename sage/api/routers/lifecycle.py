"""Lifecycle endpoints (CAS-ADR-029 v4 plural-noun convention):
- POST /sage_vaults/{vault_id}/lifecycles -- update_lifecycles (N>=1 items).
"""

from fastapi import APIRouter, Depends

from sage.api.dependencies import get_lifecycle_service, get_vault_id
from sage.api.response_docs import boundary_400, invalid_parameter_422
from sage.api.wire_route import WireRoute
from sage.models.schemas import (
    BulkLifecycleRequest,
    BulkLifecycleResponse,
    ErrorResponse,
    VaultIdStr,
)
from sage.services.lifecycle import LifecycleService

router = APIRouter(route_class=WireRoute, tags=["Document Lifecycle"])


@router.post(
    "/lifecycles",
    response_model=BulkLifecycleResponse,
    responses={
        400: boundary_400(
            path=("invalid_vault_id",),
            request=(
                "invalid_document_id",
                "invalid_sha256",
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
        422: invalid_parameter_422(),
    },
)
async def update_lifecycles(
    request: BulkLifecycleRequest,
    vault_id: VaultIdStr = Depends(get_vault_id),
    lifecycle_service: LifecycleService = Depends(get_lifecycle_service),
) -> BulkLifecycleResponse:
    return await lifecycle_service.bulk_set_lifecycle(request)
