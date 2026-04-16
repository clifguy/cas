"""Tests for BatchIngestService (TEST-BIS-001 through TEST-BIS-019).

Covers:
  - Service interface and return types (BIS-001, BIS-002)
  - File descriptor normalization (BIS-003, BIS-004)
  - Phase 1: Edge plan construction (BIS-005, BIS-006, BIS-007)
  - Phase 2: Per-file ingestion (BIS-008, BIS-009, BIS-010, BIS-011)
  - Phase 3: Post-ingest edge execution (BIS-012, BIS-013)
  - Progress callbacks (BIS-014, BIS-015, BIS-016, BIS-017)
  - Caller integration (BIS-018, BIS-019)
"""

import asyncio
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.backend.edge_inference import EdgePlan, InferenceItem, PlannedEdge
from sage.services.filename_parser import ParsedMetadata
from app.backend.ingest_service import (
    BatchIngestService,
    FileDescriptor,
    IngestSummary,
    ParsedMetadataInput,
)
from sage.models.enums import EdgeType, PipelineStatus, SourceType
from sage.models.schemas import Document, IngestRequest
from sage.services.ingestion import IngestResult


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _make_document(doc_id: str, title: str = "Test", **kwargs) -> Document:
    """Create a minimal Document for testing."""
    now = datetime.now(timezone.utc)
    defaults = dict(
        id=doc_id,
        title=title,
        source_type=SourceType.MARKDOWN,
        source_path="test.md",
        source_content_hash="sha256:abc",
        adapter_version="1.0",
        created_by="test",
        created_at=now,
        last_modified_by="test",
        updated_at=now,
    )
    defaults.update(kwargs)
    return Document(**defaults)


def _make_ingest_result(doc_id: str, is_new: bool = True, **doc_kwargs) -> IngestResult:
    """Create an IngestResult wrapping a Document."""
    return IngestResult(document=_make_document(doc_id, **doc_kwargs), is_new=is_new)


def _make_services(
    *,
    abstraction_enabled: bool = False,
    existing_docs: list[Document] | None = None,
):
    """Create a mock SAGEServices bundle."""
    services = MagicMock()

    # Config
    services.config.abstraction.enabled = abstraction_enabled

    # Graph store
    services.graph_store.list_all_documents = AsyncMock(
        return_value=existing_docs or []
    )

    # Ingestion service -- default: succeed and return new doc
    call_count = 0

    async def _ingest(request):
        nonlocal call_count
        call_count += 1
        doc_id = f"doc-{call_count}"
        return _make_ingest_result(doc_id, title=request.source)

    services.ingestion_service.ingest = AsyncMock(side_effect=_ingest)

    # Graph ops
    services.graph_ops_service.link = AsyncMock()
    services.graph_store.insert_staging_edge = AsyncMock()
    services.graph_store.get_document = AsyncMock(return_value=None)
    services.graph_store.update_document = AsyncMock()

    return services


def _fd(
    file_path: str,
    adapter: str = "markdown",
    *,
    title: str | None = None,
    date: str | None = None,
    project: str | None = None,
    codes: list[str] | None = None,
    version: str | None = None,
    doc_type: str | None = None,
) -> FileDescriptor:
    """Shorthand FileDescriptor builder."""
    pm = None
    if any(v is not None for v in (title, date, project, codes, version, doc_type)):
        pm = ParsedMetadataInput(
            title=title or Path(file_path).stem,
            date=date,
            project=project,
            codes=codes or [],
            version=version,
            doc_type=doc_type,
        )
    return FileDescriptor(file_path=file_path, adapter=adapter, parsed_metadata=pm)


# ---------------------------------------------------------------------------
# 1. Service Interface (BIS-001, BIS-002)
# ---------------------------------------------------------------------------


