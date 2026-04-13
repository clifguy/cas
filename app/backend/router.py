"""CAS Application backend router (BE-017 through BE-035).

POST /app/scan -- directory scan with filename parsing
POST /app/ingest -- batch ingest with edge inference and SSE streaming
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import AsyncGenerator

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from app.backend.ingest_service import (
    BatchIngestService,
    FileDescriptor,
    ParsedMetadataInput,
)
from app.backend.scan import ScanResult, build_extension_map, scan_directory
from sage.api.errors import VaultNotFoundError

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/app", tags=["app"])


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------

class ScanRequest(BaseModel):
    vault_id: str
    directory: str
    max_depth: int | None = None


class ParsedMetadataResponse(BaseModel):
    title: str
    date: str | None = None
    project: str | None = None
    codes: list[str] = Field(default_factory=list)
    version: str | None = None
    doc_type: str | None = None


class ScanResultResponse(BaseModel):
    file_path: str
    file_hash: str
    source_modified_at: str
    adapter: str | None = None
    parsed_metadata: ParsedMetadataResponse
    sage_status: str


class IngestFileItem(BaseModel):
    file_path: str
    adapter: str
    parsed_metadata: ParsedMetadataResponse | None = None


class IngestRequest_(BaseModel):
    """Ingest request body (underscore to avoid collision with SAGE IngestRequest)."""
    vault_id: str
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


def _scan_result_to_response(sr: ScanResult) -> ScanResultResponse:
    pm = sr.parsed_metadata
    return ScanResultResponse(
        file_path=sr.file_path,
        file_hash=sr.file_hash,
        source_modified_at=sr.source_modified_at,
        adapter=sr.adapter,
        parsed_metadata=ParsedMetadataResponse(
            title=pm.title,
            date=pm.date,
            project=pm.project,
            codes=pm.codes,
            version=pm.version,
            doc_type=pm.doc_type,
        ),
        sage_status=sr.sage_status,
    )


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

@router.post("/scan", response_model=dict)
async def scan_endpoint(body: ScanRequest, request: Request) -> dict:
    """Scan a directory and return files with status and parsed metadata."""
    directory = Path(body.directory.strip("'\""))
    if not directory.is_dir():
        raise HTTPException(
            status_code=400,
            detail="Directory not found or not readable",
        )

    services = _get_services(request, body.vault_id)

    ext_map = build_extension_map(services.ingestion_service.registered_adapters)
    results, warnings = await scan_directory(
        directory=directory,
        vault_config=services.config,
        graph_store=services.graph_store,
        extension_map=ext_map,
        max_depth=body.max_depth,
    )

    return {
        "files": [_scan_result_to_response(r) for r in results],
        "warnings": warnings,
    }


@router.post("/ingest")
async def ingest_endpoint(body: IngestRequest_, request: Request):
    """Batch ingest with two-phase edge inference, streamed via SSE."""
    if not body.files:
        raise HTTPException(status_code=400, detail="No files selected for ingestion")

    services = _get_services(request, body.vault_id)

    async def event_stream() -> AsyncGenerator[str, None]:
        queue: asyncio.Queue[dict | None] = asyncio.Queue()

        async def on_file_start(index: int, total_files: int, filename: str) -> None:
            await queue.put({
                "event_type": "progress",
                "file_index": index,
                "total_files": total_files,
                "filename": filename,
                "stage": "projection",
                "status": "started",
            })

        async def on_file_done(
            index: int, total_files: int, filename: str, document_id: str,
        ) -> None:
            await queue.put({
                "event_type": "progress",
                "file_index": index,
                "total_files": total_files,
                "filename": filename,
                "stage": "projection",
                "status": "completed",
                "document_id": document_id,
            })

        async def on_file_error(
            index: int, total_files: int, filename: str, error_message: str,
        ) -> None:
            logger.error("Failed to ingest %s: %s", filename, error_message)
            await queue.put({
                "event_type": "progress",
                "file_index": index,
                "total_files": total_files,
                "filename": filename,
                "stage": "projection",
                "status": "failed",
                "error": error_message,
            })

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
