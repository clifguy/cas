"""CAS Application backend router (BE-017 through BE-035).

POST /app/scan -- directory scan with filename parsing
POST /app/ingest -- batch ingest with edge inference and SSE streaming

Service resolution flows through the FastAPI ``Depends`` factories in
``app.backend.dependencies``, so each handler reduces to a
single-statement dispatch matching the SAGE F1 canonical shape.

Every operation here refuses a request name it does not declare, as the Core
API operations do (CAS-ADR-037, CAS-ADR-052). The refusal is attached to this
router rather than where an application includes it, because both the
co-located application and the standalone backend-for-frontend include it. The
sign-in routes are a separate router and do not carry it: an identity
provider's callback returns query parameters no operation declares.

Each route names its published operation id, which the refusal reports as the
operation it refused: the standalone backend-for-frontend serves no document
the specification's ids are overlaid on, so without it the refusal would name
a framework-generated one there.
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
from sage.api.dependencies import refuse_undeclared_parameters
from sage.api.response_docs import boundary_400
from sage.api.wire_route import WireRoute

router = APIRouter(
    route_class=WireRoute,
    prefix="/app",
    tags=["app"],
    dependencies=[Depends(refuse_undeclared_parameters)],
)


@router.post(
    "/scan",
    operation_id="list_directory",
    response_model=ScanResponse,
    responses={
        400: boundary_400(
            request=("invalid_vault_id", "unknown_parameter"),
            extra="`invalid_directory`: `directory` does not exist or is not readable.",
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
async def scan_endpoint(
    body: ScanRequest,
    service: ScanService = Depends(get_scan_service),
) -> ScanResponse:
    """Scan a directory and return files with status and parsed metadata."""
    return await service.scan(body)


@router.post(
    "/ingest",
    operation_id="bulk_ingest_document",
    responses={
        400: boundary_400(
            request=(
                "invalid_vault_id",
                "invalid_document_date",
                "unknown_parameter",
                "undeclared_key",
            ),
            extra=(
                "`empty_file_list`: `files` was empty. Choose at least one file or skip the call."
            ),
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
async def ingest_endpoint(
    body: IngestRequest,
    service: IngestStreamingService = Depends(get_ingest_streaming_service),
) -> StreamingResponse:
    """Batch ingest with two-phase edge inference, streamed via SSE."""
    return service.stream(body)