class TestServiceInterface:

    @pytest.mark.asyncio
    async def test_bis_001_returns_ingest_summary(self):
        """BatchIngestService returns IngestSummary with correct fields."""
        services = _make_services()
        svc = BatchIngestService()

        result = await svc.run(
            files=[
                _fd("/tmp/doc1.md", title="Doc1"),
                _fd("/tmp/doc2.md", title="Doc2"),
            ],
            vault_services=services,
            infer_edges=False,
        )

        assert isinstance(result, IngestSummary)
        assert result.docs_new == 2
        assert result.docs_version == 0
        assert result.error_count == 0
        assert result.edges_created == {}
        assert result.edges_staged == {}
        assert result.edges_dropped == 0

    @pytest.mark.asyncio
    async def test_bis_002_empty_file_list_raises(self):
        """Empty file list raises ValueError."""
        services = _make_services()
        svc = BatchIngestService()

        with pytest.raises(ValueError, match="No files selected"):
            await svc.run(files=[], vault_services=services, infer_edges=False)


# ---------------------------------------------------------------------------
# 2. File Descriptor Normalization (BIS-003, BIS-004)
# ---------------------------------------------------------------------------


class TestFileDescriptorNormalization:

    @pytest.mark.asyncio
    async def test_bis_003_file_descriptor_with_metadata(self):
        """Service accepts FileDescriptor with full metadata."""
        services = _make_services()
        svc = BatchIngestService()

        result = await svc.run(
            files=[_fd(
                "/tmp/test.md",
                title="Claim-Set",
                date="2026-03-09",
                project="PIM",
                codes=["PV06"],
                version="v7",
                doc_type="patent_draft",
            )],
            vault_services=services,
            infer_edges=False,
        )

        assert result.docs_new == 1

        # Verify the IngestRequest passed to ingestion service
        call_args = services.ingestion_service.ingest.call_args
        request = call_args[0][0]
        assert isinstance(request, IngestRequest)
        assert request.metadata["title"] == "Claim-Set"
        assert request.metadata["date"] == "2026-03-09"
        assert request.metadata["project"] == "PIM"
        assert request.metadata["codes"] == "PV06"
        assert request.metadata["version_label"] == "v7"
        assert request.metadata["doc_type"] == "patent_draft"

    @pytest.mark.asyncio
    async def test_bis_004_file_without_metadata(self):
        """Service handles files with no parsed_metadata."""
        services = _make_services()
        svc = BatchIngestService()

        result = await svc.run(
            files=[_fd("/tmp/bare.md")],
            vault_services=services,
            infer_edges=False,
        )

        assert result.docs_new == 1

        call_args = services.ingestion_service.ingest.call_args
        request = call_args[0][0]
        assert request.metadata is None


# ---------------------------------------------------------------------------
# 3. Phase 1: Edge Plan Construction (BIS-005, BIS-006, BIS-007)
# ---------------------------------------------------------------------------


class TestEdgePlanConstruction:

    @pytest.mark.asyncio
    async def test_bis_005_edge_plan_from_scan_and_existing(self):
        """Edge plan built from scan items and existing vault docs."""
        existing_doc = _make_document(
            "existing-v5",
            title="Claim-Set",
            version_label="v5",
            doc_type="patent_draft",
            tags=["PV06"],
            project="PIM",
        )
        services = _make_services(existing_docs=[existing_doc])
        svc = BatchIngestService()

        result = await svc.run(
            files=[
                _fd("/tmp/v6.md", title="Claim-Set", version="v6", doc_type="patent_draft", codes=["PV06"], project="PIM"),
                _fd("/tmp/v7.md", title="Claim-Set", version="v7", doc_type="patent_draft", codes=["PV06"], project="PIM"),
            ],
            vault_services=services,
            infer_edges=True,
        )

        # Should have created supersedes edges: v7->v6, v6->existing-v5
        assert result.edges_created.get("supersedes", 0) >= 2

    @pytest.mark.asyncio
    async def test_bis_006_edge_plan_skipped_when_disabled(self):
        """No edge plan when infer_edges=False."""
        services = _make_services()
        svc = BatchIngestService()

        result = await svc.run(
            files=[
                _fd("/tmp/v1.md", title="Doc", version="v1"),
                _fd("/tmp/v2.md", title="Doc", version="v2"),
            ],
            vault_services=services,
            infer_edges=False,
        )

        # list_all_documents should not have been called
        services.graph_store.list_all_documents.assert_not_called()
        assert result.edges_created == {}
        assert result.edges_staged == {}
        assert result.edges_dropped == 0

    @pytest.mark.asyncio
    async def test_bis_007_existing_doc_field_mapping(self):
        """Existing docs mapped with tags->codes, version_label->version."""
        existing_doc = _make_document(
            "doc-existing",
            title="Report",
            version_label="v3",
            doc_type="patent_draft",
            tags=["PV06", "CF-1"],
            project="PIM",
        )
        services = _make_services(existing_docs=[existing_doc])
        svc = BatchIngestService()

        # Patch EdgeInferenceEngine to capture what it receives
        with patch("app.backend.ingest_service.EdgeInferenceEngine") as MockEngine:
            mock_engine = MockEngine.return_value
            mock_engine.build_edge_plan.return_value = EdgePlan()

            await svc.run(
                files=[_fd("/tmp/test.md", title="Test")],
                vault_services=services,
                infer_edges=True,
            )

            call_args = mock_engine.build_edge_plan.call_args
            _scan_items, existing_items = call_args[0]

            assert len(existing_items) == 1
            ei = existing_items[0]
            assert ei.ref == "doc-existing"
            assert ei.is_existing is True
            assert ei.parsed.title == "Report"
            assert ei.parsed.codes == ["PV06", "CF-1"]
            assert ei.parsed.version == "v3"
            assert ei.parsed.doc_type == "patent_draft"
            assert ei.parsed.project == "PIM"


