"""Ingestion Pipeline tests: BH-018 through BH-026, BH-049 through BH-052.

Covers duplicate detection, force re-ingestion, pipeline failure quarantine,
LLM failure handling, abstraction_skipped, async pipeline execution, and
source file provenance (source_modified_at).
"""

import asyncio
import os
from datetime import datetime, timezone
from pathlib import Path

import pytest

from sage.api.errors import DuplicateContentError
from sage.models.enums import PipelineStatus, SourceType
from sage.models.schemas import IngestRequest


def _create_test_file(tmp_vault_dir: Path, relative_path: str, content: str = "# Test\n\nTest content.") -> Path:
    """Create a test Markdown file in the vault's sources directory."""
    full_path = tmp_vault_dir / "sources" / relative_path
    full_path.parent.mkdir(parents=True, exist_ok=True)
    full_path.write_text(content)
    return full_path


# ---------------------------------------------------------------------------
# BH-018: Duplicate content detection returns 409
# ---------------------------------------------------------------------------

async def test_bh_018_duplicate_content_409(
    tmp_vault_dir, graph_store, ingestion_service
):
    _create_test_file(tmp_vault_dir, "patents/doc_a.md")

    # First ingest succeeds
    request = IngestRequest(
        source="patents/doc_a.md",
        adapter=SourceType.MARKDOWN,
    )
    doc, status = await ingestion_service.ingest(request, "test_vault")
    assert status == 201

    # Allow background tasks to run
    await asyncio.sleep(0.2)

    # Second ingest (same path, same content) returns 409
    with pytest.raises(DuplicateContentError) as exc_info:
        await ingestion_service.ingest(request, "test_vault")

    err = exc_info.value
    assert err.status_code == 409
    assert err.code == "duplicate_content"
    assert err.detail["existing_document_id"] == doc.id


# ---------------------------------------------------------------------------
# BH-019: Force re-ingestion bypasses duplicate detection
# ---------------------------------------------------------------------------

async def test_bh_019_force_reingestion(
    tmp_vault_dir, graph_store, ingestion_service
):
    _create_test_file(tmp_vault_dir, "patents/doc_force.md")

    request = IngestRequest(
        source="patents/doc_force.md",
        adapter=SourceType.MARKDOWN,
    )
    doc1, status1 = await ingestion_service.ingest(request, "test_vault")
    assert status1 == 201
    original_id = doc1.id

    await asyncio.sleep(0.2)

    # Force re-ingest
    force_request = IngestRequest(
        source="patents/doc_force.md",
        adapter=SourceType.MARKDOWN,
        force=True,
    )
    doc2, status2 = await ingestion_service.ingest(force_request, "test_vault")

    assert status2 == 200
    assert doc2.id == original_id  # Same document record
    assert doc2.pipeline_status == PipelineStatus.PROJECTION_COMPLETE


# ---------------------------------------------------------------------------
# BH-020: Failed pipeline quarantines document from retrieval
# (Retrieval not implemented in this slice; test that failed docs
#  are marked correctly for quarantine)
# ---------------------------------------------------------------------------

async def test_bh_020_failed_pipeline_marks_document(
    tmp_vault_dir, graph_store, ingestion_service_failing_llm
):
    _create_test_file(tmp_vault_dir, "patents/doc_fail.md")

    request = IngestRequest(
        source="patents/doc_fail.md",
        adapter=SourceType.MARKDOWN,
    )
    doc, status = await ingestion_service_failing_llm.ingest(request, "test_vault")
    assert status == 201

    # Wait for background pipeline to fail
    await asyncio.sleep(0.5)

    fetched = await graph_store.get_document(doc.id)
    assert fetched.pipeline_status == PipelineStatus.FAILED
    assert fetched.pipeline_error is not None


# ---------------------------------------------------------------------------
# BH-022: Failed document still visible via get_document
# ---------------------------------------------------------------------------

