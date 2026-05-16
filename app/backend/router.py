"""CAS Application backend router (BE-017 through BE-035).

POST /app/scan -- directory scan with filename parsing
POST /app/ingest -- batch ingest with edge inference and SSE streaming
"""

from __future__ import annotations

import asyncio
import logging
from typing import AsyncGenerator

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from app.backend.exceptions import EmptyFileListError
from app.backend.ingest_service import (
    BatchIngestService,
    FileDescriptor,
    ParsedMetadataInput,
)
from app.backend.models import (
    DocumentsCreated,
    ErrorResponse,
    IngestFileItem,
    IngestRequest,
    ProgressEvent,
    ScanRequest,
    ScanResponse,
    SummaryEvent,
)
from app.backend.scan_service import ScanService
from sage.api.errors import VaultNotFoundError

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/app", tags=["app"])


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _get_services(request: Request, vault_id: str):
    """Look up SAGEServices by vault_id from the app registry."""
    from sage.mcp_init import SAGEServices

    registry: dict[str, SAGEServices] = request.app.state.vault_registry
    if vault_id not in registry:
        raise VaultNotFoundError(vault_id)
    return registry[vault_id]


def _sse_event(event: BaseModel) -> str:
    """Format a Server-Sent Event from a Pydantic model.

    ``exclude_none=True`` preserves the wire convention of omitting
    optional fields (``document_id``/``error`` on progress events when
    not applicable, ``edge_warnings`` on summary events when empty).
    """
    return f"data: {event.model_dump_json(exclude_none=True)}\n\n"


def _to_file_descriptor(f: IngestFileItem) -> FileDescriptor:
    """Convert router's Pydantic model to service's FileDescriptor."""
    pm = f.parsed_metadata
    parsed = None
    if pm:
        parsed = ParsedMetadataInput(
            title=pm.title,
            date=pm.date,
            project=pm.project,
            codes=pm.codes,
            version=pm.version,
            doc_type=pm.doc_type,
        )
    return FileDescriptor(
        file_path=f.file_path,
        adapter=f.adapter,
        parsed_metadata=parsed,
    )


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


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
async def scan_endpoint(body: ScanRequest, request: Request) -> ScanResponse:
    """Scan a directory and return files with status and parsed metadata."""
    services = _get_services(request, body.vault_id)
    scan_service = ScanService(
        vault_config=services.config,
        graph_store=services.graph_store,
        ingestion_service=services.ingestion_service,
    )
    return await scan_service.scan(body)


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
async def ingest_endpoint(body: IngestRequest, request: Request):
    """Batch ingest with two-phase edge inference, streamed via SSE."""
    if not body.files:
        raise EmptyFileListError()

    services = _get_services(request, body.vault_id)

    async def event_stream() -> AsyncGenerator[str, None]:
        queue: asyncio.Queue[BaseModel | None] = asyncio.Queue()

        async def on_file_start(index: int, total_files: int, filename: str) -> None:
            await queue.put(
                ProgressEvent(
                    event_type="progress",
                    file_index=index,
                    total_files=total_files,
                    filename=filename,
                    stage="projection",
                    status="started",
                )
            )

        async def on_file_done(
            index: int,
            total_files: int,
            filename: str,
            document_id: str,
        ) -> None:
            await queue.put(
                ProgressEvent(
                    event_type="progress",
                    file_index=index,
                    total_files=total_files,
                    filename=filename,
                    stage="projection",
                    status="completed",
                    document_id=document_id,
                )
            )

        async def on_file_error(
            index: int,
            total_files: int,
            filename: str,
            error_message: str,
        ) -> None:
            logger.error("Failed to ingest %s: %s", filename, error_message)
            await queue.put(
                ProgressEvent(
                    event_type="progress",
                    file_index=index,
                    total_files=total_files,
                    filename=filename,
                    stage="projection",
                    status="failed",
                    error=error_message,
                )
            )

        async def run_service() -> None:
            descriptors = [_to_file_descriptor(f) for f in body.files]
            svc = BatchIngestService()
            result = await svc.run(
                files=descriptors,
                vault_services=services,
                infer_edges=body.infer_edges,
                on_file_start=on_file_start,
                on_file_done=on_file_done,
                on_file_error=on_file_error,
            )
            await queue.put(
                SummaryEvent(
                    event_type="summary",
                    documents_created=DocumentsCreated(
                        new=result.docs_new,
                        new_version=result.docs_version,
                    ),
                    metadata_pending=result.metadata_pending,
                    edges_created=result.edges_created,
                    edges_staged=result.edges_staged,
                    edges_removed=result.edges_removed,
                    edges_dropped=result.edges_dropped,
                    abstracts_generated=result.abstracts_generated,
                    abstracts_deferred=result.abstracts_deferred,
                    error_count=result.error_count,
                    errors=result.errors,
                    edge_warnings=result.edge_warnings if result.edge_warnings else None,
                )
            )
            await queue.put(None)  # sentinel

        task = asyncio.create_task(run_service())
        while True:
            event = await queue.get()
            if event is None:
                break
            yield _sse_event(event)
        await task

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
    )
