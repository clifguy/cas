"""Maintenance router (CAS-ADR-029).

Vault-scoped operations on the SAGE Core API maintenance surface, each with
the same three-layer shape (router -> service -> MCP tool registration). The
stack-scoped read sits with the cross-vault routes, since it names no vault.
The ``/maintenance/`` URL segment is canonical; the retired ``/admin/``
paths are not served.
"""

import json
from collections.abc import AsyncGenerator

from fastapi import APIRouter, Body, Depends
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from sage.api.dependencies import (
    get_maintenance_service,
    get_vault_id,
    get_vault_registry_service,
)
from sage.api.response_docs import boundary_400
from sage.models.schemas import (
    DriftReport,
    ErrorResponse,
    MigrationReport,
    OptimizeContentStoreReport,
    OptimizeContentStoreRequest,
    ReabstractRequest,
    ReloadVaultResponse,
    SourceFileIntegrityReport,
    SourceFileIntegrityRequest,
    SourceFileRestoreReport,
    SourceFileRestoreRequest,
    UploadRecipe,
    VaultIdStr,
)
from sage.models.wire import to_wire
from sage.services.maintenance import MaintenanceService, ReabstractEvent
from sage.services.vault_registry import VaultRegistryService

router = APIRouter(tags=["Maintenance"])


def _sse_event(event: BaseModel) -> str:
    """Format an SSE ``data:`` line from a Pydantic event.

    Mirrors the helper in ``sage.services.batch_ingest_stream`` and obeys
    the same rule: keys follow the published contract per field, so an
    optional field is omitted when null (``outcome`` on the leading
    ``started`` progress event, ``error`` on non-failed events,
    ``elapsed_seconds`` on ``started`` and on ``skipped`` events) while a
    required one keeps its key whatever its value.
    """
    return f"data: {json.dumps(to_wire(event), ensure_ascii=False)}\n\n"


async def _format_reabstract_stream(
    events: AsyncGenerator[ReabstractEvent, None],
) -> AsyncGenerator[str, None]:
    """Adapter that wraps the service-layer event generator in SSE wire
    format. Keeps the route handler one line.
    """
    async for event in events:
        yield _sse_event(event)


@router.post(
    "/maintenance/migrate",
    response_model=MigrationReport,
    responses={
        400: boundary_400(path=("invalid_vault_id",), request=("unknown_parameter",)),
        404: {
            "model": ErrorResponse,
            "description": (
                "`vault_not_found`: no vault registered with that id; "
                "`detail.available_vaults` lists the registered vaults."
            ),
        },
        409: {
            "model": ErrorResponse,
            "description": (
                "`pipeline_work_in_flight`: an ingest, reabstract or recompute is "
                "queued or running on the vault. Detail carries `vault_id`; retry "
                "once it has drained.\n\n"
                "`vault_migration_in_flight`: another `migrate_vault` is running "
                "on this vault. Detail carries `vault_id` and the time it started."
            ),
        },
    },
)
async def migrate_vault(
    vault_id: VaultIdStr = Depends(get_vault_id),
    service: MaintenanceService = Depends(get_maintenance_service),
) -> MigrationReport:
    return await service.migrate_vault()


@router.post(
    "/maintenance/reload",
    operation_id="reload_vault",
    response_model=ReloadVaultResponse,
    responses={
        400: boundary_400(
            path=("invalid_vault_id",),
            request=("unknown_parameter",),
            extra="`vault_config_validation_error`: the vault's declaration on the "
            "store is not valid YAML, or does not validate as a vault "
            "configuration. `detail.errors` names each problem to correct.",
        ),
        404: {
            "model": ErrorResponse,
            "description": (
                "`vault_not_found`: no vault registered with that id; "
                "`detail.available_vaults` lists the registered vaults."
            ),
        },
    },
)
async def reload_vault(
    vault_id: VaultIdStr = Depends(get_vault_id),
    service: VaultRegistryService = Depends(get_vault_registry_service),
) -> ReloadVaultResponse:
    return await service.reload_vault(vault_id)


