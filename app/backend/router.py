"""CAS Application backend router (BE-017 through BE-035).

POST /app/scan -- directory scan with filename parsing
POST /app/ingest -- batch ingest with edge inference and SSE streaming
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import AsyncGenerator

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from app.backend.ingest_service import (
    BatchIngestService,
    FileDescriptor,
    ParsedMetadataInput,
)
from app.backend.models import ParsedMetadata, ScanRequest, ScanResponse
from app.backend.scan_service import ScanService
from sage.api.errors import VaultNotFoundError
from sage.models.schemas import VaultIdStr

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/app", tags=["app"])


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------
#
# Scan-chain models live in app.backend.models. Ingest-chain models stay
# here pending their own typing pass.


class IngestFileItem(BaseModel):
    file_path: str
    adapter: str
    parsed_metadata: ParsedMetadata | None = None


class IngestRequest_(BaseModel):
    """Ingest request body (underscore to avoid collision with SAGE IngestRequest)."""

    vault_id: VaultIdStr
    files: list[IngestFileItem]
    infer_edges: bool = True


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


def _sse_event(data: dict) -> str:
    """Format a Server-Sent Event."""
    return f"data: {json.dumps(data)}\n\n"


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


@router.post("/scan", response_model=ScanResponse)
async def scan_endpoint(body: ScanRequest, request: Request) -> ScanResponse:
    """Scan a directory and return files with status and parsed metadata."""
    services = _get_services(request, body.vault_id)
    scan_service = ScanService(
        vault_config=services.config,
        graph_store=services.graph_store,
        ingestion_service=services.ingestion_service,
    )
    return await scan_service.scan(body)


@router.post("/ingest")
async def ingest_endpoint(body: IngestRequest_, request: Request):
    """Batch ingest with two-phase edge inference, streamed via SSE."""
    if not body.files:
        raise HTTPException(status_code=400, detail="No files selected for ingestion")

    services = _get_services(request, body.vault_id)

    async def event_stream() -> AsyncGenerator[str, None]:
        queue: asyncio.Queue[dict | None] = asyncio.Queue()

        async def on_file_start(index: int, total_files: int, filename: str) -> None:
            await queue.put(
                {
                    "event_type": "progress",
                    "file_index": index,
                    "total_files": total_files,
                    "filename": filename,
                    "stage": "projection",
                    "status": "started",
                }
            )

        async def on_file_done(
            index: int,
            total_files: int,
            filename: str,
            document_id: str,
        ) -> None:
            await queue.put(
                {
                    "event_type": "progress",
                    "file_index": index,
                    "total_files": total_files,
                    "filename": filename,
                    "stage": "projection",
                    "status": "completed",
                    "document_id": document_id,
                }
            )

        async def on_file_error(
            index: int,
            total_files: int,
            filename: str,
            error_message: str,
        ) -> None:
            logger.error("Failed to ingest %s: %s", filename, error_message)
            await queue.put(
                {
                    "event_type": "progress",
                    "file_index": index,
                    "total_files": total_files,
                    "filename": filename,
                    "stage": "projection",
                    "status": "failed",
                    "error": error_message,
                }
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
            # Emit summary event
            summary = result.to_dict()
            summary["event_type"] = "summary"
            await queue.put(summary)
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
