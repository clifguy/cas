"""Maintenance/admin router (CAS-ADR-029).

Pilot operation: POST /sage_vaults/{vault_id}/admin/migrate. The first
operation on the SAGE Core API maintenance surface; subsequent
``sage_admin_*`` operations are added here with the same three-layer
shape.
"""

from collections.abc import AsyncGenerator

from fastapi import APIRouter, Body, Depends
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from sage.api.dependencies import get_maintenance_service, get_vault_id
from sage.models.schemas import (
    DriftReport,
    ErrorResponse,
    MigrationReport,
    ReabstractRequest,
    VaultIdStr,
)
from sage.services.maintenance import MaintenanceService, ReabstractEvent

router = APIRouter(tags=["Maintenance"])


def _sse_event(event: BaseModel) -> str:
    """Format an SSE ``data:`` line from a Pydantic event.

    Mirrors the helper at app/backend/ingest_streaming_service.py:48-55.
    ``exclude_none=True`` preserves the wire convention of omitting
    optional fields (e.g., ``outcome`` on the leading ``started``
    progress event, ``error`` on non-failed events, ``elapsed_seconds``
    on the leading ``started`` event and on ``skipped`` events).
    """
    return f"data: {event.model_dump_json(exclude_none=True)}\n\n"


async def _format_reabstract_stream(
    events: AsyncGenerator[ReabstractEvent, None],
) -> AsyncGenerator[str, None]:
    """Adapter that wraps the service-layer event generator in SSE wire
    format. Keeps the route handler one line.
    """
    async for event in events:
        yield _sse_event(event)


@router.post(
    "/admin/migrate",
    response_model=MigrationReport,
    responses={
        404: {
            "model": ErrorResponse,
            "description": "`vault_not_found`: no vault registered with that id.",
        },
    },
)
async def admin_migrate_vault(
    vault_id: VaultIdStr = Depends(get_vault_id),
    service: MaintenanceService = Depends(get_maintenance_service),
) -> MigrationReport:
    return await service.migrate_vault()


@router.post(
    "/admin/detect-drift",
    response_model=DriftReport,
    responses={
        404: {
            "model": ErrorResponse,
            "description": "`vault_not_found`: no vault registered with that id.",
        },
    },
)
async def admin_detect_drift(
    vault_id: VaultIdStr = Depends(get_vault_id),
    service: MaintenanceService = Depends(get_maintenance_service),
) -> DriftReport:
    return await service.detect_drift()


@router.post(
    "/admin/reabstract-deferred",
    responses={
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
            "description": "`vault_not_found`: no vault registered with that id.",
        },
        409: {
            "model": ErrorResponse,
            "description": (
                "`reabstract_already_in_flight`: a reabstract is already running on this vault."
            ),
        },
    },
)
async def admin_reabstract_deferred(
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