@router.post(
    "/maintenance/detect-drift",
    response_model=DriftReport,
    responses={
        400: boundary_400(path=("invalid_vault_id",), request=("unknown_parameter",)),
        404: {
            "model": ErrorResponse,
            "description": (
                "`vault_not_found`: no vault registered with that id; "
                "`detail.available_vaults` lists the registered vaults."
            ),
        },
    },
)
async def detect_drift(
    vault_id: VaultIdStr = Depends(get_vault_id),
    service: MaintenanceService = Depends(get_maintenance_service),
) -> DriftReport:
    return await service.detect_drift()


@router.post(
    "/maintenance/reabstract-deferred",
    responses={
        400: boundary_400(path=("invalid_vault_id",), request=("unknown_parameter",)),
        200: {
            "content": {"text/event-stream": {}},
            "description": (
                "SSE stream: one ``progress`` event per per-document "
                "state transition (started + completed/failed/skipped), "
                "then one final ``summary`` event carrying the "
                "ReabstractReport payload. See sage.models.schemas."
                "ReabstractProgressEvent and ReabstractSummaryEvent."
            ),
        },
        404: {
            "model": ErrorResponse,
            "description": (
                "`vault_not_found`: no vault registered with that id; "
                "`detail.available_vaults` lists the registered vaults."
            ),
        },
        409: {
            "model": ErrorResponse,
            "description": (
                "`reabstract_already_in_flight`: a reabstract is already running on "
                "this vault.\n\n"
                "`vault_migration_in_flight`: `migrate_vault` is running on this "
                "vault. Detail carries `vault_id` and the migration's ISO 8601 "
                "`start_time`; retry once it has returned."
            ),
        },
    },
)
async def reabstract_deferred(
    body: ReabstractRequest = Body(default_factory=ReabstractRequest),
    vault_id: VaultIdStr = Depends(get_vault_id),
    service: MaintenanceService = Depends(get_maintenance_service),
) -> StreamingResponse:
    # Route now streams per-document SSE events instead of
    # returning a synchronous ReabstractReport JSON body. The
    # ``reabstract_deferred_events`` constructor performs the
    # in-flight check and None-ingestion guard synchronously, so a 409
    # ReabstractAlreadyInFlightError raises BEFORE the StreamingResponse
    # is constructed -- the FastAPI error-handling layer then returns
    # the existing application/json ErrorResponse envelope without any
    # SSE leaking. Mirrors the EmptyFileListError pattern at
    # app/backend/ingest_streaming_service.py:109-115.
    events = service.reabstract_deferred_events(include_pdf=body.include_pdf)
    return StreamingResponse(
        _format_reabstract_stream(events),
        media_type="text/event-stream",
    )


@router.post(
    "/maintenance/optimize-content-store",
    response_model=OptimizeContentStoreReport,
    responses={
        400: boundary_400(path=("invalid_vault_id",), request=("unknown_parameter",)),
        404: {
            "model": ErrorResponse,
            "description": (
                "`vault_not_found`: no vault registered with that id; "
                "`detail.available_vaults` lists the registered vaults."
            ),
        },
    },
)
async def optimize_content_store(
    body: OptimizeContentStoreRequest = Body(default_factory=OptimizeContentStoreRequest),
    vault_id: VaultIdStr = Depends(get_vault_id),
    service: MaintenanceService = Depends(get_maintenance_service),
) -> OptimizeContentStoreReport:
    return await service.optimize_content_store(
        cleanup_older_than_days=body.cleanup_older_than_days,
    )