# ---------------------------------------------------------------------------
# 4. Phase 2: Per-File Ingestion (BIS-008, BIS-009, BIS-010, BIS-011)
# ---------------------------------------------------------------------------


class TestPerFileIngestion:

    @pytest.mark.asyncio
    async def test_bis_008_sequential_ingestion(self):
        """Files ingested sequentially in order."""
        services = _make_services()
        svc = BatchIngestService()
        paths_ingested = []

        original_ingest = services.ingestion_service.ingest.side_effect

        async def tracking_ingest(request):
            paths_ingested.append(request.source)
            return await original_ingest(request)

        services.ingestion_service.ingest = AsyncMock(side_effect=tracking_ingest)

        await svc.run(
            files=[
                _fd("/tmp/a.md", title="A"),
                _fd("/tmp/b.md", title="B"),
                _fd("/tmp/c.md", title="C"),
            ],
            vault_services=services,
            infer_edges=False,
        )

        assert paths_ingested == ["/tmp/a.md", "/tmp/b.md", "/tmp/c.md"]

    @pytest.mark.asyncio
    async def test_bis_009_metadata_conversion(self):
        """Codes joined as comma-separated, version mapped to version_label."""
        services = _make_services()
        svc = BatchIngestService()

        await svc.run(
            files=[_fd(
                "/tmp/test.md",
                title="Claim-Set",
                date="2026-03-09",
                project="PIM",
                codes=["PV06", "CF-1"],
                version="v7",
                doc_type="patent_draft",
            )],
            vault_services=services,
            infer_edges=False,
        )

        call_args = services.ingestion_service.ingest.call_args
        request = call_args[0][0]
        assert request.metadata["codes"] == "PV06,CF-1"
        assert request.metadata["version_label"] == "v7"
        assert "version" not in request.metadata

    @pytest.mark.asyncio
    async def test_bis_010_per_file_error_isolation(self):
        """Per-file errors do not abort the batch."""
        services = _make_services()
        call_idx = 0

        async def failing_ingest(request):
            nonlocal call_idx
            call_idx += 1
            if call_idx == 2:
                raise RuntimeError("Adapter failure on file 2")
            return _make_ingest_result(f"doc-{call_idx}")

        services.ingestion_service.ingest = AsyncMock(side_effect=failing_ingest)
        svc = BatchIngestService()

        result = await svc.run(
            files=[
                _fd("/tmp/ok1.md", title="OK1"),
                _fd("/tmp/bad.md", title="Bad"),
                _fd("/tmp/ok2.md", title="OK2"),
            ],
            vault_services=services,
            infer_edges=False,
        )

        assert result.docs_new == 2
        assert result.error_count == 1
        assert len(result.errors) == 1
        assert result.errors[0]["filename"] == "bad.md"
        assert "Adapter failure" in result.errors[0]["message"]

    @pytest.mark.asyncio
    async def test_bis_011_abstract_tracking(self):
        """Abstract tracking uses config.abstraction.enabled."""
        # Abstraction disabled
        services = _make_services(abstraction_enabled=False)
        svc = BatchIngestService()

        result = await svc.run(
            files=[_fd("/tmp/a.md"), _fd("/tmp/b.md")],
            vault_services=services,
            infer_edges=False,
        )

        assert result.abstracts_generated == 0
        assert result.abstracts_deferred == 2

        # Abstraction enabled
        services2 = _make_services(abstraction_enabled=True)
        result2 = await svc.run(
            files=[_fd("/tmp/c.md"), _fd("/tmp/d.md")],
            vault_services=services2,
            infer_edges=False,
        )

        assert result2.abstracts_generated == 2
        assert result2.abstracts_deferred == 0


