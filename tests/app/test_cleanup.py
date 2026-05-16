"""Backend cleanup tests (CL-001 through CL-006).

Verifies import hygiene and metadata conversion fidelity after
iterative development cleanup of the app/backend modules.
"""

from __future__ import annotations

import importlib
import inspect

from app.backend.ingest_streaming_service import _to_file_descriptor
from app.backend.models import IngestFileItem
from app.backend.models import ParsedMetadata as ApiParsedMetadata
from app.backend.scan import ScanResult
from app.backend.scan_service import _scan_result_to_response
from sage.services.filename_parser import ParsedMetadata

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _full_parsed_metadata() -> ParsedMetadata:
    """ParsedMetadata with all fields populated."""
    return ParsedMetadata(
        title="Claim-Set",
        date="2026-03-09",
        project="PIM",
        codes=["PV06", "CF-1"],
        version="v7",
        doc_type="patent_draft",
    )


def _sparse_parsed_metadata() -> ParsedMetadata:
    """ParsedMetadata with only required fields."""
    return ParsedMetadata(title="Untitled")


def _full_scan_result() -> ScanResult:
    return ScanResult(
        file_path="/tmp/2026-03-09_PIM_PV06_Claim-Set_v7.docx",
        file_hash="sha256:" + "a" * 64,
        source_modified_at="2026-03-09T10:00:00+00:00",
        adapter="docx",
        parsed_metadata=_full_parsed_metadata(),
        sage_status="new",
    )


def _sparse_scan_result() -> ScanResult:
    return ScanResult(
        file_path="/tmp/notes.md",
        file_hash="sha256:" + "b" * 64,
        source_modified_at="2026-04-01T12:00:00+00:00",
        adapter="markdown",
        parsed_metadata=_sparse_parsed_metadata(),
        sage_status="new",
    )


# ---------------------------------------------------------------------------
# 1. Import Hygiene
# ---------------------------------------------------------------------------


class TestImportHygiene:
    """CL-001 and CL-002: verify dead imports removed from backend modules."""

    def test_cl_001_edge_inference_no_unused_document_import(self) -> None:
        """CL-001: edge_inference should not import unused Document."""
        mod = importlib.import_module("app.backend.edge_inference")
        source = inspect.getsource(mod)
        # Document should not appear in any import statement
        import_lines = [
            line.strip()
            for line in source.splitlines()
            if line.strip().startswith(("import ", "from "))
        ]
        for line in import_lines:
            assert "Document" not in line, f"Unused 'Document' found in import: {line}"

    def test_cl_002_scan_no_os_import(self) -> None:
        """CL-002: scan module should not import os."""
        mod = importlib.import_module("app.backend.scan")
        source = inspect.getsource(mod)
        import_lines = [
            line.strip()
            for line in source.splitlines()
            if line.strip().startswith(("import ", "from "))
        ]
        for line in import_lines:
            assert line != "import os", f"Vestigial 'import os' found in scan module: {line}"


# ---------------------------------------------------------------------------
# 2. Metadata Conversion Fidelity
# ---------------------------------------------------------------------------


class TestMetadataConversion:
    """CL-004 through CL-006: verify metadata field preservation
    through the router's conversion helpers."""

    def test_cl_004_scan_result_to_response_full(self) -> None:
        """CL-004: _scan_result_to_response preserves all metadata fields."""
        sr = _full_scan_result()
        resp = _scan_result_to_response(sr)

        assert resp.file_path == sr.file_path
        assert resp.file_hash == sr.file_hash
        assert resp.source_modified_at == sr.source_modified_at
        assert resp.adapter == sr.adapter
        assert resp.sage_status == sr.sage_status

        pm = resp.parsed_metadata
        assert pm.title == "Claim-Set"
        assert pm.date == "2026-03-09"
        assert pm.project == "PIM"
        assert pm.codes == ["PV06", "CF-1"]
        assert pm.version == "v7"
        assert pm.doc_type == "patent_draft"

    def test_cl_004_scan_result_to_response_sparse(self) -> None:
        """CL-004: _scan_result_to_response handles sparse metadata."""
        sr = _sparse_scan_result()
        resp = _scan_result_to_response(sr)

        pm = resp.parsed_metadata
        assert pm.title == "Untitled"
        assert pm.date is None
        assert pm.project is None
        assert pm.codes == []
        assert pm.version is None
        assert pm.doc_type is None

    def test_cl_005_to_file_descriptor_full(self) -> None:
        """CL-005: _to_file_descriptor preserves all metadata fields."""
        item = IngestFileItem(
            file_path="/path/to/doc.docx",
            adapter="docx",
            parsed_metadata=ApiParsedMetadata(
                title="Claim-Set",
                date="2026-03-09",
                project="PIM",
                codes=["PV06"],
                version="v7",
                doc_type="patent_draft",
            ),
        )
        fd = _to_file_descriptor(item)

        assert fd.file_path == "/path/to/doc.docx"
        assert fd.adapter == "docx"
        assert fd.parsed_metadata is not None
        assert fd.parsed_metadata.title == "Claim-Set"
        assert fd.parsed_metadata.date == "2026-03-09"
        assert fd.parsed_metadata.project == "PIM"
        assert fd.parsed_metadata.codes == ["PV06"]
        assert fd.parsed_metadata.version == "v7"
        assert fd.parsed_metadata.doc_type == "patent_draft"

    def test_cl_005_to_file_descriptor_none_metadata(self) -> None:
        """CL-005: _to_file_descriptor handles None parsed_metadata."""
        item = IngestFileItem(
            file_path="/path/to/doc.md",
            adapter="markdown",
            parsed_metadata=None,
        )
        fd = _to_file_descriptor(item)

        assert fd.file_path == "/path/to/doc.md"
        assert fd.adapter == "markdown"
        assert fd.parsed_metadata is None

    def test_cl_006_round_trip_full(self) -> None:
        """CL-006: ScanResult -> Response -> FileDescriptor preserves all fields."""
        sr = _full_scan_result()
        original = sr.parsed_metadata

        # Step 1: ScanResult -> ScanResultResponse
        resp = _scan_result_to_response(sr)

        # Step 2: Build IngestFileItem from response (simulating frontend)
        item = IngestFileItem(
            file_path=resp.file_path,
            adapter=resp.adapter or "",
            parsed_metadata=resp.parsed_metadata,
        )

        # Step 3: IngestFileItem -> FileDescriptor
        fd = _to_file_descriptor(item)

        # Verify round-trip fidelity
        pm = fd.parsed_metadata
        assert pm is not None
        assert pm.title == original.title
        assert pm.date == original.date
        assert pm.project == original.project
        assert pm.codes == original.codes
        assert pm.version == original.version
        assert pm.doc_type == original.doc_type

    def test_cl_006_round_trip_sparse(self) -> None:
        """CL-006: Round-trip with sparse (all-None) metadata."""
        sr = _sparse_scan_result()
        original = sr.parsed_metadata

        resp = _scan_result_to_response(sr)
        item = IngestFileItem(
            file_path=resp.file_path,
            adapter=resp.adapter or "",
            parsed_metadata=resp.parsed_metadata,
        )
        fd = _to_file_descriptor(item)

        pm = fd.parsed_metadata
        assert pm is not None
        assert pm.title == original.title
        assert pm.date is None
        assert pm.project is None
        assert pm.codes == []
        assert pm.version is None
        assert pm.doc_type is None
