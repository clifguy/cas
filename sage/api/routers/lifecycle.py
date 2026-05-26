"""Lifecycle endpoints:
- POST /sage_vaults/{vault_id}/documents/{document_id}/lifecycle -- set_lifecycle
- POST /sage_vaults/{vault_id}/lifecycle/bulk -- bulk_set_lifecycle
"""

from fastapi import APIRouter, Depends

from sage.api.dependencies import get_lifecycle_service, get_vault_id
from sage.models.schemas import (
    BulkLifecycleRequest,
    BulkLifecycleResponse,
    DocumentIdStr,
    ErrorResponse,
    SetLifecycleRequest,
    SetLifecycleResponse,
    VaultIdStr,
)
from sage.services.lifecycle import LifecycleService

router = APIRouter(tags=["Document Lifecycle"])


@router.post(
    "/documents/{document_id}/lifecycle",
    response_model=SetLifecycleResponse,
    responses={
        400: {
            "model": ErrorResponse,
            "description": (
                "`invalid_action`: the `action` string is not in any "
                "transition table for this vault."
            ),
        },
        403: {
            "model": ErrorResponse,
            "description": "Caller is not a registered editor for this document.",
        },
        404: {
            "model": ErrorResponse,
            "description": "Vault or document not found.",
        },
        409: {
            "model": ErrorResponse,
            "description": (
                "`invalid_lifecycle_transition`: action is known but not "
                "valid from the document's current lifecycle state. Detail "
                "includes `current_state`, `attempted_action`, and "
                "`valid_actions` (the list of actions valid from the current "
                "state).\n\n"
                "`supersede_target_not_active`: `action=supersede` was "
                "requested against a document not in `active`. Run the "
                "archive → reactivate dance first.\n\n"
                "`pipeline_incomplete`: emitted by some transitions (e.g. "
                "`complete`) when the document's `pipeline_status` is not yet "
                "terminal.\n\n"
                "`identical_content_supersede`: `action=supersede` with "
                "`successor_id` whose content hash matches the "
                "predecessor's; supersede chains require distinct content "
                "per step."
            ),
        },
    },
)
async def set_lifecycle(
    document_id: DocumentIdStr,
    request: SetLifecycleRequest,
    vault_id: VaultIdStr = Depends(get_vault_id),
    lifecycle_service: LifecycleService = Depends(get_lifecycle_service),
) -> SetLifecycleResponse:
    return await lifecycle_service.set_lifecycle(document_id, request)


@router.post(
    "/lifecycle/bulk",
    response_model=BulkLifecycleResponse,
    description=(
        "Apply one lifecycle transition per item; per-item per-document lock "
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
async def bulk_set_lifecycle(
    request: BulkLifecycleRequest,
    vault_id: VaultIdStr = Depends(get_vault_id),
    lifecycle_service: LifecycleService = Depends(get_lifecycle_service),
) -> BulkLifecycleResponse:
    return await lifecycle_service.bulk_set_lifecycle(request)
