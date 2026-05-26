"""HTTP-delivery streaming service for ``/app/ingest``.

This module owns the SSE glue that bridges the caller-neutral
``BatchIngestService`` pipeline to FastAPI's ``StreamingResponse``.
Keeping the streaming concerns here lets the router handler reduce
to a single line and leaves ``BatchIngestService`` free of HTTP
coupling so the MCP tool path keeps using it directly.

Pre-stream validation is load-bearing: ``EmptyFileListError`` must
raise inside ``stream()`` BEFORE the ``StreamingResponse`` is
returned, otherwise the client would see a started 200 stream that
then errors mid-body. The Depends factory raises
``VaultNotFoundError`` even earlier (before this service is
constructed).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncGenerator
from typing import TYPE_CHECKING

from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from app.backend.exceptions import EmptyFileListError
from app.backend.ingest_service import (
    BatchIngestService,
    FileDescriptor,
    IngestSummary,
    ParsedMetadataInput,
)
from app.backend.models import (
    DocumentsCreated,
    IngestFileItem,
    IngestRequest,
    ProgressEvent,
    SummaryEvent,
)

if TYPE_CHECKING:
    from sage.mcp_init import SAGEServices

logger = logging.getLogger(__name__)


def _sse_event(event: BaseModel) -> str:
    """Format an SSE ``data:`` line from a Pydantic event.

    ``exclude_none=True`` preserves the wire convention of omitting
    optional fields (``document_id``/``error`` on progress events when
    not applicable, ``edge_warnings`` on summary events when empty).
    """
    return f"data: {event.model_dump_json(exclude_none=True)}\n\n"


def _to_file_descriptor(f: IngestFileItem) -> FileDescriptor:
    """Convert the router's Pydantic file item into the service's
    neutral ``FileDescriptor`` dataclass."""
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
        source_type=f.source_type,
        parsed_metadata=parsed,
    )


def _summary_event_from(summary: IngestSummary) -> SummaryEvent:
    """Pure mapping from the pipeline's ``IngestSummary`` to the
    wire ``SummaryEvent``. Empty ``edge_warnings`` flattens to
    ``None`` so ``exclude_none`` drops the field on serialization."""
    return SummaryEvent(
        event_type="summary",
        documents_created=DocumentsCreated(
            new=summary.docs_new,
            new_version=summary.docs_version,
        ),
        metadata_pending=summary.metadata_pending,
        edges_created=summary.edges_created,
        edges_staged=summary.edges_staged,
        edges_removed=summary.edges_removed,
        edges_dropped=summary.edges_dropped,
        abstracts_generated=summary.abstracts_generated,
        abstracts_deferred=summary.abstracts_deferred,
        error_count=summary.error_count,
        errors=summary.errors,
        edge_warnings=summary.edge_warnings if summary.edge_warnings else None,
    )


class IngestStreamingService:
    """HTTP-delivery glue for ``/app/ingest``: composes
    ``BatchIngestService`` with SSE delivery."""

    def __init__(self, vault_services: SAGEServices) -> None:
        self.vault_services = vault_services

    def stream(self, body: IngestRequest) -> StreamingResponse:
        if not body.files:
            raise EmptyFileListError()
        return StreamingResponse(
            self._event_stream(body),
            media_type="text/event-stream",
        )

    async def _event_stream(self, body: IngestRequest) -> AsyncGenerator[str, None]:
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
                vault_services=self.vault_services,
                infer_edges=body.infer_edges,
                on_file_start=on_file_start,
                on_file_done=on_file_done,
                on_file_error=on_file_error,
            )
            await queue.put(_summary_event_from(result))
            await queue.put(None)

        task = asyncio.create_task(run_service())
        while True:
            event = await queue.get()
            if event is None:
                break
            yield _sse_event(event)
        await task
