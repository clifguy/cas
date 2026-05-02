"""Pydantic round-trip coverage for the CAS-ADR-021 substrate additions.

Chunk 1 of the ADR-021 implementation (per CAS-ADR-021_implementation.md)
adds `needs_review` to IngestRequest and introduces ParseFilenameRequest
and ParseFilenameResponse. These tests exercise the model surface only;
runtime behavior changes are covered by the TEST-AD021-NNN suite that
arrives in Chunk 2.
"""

import json

from sage.models.enums import SourceType
from sage.models.schemas import (
    IngestRequest,
    ParseFilenameRequest,
    ParseFilenameResponse,
)


def test_ingest_request_needs_review_default_false():
    req = IngestRequest(source="docs/example.md", adapter=SourceType.MARKDOWN)
    assert req.needs_review is False


def test_ingest_request_needs_review_round_trip_true():
    req = IngestRequest(
        source="docs/example.md",
        adapter=SourceType.MARKDOWN,
        needs_review=True,
    )
    dumped = req.model_dump()
    assert dumped["needs_review"] is True
    revived = IngestRequest.model_validate(dumped)
    assert revived.needs_review is True


def test_ingest_request_needs_review_json_serialization():
    req = IngestRequest(
        source="docs/example.md",
        adapter=SourceType.MARKDOWN,
        needs_review=True,
    )
    payload = json.loads(req.model_dump_json())
    assert payload["needs_review"] is True


def test_parse_filename_request_requires_filename_and_adapter():
    req = ParseFilenameRequest(filename="PV03_CD-2_v1.md", adapter=SourceType.MARKDOWN)
    assert req.filename == "PV03_CD-2_v1.md"
    assert req.adapter == SourceType.MARKDOWN


def test_parse_filename_response_accepts_all_null_fields():
    resp = ParseFilenameResponse()
    assert resp.title is None
    assert resp.project is None
    assert resp.version_label is None
    assert resp.document_date is None
    assert resp.doc_type is None
    assert resp.codes is None


def test_parse_filename_response_round_trip_populated():
    resp = ParseFilenameResponse(
        title="Checklist Apparatus Design",
        project="PV03",
        version_label="v1",
        document_date="2026-04-26",
        doc_type="design_context",
        codes=["PV03", "CD-2"],
    )
    revived = ParseFilenameResponse.model_validate(resp.model_dump())
    assert revived.title == "Checklist Apparatus Design"
    assert revived.codes == ["PV03", "CD-2"]
