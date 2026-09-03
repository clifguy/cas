"""Shared SSE delivery glue for batch ingestion.

Bridges the caller-neutral ``BatchIngestService`` pipeline to a
Server-Sent-Events byte stream. Both bulk-ingest delivery paths consume
this single generator so they emit byte-identical SSE: the in-process
``/app/ingest`` route (co-located profile) and the SAGE Core
upload+stream endpoint (hosted profile). Keeping the wire format in one
place makes cross-profile parity structural rather than merely tested.

This module yields ``data: <json>\\n\\n`` strings only; wrapping them
in a FastAPI ``StreamingResponse`` is the caller's job, so the service
layer stays free of HTTP-framework coupling.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
import tempfile
from collections.abc import AsyncGenerator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from sage.models.schemas import DocumentsCreated, ProgressEvent, SummaryEvent
from sage.services.batch_ingest import (
    BatchIngestService,
    FileDescriptor,
    IngestSummary,
    ParsedMetadataInput,
)
from sage.services.transfer import staging_name

logger = logging.getLogger(__name__)


@dataclass
class UploadedFile:
    """One uploaded file's content plus its caller-supplied descriptors.

    Neutral over the delivery transport: the FastAPI route reads
    ``UploadFile`` bytes into this shape so the staging-and-stream service
    carries no framework coupling.
    """

    filename: str
    content: bytes
    source_type: str
    parsed_metadata: dict[str, Any] | None = None


def _parsed_metadata_input(
    parsed: dict | None,
    default_title: str,
) -> ParsedMetadataInput | None:
    """Build a ``ParsedMetadataInput`` from a caller-supplied dict.

    Mirrors the dict-to-input conversion the MCP ``bulk_ingest_document``
    tool performs: the file stem seeds the title when the caller omits it.
    """
    if parsed is None:
        return None
    return ParsedMetadataInput(
        title=parsed.get("title", default_title),
        date=parsed.get("date"),
        project=parsed.get("project"),
        codes=parsed.get("codes", []),
        version=parsed.get("version"),
        doc_type=parsed.get("doc_type"),
    )


def _sse_event(event: BaseModel) -> str:
    """Format an SSE ``data:`` line from a Pydantic event.

    ``exclude_none=True`` preserves the wire convention of omitting
    optional fields (``document_id``/``error`` on progress events when
    not applicable, ``edge_warnings`` on summary events when empty).
    """
    return f"data: {event.model_dump_json(exclude_none=True)}\n\n"


def _summary_event_from(summary: IngestSummary) -> SummaryEvent:
    """Pure mapping from the pipeline's ``IngestSummary`` to the wire
    ``SummaryEvent``. Empty ``edge_warnings`` flattens to ``None`` so
    ``exclude_none`` drops the field on serialization."""
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


async def batch_ingest_sse_stream(
    descriptors: list[FileDescriptor],
    vault_services: object,
    infer_edges: bool = True,
    needs_review: bool = True,
) -> AsyncGenerator[str, None]:
    """Run a batch ingest and yield its progress/summary events as SSE lines.

    The per-file callbacks enqueue ``ProgressEvent`` payloads as the
    batch advances; a final ``SummaryEvent`` is enqueued when the run
    completes. A background task drives the pipeline while this
    generator drains the queue, so progress streams to the client as
    each file lands rather than all at once at the end.
    """
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
        svc = BatchIngestService()
        result = await svc.run(
            files=descriptors,
            vault_services=vault_services,
            infer_edges=infer_edges,
            needs_review=needs_review,
            on_file_start=on_file_start,
            on_file_done=on_file_done,
            on_file_error=on_file_error,
        )
        await queue.put(_summary_event_from(result))
        await queue.put(None)

    task = asyncio.create_task(run_service())
    try:
        while True:
            event = await queue.get()
            if event is None:
                break
            yield _sse_event(event)
    finally:
        await task


async def stream_uploaded_batch_ingest(
    uploads: list[UploadedFile],
    vault_services: object,
    infer_edges: bool = True,
    needs_review: bool = True,
) -> AsyncGenerator[str, None]:
    """Stage uploaded file content and stream a batch ingest as SSE.

    The hosted-profile entry point: each upload's bytes are written to a
    temporary directory under the SAGE process with the original filename
    preserved (so the vault's FilenameParser sees the right stem and
    provenance hashes the uploaded bytes), then ingested through the same
    ``batch_ingest_sse_stream`` the co-located profile drives -- so both
    profiles produce equivalent summaries. Each part is staged in its own
    subdirectory, named by its position in the batch: two uploads may share
    a filename, and staging them side by side under one directory would let
    the later one replace the earlier one's bytes before either is ingested.
    The separator is a directory rather than a change to the basename, so
    what the parser and the vault's retention see is still the upload's own
    name. A filename that reduces to no usable basename (``"."``, ``".."``,
    or nothing at all) is staged under a synthetic name, the same reduction
    the token leg applies, rather than resolving to the staging directory
    itself and failing mid-stream. A per-file failure names the file by the
    upload's own filename, not by the staging path it was written to. The
    staging directory is removed once the stream is exhausted.
    """
    staging_dir = Path(tempfile.mkdtemp(prefix="sage-batch-ingest-"))
    try:
        descriptors: list[FileDescriptor] = []
        for index, upload in enumerate(uploads):
            declared = upload.filename or f"upload_{index}"
            safe_name = staging_name(declared, f"upload_{index}")
            part_dir = staging_dir / str(index)
            part_dir.mkdir()
            dest = part_dir / safe_name
            dest.write_bytes(upload.content)
            descriptors.append(
                FileDescriptor(
                    file_path=str(dest),
                    source_type=upload.source_type,
                    parsed_metadata=_parsed_metadata_input(upload.parsed_metadata, dest.stem),
                    # The staged path is where the bytes are; the upload's
                    # own name is what a refusal names back at the caller.
                    declared_source=declared,
                )
            )
        async for chunk in batch_ingest_sse_stream(
            descriptors,
            vault_services,
            infer_edges=infer_edges,
            needs_review=needs_review,
        ):
            yield chunk
    finally:
        shutil.rmtree(staging_dir, ignore_errors=True)