@router.post(
    "/maintenance/verify-source-files",
    response_model=SourceFileIntegrityReport,
    responses={
        400: boundary_400(
            path=("invalid_vault_id",), request=("invalid_document_id", "unknown_parameter")
        ),
        404: {
            "model": ErrorResponse,
            "description": (
                "`vault_not_found`: no vault registered with that id; "
                "`detail.available_vaults` lists the registered vaults. "
                "`document_scope_unmatched`: `document_ids` names an id with no "
                "document in the vault; `detail.unmatched_ids` lists every such id."
            ),
        },
        502: {
            "model": ErrorResponse,
            "description": (
                "`vault_source_store_refused`: the vault-source store declined the "
                "operation on its merits -- quota, a permission it withdrew, a reply "
                "that could not be used. Resolve it at the store before retrying; "
                "`detail.store_status` carries the status it declined with."
            ),
        },
        503: {
            "model": ErrorResponse,
            "description": (
                "`vault_source_store_unavailable`: the vault-source store declined to "
                "serve the operation just now -- throttling, or a transient backend "
                "signal. The same request may succeed on a later attempt."
            ),
        },
    },
)
async def verify_vault_source_files(
    body: SourceFileIntegrityRequest = Body(default_factory=SourceFileIntegrityRequest),
    vault_id: VaultIdStr = Depends(get_vault_id),
    service: MaintenanceService = Depends(get_maintenance_service),
) -> SourceFileIntegrityReport:
    return await service.verify_vault_source_files(
        check_hashes=body.check_hashes, document_ids=body.document_ids
    )


@router.post(
    "/maintenance/restore-source-file",
    response_model=SourceFileRestoreReport | UploadRecipe,
    responses={
        200: {
            "description": (
                "A `SourceFileRestoreReport` for a restore that ran. For an "
                "absolute `source` on a machine the server cannot reach, an "
                "`UploadRecipe` (`status: upload_required`) instead: deliver "
                "the file to the recipe's URL with its token, then repeat the "
                "call with `transfer_token`."
            ),
        },
        400: boundary_400(
            path=("invalid_vault_id",),
            request=("invalid_document_id", "unknown_parameter"),
            extra="`ambiguous_ingest_source`: both `source` and `transfer_token` "
            "were supplied. "
            "`missing_ingest_source`: neither `source` nor `transfer_token` was "
            "supplied. "
            "`restore_provenance_mismatch`: the pinned document was not "
            "ingested from the delivered bytes. "
            "`restore_source_not_absolute`: the source path is not absolute. "
            "`vault_source_path_refused`: the document's source_path cannot be "
            "written at the path it names.",
        ),
        404: {
            "model": ErrorResponse,
            "description": (
                "`vault_not_found`: no vault registered with that id; "
                "`detail.available_vaults` lists the registered vaults. "
                "`restore_target_unresolved`: no single document claims the "
                "delivered bytes as its provenance. "
                "`document_not_found`: the supplied document_id names no document. "
                "`source_file_not_found`: the delivered source path does not exist."
            ),
        },
        409: {
            "model": ErrorResponse,
            "description": (
                "`transfer_not_staged`: `transfer_token` names a transfer whose "
                "bytes have not been delivered to the upload endpoint yet."
            ),
        },
        410: {
            "model": ErrorResponse,
            "description": (
                "`transfer_token_invalid`: `transfer_token` names no redeemable "
                "pending transfer -- unknown, expired, already spent, or scoped "
                "to another vault. Re-issue the originating call to mint a "
                "fresh recipe."
            ),
        },
        500: {
            "model": ErrorResponse,
            "description": (
                "`transfer_endpoint_not_configured`: an absolute `source` needs "
                "the caller-local transfer, but this deployment declares no "
                "public transfer endpoint, so no recipe can be minted."
            ),
        },
        502: {
            "model": ErrorResponse,
            "description": (
                "`vault_source_store_refused`: the vault-source store declined the "
                "operation on its merits -- quota, a permission it withdrew, a reply "
                "that opened no usable upload session. Resolve it at the store before "
                "retrying; `detail.store_status` carries the status it declined with."
            ),
        },
        503: {
            "model": ErrorResponse,
            "description": (
                "`vault_source_store_unavailable`: the vault-source store declined to "
                "serve the operation just now -- throttling, a transient backend "
                "signal, an upload session it expired. The same request may succeed "
                "on a later attempt."
            ),
        },
    },
)
async def restore_vault_source_file(
    body: SourceFileRestoreRequest,
    vault_id: VaultIdStr = Depends(get_vault_id),
    service: MaintenanceService = Depends(get_maintenance_service),
) -> SourceFileRestoreReport | UploadRecipe:
    return await service.restore_vault_source_file(
        source=body.source, document_id=body.document_id, transfer_token=body.transfer_token
    )
