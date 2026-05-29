"""Lifecycle endpoints (CAS-ADR-029 v4 plural-noun convention):
- POST /sage_vaults/{vault_id}/lifecycles -- update_lifecycles (N>=1 items).
"""

from fastapi import APIRouter, Depends

from sage.api.dependencies import get_lifecycle_service, get_vault_id
from sage.models.schemas import (
    BulkLifecycleRequest,
    BulkLifecycleResponse,
    ErrorResponse,
    VaultIdStr,
)
from sage.services.lifecycle import LifecycleService

router = APIRouter(tags=["Document Lifecycle"])


@router.post(
    "/lifecycles",
    response_model=BulkLifecycleResponse,
    description=(
        "Apply one or more lifecycle state transitions in a single call "
        "(CAS-ADR-029 v4 plural-noun convention). Accepts an `items` array "
        "of N>=1 per-item transition requests; length-1 is fully supported. "
        "Each item is processed under its own per-document lock and per-item "
        "SQLite transaction. The batch is NOT atomic: a per-item SAGEError "
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
    },
)
async def update_lifecycles(
    request: BulkLifecycleRequest,
    vault_id: VaultIdStr = Depends(get_vault_id),
    lifecycle_service: LifecycleService = Depends(get_lifecycle_service),
) -> BulkLifecycleResponse:
    return await lifecycle_service.bulk_set_lifecycle(request)