# ---------------------------------------------------------------------------
# 5. Phase 3: Post-Ingest Edge Execution (BIS-012, BIS-013)
# ---------------------------------------------------------------------------


class TestEdgeExecution:

    @pytest.mark.asyncio
    async def test_bis_012_edge_plan_executed_after_ingestion(self):
        """Edge plan resolved and executed after all files ingested."""
        services = _make_services()
        svc = BatchIngestService()

        result = await svc.run(
            files=[
                _fd("/tmp/v1.md", title="Doc", version="v1", doc_type="patent_draft"),
                _fd("/tmp/v2.md", title="Doc", version="v2", doc_type="patent_draft"),
            ],
            vault_services=services,
            infer_edges=True,
        )

        # Should have a supersedes edge
        assert result.edges_created.get("supersedes", 0) >= 1

    @pytest.mark.asyncio
    async def test_bis_013_edges_dropped_for_failed_files(self):
        """Edges dropped when referenced file failed ingestion."""
        services = _make_services()
        call_idx = 0

        async def partial_ingest(request):
            nonlocal call_idx
            call_idx += 1
            if call_idx == 1:
                return _make_ingest_result("doc-v1")
            raise RuntimeError("File 2 failed")

        services.ingestion_service.ingest = AsyncMock(side_effect=partial_ingest)
        svc = BatchIngestService()

        result = await svc.run(
            files=[
                _fd("/tmp/v1.md", title="Doc", version="v1", doc_type="patent_draft"),
                _fd("/tmp/v2.md", title="Doc", version="v2", doc_type="patent_draft"),
            ],
            vault_services=services,
            infer_edges=True,
        )

        assert result.error_count == 1
        # The supersedes edge referencing v2 should be dropped
        assert result.edges_dropped >= 1
        assert len(result.edge_warnings) >= 1
        assert result.edge_warnings[0]["reason"] == "ingestion_failed"


# ---------------------------------------------------------------------------
# 6. Progress Callbacks (BIS-014, BIS-015, BIS-016, BIS-017)
# ---------------------------------------------------------------------------


