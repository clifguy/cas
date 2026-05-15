"""Graph operations: link, check_preconditions, traverse, chain."""

from fastapi import APIRouter, Depends

from sage.api.dependencies import get_graph_ops_service, get_vault_id
from sage.models.schemas import (
    ChainRequest,
    ChainResponse,
    Edge,
    EdgeIdStr,
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


@router.post("/edges", response_model=Edge, status_code=201)
async def link(
    request: LinkRequest,
    vault_id: VaultIdStr = Depends(get_vault_id),
    service: GraphOpsService = Depends(get_graph_ops_service),
) -> Edge:
    return await service.link(request)


@router.delete("/edges/{edge_id}", response_model=UnlinkResponse)
async def unlink(
    edge_id: EdgeIdStr,
    vault_id: VaultIdStr = Depends(get_vault_id),
    service: GraphOpsService = Depends(get_graph_ops_service),
) -> UnlinkResponse:
    return await service.unlink(edge_id)


@router.get("/preconditions/{function_id}", response_model=PreconditionResult)
async def check_preconditions(
    function_id: FunctionIdStr,
    vault_id: VaultIdStr = Depends(get_vault_id),
    service: GraphOpsService = Depends(get_graph_ops_service),
) -> PreconditionResult:
    return await service.check_preconditions(function_id)


@router.post("/traverse", response_model=TraverseResponse)
async def traverse(
    request: TraverseRequest,
    vault_id: VaultIdStr = Depends(get_vault_id),
    service: GraphOpsService = Depends(get_graph_ops_service),
) -> TraverseResponse:
    return await service.traverse(request)


@router.post("/chain", response_model=ChainResponse)
async def chain(
    request: ChainRequest,
    vault_id: VaultIdStr = Depends(get_vault_id),
    service: GraphOpsService = Depends(get_graph_ops_service),
) -> ChainResponse:
    return await service.chain(request)