async def test_bh_022_failed_document_visible(
    tmp_vault_dir, graph_store, ingestion_service_failing_llm
):
    _create_test_file(tmp_vault_dir, "patents/doc_visible.md")

    request = IngestRequest(
        source="patents/doc_visible.md",
        adapter=SourceType.MARKDOWN,
    )
    doc, _ = await ingestion_service_failing_llm.ingest(request, "test_vault")
    await asyncio.sleep(0.5)

    fetched = await graph_store.get_document(doc.id)
    assert fetched is not None
    assert fetched.pipeline_status == PipelineStatus.FAILED
    assert fetched.pipeline_error is not None
    assert "LLM unavailable" in fetched.pipeline_error


# ---------------------------------------------------------------------------
# BH-024: LLM failure during abstraction results in failed status
# ---------------------------------------------------------------------------

async def test_bh_024_llm_failure_results_in_failed(
    tmp_vault_dir, graph_store, ingestion_service_failing_llm
):
    _create_test_file(tmp_vault_dir, "patents/doc_llm_fail.md", "# Document\n\nContent for abstraction.")

    request = IngestRequest(
        source="patents/doc_llm_fail.md",
        adapter=SourceType.MARKDOWN,
    )
    doc, status = await ingestion_service_failing_llm.ingest(request, "test_vault")
    assert status == 201
    assert doc.pipeline_status == PipelineStatus.PROJECTION_COMPLETE

    # Wait for background pipeline to reach Stage 3 and fail
    await asyncio.sleep(0.5)

    fetched = await graph_store.get_document(doc.id)
    assert fetched.pipeline_status == PipelineStatus.FAILED
    assert fetched.pipeline_error is not None
    assert fetched.semantic_abstract is None


# ---------------------------------------------------------------------------
# BH-025: Abstraction disabled produces abstraction_skipped
# ---------------------------------------------------------------------------

async def test_bh_025_abstraction_disabled(
    tmp_vault_dir, graph_store, ingestion_service_no_abstraction
):
    _create_test_file(tmp_vault_dir, "patents/doc_no_abstract.md")

    request = IngestRequest(
        source="patents/doc_no_abstract.md",
        adapter=SourceType.MARKDOWN,
    )
    doc, status = await ingestion_service_no_abstraction.ingest(request, "test_vault")
    assert status == 201

    # Wait for background pipeline
    await asyncio.sleep(0.5)

    fetched = await graph_store.get_document(doc.id)
    assert fetched.pipeline_status == PipelineStatus.ABSTRACTION_SKIPPED
    assert fetched.semantic_abstract is None


# ---------------------------------------------------------------------------
# BH-026: Pipeline stages run as asyncio background tasks
# ---------------------------------------------------------------------------

async def test_bh_026_async_pipeline(
    tmp_vault_dir, graph_store, ingestion_service
):
    _create_test_file(tmp_vault_dir, "patents/doc_async.md", "# Async Test\n\nContent here.")

    request = IngestRequest(
        source="patents/doc_async.md",
        adapter=SourceType.MARKDOWN,
    )
    doc, status = await ingestion_service.ingest(request, "test_vault")

    # Ingest returns immediately with projection_complete
    assert status == 201
    assert doc.pipeline_status == PipelineStatus.PROJECTION_COMPLETE

    # Wait for background pipeline to complete
    await asyncio.sleep(0.5)

    fetched = await graph_store.get_document(doc.id)
    assert fetched.pipeline_status == PipelineStatus.ABSTRACTION_COMPLETE
    assert fetched.indexed_at is not None
    assert fetched.semantic_abstract is not None


# ---------------------------------------------------------------------------
# BH-049: New document ingestion sets source_modified_at from file mtime
# ---------------------------------------------------------------------------

async def test_bh_049_source_modified_at_set_on_ingest(
    tmp_vault_dir, graph_store, ingestion_service
):
    full_path = _create_test_file(tmp_vault_dir, "patents/doc_mtime.md")

    # Set a known mtime in the past
    known_mtime = datetime(2023, 6, 15, 12, 0, 0, tzinfo=timezone.utc)
    os.utime(full_path, (full_path.stat().st_atime, known_mtime.timestamp()))

    request = IngestRequest(
        source="patents/doc_mtime.md",
        adapter=SourceType.MARKDOWN,
    )
    doc, status = await ingestion_service.ingest(request, "test_vault")
    assert status == 201

    assert doc.source_modified_at is not None
    assert doc.source_modified_at.tzinfo is not None  # timezone-aware
    assert abs((doc.source_modified_at - known_mtime).total_seconds()) < 1.0