class TestProgressCallbacks:

    @pytest.mark.asyncio
    async def test_bis_014_on_file_start_called(self):
        """on_file_start callback invoked before each file."""
        services = _make_services()
        svc = BatchIngestService()
        started = []

        async def on_start(index, total, filename):
            started.append((index, total, filename))

        await svc.run(
            files=[
                _fd("/tmp/doc1.md", title="Doc1"),
                _fd("/tmp/doc2.md", title="Doc2"),
            ],
            vault_services=services,
            infer_edges=False,
            on_file_start=on_start,
        )

        assert started == [(0, 2, "doc1.md"), (1, 2, "doc2.md")]

    @pytest.mark.asyncio
    async def test_bis_015_on_file_done_called(self):
        """on_file_done callback invoked after successful ingestion."""
        services = _make_services()
        svc = BatchIngestService()
        done = []

        async def on_done(index, total, filename, document_id):
            done.append((index, total, filename, document_id))

        await svc.run(
            files=[
                _fd("/tmp/doc1.md", title="Doc1"),
                _fd("/tmp/doc2.md", title="Doc2"),
            ],
            vault_services=services,
            infer_edges=False,
            on_file_done=on_done,
        )

        assert len(done) == 2
        assert done[0][0] == 0
        assert done[0][2] == "doc1.md"
        assert done[0][3] is not None  # document_id
        assert done[1][0] == 1
        assert done[1][2] == "doc2.md"

    @pytest.mark.asyncio
    async def test_bis_016_on_file_error_called(self):
        """on_file_error callback invoked on per-file failure."""
        services = _make_services()
        call_idx = 0

        async def failing_ingest(request):
            nonlocal call_idx
            call_idx += 1
            if call_idx == 2:
                raise RuntimeError("Adapter crash")
            return _make_ingest_result(f"doc-{call_idx}")

        services.ingestion_service.ingest = AsyncMock(side_effect=failing_ingest)
        svc = BatchIngestService()

        done = []
        errors = []

        async def on_done(index, total, filename, document_id):
            done.append(filename)

        async def on_error(index, total, filename, error_message):
            errors.append((filename, error_message))

        await svc.run(
            files=[
                _fd("/tmp/ok1.md"),
                _fd("/tmp/bad.md"),
                _fd("/tmp/ok2.md"),
            ],
            vault_services=services,
            infer_edges=False,
            on_file_done=on_done,
            on_file_error=on_error,
        )

        assert len(done) == 2
        assert len(errors) == 1
        assert errors[0][0] == "bad.md"
        assert "Adapter crash" in errors[0][1]

    @pytest.mark.asyncio
    async def test_bis_017_callbacks_optional(self):
        """Callbacks default to None with no errors."""
        services = _make_services()
        call_idx = 0

        async def mixed_ingest(request):
            nonlocal call_idx
            call_idx += 1
            if call_idx == 2:
                raise RuntimeError("fail")
            return _make_ingest_result(f"doc-{call_idx}")

        services.ingestion_service.ingest = AsyncMock(side_effect=mixed_ingest)
        svc = BatchIngestService()

        # No callbacks passed -- should not raise
        result = await svc.run(
            files=[_fd("/tmp/a.md"), _fd("/tmp/b.md")],
            vault_services=services,
            infer_edges=False,
        )

        assert result.docs_new == 1
        assert result.error_count == 1


# ---------------------------------------------------------------------------
# 7. Caller Integration (BIS-018, BIS-019)
# ---------------------------------------------------------------------------


class TestCallerIntegration:

    @pytest.mark.asyncio
    async def test_bis_018_summary_dict_structure(self):
        """IngestSummary.to_dict() produces the JSON structure both callers need."""
        services = _make_services()
        svc = BatchIngestService()

        result = await svc.run(
            files=[_fd("/tmp/test.md", title="Test")],
            vault_services=services,
            infer_edges=False,
        )

        d = result.to_dict()
        assert d["documents_created"] == {"new": 1, "new_version": 0}
        assert d["metadata_pending"] == 1
        assert d["edges_created"] == {}
        assert d["edges_staged"] == {}
        assert d["edges_dropped"] == 0
        assert "edge_warnings" not in d  # omitted when empty
        assert d["abstracts_deferred"] == 1
        assert d["abstracts_generated"] == 0
        assert d["error_count"] == 0
        assert d["errors"] == []

    @pytest.mark.asyncio
    async def test_bis_019_summary_with_edges_and_errors(self):
        """Summary captures edges and errors from a mixed batch."""
        services = _make_services()
        call_idx = 0

        async def mixed_ingest(request):
            nonlocal call_idx
            call_idx += 1
            if call_idx == 3:
                raise RuntimeError("corrupt file")
            return _make_ingest_result(f"doc-{call_idx}")

        services.ingestion_service.ingest = AsyncMock(side_effect=mixed_ingest)
        svc = BatchIngestService()

        result = await svc.run(
            files=[
                _fd("/tmp/v1.md", title="Doc", version="v1", doc_type="patent_draft"),
                _fd("/tmp/v2.md", title="Doc", version="v2", doc_type="patent_draft"),
                _fd("/tmp/bad.md", title="Bad"),
            ],
            vault_services=services,
            infer_edges=True,
        )

        d = result.to_dict()
        assert d["documents_created"]["new"] == 2
        assert d["error_count"] == 1
        assert len(d["errors"]) == 1
        assert d["errors"][0]["filename"] == "bad.md"
        # Should have at least one supersedes edge from the version chain
        assert d["edges_created"].get("supersedes", 0) >= 1
