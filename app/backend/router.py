"""CAS Application backend router (BE-017 through BE-035).

POST /app/scan -- directory scan with filename parsing
POST /app/ingest -- batch ingest with edge inference and SSE streaming
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import AsyncGenerator

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from app.backend.edge_inference import (
    EdgeInferenceEngine,
    InferenceItem,
    resolve_and_execute,
)
from app.backend.filename_parser import ParsedMetadata
from app.backend.scan import ScanResult, scan_directory
from sage.api.errors import VaultNotFoundError
from sage.models.enums import SourceType
from sage.models.schemas import IngestRequest

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


def _metadata_dict_from_parsed(pm: ParsedMetadataResponse | None) -> dict[str, str] | None:
    """Convert ParsedMetadataResponse to flat string dict for IngestRequest.metadata."""
    if pm is None:
        return None
    d: dict[str, str] = {}
    if pm.title:
        d["title"] = pm.title
    if pm.date:
        d["date"] = pm.date
    if pm.project:
        d["project"] = pm.project
    if pm.codes:
        d["codes"] = ",".join(pm.codes)
    if pm.version:
        d["version_label"] = pm.version
    if pm.doc_type:
        d["doc_type"] = pm.doc_type
    return d or None


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.post("/scan", response_model=dict)
async def scan_endpoint(body: ScanRequest, request: Request) -> dict:
    """Scan a directory and return files with status and parsed metadata."""
    directory = Path(body.directory)
    if not directory.is_dir():
        from fastapi import HTTPException
        raise HTTPException(
            status_code=400,
            detail="Directory not found or not readable",
        )

    services = _get_services(request, body.vault_id)

    results, warnings = await scan_directory(
        directory=directory,
        vault_config=services.config,
        graph_store=services.graph_store,
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
        from fastapi import HTTPException
        raise HTTPException(status_code=400, detail="No files selected for ingestion")

    services = _get_services(request, body.vault_id)

    async def event_stream() -> AsyncGenerator[str, None]:
        total = len(body.files)
        path_to_id: dict[str, str] = {}
        error_count = 0
        abstracts_generated = 0
        abstracts_deferred = 0
        docs_new = 0
        docs_version = 0

        # Phase 1: Pre-ingest edge plan
        engine = EdgeInferenceEngine()

        # Build inference items from request
        scan_items: list[InferenceItem] = []
        for f in body.files:
            pm = f.parsed_metadata
            if pm:
                parsed = ParsedMetadata(
                    title=pm.title,
                    date=pm.date,
                    project=pm.project,
                    codes=pm.codes,
                    version=pm.version,
                    doc_type=pm.doc_type,
                )
            else:
                parsed = ParsedMetadata(title=Path(f.file_path).stem)
            scan_items.append(InferenceItem(
                ref=f.file_path,
                is_existing=False,
                parsed=parsed,
            ))

        # Get existing vault documents for edge plan context
        existing_items: list[InferenceItem] = []
        all_docs = await services.graph_store.list_all_documents()
        for doc in all_docs:
            existing_items.append(InferenceItem(
                ref=doc.id,
                is_existing=True,
                parsed=ParsedMetadata(
                    title=doc.title,
                    date=None,
                    project=doc.project,
                    codes=doc.tags,  # codes stored in tags if available
                    version=doc.version_label,
                    doc_type=doc.doc_type,
                ),
            ))

        edge_plan = engine.build_edge_plan(scan_items, existing_items)

        # Phase 2: Per-file ingestion
        for i, file_item in enumerate(body.files):
            filename = Path(file_item.file_path).name

            # Emit started event
            yield _sse_event({
                "event_type": "progress",
                "file_index": i,
                "total_files": total,
                "filename": filename,
                "stage": "projection",
                "status": "started",
            })

            try:
                metadata_dict = _metadata_dict_from_parsed(file_item.parsed_metadata)
                sage_request = IngestRequest(
                    source=file_item.file_path,
                    adapter=SourceType(file_item.adapter),
                    metadata=metadata_dict,
                )
                doc, http_status = await services.ingestion_service.ingest(
                    sage_request, body.vault_id
                )
                path_to_id[file_item.file_path] = doc.id

                if http_status == 201:
                    docs_new += 1
                else:
                    docs_version += 1

                # Check abstraction config
                if services.config.abstraction.enabled:
                    abstracts_generated += 1
                else:
                    abstracts_deferred += 1

                yield _sse_event({
                    "event_type": "progress",
                    "file_index": i,
                    "total_files": total,
                    "filename": filename,
                    "stage": "projection",
                    "status": "completed",
                    "document_id": doc.id,
                })
            except Exception as exc:
                error_count += 1
                logger.exception("Failed to ingest %s", file_item.file_path)
                yield _sse_event({
                    "event_type": "progress",
                    "file_index": i,
                    "total_files": total,
                    "filename": filename,
                    "stage": "projection",
                    "status": "failed",
                    "error": str(exc),
                })

        # Phase 3: Post-ingest edge creation
        edge_result = await resolve_and_execute(
            edge_plan,
            path_to_id,
            services.graph_store,
            services.graph_ops_service,
        )

        # Emit summary event
        yield _sse_event({
            "event_type": "summary",
            "documents_created": {"new": docs_new, "new_version": docs_version},
            "metadata_pending": docs_new + docs_version,  # all new docs pending
            "edges_created": edge_result.edges_created,
            "edges_staged": edge_result.edges_staged,
            "edges_dropped": edge_result.edges_dropped,
            "abstracts_generated": abstracts_generated,
            "abstracts_deferred": abstracts_deferred,
            "error_count": error_count,
        })

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
    )
