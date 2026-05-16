"""Graph operations: link, check_preconditions, traverse, chain."""

from fastapi import APIRouter, Depends

from sage.api.dependencies import get_graph_ops_service, get_vault_id
from sage.models.schemas import (
    ChainRequest,
    ChainResponse,
    Edge,
    EdgeIdStr,
    ErrorResponse,
    FunctionIdStr,
    LinkRequest,
    PreconditionResult,
    TraverseRequest,
    TraverseResponse,
    UnlinkResponse,
    VaultIdStr,
)
from sage.services.graph_ops import GraphOpsService

router = APIRouter(tags=["Graph Operations"])


@router.post(
    "/edges",
    response_model=Edge,
    status_code=201,
    responses={
        400: {
            "model": ErrorResponse,
            "description": (
                "One of the following error codes:\n\n"
                "`edge_anchor_policy_violation`: anchor field missing where "
                "required, present where forbidden, or pointing at a "
                "document not in the endpoint's supersedes lineage.\n\n"
                "`retract_target_not_edge`: `retracted_edge_id` does not "
                "identify an existing edge in this vault.\n\n"
                "`merged_from_validation`: a `merged_from` edge violates the "
                "merge-tombstone invariants (chain-head / chain-terminal "
                "validation failed).\n\n"
                "`tbd_policy_edge`: the requested `edge_type` has no shipped "
                "resolution policy and cannot be created.\n\n"
                "`self_referential_edge`: `source_id` and `target_id` "
                "resolve to the same document."
            ),
        },
        403: {
            "model": ErrorResponse,
            "description": "Caller is not a registered editor for the source document.",
        },
        404: {
            "model": ErrorResponse,
            "description": "Vault, source document, or target document not found.",
        },
    },
)
async def link(
    request: LinkRequest,
    vault_id: VaultIdStr = Depends(get_vault_id),
    service: GraphOpsService = Depends(get_graph_ops_service),
) -> Edge:
    return await service.link(request)


@router.delete(
    "/edges/{edge_id}",
    response_model=UnlinkResponse,
    responses={
        404: {
            "model": ErrorResponse,
            "description": (
                "`edge_not_found`: no production edge with that id; or vault not found."
            ),
        },
    },
)
async def unlink(
    edge_id: EdgeIdStr,
    vault_id: VaultIdStr = Depends(get_vault_id),
    service: GraphOpsService = Depends(get_graph_ops_service),
) -> UnlinkResponse:
    return await service.unlink(edge_id)


@router.get(
    "/preconditions/{function_id}",
    response_model=PreconditionResult,
    responses={
        404: {
            "model": ErrorResponse,
            "description": ("`document_not_found`: no document with that id; or vault not found."),
        },
    },
)
async def check_preconditions(
    function_id: FunctionIdStr,
    vault_id: VaultIdStr = Depends(get_vault_id),
    service: GraphOpsService = Depends(get_graph_ops_service),
) -> PreconditionResult:
    return await service.check_preconditions(function_id)


@router.post(
    "/traverse",
    response_model=TraverseResponse,
    responses={
        400: {
            "model": ErrorResponse,
            "description": "Invalid edge type or direction.",
        },
        404: {
            "model": ErrorResponse,
            "description": "Vault or starting document not found.",
        },
    },
)
async def traverse(
    request: TraverseRequest,
    vault_id: VaultIdStr = Depends(get_vault_id),
    service: GraphOpsService = Depends(get_graph_ops_service),
) -> TraverseResponse:
    return await service.traverse(request)


@router.post(
    "/chain",
    response_model=ChainResponse,
    responses={
        400: {
            "model": ErrorResponse,
            "description": "Invalid edge type.",
        },
        404: {
            "model": ErrorResponse,
            "description": ("`document_not_found`: no document with that id; or vault not found."),
        },
    },
)
async def chain(
    request: ChainRequest,
    vault_id: VaultIdStr = Depends(get_vault_id),
    service: GraphOpsService = Depends(get_graph_ops_service),
) -> ChainResponse:
    return await service.chain(request)