# ---------------------------------------------------------------------------
# BH-050: Force re-ingestion updates source_modified_at
# ---------------------------------------------------------------------------

async def test_bh_050_force_reingestion_updates_source_modified_at(
    tmp_vault_dir, graph_store, ingestion_service
):
    full_path = _create_test_file(tmp_vault_dir, "patents/doc_mtime_force.md")

    # Set old mtime
    old_mtime = datetime(2022, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
    os.utime(full_path, (full_path.stat().st_atime, old_mtime.timestamp()))

    request = IngestRequest(
        source="patents/doc_mtime_force.md",
        adapter=SourceType.MARKDOWN,
    )
    doc1, _ = await ingestion_service.ingest(request, "test_vault")
    await asyncio.sleep(0.2)

    original_source_mtime = doc1.source_modified_at

    # Touch the file to update mtime
    new_mtime = datetime(2024, 7, 1, 0, 0, 0, tzinfo=timezone.utc)
    os.utime(full_path, (full_path.stat().st_atime, new_mtime.timestamp()))

    force_request = IngestRequest(
        source="patents/doc_mtime_force.md",
        adapter=SourceType.MARKDOWN,
        force=True,
    )
    doc2, status2 = await ingestion_service.ingest(force_request, "test_vault")
    assert status2 == 200

    # Retrieve from store to verify persistence
    fetched = await graph_store.get_document(doc2.id)
    assert abs((fetched.source_modified_at - new_mtime).total_seconds()) < 1.0
    assert fetched.source_modified_at != original_source_mtime


# ---------------------------------------------------------------------------
# BH-051: source_modified_at round-trips through graph store
# ---------------------------------------------------------------------------

async def test_bh_051_source_modified_at_round_trip(
    tmp_vault_dir, graph_store, ingestion_service
):
    full_path = _create_test_file(tmp_vault_dir, "patents/doc_roundtrip.md")

    known_mtime = datetime(2021, 3, 10, 8, 30, 0, tzinfo=timezone.utc)
    os.utime(full_path, (full_path.stat().st_atime, known_mtime.timestamp()))

    request = IngestRequest(
        source="patents/doc_roundtrip.md",
        adapter=SourceType.MARKDOWN,
    )
    doc, _ = await ingestion_service.ingest(request, "test_vault")

    fetched = await graph_store.get_document(doc.id)
    assert fetched.source_modified_at is not None
    assert fetched.source_modified_at.tzinfo is not None
    assert abs((fetched.source_modified_at - known_mtime).total_seconds()) < 1.0


# ---------------------------------------------------------------------------
# BH-052: created_at remains SAGE ingestion time, distinct from source_modified_at
# ---------------------------------------------------------------------------

async def test_bh_052_created_at_is_ingestion_time(
    tmp_vault_dir, graph_store, ingestion_service
):
    full_path = _create_test_file(tmp_vault_dir, "patents/doc_old.md")

    # Set mtime well in the past
    old_mtime = datetime(2020, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
    os.utime(full_path, (full_path.stat().st_atime, old_mtime.timestamp()))

    before_ingest = datetime.now(timezone.utc)

    request = IngestRequest(
        source="patents/doc_old.md",
        adapter=SourceType.MARKDOWN,
    )
    doc, _ = await ingestion_service.ingest(request, "test_vault")

    # created_at should be close to now, not the old file mtime
    assert abs((doc.created_at - before_ingest).total_seconds()) < 5.0

    # source_modified_at should match the old mtime
    assert abs((doc.source_modified_at - old_mtime).total_seconds()) < 1.0

    # They must be different
    assert doc.created_at != doc.source_modified_at
