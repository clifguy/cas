"""Graph operations: link, check_preconditions, traverse, discover stub."""

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from sage.api.dependencies import get_graph_ops_service, get_vault_id
from sage.models.schemas import (
    Edge,
    LinkRequest,
    PreconditionResult,
    TraverseRequest,
    TraverseResponse,
)
from sage.services.graph_ops import GraphOpsService

router = APIRouter(tags=["Graph Operations"])


@router.post("/edges", response_model=Edge, status_code=201)
async def link(
    request: LinkRequest,
    vault_id: str = Depends(get_vault_id),
    service: GraphOpsService = Depends(get_graph_ops_service),
) -> Edge:
    return await service.link(request)


@router.get("/preconditions/{function_id}", response_model=PreconditionResult)
async def check_preconditions(
    function_id: str,
    vault_id: str = Depends(get_vault_id),
    service: GraphOpsService = Depends(get_graph_ops_service),
) -> PreconditionResult:
    return await service.check_preconditions(function_id)


@router.post("/traverse", response_model=TraverseResponse)
async def traverse(
    request: TraverseRequest,
    vault_id: str = Depends(get_vault_id),
    service: GraphOpsService = Depends(get_graph_ops_service),
) -> TraverseResponse:
    return await service.traverse(request)


@router.post("/discover")
async def discover(
    request: dict,
    vault_id: str = Depends(get_vault_id),
    service: GraphOpsService = Depends(get_graph_ops_service),
) -> JSONResponse:
    """Minimal stub: gates deterministic mode on pipeline status (BH-021).

    Full discover implementation replaces this in Slice 3.
    """
    mode = request.get("mode")
    document_id = request.get("document_id")

    if mode == "deterministic" and document_id:
        await service.check_pipeline_for_retrieval(document_id)

    return JSONResponse(
        status_code=501,
        content={"code": "not_implemented", "message": "Discover not yet implemented"},
    )
