"""POST /sage_vaults/{vault_id}/documents/{document_id}/lifecycle -- set_lifecycle."""

from fastapi import APIRouter, Depends

from sage.api.dependencies import get_lifecycle_service, get_vault_id
from sage.models.schemas import (
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
                "`new_version_id` whose content hash matches the "
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
