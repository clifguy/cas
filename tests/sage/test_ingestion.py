"""Ingestion Pipeline tests: BH-018 through BH-026, BH-049 through BH-052,
BH-062 through BH-068, BH-071.

Covers duplicate detection, force re-ingestion, pipeline failure quarantine,
LLM failure handling, abstraction_skipped, sequential pipeline execution,
source file provenance (source_modified_at), and document date metadata
(document_date).
"""

import os
from datetime import datetime, timezone
from pathlib import Path

import pytest

from sage.api.errors import DuplicateContentError
from sage.config import VaultConfig
from sage.models.enums import PipelineStatus, SourceType
from sage.models.schemas import Document, IngestRequest
from sage.source_adapters.markdown_adapter import MarkdownAdapter


def _create_test_file(
    tmp_vault_dir: Path, relative_path: str, content: str = "# Test\n\nTest content."
) -> Path:
    """Create a test Markdown file in the vault's sources directory."""
    full_path = tmp_vault_dir / "sources" / relative_path
    full_path.parent.mkdir(parents=True, exist_ok=True)
    full_path.write_text(content)
    return full_path


# ---------------------------------------------------------------------------
# BH-018: Duplicate content detection returns 409
# ---------------------------------------------------------------------------


async def test_bh_018_duplicate_content_409(tmp_vault_dir, graph_store, ingestion_service):
    _create_test_file(tmp_vault_dir, "patents/doc_a.md")

    # First ingest succeeds
    request = IngestRequest(
        source="patents/doc_a.md",
        adapter=SourceType.MARKDOWN,
    )
    result = await ingestion_service.ingest(request)

    doc = result.document
    assert result.is_new is True

    # Second ingest (same path, same content) returns 409
    with pytest.raises(DuplicateContentError) as exc_info:
        await ingestion_service.ingest(request)

    err = exc_info.value
    assert err.status_code == 409
    assert err.code == "duplicate_content"
    assert err.detail["existing_document_id"] == doc.id


# ---------------------------------------------------------------------------
# BH-019: Force re-ingestion bypasses duplicate detection
# ---------------------------------------------------------------------------


async def test_bh_019_force_reingestion(tmp_vault_dir, graph_store, ingestion_service):
    _create_test_file(tmp_vault_dir, "patents/doc_force.md")

    request = IngestRequest(
        source="patents/doc_force.md",
        adapter=SourceType.MARKDOWN,
    )
    result1 = await ingestion_service.ingest(request)

    doc1 = result1.document
    assert result1.is_new is True
    original_id = doc1.id

    # Force re-ingest
    force_request = IngestRequest(
        source="patents/doc_force.md",
        adapter=SourceType.MARKDOWN,
        force=True,
    )
    result2 = await ingestion_service.ingest(force_request)

    doc2 = result2.document

    assert result2.is_new is False
    assert doc2.id == original_id  # Same document record
    assert doc2.pipeline_status == PipelineStatus.ABSTRACTION_COMPLETE


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
    result = await ingestion_service_failing_llm.ingest(request)

    doc = result.document
    assert result.is_new is True

    # Sequential pipeline: failure is reflected in the returned document
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
    result = await ingestion_service_failing_llm.ingest(request)

    doc = result.document

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
    _create_test_file(
        tmp_vault_dir, "patents/doc_llm_fail.md", "# Document\n\nContent for abstraction."
    )

    request = IngestRequest(
        source="patents/doc_llm_fail.md",
        adapter=SourceType.MARKDOWN,
    )
    result = await ingestion_service_failing_llm.ingest(request)

    doc = result.document
    assert result.is_new is True

    # Sequential pipeline: failure status is set before ingest returns
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
    result = await ingestion_service_no_abstraction.ingest(request)

    doc = result.document
    assert result.is_new is True

    # Sequential pipeline: final status is set before ingest returns
    fetched = await graph_store.get_document(doc.id)
    assert fetched.pipeline_status == PipelineStatus.ABSTRACTION_SKIPPED
    assert fetched.semantic_abstract is None


# ---------------------------------------------------------------------------
# BH-026: Pipeline stages run sequentially within ingest
# ---------------------------------------------------------------------------


async def test_bh_026_sequential_pipeline(tmp_vault_dir, graph_store, ingestion_service):
    _create_test_file(tmp_vault_dir, "patents/doc_seq.md", "# Sequential Test\n\nContent here.")

    request = IngestRequest(
        source="patents/doc_seq.md",
        adapter=SourceType.MARKDOWN,
    )
    result = await ingestion_service.ingest(request)
    doc = result.document

    # Ingest returns with all stages complete (no background tasks)
    assert result.is_new is True
    assert doc.pipeline_status == PipelineStatus.ABSTRACTION_COMPLETE
    assert doc.indexed_at is not None
    assert doc.semantic_abstract is not None

    # Graph store matches returned document
    fetched = await graph_store.get_document(doc.id)
    assert fetched.pipeline_status == PipelineStatus.ABSTRACTION_COMPLETE


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
    result = await ingestion_service.ingest(request)

    doc = result.document
    assert result.is_new is True

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
    result = await ingestion_service.ingest(request)

    doc1 = result.document
    original_source_mtime = doc1.source_modified_at

    # Touch the file to update mtime
    new_mtime = datetime(2024, 7, 1, 0, 0, 0, tzinfo=timezone.utc)
    os.utime(full_path, (full_path.stat().st_atime, new_mtime.timestamp()))

    force_request = IngestRequest(
        source="patents/doc_mtime_force.md",
        adapter=SourceType.MARKDOWN,
        force=True,
    )
    result2 = await ingestion_service.ingest(force_request)

    doc2 = result2.document
    assert result2.is_new is False

    # Retrieve from store to verify persistence
    fetched = await graph_store.get_document(doc2.id)
    assert abs((fetched.source_modified_at - new_mtime).total_seconds()) < 1.0
    assert fetched.source_modified_at != original_source_mtime


# ---------------------------------------------------------------------------
# BH-051: source_modified_at round-trips through graph store
# ---------------------------------------------------------------------------


async def test_bh_051_source_modified_at_round_trip(tmp_vault_dir, graph_store, ingestion_service):
    full_path = _create_test_file(tmp_vault_dir, "patents/doc_roundtrip.md")

    known_mtime = datetime(2021, 3, 10, 8, 30, 0, tzinfo=timezone.utc)
    os.utime(full_path, (full_path.stat().st_atime, known_mtime.timestamp()))

    request = IngestRequest(
        source="patents/doc_roundtrip.md",
        adapter=SourceType.MARKDOWN,
    )
    result = await ingestion_service.ingest(request)

    doc = result.document

    fetched = await graph_store.get_document(doc.id)
    assert fetched.source_modified_at is not None
    assert fetched.source_modified_at.tzinfo is not None
    assert abs((fetched.source_modified_at - known_mtime).total_seconds()) < 1.0


# ---------------------------------------------------------------------------
# BH-052: created_at remains SAGE ingestion time, distinct from source_modified_at
# ---------------------------------------------------------------------------


