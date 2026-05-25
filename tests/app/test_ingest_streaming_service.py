"""Unit tests for IngestStreamingService (T-0049).

The streaming service owns the SSE delivery glue extracted from
``app.backend.router.ingest_endpoint``. These tests exercise:

  - Pre-stream validation: empty file lists must raise
    ``EmptyFileListError`` BEFORE ``StreamingResponse`` is constructed,
    so the client receives a 400 (not a started 200 stream).
  - Response shape: ``stream()`` returns a ``StreamingResponse`` with
    ``media_type == "text/event-stream"``.
  - Pure field mapping: ``_summary_event_from`` turns an
    ``IngestSummary`` into a ``SummaryEvent`` correctly.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from fastapi.responses import StreamingResponse

from app.backend.exceptions import EmptyFileListError
from app.backend.ingest_service import IngestSummary
from app.backend.ingest_streaming_service import (
    IngestStreamingService,
    _summary_event_from,
)
from app.backend.models import (
    IngestFileItem,
    IngestRequest,
    ParsedMetadata,
    SummaryEvent,
)


def _stub_services() -> MagicMock:
    """Lightweight SAGEServices stub. ``stream()`` should never touch
    these for the empty-list case, and only references them once it
    constructs the StreamingResponse (the generator is lazy)."""
    return MagicMock()


class TestStreamValidation:
    def test_stream_raises_empty_file_list_for_empty_files(self) -> None:
        service = IngestStreamingService(vault_services=_stub_services())
        body = IngestRequest(vault_id="pim_health", files=[])
        with pytest.raises(EmptyFileListError):
            service.stream(body)


class TestStreamResponseShape:
    def test_stream_returns_streaming_response_with_sse_media_type(self) -> None:
        service = IngestStreamingService(vault_services=_stub_services())
        body = IngestRequest(
            vault_id="pim_health",
            files=[
                IngestFileItem(
                    file_path="/tmp/probe.md",
                    source_type="markdown",
                    parsed_metadata=ParsedMetadata(title="probe"),
                )
            ],
        )
        response = service.stream(body)
        assert isinstance(response, StreamingResponse)
        assert response.media_type == "text/event-stream"


class TestSummaryEventFrom:
    def test_summary_event_from_maps_ingest_summary_fields(self) -> None:
        summary = IngestSummary(
            docs_new=3,
            docs_version=1,
            metadata_pending=2,
            abstracts_generated=4,
            abstracts_deferred=0,
            edges_created={"supersedes": 2},
            edges_staged={"covers": 1},
            edges_removed=1,
            edges_dropped=0,
            edge_warnings=[],
            error_count=1,
            errors=[{"filename": "bad.md", "message": "boom"}],
        )
        event = _summary_event_from(summary)
        assert isinstance(event, SummaryEvent)
        assert event.event_type == "summary"
        assert event.documents_created.new == 3
        assert event.documents_created.new_version == 1
        assert event.metadata_pending == 2
        assert event.abstracts_generated == 4
        assert event.abstracts_deferred == 0
        assert event.edges_created == {"supersedes": 2}
        assert event.edges_staged == {"covers": 1}
        assert event.edges_removed == 1
        assert event.edges_dropped == 0
        assert event.error_count == 1
        assert event.errors == [{"filename": "bad.md", "message": "boom"}]
        # Empty warnings list maps to None on the event (preserves the
        # wire convention: exclude_none drops the field).
        assert event.edge_warnings is None

    def test_summary_event_from_passes_through_nonempty_edge_warnings(self) -> None:
        summary = IngestSummary(
            edge_warnings=[{"edge_type": "supersedes", "warning": "skew"}],
        )
        event = _summary_event_from(summary)
        assert event.edge_warnings == [{"edge_type": "supersedes", "warning": "skew"}]
