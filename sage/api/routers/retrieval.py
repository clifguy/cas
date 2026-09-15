"""Retrieval router: discover endpoint (semantic + deterministic modes)."""

from fastapi import APIRouter, Depends

from sage.api.dependencies import get_retrieval_service, get_vault_id
from sage.api.response_docs import boundary_400
from sage.models.schemas import DiscoverRequest, DiscoverResponse, ErrorResponse, VaultIdStr
from sage.services.retrieval import RetrievalService

router = APIRouter(tags=["Retrieval"])


@router.post(
    "/discover",
    response_model=DiscoverResponse,
    responses={
        400: boundary_400(
            path=("invalid_vault_id",),
            request=("invalid_document_id", "unknown_parameter"),
            extra="Invalid parameters (unknown mode, scope, or filter field), or "
            "`mode_parameter_mismatch` when a parameter is set that the chosen "
            "mode or the chosen target forbids. Its detail carries `mode`, "
            "`target`, `forbidden_param`, and the allowed set for whichever "
            "axis the constraint is on -- `allowed_modes` or `allowed_targets`, "
            "never both.",
        ),
        404: {
            "model": ErrorResponse,
            "description": (
                "Document not found (for specific/deterministic scope).\n\n"
                "`vault_not_found`: no vault registered with that id; `detail.available_vaults` "
                "lists the registered vaults."
            ),
        },
    },
)
async def discover(
    request: DiscoverRequest,
    vault_id: VaultIdStr = Depends(get_vault_id),
    service: RetrievalService = Depends(get_retrieval_service),
) -> DiscoverResponse:
    return await service.discover(request)
