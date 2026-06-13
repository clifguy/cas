"""In-process SSE delivery for ``/app/ingest`` (co-located profile).

Bridges the BFF's ``IngestRequest`` body to the shared batch-ingest SSE
generator in ``sage.services.batch_ingest_stream``. The same generator
backs the SAGE Core upload+stream endpoint, so both deployment profiles
emit byte-identical SSE; this module only adapts the BFF request shape
and validates it before the stream opens.

Pre-stream validation is load-bearing: ``EmptyFileListError`` must
raise inside ``stream()`` BEFORE the ``StreamingResponse`` is
returned, otherwise the client would see a started 200 stream that
then errors mid-body. The Depends factory raises
``VaultNotFoundError`` even earlier (before this service is
constructed).

``_summary_event_from`` is re-exported here from the shared sage module
for callers (and tests) that import it from the BFF surface.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi.responses import StreamingResponse

from app.backend.exceptions import EmptyFileListError
from app.backend.models import IngestFileItem, IngestRequest
from sage.services.batch_ingest import FileDescriptor, ParsedMetadataInput
from sage.services.batch_ingest_stream import (
    _summary_event_from,  # noqa: F401 -- re-exported for the BFF import surface
    batch_ingest_sse_stream,
)

if TYPE_CHECKING:
    from sage.mcp_init import SAGEServices


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


class IngestStreamingService:
    """HTTP-delivery glue for ``/app/ingest``: adapts the BFF request to
    the shared ``batch_ingest_sse_stream`` generator."""

    def __init__(self, vault_services: SAGEServices) -> None:
        self.vault_services = vault_services

    def stream(self, body: IngestRequest) -> StreamingResponse:
        if not body.files:
            raise EmptyFileListError()
        descriptors = [_to_file_descriptor(f) for f in body.files]
        return StreamingResponse(
            batch_ingest_sse_stream(
                descriptors,
                self.vault_services,
                infer_edges=body.infer_edges,
            ),
            media_type="text/event-stream",
        )
