"""CAS Application backend router (BE-017 through BE-035).

POST /app/scan -- directory scan with filename parsing
POST /app/ingest -- batch ingest with edge inference and SSE streaming

Service resolution flows through the FastAPI ``Depends`` factories in
``app.backend.dependencies`` (T-0049), so each handler reduces to a
single-statement dispatch matching the SAGE F1 canonical shape.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse

from app.backend.dependencies import (
    get_ingest_streaming_service,
    get_scan_service,
)
from app.backend.ingest_streaming_service import IngestStreamingService
from app.backend.models import (
    ErrorResponse,
    IngestRequest,
    ScanRequest,
    ScanResponse,
)
from app.backend.scan_service import ScanService

router = APIRouter(prefix="/app", tags=["app"])


@router.post(
    "/scan",
    response_model=ScanResponse,
    responses={
        400: {
            "model": ErrorResponse,
            "description": ("`invalid_directory`: `directory` does not exist or is not readable."),
        },
        404: {
            "model": ErrorResponse,
            "description": "`vault_not_found`: no vault with that id.",
        },
    },
)
async def scan_endpoint(
    body: ScanRequest,
    service: ScanService = Depends(get_scan_service),
) -> ScanResponse:
    """Scan a directory and return files with status and parsed metadata."""
    return await service.scan(body)


@router.post(
    "/ingest",
    responses={
        400: {
            "model": ErrorResponse,
            "description": (
                "`empty_file_list`: `files` was empty. Choose at least one file or skip the call."
            ),
        },
        404: {
            "model": ErrorResponse,
            "description": "`vault_not_found`: no vault with that id.",
        },
    },
)
async def ingest_endpoint(
    body: IngestRequest,
    service: IngestStreamingService = Depends(get_ingest_streaming_service),
) -> StreamingResponse:
    """Batch ingest with two-phase edge inference, streamed via SSE."""
    return service.stream(body)