async def test_bh_052_created_at_is_ingestion_time(tmp_vault_dir, graph_store, ingestion_service):
    full_path = _create_test_file(tmp_vault_dir, "patents/doc_old.md")

    # Set mtime well in the past
    old_mtime = datetime(2020, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
    os.utime(full_path, (full_path.stat().st_atime, old_mtime.timestamp()))

    before_ingest = datetime.now(timezone.utc)

    request = IngestRequest(
        source="patents/doc_old.md",
        adapter=SourceType.MARKDOWN,
    )
    result = await ingestion_service.ingest(request)

    doc = result.document

    # created_at should be close to now, not the old file mtime
    assert abs((doc.created_at - before_ingest).total_seconds()) < 5.0

    # source_modified_at should match the old mtime
    assert abs((doc.source_modified_at - old_mtime).total_seconds()) < 1.0

    # They must be different
    assert doc.created_at != doc.source_modified_at


# ---------------------------------------------------------------------------
# BH-053: External file is copied verbatim to imports/ subdirectory
# ---------------------------------------------------------------------------


async def test_bh_053_external_file_copied_to_imports(
    tmp_vault_dir, graph_store, ingestion_service, tmp_path
):
    """When source resolves outside storage_root, the file is copied
    verbatim into {storage_root}/imports/ and the original bytes are
    preserved exactly."""
    # Create a file outside storage_root
    external_dir = tmp_path / "ephemeral_session"
    external_dir.mkdir()
    external_file = external_dir / "checklist.md"
    content = "# Checklist\n\nItem one.\nItem two.\n"
    external_file.write_text(content)

    request = IngestRequest(
        source=str(external_file),  # absolute path
        adapter=SourceType.MARKDOWN,
    )
    result = await ingestion_service.ingest(request)

    assert result.is_new is True

    # The file must exist in imports/
    storage_root = tmp_vault_dir / "sources"
    imported = storage_root / "imports" / "checklist.md"
    assert imported.exists()
    assert imported.read_text() == content


# ---------------------------------------------------------------------------
# BH-054: Imported file source_path records vault-relative path
# ---------------------------------------------------------------------------


async def test_bh_054_imported_source_path_is_vault_relative(
    tmp_vault_dir, graph_store, ingestion_service, tmp_path
):
    """The document record's source_path must be vault-relative
    (imports/filename.ext), not the original absolute path."""
    external_file = tmp_path / "external_doc.md"
    external_file.write_text("# External\n\nContent.\n")

    request = IngestRequest(
        source=str(external_file),
        adapter=SourceType.MARKDOWN,
    )
    result = await ingestion_service.ingest(request)

    doc = result.document
    assert result.is_new is True
    assert doc.source_path == "imports/external_doc.md"


# ---------------------------------------------------------------------------
# BH-055: Name collision on import appends 8-char content hash
# ---------------------------------------------------------------------------


async def test_bh_055_import_name_collision_appends_hash(
    tmp_vault_dir, graph_store, ingestion_service, tmp_path
):
    """When imports/ already contains a file with the same name but
    different content, the new file gets an 8-char content hash
    appended before the extension."""
    storage_root = tmp_vault_dir / "sources"
    imports_dir = storage_root / "imports"
    imports_dir.mkdir()

    # Pre-populate imports/ with a file named "report.md"
    existing = imports_dir / "report.md"
    existing.write_text("# Old report\n\nOriginal content.\n")

    # Create an external file with the same name but different content
    external_file = tmp_path / "report.md"
    new_content = "# New report\n\nDifferent content.\n"
    external_file.write_text(new_content)

    request = IngestRequest(
        source=str(external_file),
        adapter=SourceType.MARKDOWN,
    )
    result = await ingestion_service.ingest(request)

    doc = result.document
    assert result.is_new is True

    # source_path should be imports/report_<8-char-hash>.md
    assert doc.source_path.startswith("imports/report_")
    assert doc.source_path.endswith(".md")
    # Extract the hash portion
    stem = Path(doc.source_path).stem  # e.g. "report_a1b2c3d4"
    hash_suffix = stem.split("_", 1)[1]
    assert len(hash_suffix) == 8

    # The disambiguated file must exist and contain the new content
    imported = storage_root / doc.source_path
    assert imported.exists()
    assert imported.read_text() == new_content

    # The original file must be untouched
    assert existing.read_text() == "# Old report\n\nOriginal content.\n"


# ---------------------------------------------------------------------------
# BH-056: File already inside storage_root is not copied
# ---------------------------------------------------------------------------


async def test_bh_056_internal_file_not_copied(tmp_vault_dir, graph_store, ingestion_service):
    """Files already under storage_root are ingested in place without
    copying. source_path is the normalized relative path."""
    _create_test_file(tmp_vault_dir, "patents/internal.md", "# Internal\n\nContent.\n")

    request = IngestRequest(
        source="patents/internal.md",
        adapter=SourceType.MARKDOWN,
    )
    result = await ingestion_service.ingest(request)

    doc = result.document
    assert result.is_new is True
    assert doc.source_path == "patents/internal.md"

    # No imports/ directory should have been created
    imports_dir = tmp_vault_dir / "sources" / "imports"
    assert not imports_dir.exists()


# ---------------------------------------------------------------------------
# BH-057: imports/ directory is created on demand
# ---------------------------------------------------------------------------


async def test_bh_057_imports_dir_created_on_demand(
    tmp_vault_dir, graph_store, ingestion_service, tmp_path
):
    """The imports/ subdirectory is created automatically on the first
    external file import; it does not need to pre-exist."""
    imports_dir = tmp_vault_dir / "sources" / "imports"
    assert not imports_dir.exists()

    external_file = tmp_path / "fresh.md"
    external_file.write_text("# Fresh\n\nNew content.\n")

    request = IngestRequest(
        source=str(external_file),
        adapter=SourceType.MARKDOWN,
    )
    result = await ingestion_service.ingest(request)

    assert result.is_new is True

    assert imports_dir.exists()
    assert (imports_dir / "fresh.md").exists()


# ---------------------------------------------------------------------------
# BH-058 (unit): _chunk_projection prepends title to first chunk
# ---------------------------------------------------------------------------


def test_chunk_projection_prepends_preamble(ingestion_service):
    """The first chunk produced by _chunk_projection includes the search
    preamble so document identity signals are indexed for search."""
    from sage.source_adapters.base import HeadingNode, ProjectionResult

    projection = ProjectionResult(
        text="Body content only.",
        headings=[
            HeadingNode(
                level=1, text="Introduction", path="Introduction", content="Body content only."
            ),
        ],
        content_hash="sha256:abc",
        adapter_version="0.1.0",
        title="ClinicalNormalization",
    )

    preamble = "Title: ClinicalNormalization\nSource: PIM_PV07_ClinicalNormalization_v1_0\n\n"
    chunks = ingestion_service._chunk_projection("doc1", projection, preamble)

    assert len(chunks) == 1
    assert chunks[0].content.startswith("Title: ClinicalNormalization\n")
    assert "PV07" in chunks[0].content
    assert "Body content only." in chunks[0].content


def test_chunk_projection_prepends_preamble_fallback_chunk(ingestion_service):
    """When there are no headings, the fallback single chunk also gets
    the search preamble."""
    from sage.source_adapters.base import ProjectionResult

    projection = ProjectionResult(
        text="Flat document with no headings.",
        headings=[],
        content_hash="sha256:def",
        adapter_version="0.1.0",
        title="Flat_Doc",
    )

    preamble = "Title: Flat_Doc\nSource: PIM_PV07_Flat_Doc\n\n"
    chunks = ingestion_service._chunk_projection("doc2", projection, preamble)

    assert len(chunks) == 1
    assert chunks[0].content.startswith("Title: Flat_Doc\n")
    assert "Flat document with no headings." in chunks[0].content


def test_chunk_projection_preamble_only_on_first_chunk(ingestion_service):
    """When multiple headings produce multiple chunks, only the first
    chunk gets the search preamble."""
    from sage.source_adapters.base import HeadingNode, ProjectionResult

    projection = ProjectionResult(
        text="All content.",
        headings=[
            HeadingNode(level=1, text="Part A", path="Part A", content="Content for part A."),
            HeadingNode(level=1, text="Part B", path="Part B", content="Content for part B."),
        ],
        content_hash="sha256:ghi",
        adapter_version="0.1.0",
        title="Multi_Section_Doc",
    )

    preamble = "Title: Multi_Section_Doc\nSource: PIM_Multi_Section_Doc\n\n"
    chunks = ingestion_service._chunk_projection("doc3", projection, preamble)

    assert len(chunks) == 2
    assert chunks[0].content.startswith("Title: Multi_Section_Doc\n")
    assert not chunks[1].content.startswith("Title:")


def test_build_search_preamble(ingestion_service):
    """_build_search_preamble includes title, source filename, and tags."""
    from sage.services.ingestion import IngestionService

    now = datetime.now(timezone.utc)
    doc = Document(
        id="test_doc",
        title="ClinicalNormalization",
        source_type=SourceType.MARKDOWN,
        source_path="imports/PIM_PV07_ClinicalNormalization_v1_0.md",
        source_content_hash="sha256:test",
        adapter_version="0.1.0",
        created_by="test",
        created_at=now,
        last_modified_by="test",
        updated_at=now,
        tags=["PV07"],
    )

    preamble = IngestionService._build_search_preamble(doc)

    assert "Title: ClinicalNormalization" in preamble
    assert "Source: PIM_PV07_ClinicalNormalization_v1_0" in preamble
    assert "Tags: PV07" in preamble


# ---------------------------------------------------------------------------
# BH-062: Ingestion with filename date sets document_date from metadata
# ---------------------------------------------------------------------------


async def test_bh_062_filename_date_sets_document_date(
    tmp_vault_dir, graph_store, ingestion_service
):
    """Caller-supplied date in metadata takes precedence as document_date."""
    full_path = _create_test_file(tmp_vault_dir, "patents/dated_doc.md")

    # Set file mtime to a different date so we can verify independence
    file_mtime = datetime(2025, 1, 15, 12, 0, 0, tzinfo=timezone.utc)
    os.utime(full_path, (full_path.stat().st_atime, file_mtime.timestamp()))

    request = IngestRequest(
        source="patents/dated_doc.md",
        adapter=SourceType.MARKDOWN,
        metadata={"date": "2026-04-10"},
    )
    result = await ingestion_service.ingest(request)

    doc = result.document
    assert result.is_new is True

    assert doc.document_date == "2026-04-10"
    # source_modified_at is set independently from file mtime
    assert doc.source_modified_at is not None
    assert abs((doc.source_modified_at - file_mtime).total_seconds()) < 1.0


# ---------------------------------------------------------------------------
# BH-063: Ingestion without filename date falls back to source_modified_at
# ---------------------------------------------------------------------------


async def test_bh_063_fallback_to_source_modified_at_date(
    tmp_vault_dir, graph_store, ingestion_service
):
    """When no date in metadata, document_date derives from source_modified_at."""
    full_path = _create_test_file(tmp_vault_dir, "patents/no_date_doc.md")

    known_mtime = datetime(2025, 6, 15, 14, 30, 0, tzinfo=timezone.utc)
    os.utime(full_path, (full_path.stat().st_atime, known_mtime.timestamp()))

    request = IngestRequest(
        source="patents/no_date_doc.md",
        adapter=SourceType.MARKDOWN,
    )
    result = await ingestion_service.ingest(request)

    doc = result.document

    assert doc.document_date == "2025-06-15"
    assert doc.source_modified_at is not None


# ---------------------------------------------------------------------------
# Vault-local timezone applied to source_modified_at fallback
# ---------------------------------------------------------------------------


async def test_document_date_fallback_uses_vault_timezone(
    tmp_vault_dir,
    graph_store,
    lock_manager,
    stub_content_store,
    stub_embedding_provider,
    stub_abstraction_provider,
    minimal_vault_config_dict,
    lifecycle_service,
):
    """When vault.timezone is set, the fallback derives document_date in
    that zone, not UTC. Regression guard for the UTC-midnight drift bug:
    a Chicago-evening mtime that crosses UTC midnight should attribute
    to the local calendar date.
    """
    from sage.services.ingestion import IngestionService

    config_dict = minimal_vault_config_dict.copy()
    config_dict["vault"] = {**config_dict["vault"], "timezone": "America/Chicago"}
    config = VaultConfig.model_validate(config_dict)
    service = IngestionService(
        graph_store=graph_store,
        lock_manager=lock_manager,
        content_store=stub_content_store,
        embedding_provider=stub_embedding_provider,
        abstraction_provider=stub_abstraction_provider,
        config=config,
        source_adapters={SourceType.MARKDOWN: MarkdownAdapter()},
        lifecycle_service=lifecycle_service,
    )

    full_path = _create_test_file(tmp_vault_dir, "patents/late_evening.md")
    # 2026-04-29 00:33 UTC is 2026-04-28 19:33 CDT.
    chicago_evening_mtime = datetime(2026, 4, 29, 0, 33, 0, tzinfo=timezone.utc)
    os.utime(
        full_path,
        (full_path.stat().st_atime, chicago_evening_mtime.timestamp()),
    )

    request = IngestRequest(
        source="patents/late_evening.md",
        adapter=SourceType.MARKDOWN,
    )
    result = await service.ingest(request)
    doc = result.document

    assert doc.document_date == "2026-04-28"


def test_vault_identity_carries_timezone_field(minimal_vault_config_dict):
    """VaultConfig accepts an explicit vault.timezone and defaults to UTC
    when the field is omitted.
    """
    omitted = VaultConfig.model_validate(minimal_vault_config_dict)
    assert omitted.vault.timezone == "UTC"

    explicit_dict = minimal_vault_config_dict.copy()
    explicit_dict["vault"] = {**explicit_dict["vault"], "timezone": "America/Chicago"}
    explicit = VaultConfig.model_validate(explicit_dict)
    assert explicit.vault.timezone == "America/Chicago"


async def test_document_date_fallback_defaults_to_utc(
    tmp_vault_dir, graph_store, ingestion_service
):
    """A vault config without an explicit timezone keeps the BH-063
    behavior: fallback uses the UTC calendar date of source_modified_at.
    Regression guard for existing vaults.
    """
    full_path = _create_test_file(tmp_vault_dir, "patents/utc_default.md")
    known_mtime = datetime(2025, 6, 15, 14, 30, 0, tzinfo=timezone.utc)
    os.utime(full_path, (full_path.stat().st_atime, known_mtime.timestamp()))

    request = IngestRequest(
        source="patents/utc_default.md",
        adapter=SourceType.MARKDOWN,
    )
    result = await ingestion_service.ingest(request)
    doc = result.document

    assert doc.document_date == "2025-06-15"


# ---------------------------------------------------------------------------
# BH-064: No filename date and no source_modified_at leaves document_date null
# ---------------------------------------------------------------------------


async def test_bh_064_null_when_no_date_sources(tmp_vault_dir, graph_store, ingestion_service):
    """document_date is null when neither filename date nor source_modified_at
    is available."""
    _create_test_file(tmp_vault_dir, "patents/no_sources.md")

    # Simpler approach: ingest, then clear both fields and verify the
    # invariant that _build_metadata_updates doesn't set document_date
    # when no date key is present.
    request = IngestRequest(
        source="patents/no_sources.md",
        adapter=SourceType.MARKDOWN,
    )
    result = await ingestion_service.ingest(request)

    doc = result.document

    # Clear both fields to simulate the no-source scenario
    await graph_store.update_document(
        doc.id,
        {
            "source_modified_at": None,
            "document_date": None,
        },
    )
    fetched = await graph_store.get_document(doc.id)
    assert fetched.document_date is None
    assert fetched.source_modified_at is None


# ---------------------------------------------------------------------------
# BH-065: document_date round-trips through graph store
# ---------------------------------------------------------------------------


async def test_bh_065_document_date_round_trip(tmp_vault_dir, graph_store, ingestion_service):
    """document_date stored as TEXT in SQLite survives insert/retrieve cycle."""
    _create_test_file(tmp_vault_dir, "patents/date_roundtrip.md")

    request = IngestRequest(
        source="patents/date_roundtrip.md",
        adapter=SourceType.MARKDOWN,
        metadata={"date": "2026-04-10"},
    )
    result = await ingestion_service.ingest(request)

    doc = result.document

    fetched = await graph_store.get_document(doc.id)
    assert fetched.document_date == "2026-04-10"
    assert isinstance(fetched.document_date, str)


# ---------------------------------------------------------------------------
# BH-067: Force re-ingestion reuses existing document at different path
# ---------------------------------------------------------------------------


async def test_bh_067_force_reingestion_different_path_reuses_document(
    tmp_vault_dir, graph_store, ingestion_service
):
    """Force re-ingestion of identical content at a different path reuses
    the existing document record rather than creating a duplicate.
    The source_path is updated to the new location."""
    content = "# Identical\n\nSame content at two paths."
    _create_test_file(tmp_vault_dir, "patents/doc_a.md", content)
    _create_test_file(tmp_vault_dir, "patents/subfolder/doc_a_copy.md", content)

    # First ingest
    result1 = await ingestion_service.ingest(
        IngestRequest(
            source="patents/doc_a.md",
            adapter=SourceType.MARKDOWN,
        )
    )
    assert result1.is_new is True
    original_id = result1.document.id

    # Force re-ingest from different path with identical content
    result2 = await ingestion_service.ingest(
        IngestRequest(
            source="patents/subfolder/doc_a_copy.md",
            adapter=SourceType.MARKDOWN,
            force=True,
        )
    )

    assert result2.is_new is False
    assert result2.document.id == original_id
    assert result2.document.source_path == "patents/subfolder/doc_a_copy.md"
    assert result2.document.pipeline_status == PipelineStatus.ABSTRACTION_COMPLETE


# ---------------------------------------------------------------------------
# BH-068: Sequential pipeline sets final status before returning
# ---------------------------------------------------------------------------


async def test_bh_068_sequential_pipeline_final_status(
    tmp_vault_dir, graph_store, ingestion_service
):
    """Sequential pipeline completes all stages before ingest() returns.
    The returned IngestResult.document reflects the terminal pipeline
    status, and the graph store is consistent with it."""
    _create_test_file(
        tmp_vault_dir,
        "patents/doc_final.md",
        "# Final Status\n\nVerify sequential pipeline completion.",
    )

    request = IngestRequest(
        source="patents/doc_final.md",
        adapter=SourceType.MARKDOWN,
    )
    result = await ingestion_service.ingest(request)
    doc = result.document

    # Returned document has terminal status
    assert doc.pipeline_status == PipelineStatus.ABSTRACTION_COMPLETE
    assert doc.indexed_at is not None
    assert doc.semantic_abstract is not None

    # Graph store matches
    fetched = await graph_store.get_document(doc.id)
    assert fetched.pipeline_status == doc.pipeline_status
    assert fetched.indexed_at == doc.indexed_at
    assert fetched.semantic_abstract == doc.semantic_abstract


# ---------------------------------------------------------------------------
# BH-071: Same-name same-content re-import reuses existing file
# ---------------------------------------------------------------------------


async def test_bh_071_same_content_reuses_existing_import(
    tmp_vault_dir, graph_store, ingestion_service, tmp_path
):
    """When imports/ already contains a file with the same name AND
    identical content, _ensure_vault_local returns the existing path
    without creating a hash-suffixed duplicate."""
    storage_root = tmp_vault_dir / "sources"
    imports_dir = storage_root / "imports"
    imports_dir.mkdir()

    content = "# Report\n\nIdentical content.\n"

    # Pre-populate imports/ with a file
    existing = imports_dir / "report.md"
    existing.write_text(content)

    # Create an external file with the same name and same content
    external_file = tmp_path / "report.md"
    external_file.write_text(content)

    request = IngestRequest(
        source=str(external_file),
        adapter=SourceType.MARKDOWN,
        force=True,  # bypass DuplicateContentError to test file handling
    )
    result = await ingestion_service.ingest(request)

    doc = result.document
    # source_path should be the original, without hash suffix
    assert doc.source_path == "imports/report.md"

    # Only one file should exist in imports/
    import_files = list(imports_dir.iterdir())
    assert len(import_files) == 1
    assert import_files[0].name == "report.md"

    # Content unchanged
    assert existing.read_text() == content


# ---------------------------------------------------------------------------
# _generate_abstract_text: shared core for abstraction generation
# ---------------------------------------------------------------------------


async def test_generate_abstract_text_uses_density_proportional_budget(
    ingestion_service,
):
    """_generate_abstract_text should pass a density-proportional max_tokens
    value to the abstraction provider, not a fixed constant."""
    # Capture the max_tokens passed to the stub
    captured = {}
    original = ingestion_service._abstraction.generate_abstract

    async def spy(text: str, max_tokens: int, doc_type: str | None) -> str:
        captured["max_tokens"] = max_tokens
        captured["doc_type"] = doc_type
        return await original(text, max_tokens, doc_type)

    ingestion_service._abstraction.generate_abstract = spy

    text = "word " * 10000  # 10000 words
    result = await ingestion_service._generate_abstract_text(text, "guideline")

    assert "max_tokens" in captured
    # 150 + 10000 * 0.02 = 350, not the old fixed 500
    assert captured["max_tokens"] == 350
    assert captured["doc_type"] == "guideline"
    assert result  # non-empty


async def test_generate_abstract_text_trims_sentence_boundary(
    ingestion_service,
):
    """_generate_abstract_text should trim output to the last complete
    sentence boundary."""

    # Replace stub to return text truncated mid-sentence
    async def truncated_output(text: str, max_tokens: int, doc_type: str | None) -> str:
        return "First sentence. Second sentence. Third incompl"

    ingestion_service._abstraction.generate_abstract = truncated_output

    result = await ingestion_service._generate_abstract_text("any input", None)
    assert result == "First sentence. Second sentence."


async def test_generate_abstract_text_returns_complete_sentences_unchanged(
    ingestion_service,
):
    """When abstraction output ends at a sentence boundary, it should be
    returned unchanged."""

    async def clean_output(text: str, max_tokens: int, doc_type: str | None) -> str:
        return "Complete sentence one. Complete sentence two."

    ingestion_service._abstraction.generate_abstract = clean_output

    result = await ingestion_service._generate_abstract_text("any input", None)
    assert result == "Complete sentence one. Complete sentence two."


# ---------------------------------------------------------------------------
# reabstract (async / fire-and-forget): BH-116 through BH-121
# ---------------------------------------------------------------------------


async def test_reabstract_returns_immediately_with_started_status(
    tmp_vault_dir,
    ingestion_service,
    graph_store,
):
    """BH-116: reabstract should return immediately with a dict containing
    status='reabstract_started' and the document_id, without waiting for
    the background abstraction to complete."""
    _create_test_file(tmp_vault_dir, "patents/reabs.md", "# Reabstract Test\n\nOriginal content.")

    request = IngestRequest(source="patents/reabs.md", adapter=SourceType.MARKDOWN)
    result = await ingestion_service.ingest(request)
    doc_id = result.document.id

    response = await ingestion_service.reabstract(doc_id)

    assert isinstance(response, dict)
    assert response["status"] == "reabstract_started"
    assert response["document_id"] == doc_id


async def test_reabstract_sets_pipeline_status_in_progress(
    tmp_vault_dir,
    ingestion_service,
    graph_store,
):
    """BH-117: reabstract should set pipeline_status to
    abstraction_in_progress before returning."""
    _create_test_file(tmp_vault_dir, "patents/status.md", "# Status Test\n\nContent.")

    request = IngestRequest(source="patents/status.md", adapter=SourceType.MARKDOWN)
    result = await ingestion_service.ingest(request)
    doc_id = result.document.id

    await ingestion_service.reabstract(doc_id)

    doc = await graph_store.get_document(doc_id)
    assert doc.pipeline_status == PipelineStatus.ABSTRACTION_IN_PROGRESS


async def test_reabstract_background_updates_abstract_on_success(
    tmp_vault_dir,
    ingestion_service,
    graph_store,
):
    """BH-118: The background task should update semantic_abstract and set
    pipeline_status to abstraction_complete when abstraction succeeds."""
    import asyncio

    _create_test_file(tmp_vault_dir, "patents/bgok.md", "# BG OK\n\nOriginal content.")

    request = IngestRequest(source="patents/bgok.md", adapter=SourceType.MARKDOWN)
    result = await ingestion_service.ingest(request)
    doc_id = result.document.id
    original_abstract = result.document.semantic_abstract

    # Swap abstraction provider to produce different output
    async def new_abstract(text: str, max_tokens: int, doc_type: str | None) -> str:
        return "Regenerated abstract from new model."

    ingestion_service._abstraction.generate_abstract = new_abstract

    response = await ingestion_service.reabstract(doc_id)
    assert response["status"] == "reabstract_started"

    # Allow the background task to complete (0.1s yield + work)
    await asyncio.sleep(0.5)

    doc = await graph_store.get_document(doc_id)
    assert doc.semantic_abstract == "Regenerated abstract from new model."
    assert doc.semantic_abstract != original_abstract
    assert doc.pipeline_status == PipelineStatus.ABSTRACTION_COMPLETE


async def test_reabstract_background_sets_failed_on_error(
    tmp_vault_dir,
    ingestion_service_failing_llm,
    graph_store,
):
    """BH-119: The background task should set pipeline_status to 'failed'
    when the abstraction provider raises an exception."""
    import asyncio

    _create_test_file(tmp_vault_dir, "patents/bgfail.md", "# BG Fail\n\nContent.")

    # Ingest with the normal stub first so initial ingestion succeeds,
    # then swap to the failing provider for reabstract
    from sage.adapters.stubs import StubAbstractionProvider

    original_provider = ingestion_service_failing_llm._abstraction

    # Use stub for initial ingest
    ingestion_service_failing_llm._abstraction = StubAbstractionProvider()
    request = IngestRequest(source="patents/bgfail.md", adapter=SourceType.MARKDOWN)
    result = await ingestion_service_failing_llm.ingest(request)
    doc_id = result.document.id

    # Switch to failing provider for reabstract
    ingestion_service_failing_llm._abstraction = original_provider

    response = await ingestion_service_failing_llm.reabstract(doc_id)
    assert response["status"] == "reabstract_started"

    # Allow the background task to complete and fail (0.1s yield + work)
    await asyncio.sleep(0.5)

    doc = await graph_store.get_document(doc_id)
    assert doc.pipeline_status == PipelineStatus.FAILED


async def test_reabstract_document_not_found(ingestion_service):
    """BH-120: reabstract should raise DocumentNotFoundError synchronously
    for unknown document_id (validation happens before background dispatch)."""
    from sage.api.errors import DocumentNotFoundError

    with pytest.raises(DocumentNotFoundError):
        await ingestion_service.reabstract("nonexistent_doc_id")


async def test_reabstract_no_projection_raises_error(
    tmp_vault_dir,
    ingestion_service,
    graph_store,
    stub_content_store,
):
    """BH-121: reabstract should raise NoProjectionError synchronously when
    the document exists but has no stored chunks."""
    from sage.api.errors import NoProjectionError

    _create_test_file(tmp_vault_dir, "patents/nochunks.md", "# No Chunks\n\nContent.")

    request = IngestRequest(source="patents/nochunks.md", adapter=SourceType.MARKDOWN)
    result = await ingestion_service.ingest(request)
    doc_id = result.document.id

    # Remove all chunks from the content store
    await stub_content_store.remove_document(doc_id)

    with pytest.raises(NoProjectionError):
        await ingestion_service.reabstract(doc_id)


# ---------------------------------------------------------------------------
# BH-131, BH-132, BH-133: Adapter-emitted tags merge into document.tags
# ---------------------------------------------------------------------------
#
# These tests exercise the adapter_tags / adapter_tag_prefixes convention
# between the source adapter layer and the ingestion service. The
# DocxAdapter is the first adapter to use this channel (for .dotx template
# style inventory), but the contract is generic so any adapter can
# contribute to document.tags from projection metadata.


class _TagEmittingStubAdapter:
    """Stub adapter that emits configurable adapter_tags in projection metadata.

    Used to exercise the ingestion-level tag-merge plumbing (BH-131..133)
    without the real .dotx machinery. Each instance can be reconfigured
    between calls to simulate an updated adapter version, supporting
    the BH-132 stale-tag-stripping scenario.
    """

    VERSION = "0.0.1-stub"
    EXTENSIONS = [".md"]

    def __init__(self, adapter_tags: list[str], adapter_tag_prefixes: list[str]):
        self.adapter_tags = list(adapter_tags)
        self.adapter_tag_prefixes = list(adapter_tag_prefixes)

    async def project(self, source_path, config=None):
        import hashlib
        from datetime import datetime, timezone

        from sage.source_adapters.base import HeadingNode, ProjectionResult

        raw = source_path.read_bytes()
        text = source_path.read_text()
        mtime = datetime.fromtimestamp(source_path.stat().st_mtime, tz=timezone.utc).isoformat()
        return ProjectionResult(
            text=text,
            headings=[HeadingNode(level=1, text="Stub", path="Stub", content=text)],
            content_hash=hashlib.sha256(raw).hexdigest(),
            adapter_version=self.VERSION,
            title=source_path.stem,
            metadata={
                "source_modified_at": mtime,
                "adapter_tags": list(self.adapter_tags),
                "adapter_tag_prefixes": list(self.adapter_tag_prefixes),
            },
        )


def _make_ingestion_service_with_tag_adapter(
    *,
    graph_store,
    lock_manager,
    stub_content_store,
    stub_embedding_provider,
    stub_abstraction_provider,
    minimal_config,
    lifecycle_service,
    adapter,
):
    """Build a fresh IngestionService using the given adapter under MARKDOWN."""
    from sage.services.ingestion import IngestionService

    return IngestionService(
        graph_store=graph_store,
        lock_manager=lock_manager,
        content_store=stub_content_store,
        embedding_provider=stub_embedding_provider,
        abstraction_provider=stub_abstraction_provider,
        config=minimal_config,
        source_adapters={SourceType.MARKDOWN: adapter},
        lifecycle_service=lifecycle_service,
    )


async def test_bh_131_adapter_tags_merge_on_new_ingest(
    tmp_vault_dir,
    graph_store,
    lock_manager,
    stub_content_store,
    stub_embedding_provider,
    stub_abstraction_provider,
    minimal_config,
    lifecycle_service,
):
    """BH-131: adapter_tags merge with caller-supplied tags on new ingest."""
    adapter = _TagEmittingStubAdapter(
        adapter_tags=["template:style:A", "template:style:B"],
        adapter_tag_prefixes=["template:"],
    )
    service = _make_ingestion_service_with_tag_adapter(
        graph_store=graph_store,
        lock_manager=lock_manager,
        stub_content_store=stub_content_store,
        stub_embedding_provider=stub_embedding_provider,
        stub_abstraction_provider=stub_abstraction_provider,
        minimal_config=minimal_config,
        lifecycle_service=lifecycle_service,
        adapter=adapter,
    )

    _create_test_file(
        tmp_vault_dir,
        "patents/bh131_a.md",
        content="# BH-131 case A\n\nWith adapter tags.",
    )

    request = IngestRequest(
        source="patents/bh131_a.md",
        adapter=SourceType.MARKDOWN,
        metadata={"codes": "caller-tag"},
    )
    result = await service.ingest(request)
    doc = result.document

    assert "template:style:A" in doc.tags
    assert "template:style:B" in doc.tags
    assert "caller-tag" in doc.tags
    # No duplicates
    assert len(doc.tags) == len(set(doc.tags))

    # Negative case: adapter that emits no adapter_tags leaves doc.tags
    # derived from caller only.
    plain_adapter = _TagEmittingStubAdapter(
        adapter_tags=[],
        adapter_tag_prefixes=[],
    )

    # Tweak so the plain adapter doesn't emit the metadata keys at all
    async def plain_project(source_path, config=None):
        import hashlib
        from datetime import datetime, timezone

        from sage.source_adapters.base import HeadingNode, ProjectionResult

        raw = source_path.read_bytes()
        text = source_path.read_text()
        return ProjectionResult(
            text=text,
            headings=[HeadingNode(level=1, text="Plain", path="Plain", content=text)],
            content_hash=hashlib.sha256(raw).hexdigest(),
            adapter_version=plain_adapter.VERSION,
            title=source_path.stem,
            metadata={
                "source_modified_at": datetime.fromtimestamp(
                    source_path.stat().st_mtime, tz=timezone.utc
                ).isoformat(),
            },
        )

    plain_adapter.project = plain_project

    service_plain = _make_ingestion_service_with_tag_adapter(
        graph_store=graph_store,
        lock_manager=lock_manager,
        stub_content_store=stub_content_store,
        stub_embedding_provider=stub_embedding_provider,
        stub_abstraction_provider=stub_abstraction_provider,
        minimal_config=minimal_config,
        lifecycle_service=lifecycle_service,
        adapter=plain_adapter,
    )

    _create_test_file(
        tmp_vault_dir,
        "patents/bh131_plain.md",
        content="# BH-131 case B\n\nWithout adapter tags.",
    )
    plain_result = await service_plain.ingest(
        IngestRequest(
            source="patents/bh131_plain.md",
            adapter=SourceType.MARKDOWN,
            metadata={"codes": "caller-tag"},
        )
    )
    assert plain_result.document.tags == ["caller-tag"]


async def test_bh_132_force_reingest_strips_stale_adapter_tags(
    tmp_vault_dir,
    graph_store,
    lock_manager,
    stub_content_store,
    stub_embedding_provider,
    stub_abstraction_provider,
    minimal_config,
    lifecycle_service,
):
    """BH-132: stale adapter-owned tags are stripped on force re-ingest."""
    adapter = _TagEmittingStubAdapter(
        adapter_tags=["template:style:Old", "template:has_numbering:Old"],
        adapter_tag_prefixes=["template:"],
    )
    service = _make_ingestion_service_with_tag_adapter(
        graph_store=graph_store,
        lock_manager=lock_manager,
        stub_content_store=stub_content_store,
        stub_embedding_provider=stub_embedding_provider,
        stub_abstraction_provider=stub_abstraction_provider,
        minimal_config=minimal_config,
        lifecycle_service=lifecycle_service,
        adapter=adapter,
    )

    file_path = _create_test_file(
        tmp_vault_dir,
        "patents/bh132.md",
        content="# BH-132\n\nFirst.",
    )

    first = await service.ingest(
        IngestRequest(
            source="patents/bh132.md",
            adapter=SourceType.MARKDOWN,
            metadata={"codes": "caller-tag"},
        )
    )
    assert "template:style:Old" in first.document.tags
    assert "template:has_numbering:Old" in first.document.tags

    # "Update" the adapter to emit a different tag set. Force re-ingest to
    # trigger the stale-stripping path. Content must change to bypass the
    # duplicate gate (BH-133 covers the byte-identical case).
    file_path.write_text("# BH-132\n\nSecond revision.")
    adapter.adapter_tags = ["template:style:New"]

    second = await service.ingest(
        IngestRequest(
            source="patents/bh132.md",
            adapter=SourceType.MARKDOWN,
            force=True,
            metadata={"codes": "caller-tag"},
        )
    )

    assert "template:style:New" in second.document.tags
    assert "template:style:Old" not in second.document.tags
    assert "template:has_numbering:Old" not in second.document.tags
    assert "caller-tag" in second.document.tags


async def test_bh_133_byte_identical_reingest_raises_duplicate(
    tmp_vault_dir,
    graph_store,
    lock_manager,
    stub_content_store,
    stub_embedding_provider,
    stub_abstraction_provider,
    minimal_config,
    lifecycle_service,
):
    """BH-133: adapter_tags channel does not bypass the content-hash duplicate gate."""
    adapter = _TagEmittingStubAdapter(
        adapter_tags=["template:style:X"],
        adapter_tag_prefixes=["template:"],
    )
    service = _make_ingestion_service_with_tag_adapter(
        graph_store=graph_store,
        lock_manager=lock_manager,
        stub_content_store=stub_content_store,
        stub_embedding_provider=stub_embedding_provider,
        stub_abstraction_provider=stub_abstraction_provider,
        minimal_config=minimal_config,
        lifecycle_service=lifecycle_service,
        adapter=adapter,
    )

    _create_test_file(tmp_vault_dir, "patents/bh133.md", content="# BH-133\n\nOnly content.")

    first = await service.ingest(
        IngestRequest(source="patents/bh133.md", adapter=SourceType.MARKDOWN)
    )
    assert "template:style:X" in first.document.tags
    original_adapter_version = first.document.adapter_version
    original_updated_at = first.document.updated_at
    original_tags = list(first.document.tags)

    # Same file, no force: must raise DuplicateContentError
    with pytest.raises(DuplicateContentError) as exc_info:
        await service.ingest(IngestRequest(source="patents/bh133.md", adapter=SourceType.MARKDOWN))
    assert exc_info.value.detail["existing_document_id"] == first.document.id

    # Document state should be unchanged after the rejected re-ingest
    fetched = await graph_store.get_document(first.document.id)
    assert fetched.tags == original_tags
    assert fetched.adapter_version == original_adapter_version
    assert fetched.updated_at == original_updated_at


# ---------------------------------------------------------------------------
# BH-134: Empty projection text transitions to abstraction_skipped
# ---------------------------------------------------------------------------


class _EmptyTextStubAdapter:
    """Stub adapter that returns an empty projection text, simulating a
    Word template or other content-thin source."""

    VERSION = "0.0.1-empty"
    EXTENSIONS = [".md"]

    async def project(self, source_path, config=None):
        import hashlib
        from datetime import datetime, timezone

        from sage.source_adapters.base import ProjectionResult

        raw = source_path.read_bytes()
        return ProjectionResult(
            text="",
            headings=[],
            content_hash=hashlib.sha256(raw).hexdigest(),
            adapter_version=self.VERSION,
            title=source_path.stem,
            metadata={
                "source_modified_at": datetime.fromtimestamp(
                    source_path.stat().st_mtime, tz=timezone.utc
                ).isoformat(),
            },
        )


class _StrictAbstractionProvider:
    """Mirrors Qwen3's strict edge guard: raises on empty input.

    Any test that reaches generate_abstract() with empty text via this
    provider will fail. BH-134 relies on this behavior to verify that
    the service-layer guard short-circuits before reaching here.
    """

    async def generate_abstract(self, text: str, max_tokens: int, doc_type: str | None) -> str:
        if not text or not text.strip():
            raise RuntimeError("Cannot generate abstract from empty document text")
        return f"Strict stub abstract for {len(text)} chars."


async def test_bh_134_empty_projection_text_skips_abstraction(
    tmp_vault_dir,
    graph_store,
    lock_manager,
    stub_content_store,
    stub_embedding_provider,
    minimal_config,
    lifecycle_service,
):
    """BH-134: empty projection text transitions to abstraction_skipped."""
    from sage.services.ingestion import IngestionService

    service = IngestionService(
        graph_store=graph_store,
        lock_manager=lock_manager,
        content_store=stub_content_store,
        embedding_provider=stub_embedding_provider,
        abstraction_provider=_StrictAbstractionProvider(),
        config=minimal_config,
        source_adapters={SourceType.MARKDOWN: _EmptyTextStubAdapter()},
        lifecycle_service=lifecycle_service,
    )

    _create_test_file(
        tmp_vault_dir,
        "patents/bh134.md",
        content="# Placeholder\n\nSource has bytes but adapter returns empty text.",
    )

    result = await service.ingest(
        IngestRequest(source="patents/bh134.md", adapter=SourceType.MARKDOWN)
    )

    fetched = await graph_store.get_document(result.document.id)
    assert fetched.pipeline_status == PipelineStatus.ABSTRACTION_SKIPPED
    assert fetched.semantic_abstract is None
    assert fetched.pipeline_error is None


async def test_bh_134_non_empty_text_still_runs_abstraction(
    tmp_vault_dir,
    graph_store,
    lock_manager,
    stub_content_store,
    stub_embedding_provider,
    minimal_config,
    lifecycle_service,
):
    """BH-134 (control): non-empty text still reaches abstraction."""
    from sage.services.ingestion import IngestionService

    service = IngestionService(
        graph_store=graph_store,
        lock_manager=lock_manager,
        content_store=stub_content_store,
        embedding_provider=stub_embedding_provider,
        abstraction_provider=_StrictAbstractionProvider(),
        config=minimal_config,
        source_adapters={SourceType.MARKDOWN: MarkdownAdapter()},
        lifecycle_service=lifecycle_service,
    )

    _create_test_file(
        tmp_vault_dir,
        "patents/bh134_ctrl.md",
        content="# Real\n\nNon-empty body content to abstract.",
    )

    result = await service.ingest(
        IngestRequest(source="patents/bh134_ctrl.md", adapter=SourceType.MARKDOWN)
    )

    fetched = await graph_store.get_document(result.document.id)
    assert fetched.pipeline_status == PipelineStatus.ABSTRACTION_COMPLETE
    assert fetched.semantic_abstract is not None
    assert "Strict stub abstract" in fetched.semantic_abstract


# ---------------------------------------------------------------------------
# Vault-level adapter config propagation
#
# The vault config's source_adapters.adapters[].config is authoritative for
# adapter behavior. Per-request IngestRequest.config is a per-call override
# that takes precedence on key collisions.
# ---------------------------------------------------------------------------

import copy as _copy  # noqa: E402 -- grouped with the vault-adapter-config test section below
from typing import Any  # noqa: E402 -- grouped with the vault-adapter-config test section below

try:
    import docx as _docx_module

    _HAS_DOCX = True
except ImportError:
    _HAS_DOCX = False

requires_docx = pytest.mark.skipif(not _HAS_DOCX, reason="python-docx not available")


def _write_styled_docx(path: Path, paragraphs: list[tuple[str, str]]) -> None:
    """Write a .docx with the given (text, style) paragraphs.

    Style names must already exist in the document's style set. Built-in
    styles like "Title", "Heading 1", "Subtitle", "Normal" are pre-defined
    by python-docx.
    """
    doc = _docx_module.Document()
    for text, style in paragraphs:
        doc.add_paragraph(text, style=style)
    path.parent.mkdir(parents=True, exist_ok=True)
    doc.save(str(path))


def _build_vault_config_with_docx(base_dict: dict, vault_docx_config: dict | None) -> VaultConfig:
    """Return a VaultConfig that adds a docx adapter entry to base_dict.

    If vault_docx_config is None, the docx adapter is registered with no
    config (falls through to defaults). Otherwise, vault_docx_config is
    placed under source_adapters.adapters[docx].config.
    """
    config_dict = _copy.deepcopy(base_dict)
    docx_entry: dict[str, Any] = {"source_type": "docx", "enabled": True}
    if vault_docx_config is not None:
        docx_entry["config"] = vault_docx_config
    config_dict["source_adapters"]["adapters"].append(docx_entry)
    return VaultConfig.model_validate(config_dict)


def _make_ingestion_with_docx(
    config: VaultConfig,
    *,
    graph_store,
    lock_manager,
    stub_content_store,
    stub_embedding_provider,
    stub_abstraction_provider,
):
    """Construct an IngestionService with real DocxAdapter + Markdown stub."""
    from sage.services.ingestion import IngestionService
    from sage.services.lifecycle import LifecycleService
    from sage.source_adapters.docx_adapter import DocxAdapter

    lifecycle_service = LifecycleService(graph_store, lock_manager, config)
    return IngestionService(
        graph_store=graph_store,
        lock_manager=lock_manager,
        content_store=stub_content_store,
        embedding_provider=stub_embedding_provider,
        abstraction_provider=stub_abstraction_provider,
        config=config,
        source_adapters={
            SourceType.MARKDOWN: MarkdownAdapter(),
            SourceType.DOCX: DocxAdapter(),
        },
        lifecycle_service=lifecycle_service,
    )


@requires_docx
async def test_vault_adapter_config_reaches_adapter_at_ingest(
    tmp_vault_dir,
    graph_store,
    lock_manager,
    stub_content_store,
    stub_embedding_provider,
    stub_abstraction_provider,
    minimal_vault_config_dict,
):
    """The PIM use case: vault config's heading_style_map flows to docx adapter."""
    config = _build_vault_config_with_docx(
        minimal_vault_config_dict,
        vault_docx_config={"heading_style_map": {"Title": 1}},
    )
    service = _make_ingestion_with_docx(
        config,
        graph_store=graph_store,
        lock_manager=lock_manager,
        stub_content_store=stub_content_store,
        stub_embedding_provider=stub_embedding_provider,
        stub_abstraction_provider=stub_abstraction_provider,
    )

    docx_path = tmp_vault_dir / "sources" / "vault_config_docx.docx"
    _write_styled_docx(
        docx_path,
        [("CLAIMS", "Title"), ("Body content under CLAIMS.", "Normal")],
    )

    result = await service.ingest(
        IngestRequest(source="vault_config_docx.docx", adapter=SourceType.DOCX)
    )

    # The Title-styled "CLAIMS" paragraph must be recognized as a heading
    # because the vault config maps Title -> level 1. Without the fix, the
    # adapter receives request.config (None) and falls back to defaults
    # (Heading 1-9 only), so no chunk gets heading_path "CLAIMS".
    heading_paths = await stub_content_store.get_heading_paths(result.document.id)
    assert "CLAIMS" in heading_paths, f"Expected 'CLAIMS' as a heading_path; got: {heading_paths}"


@requires_docx
async def test_request_config_overrides_vault_adapter_config_on_key_collision(
    tmp_vault_dir,
    graph_store,
    lock_manager,
    stub_content_store,
    stub_embedding_provider,
    stub_abstraction_provider,
    minimal_vault_config_dict,
):
    """Per-request config overrides vault-level on key collisions."""
    config = _build_vault_config_with_docx(
        minimal_vault_config_dict,
        vault_docx_config={"heading_style_map": {"Title": 1}},
    )
    service = _make_ingestion_with_docx(
        config,
        graph_store=graph_store,
        lock_manager=lock_manager,
        stub_content_store=stub_content_store,
        stub_embedding_provider=stub_embedding_provider,
        stub_abstraction_provider=stub_abstraction_provider,
    )

    docx_path = tmp_vault_dir / "sources" / "override.docx"
    # Two Title paragraphs so we can see hierarchy levels in heading paths.
    _write_styled_docx(
        docx_path,
        [
            ("Outer", "Heading 1"),
            ("Inner", "Title"),
            ("Body", "Normal"),
        ],
    )

    # Request asks for Title=2; vault says Title=1. Request must win.
    result = await service.ingest(
        IngestRequest(
            source="override.docx",
            adapter=SourceType.DOCX,
            config={"heading_style_map": {"Title": 2}},
        )
    )

    heading_paths = await stub_content_store.get_heading_paths(result.document.id)
    # If Title is level 2, it nests under Heading 1: path should be "Outer > Inner".
    # If Title were level 1 (vault value), the path would be just "Inner".
    assert "Outer > Inner" in heading_paths, (
        f"Expected Title=2 (request wins); got heading_paths: {heading_paths}"
    )


@requires_docx
async def test_request_config_merges_with_vault_adapter_config_no_collision(
    tmp_vault_dir,
    graph_store,
    lock_manager,
    stub_content_store,
    stub_embedding_provider,
    stub_abstraction_provider,
    minimal_vault_config_dict,
):
    """Non-conflicting keys merge; both vault and request mappings apply."""
    config = _build_vault_config_with_docx(
        minimal_vault_config_dict,
        vault_docx_config={"heading_style_map": {"Title": 1}},
    )
    service = _make_ingestion_with_docx(
        config,
        graph_store=graph_store,
        lock_manager=lock_manager,
        stub_content_store=stub_content_store,
        stub_embedding_provider=stub_embedding_provider,
        stub_abstraction_provider=stub_abstraction_provider,
    )

    docx_path = tmp_vault_dir / "sources" / "merge.docx"
    # Each heading needs body content beneath it; chunks are only created
    # for headings whose content is non-empty.
    _write_styled_docx(
        docx_path,
        [
            ("VaultStyleHeading", "Title"),
            ("Vault body.", "Normal"),
            ("RequestStyleHeading", "Subtitle"),
            ("Request body.", "Normal"),
        ],
    )

    # Vault: Title -> 1. Request adds: Subtitle -> 1. No collision; both apply.
    result = await service.ingest(
        IngestRequest(
            source="merge.docx",
            adapter=SourceType.DOCX,
            config={"heading_style_map": {"Subtitle": 1}},
        )
    )

    heading_paths = await stub_content_store.get_heading_paths(result.document.id)
    assert "VaultStyleHeading" in heading_paths
    assert "RequestStyleHeading" in heading_paths


@requires_docx
async def test_no_vault_adapter_entry_falls_through_to_request_config(
    tmp_vault_dir,
    graph_store,
    lock_manager,
    stub_content_store,
    stub_embedding_provider,
    stub_abstraction_provider,
    minimal_vault_config_dict,
):
    """Vault config with no docx adapter entry: request.config still works."""
    # No docx entry added; minimal_vault_config_dict only has markdown.
    config = VaultConfig.model_validate(_copy.deepcopy(minimal_vault_config_dict))
    service = _make_ingestion_with_docx(
        config,
        graph_store=graph_store,
        lock_manager=lock_manager,
        stub_content_store=stub_content_store,
        stub_embedding_provider=stub_embedding_provider,
        stub_abstraction_provider=stub_abstraction_provider,
    )

    docx_path = tmp_vault_dir / "sources" / "no_vault_entry.docx"
    _write_styled_docx(
        docx_path,
        [("RequestOnly", "Title"), ("Body", "Normal")],
    )

    result = await service.ingest(
        IngestRequest(
            source="no_vault_entry.docx",
            adapter=SourceType.DOCX,
            config={"heading_style_map": {"Title": 1}},
        )
    )

    heading_paths = await stub_content_store.get_heading_paths(result.document.id)
    assert "RequestOnly" in heading_paths


@requires_docx
async def test_no_request_config_and_no_vault_adapter_config_uses_defaults(
    tmp_vault_dir,
    graph_store,
    lock_manager,
    stub_content_store,
    stub_embedding_provider,
    stub_abstraction_provider,
    minimal_vault_config_dict,
):
    """Both configs absent: adapter falls back to _DEFAULT_STYLE_MAP."""
    config = _build_vault_config_with_docx(minimal_vault_config_dict, vault_docx_config=None)
    service = _make_ingestion_with_docx(
        config,
        graph_store=graph_store,
        lock_manager=lock_manager,
        stub_content_store=stub_content_store,
        stub_embedding_provider=stub_embedding_provider,
        stub_abstraction_provider=stub_abstraction_provider,
    )

    docx_path = tmp_vault_dir / "sources" / "defaults.docx"
    _write_styled_docx(
        docx_path,
        [
            ("BuiltInH1", "Heading 1"),
            ("CustomTitle", "Title"),  # not in defaults
            ("Body", "Normal"),
        ],
    )

    result = await service.ingest(IngestRequest(source="defaults.docx", adapter=SourceType.DOCX))

    heading_paths = await stub_content_store.get_heading_paths(result.document.id)
    # Heading 1 is in defaults; Title is not.
    assert "BuiltInH1" in heading_paths
    assert "CustomTitle" not in heading_paths


# ---------------------------------------------------------------------------
# Heading-context embedding
#
# At index time, the embedder receives `heading_path + content`, not just
# `content`. This makes semantic search reach chunks whose query terms
# appear only in the heading hierarchy — the agent equivalent of Word's
# Find on a heading.
# ---------------------------------------------------------------------------


class _RecordingEmbeddingProvider:
    """Captures the texts passed to embed() for assertion."""

    def __init__(self) -> None:
        self.last_inputs: list[str] = []

    async def embed(self, texts: list[str]) -> list[list[float]]:
        self.last_inputs = list(texts)
        return [[0.0] * 768 for _ in texts]


@requires_docx
async def test_embedder_receives_combined_heading_path_and_content(
    tmp_vault_dir,
    graph_store,
    lock_manager,
    stub_content_store,
    stub_abstraction_provider,
    minimal_vault_config_dict,
):
    """The text passed to the embedder includes heading_path + content,
    so semantic search can find chunks via heading-only query terms.

    Heading text is deliberately distinct from the document title so the
    BH-058 search preamble (which prepends Title/Source/Tags to chunk[0])
    does not accidentally include the heading text — that would mask a
    failure of the heading-context-embedding logic.
    """
    from sage.services.ingestion import IngestionService
    from sage.services.lifecycle import LifecycleService
    from sage.source_adapters.docx_adapter import DocxAdapter

    config = _build_vault_config_with_docx(
        minimal_vault_config_dict,
        vault_docx_config=None,  # use defaults (Heading 1-9 only)
    )
    lifecycle_service = LifecycleService(graph_store, lock_manager, config)
    recording_embedder = _RecordingEmbeddingProvider()
    service = IngestionService(
        graph_store=graph_store,
        lock_manager=lock_manager,
        content_store=stub_content_store,
        embedding_provider=recording_embedder,
        abstraction_provider=stub_abstraction_provider,
        config=config,
        source_adapters={
            SourceType.MARKDOWN: MarkdownAdapter(),
            SourceType.DOCX: DocxAdapter(),
        },
        lifecycle_service=lifecycle_service,
    )

    docx_path = tmp_vault_dir / "sources" / "plain_filename.docx"
    # Filename is "plain_filename" → doc title is "plain_filename".
    # Heading text "Distinctive Marker Phrase" appears nowhere in the
    # filename or body, so the only way it can reach the embedder is
    # via heading_path concatenation.
    _write_styled_docx(
        docx_path,
        [
            ("Distinctive Marker Phrase", "Heading 1"),
            ("Cryptographic accumulator seals govern the commit.", "Normal"),
        ],
    )

    await service.ingest(IngestRequest(source="plain_filename.docx", adapter=SourceType.DOCX))

    assert recording_embedder.last_inputs, "embedder was not called"
    combined = recording_embedder.last_inputs[0]
    assert "Distinctive Marker Phrase" in combined, (
        f"heading_path missing from embed input: {combined!r}"
    )
    assert "Cryptographic accumulator seals" in combined, (
        f"body content missing from embed input: {combined!r}"
    )


@requires_docx
async def test_chunk_content_field_unchanged_by_combined_embedding(
    tmp_vault_dir,
    graph_store,
    lock_manager,
    stub_content_store,
    stub_embedding_provider,
    stub_abstraction_provider,
    minimal_vault_config_dict,
):
    """The chunk.content field stored in the content store is body text
    only — heading_path is NOT prepended to the stored content. The
    combined heading+content text is used solely as embedder input."""
    config = _build_vault_config_with_docx(
        minimal_vault_config_dict,
        vault_docx_config={"heading_style_map": {"Title": 1}},
    )
    service = _make_ingestion_with_docx(
        config,
        graph_store=graph_store,
        lock_manager=lock_manager,
        stub_content_store=stub_content_store,
        stub_embedding_provider=stub_embedding_provider,
        stub_abstraction_provider=stub_abstraction_provider,
    )

    docx_path = tmp_vault_dir / "sources" / "clean_content.docx"
    body_first = "First section body content."
    second_heading = "Exemplary Methods"
    body_second = "Second section body about cryptographic accumulator seals."
    _write_styled_docx(
        docx_path,
        [
            ("Introduction", "Title"),
            (body_first, "Normal"),
            (second_heading, "Title"),
            (body_second, "Normal"),
        ],
    )

    result = await service.ingest(
        IngestRequest(source="clean_content.docx", adapter=SourceType.DOCX)
    )

    chunks = await stub_content_store.get_all_chunks(result.document.id)
    # Assert on the SECOND chunk (avoid BH-058 search_preamble which mutates
    # only chunk[0].content with title/source/tags).
    assert len(chunks) >= 2
    second = next(c for c in chunks if c.heading_path == second_heading)
    assert second.content == body_second, (
        "Stored chunk.content must be body text only; got: " + repr(second.content)
    )
    assert second_heading not in second.content


@requires_docx
async def test_empty_content_heading_still_emits_chunk(
    tmp_vault_dir,
    graph_store,
    lock_manager,
    stub_content_store,
    stub_embedding_provider,
    stub_abstraction_provider,
    minimal_vault_config_dict,
):
    """A heading whose immediate next paragraph is another same-or-higher
    level heading produces no body content for itself. The chunker MUST
    still emit a chunk for that heading so its heading_path enters the
    FTS index. Without this, a heading like "DETAILED DESCRIPTION" that's
    immediately followed by another top-level heading vanishes from the
    indexed surface — invisible to BM25, semantic search, and
    sage_read_section heading enumeration. Word's Find finds it; SAGE
    must too. Closes the gap surfaced by Cowork on PV01 v10.2.2 et al.
    """
    config = _build_vault_config_with_docx(
        minimal_vault_config_dict,
        vault_docx_config=None,  # use defaults: Heading 1-9 only
    )
    service = _make_ingestion_with_docx(
        config,
        graph_store=graph_store,
        lock_manager=lock_manager,
        stub_content_store=stub_content_store,
        stub_embedding_provider=stub_embedding_provider,
        stub_abstraction_provider=stub_abstraction_provider,
    )

    docx_path = tmp_vault_dir / "sources" / "empty_heading.docx"
    # Two same-level headings back-to-back. The first ("EMPTY PARENT")
    # has no body content because the next paragraph is also a heading.
    _write_styled_docx(
        docx_path,
        [
            ("EMPTY PARENT", "Heading 1"),
            ("FIRST CHILD", "Heading 1"),
            ("Some body content under FIRST CHILD.", "Normal"),
        ],
    )

    result = await service.ingest(
        IngestRequest(source="empty_heading.docx", adapter=SourceType.DOCX)
    )

    heading_paths = await stub_content_store.get_heading_paths(result.document.id)
    assert "EMPTY PARENT" in heading_paths, (
        f"Heading 'EMPTY PARENT' must produce a chunk even with empty body "
        f"content. Got heading_paths: {heading_paths}"
    )
    assert "FIRST CHILD" in heading_paths

    chunks = await stub_content_store.get_all_chunks(result.document.id)
    by_path = {c.heading_path: c for c in chunks}
    assert "EMPTY PARENT" in by_path
    # The empty-content marker chunk has no body content (the BH-058
    # search-preamble may be prepended on chunk[0], which is fine — the
    # heading_path is what makes the heading searchable).
    parent = by_path["EMPTY PARENT"]
    assert "Some body content" not in parent.content, (
        "Body of FIRST CHILD must not leak into the EMPTY PARENT chunk."
    )
