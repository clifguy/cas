"""Graph operations: link, check_preconditions, traverse, chain."""

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
    LinkRequest,
    LinkResponse,
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
    response_model=LinkResponse,
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
                "resolve to the same document.\n\n"
                "`synced_from_inapplicable_edge_type` (T-0111): "
                "`synced_from_version` or `synced_from_content_hash` was set "
                "on an edge type other than `sync_target` or `derived_from`. "
                "Provenance fields are only meaningful on those two types.\n\n"
                "`synced_from_version_not_in_source_chain` (T-0111): "
                "`synced_from_version` references a document that is not a "
                "member of `target_id`'s supersedes chain (covers both "
                "wrong-document and missing-document cases — surfaced as "
                "this dedicated code so operators can distinguish them "
                "from `document_not_found`)."
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
) -> LinkResponse:
    return await service.link(request)


@router.post(
    "/edges/bulk",
    response_model=BulkLinkResponse,
    description=(
        "Create many edges in one call (T-0165, CAS-ADR-029). Each item "
        "is dispatched through the idempotent variant of `link`: a "
        "duplicate natural-key triple (source_id, target_id, edge_type) "
        "returns the existing edge with `created=false` rather than "
        "raising (T-0079). Items are processed in order under the "
        "process-wide link lock and per-item SQLite transactions. The "
        "batch is NOT atomic (CAS-ADR-029): a per-item SAGEError "
        "surfaces in the response's per-item error envelope while "
        "earlier-or-later successful items remain committed. The "
        "endpoint returns 200 even when some items fail; check "
        "`success_count` / `error_count` on the response. Request body "
        "accepts an optional `response_mode` (`light` | `full`) per "
        "T-0153 / T-0158: `light` drops the per-item `edge` body from "
        "success entries to stay within the inline-output budget; "
        "failure entries always carry the full structured error "
        "envelope. The `created` and `existing_rationale` fields are "
        "preserved under `light` because they are the only signals "
        "callers have for the natural-key idempotency outcome. When "
        "unset, batches with more than 5 items default to `light`, "
        "smaller batches default to `full`."
    ),
    responses={
        404: {
            "model": ErrorResponse,
            "description": "`vault_not_found`: no vault registered with that id.",
        },
    },
)
async def bulk_link(
    request: BulkLinkRequest,
    vault_id: VaultIdStr = Depends(get_vault_id),
    service: GraphOpsService = Depends(get_graph_ops_service),
) -> BulkLinkResponse:
    return await service.bulk_link(request)


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
    # T-0152: dry_run is a query parameter (DELETE endpoints have no
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
