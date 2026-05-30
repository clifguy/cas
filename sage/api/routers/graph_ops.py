"""Graph operations (CAS-ADR-029 v4 plural-noun convention):
- POST /sage_vaults/{vault_id}/edges -- create_edges (N>=1 items).
- DELETE /sage_vaults/{vault_id}/edges/{edge_id} -- delete one edge.
- GET /sage_vaults/{vault_id}/preconditions/{function_id} -- check_preconditions.
- POST /sage_vaults/{vault_id}/traverse -- traverse.
- POST /sage_vaults/{vault_id}/chain -- chain.
"""

from fastapi import APIRouter, Depends

from sage.api.dependencies import get_graph_ops_service, get_vault_id
from sage.models.schemas import (
    BulkLinkRequest,
    BulkLinkResponse,
    ChainRequest,
    ChainResponse,
    EdgeIdStr,
    ErrorResponse,
    FunctionIdStr,
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
    response_model=BulkLinkResponse,
    status_code=200,
    description=(
        "Create one or more typed edges in a single call (CAS-ADR-029 v4 "
        "plural-noun convention). Accepts an `items` array of N>=1 per-item "
        "edge specifications; length-1 is fully supported. Each item is "
        "dispatched through the idempotent variant: a duplicate natural-key "
        "triple (source_id, target_id, edge_type) returns the existing edge "
        "with `created=false` rather than raising. Items are processed in "
        "order under the process-wide link lock and per-item SQLite "
        "transactions. The batch is NOT atomic: a per-item SAGEError "
        "surfaces in the per-item error envelope while earlier-or-later "
        "successful items remain committed. The endpoint returns 200 even "
        "when some items fail; check `success_count` / `error_count` on "
        "the response. Request body accepts an optional `response_mode` "
        "(`light` | `full`): `light` drops the per-item `edge` body from "
        "success entries to stay within the inline-output budget; failure "
        "entries always carry the full structured error envelope. The "
        "`created` and `existing_rationale` fields are preserved under "
        "`light` because they are the only signals callers have for the "
        "natural-key idempotency outcome. When unset, batches with more "
        "than 5 items default to `light`, smaller batches default to "
        "`full`."
    ),
    responses={
        404: {
            "model": ErrorResponse,
            "description": "`vault_not_found`: no vault registered with that id.",
        },
    },
)
async def create_edges(
    request: BulkLinkRequest,
    vault_id: VaultIdStr = Depends(get_vault_id),
    service: GraphOpsService = Depends(get_graph_ops_service),
) -> BulkLinkResponse:
    return await service.create_edges(request)


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
    dry_run: bool = False,
    vault_id: VaultIdStr = Depends(get_vault_id),
    service: GraphOpsService = Depends(get_graph_ops_service),
) -> UnlinkResponse:
    # Dry_run is a query parameter (DELETE endpoints have no
    # body; the schema-side convention used for other tools doesn't
    # apply). When true, the edge-existence check still runs and
    # raises edge_not_found, but no delete is performed.
    return await service.unlink(edge_id, dry_run=dry_run)


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
