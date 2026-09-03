"""Unit tests for IngestStreamingService.

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

import json
from unittest.mock import MagicMock

import pytest
from fastapi.responses import StreamingResponse
from pydantic import ValidationError

from app.backend.exceptions import EmptyFileListError
from app.backend.ingest_streaming_service import (
    IngestStreamingService,
    _summary_event_from,
)
from app.backend.models import (
    BatchIngestFileError,
    EdgeWarning,
    IngestFileItem,
    IngestRequest,
    ParsedMetadata,
    ProgressEvent,
    SummaryEvent,
)
from sage.services.batch_ingest import IngestSummary


def _stub_services() -> MagicMock:
    """Lightweight SAGEServices stub. ``stream()`` should never touch
    these for the empty-list case, and only references them once it
    constructs the StreamingResponse (the generator is lazy)."""
    return MagicMock()


class TestStreamValidation:
    def test_stream_raises_empty_file_list_for_empty_files(self) -> None:
        service = IngestStreamingService(vault_services=_stub_services())
        body = IngestRequest(vault_id="example_vault", files=[])
        with pytest.raises(EmptyFileListError):
            service.stream(body)


class TestStreamResponseShape:
    def test_stream_returns_streaming_response_with_sse_media_type(self) -> None:
        service = IngestStreamingService(vault_services=_stub_services())
        body = IngestRequest(
            vault_id="example_vault",
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
            errors=[
                BatchIngestFileError(
                    file_index=0,
                    filename="bad.md",
                    source_path="/in/bad.md",
                    message="boom",
                )
            ],
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
        assert [e.model_dump(exclude_none=True) for e in event.errors] == [
            {
                "file_index": 0,
                "filename": "bad.md",
                "source_path": "/in/bad.md",
                "message": "boom",
            }
        ]
        # Empty warnings list maps to None on the event (preserves the
        # wire convention: exclude_none drops the field).
        assert event.edge_warnings is None

    def test_summary_event_from_passes_through_nonempty_edge_warnings(self) -> None:
        warning = {
            "source": "doc_v2",
            "target": "doc_v1",
            "edge_type": "supersedes",
            "reason": "supersede_target_not_transitionable",
            "detail": "Cannot supersede document doc_v1: observed state 'completed'",
        }
        summary = IngestSummary(edge_warnings=[EdgeWarning(**warning)])
        event = _summary_event_from(summary)
        assert event.edge_warnings is not None
        assert [entry.model_dump() for entry in event.edge_warnings] == [warning]

    def test_summary_event_serializes_edge_warnings_unchanged(self) -> None:
        """The wire payload preserves the producer's warning entries
        key-for-key and order-for-order, and drops unknown keys instead of
        raising — a producer that carries a key the wire shape does not
        declare must degrade, never break the stream. That matters more now
        than it did: a raise here no longer merely mis-serializes, it
        terminates the stream without a summary."""
        warning = {
            "source": "imports/new.md",
            "target": "imports/old.md",
            "edge_type": "supersedes",
            "reason": "ingestion_failed",
            "detail": "Target file failed ingestion: imports/old.md",
        }
        summary = IngestSummary(
            edges_dropped=1,
            edge_warnings=[EdgeWarning(**{**warning, "hint": "not part of the wire shape"})],
        )
        event = _summary_event_from(summary)
        payload = json.loads(event.model_dump_json(exclude_none=True))
        assert payload["edge_warnings"] == [warning]
        assert list(payload["edge_warnings"][0]) == list(warning)

    def test_summary_event_rejects_warning_entry_missing_required_keys(self) -> None:
        """Every warning field is required: an entry that lacks the
        emitter's key set fails validation rather than reaching the wire
        half-shaped.

        Anti-coincidental-pass: an EdgeWarning whose fields carried
        defaults would pass the round-trip tests above unchanged; only a
        rejected under-shaped entry separates required fields from
        defaulted ones.

        These four tests are the only ones in this class that still hand
        ``IngestSummary`` a raw dict, and deliberately so. The shared
        builders now emit validated models, so no production path can put a
        half-shaped entry in front of this boundary -- but ``IngestSummary``
        is a plain dataclass whose annotation checks nothing at runtime, so a
        producer that appends to the collection directly instead of going
        through the builder still can. That bypass is what these guard, and a
        raw dict is the only way to express it.
        """
        summary = IngestSummary(
            edge_warnings=[{"edge_type": "supersedes", "warning": "skew"}],
        )
        with pytest.raises(ValidationError):
            _summary_event_from(summary)

    def test_summary_event_serializes_message_only_entry_without_code_or_detail(self) -> None:
        """A per-file failure that carried no typed error serializes with the
        filename, the caller's path, and the message -- and no ``code`` or
        ``detail`` key -- so a consumer sees the absence, not a null.

        Anti-coincidental-pass: equality on the wire payload. A model that
        emitted the optional fields as ``null`` would satisfy any per-key
        containment check while misreporting a typed detail that does not
        exist.
        """
        entry = {
            "file_index": 0,
            "filename": "bad.md",
            "source_path": "/in/bad.md",
            "message": "boom",
        }
        summary = IngestSummary(error_count=1, errors=[BatchIngestFileError(**entry)])
        payload = json.loads(_summary_event_from(summary).model_dump_json(exclude_none=True))
        assert payload["errors"] == [entry]

    def test_summary_event_carries_code_and_detail_for_a_typed_entry(self) -> None:
        """A per-file failure that was a typed error serializes with its code
        and its detail dict intact, alongside the message-only fields.

        Anti-coincidental-pass: the ``detail`` value is a nested dict with a
        non-string leaf, so a model that flattened or stringified it would
        fail the equality.
        """
        entry = {
            "file_index": 3,
            "filename": "bad.md",
            "source_path": "/in/bad.md",
            "message": "refused",
            "code": "vault_source_path_refused",
            "detail": {"source_path": "/in/bad.md", "attempt": 2},
        }
        summary = IngestSummary(error_count=1, errors=[BatchIngestFileError(**entry)])
        payload = json.loads(_summary_event_from(summary).model_dump_json(exclude_none=True))
        assert payload["errors"] == [entry]

    def test_summary_event_rejects_error_entry_missing_message(self) -> None:
        """The message-only fields are required: an entry that lacks its
        ``message`` fails validation rather than reaching the wire
        half-shaped.

        Anti-coincidental-pass: an ``errors`` field typed as a list of
        untyped dicts passes every round-trip test above unchanged; only a
        rejected under-shaped entry proves the entry is a typed model, and
        the error must name ``message`` so a rejection for some other
        reason cannot stand in.

        These four tests are the only ones in this class that still hand
        ``IngestSummary`` a raw dict, and deliberately so. The shared
        builders now emit validated models, so no production path can put a
        half-shaped entry in front of this boundary -- but ``IngestSummary``
        is a plain dataclass whose annotation checks nothing at runtime, so a
        producer that appends to the collection directly instead of going
        through the builder still can. That bypass is what these guard, and a
        raw dict is the only way to express it.
        """
        summary = IngestSummary(
            error_count=1,
            errors=[{"file_index": 0, "filename": "bad.md", "source_path": "/in/bad.md"}],
        )
        with pytest.raises(ValidationError) as excinfo:
            _summary_event_from(summary)
        assert "message" in str(excinfo.value)

    def test_summary_event_rejects_error_entry_missing_file_index(self) -> None:
        """The file's position in the batch is required on every entry: an
        entry that lacks ``file_index`` fails validation rather than reaching
        the wire without the one field that separates two same-named files.

        Anti-coincidental-pass: an optional ``file_index`` would accept this
        entry and serialize it without the key; only a rejection proves the
        field is required, and the error must name it.

        These four tests are the only ones in this class that still hand
        ``IngestSummary`` a raw dict, and deliberately so. The shared
        builders now emit validated models, so no production path can put a
        half-shaped entry in front of this boundary -- but ``IngestSummary``
        is a plain dataclass whose annotation checks nothing at runtime, so a
        producer that appends to the collection directly instead of going
        through the builder still can. That bypass is what these guard, and a
        raw dict is the only way to express it.
        """
        summary = IngestSummary(
            error_count=1,
            errors=[{"filename": "bad.md", "source_path": "/in/bad.md", "message": "boom"}],
        )
        with pytest.raises(ValidationError) as excinfo:
            _summary_event_from(summary)
        assert "file_index" in str(excinfo.value)

    def test_summary_event_rejects_negative_file_index(self) -> None:
        """``file_index`` is a zero-based position: a negative value fails
        validation.

        Anti-coincidental-pass: a bare ``int`` field accepts ``-1``; only the
        lower bound rejects it. A bound set one too high (``ge=1``) also
        rejects ``-1`` and would pass this test alone; it is excluded by the
        round-trip tests in this class, whose entries all carry
        ``file_index: 0`` on the success path, so the discriminating
        evidence is that pair rather than this rejection by itself.

        These four tests are the only ones in this class that still hand
        ``IngestSummary`` a raw dict, and deliberately so. The shared
        builders now emit validated models, so no production path can put a
        half-shaped entry in front of this boundary -- but ``IngestSummary``
        is a plain dataclass whose annotation checks nothing at runtime, so a
        producer that appends to the collection directly instead of going
        through the builder still can. That bypass is what these guard, and a
        raw dict is the only way to express it.
        """
        summary = IngestSummary(
            error_count=1,
            errors=[
                {
                    "file_index": -1,
                    "filename": "bad.md",
                    "source_path": "/in/bad.md",
                    "message": "boom",
                }
            ],
        )
        with pytest.raises(ValidationError):
            _summary_event_from(summary)


class TestProgressEventShape:
    def test_progress_event_rejects_negative_file_index(self) -> None:
        """``ProgressEvent.file_index`` is the same zero-based position the
        summary's error entries carry, and is bounded the same way: a
        negative value fails validation while zero constructs.

        Anti-coincidental-pass: a bare ``int`` accepts ``-1``; a bound set
        one too high rejects the zero the success-path half constructs, so
        the pair excludes both.
        """
        fields = {
            "event_type": "progress",
            "total_files": 1,
            "filename": "a.md",
            "stage": "projection",
            "status": "started",
        }
        assert ProgressEvent(file_index=0, **fields).file_index == 0
        with pytest.raises(ValidationError):
            ProgressEvent(file_index=-1, **fields)
