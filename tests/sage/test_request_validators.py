"""Boundary validators on caller-supplied request models.

Pins shape contracts for fields that the substrate stores verbatim from
caller input. Today's gap: ``document_date`` is typed ``str | None`` and
the schema/OpenAPI specifies the YYYY-MM-DD shape, but no validator
enforces it. As a result, callers passing datetime-ISO strings such as
``2026-05-05T00:00:00Z`` poisoned the live PIM Health vault and made
``sage_traverse`` raise ``ValueError`` from ``strptime``.

These tests pin the strict-reject contract on the two boundary models
that accept ``document_date`` from callers: ``UpdateMetadataRequest``
(direct field) and ``IngestRequest`` (via the generic ``metadata`` dict
under either the ``document_date`` or filename-parser-keyed ``date``
slot).
"""

import pytest
from pydantic import ValidationError

from sage.models.enums import SourceType
from sage.models.schemas import IngestRequest, UpdateMetadataRequest


# ---------------------------------------------------------------------------
# UpdateMetadataRequest.document_date
# ---------------------------------------------------------------------------


def test_update_metadata_accepts_yyyy_mm_dd():
    """The contract shape constructs without error and is preserved verbatim."""
    req = UpdateMetadataRequest(document_date="2026-05-05")
    assert req.document_date == "2026-05-05"


def test_update_metadata_rejects_iso_with_z():
    """Datetime-ISO with a trailing Z is rejected (this is the live-vault bug)."""
    with pytest.raises(ValidationError) as excinfo:
        UpdateMetadataRequest(document_date="2026-05-05T00:00:00Z")
    msg = str(excinfo.value)
    assert "document_date" in msg


def test_update_metadata_rejects_garbage():
    """Any string outside YYYY-MM-DD is rejected."""
    with pytest.raises(ValidationError):
        UpdateMetadataRequest(document_date="not a date")


# ---------------------------------------------------------------------------
# IngestRequest.metadata document-date keys
# ---------------------------------------------------------------------------


def test_ingest_metadata_document_date_yyyy_mm_dd_accepted():
    req = IngestRequest(
        source="x",
        adapter=SourceType.MARKDOWN,
        metadata={"document_date": "2026-05-05"},
    )
    assert req.metadata["document_date"] == "2026-05-05"


def test_ingest_metadata_document_date_iso_with_z_rejected():
    with pytest.raises(ValidationError) as excinfo:
        IngestRequest(
            source="x",
            adapter=SourceType.MARKDOWN,
            metadata={"document_date": "2026-05-05T00:00:00Z"},
        )
    assert "document_date" in str(excinfo.value)


def test_ingest_metadata_date_iso_with_z_rejected():
    """The filename-parser-keyed ``date`` slot maps to document_date too."""
    with pytest.raises(ValidationError) as excinfo:
        IngestRequest(
            source="x",
            adapter=SourceType.MARKDOWN,
            metadata={"date": "2026-05-05T00:00:00Z"},
        )
    assert "date" in str(excinfo.value)
