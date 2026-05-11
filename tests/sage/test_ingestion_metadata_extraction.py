"""Ingestion metadata extraction tests (TEST-SAGE-ME-001 through ME-009).

Behavioral tests for vault-driven metadata extraction in the SAGE
ingestion pipeline. See tests/sage/metadata_extraction_tests.md for
the full specifications and rationale.

Validates CAS-ADR-015: metadata extraction is a SAGE-level capability,
not a caller responsibility. The IngestionService invokes the vault's
configured FilenameParser on every ingest and merges parsed metadata
into the document record per declared precedence (caller > content >
filename).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from sage.config import VaultConfig
from sage.models.enums import SourceType
from sage.models.schemas import IngestRequest
from sage.services.ingestion import IngestionService
from sage.source_adapters.docx_adapter import DocxAdapter
from sage.source_adapters.markdown_adapter import MarkdownAdapter

try:
    import docx as _docx_pkg

    _HAS_DOCX = True
except ImportError:
    _HAS_DOCX = False

requires_docx = pytest.mark.skipif(not _HAS_DOCX, reason="python-docx not available")


# ---------------------------------------------------------------------------
# Fixtures: PIM-Health-style vault config with filename_extraction enabled
# ---------------------------------------------------------------------------


def _pim_metadata_extraction() -> dict:
    """PIM Health-like metadata extraction config.

    Mirrors the fixture used in tests/app/test_app_backend.py so that
    the SAGE-side behavior is validated against the same rule set the
    app backend has always validated against.
    """
    return {
        "filename_extraction": {
            "pattern": "{date}_{project}_{code}_{title}_{version}",
            "separator": "_",
            "project_identifier": "PIM",
            "segment_fields": {
                "date": "doc_date",
                "project": "project",
                "code": "doc_code",
                "title": "title",
                "version": "version",
            },
            "known_code_patterns": [
                "^[A-Z][A-Z0-9]{1,7}$",
                "^[A-Z]+-\\d+$",
                "^PVMaster$",
                "^TDMaster$",
            ],
            "keyword_to_doc_type": [
                {"keyword": "Checklist", "doc_type": "checklist"},
                {"keyword": "Plan", "doc_type": "work_plan"},
            ],
            "code_to_doc_type": [
                {
                    "code": "REF",
                    "title_contains": "Glossary",
                    "doc_type": "glossary",
                },
                {"code": "REF", "doc_type": "reference_document"},
                {"code": "PVMaster", "doc_type": "patent_draft"},
                {"code": "PV", "doc_type": "patent_draft"},
            ],
        },
    }


def _pim_vault_config_dict(tmp_vault_dir: Path) -> dict:
    """Build a PIM-Health-like vault config for tests in this module.

    Uses both markdown and docx adapters so ME-007 can exercise both
    paths against structurally identical filenames.
    """
    return {
        "vault": {
            "id": "test_metadata_vault",
            "name": "Test Metadata Extraction Vault",
            "owner": "testuser",
            "storage_root": str(tmp_vault_dir / "sources"),
            "brain_root": str(tmp_vault_dir / "brain"),
            "visibility": "personal",
        },
        "document_types": {
            "doc_types": [
                {"value": "patent_draft", "label": "Patent Draft"},
                {"value": "glossary", "label": "Glossary"},
                {"value": "reference_document", "label": "Reference Document"},
                {"value": "checklist", "label": "Checklist"},
                {"value": "work_plan", "label": "Work Plan"},
                {"value": "misc", "label": "Miscellaneous"},
            ],
        },
        "lifecycle": {
            "base_states_required": True,
            "states": [
                {"value": "active", "label": "Active"},
                {"value": "completed", "label": "Completed"},
                {"value": "archived", "label": "Archived", "is_terminal": True},
            ],
            "transitions": [
                {"from_state": "(new)", "action": "ingest", "to_state": "active"},
                {
                    "from_state": "active",
                    "action": "supersede",
                    "to_state": "archived",
                    "creates_edge": "supersedes",
                },
                {"from_state": "active", "action": "complete", "to_state": "completed"},
                {"from_state": "active", "action": "archive", "to_state": "archived"},
                {"from_state": "completed", "action": "archive", "to_state": "archived"},
                {"from_state": "archived", "action": "reactivate", "to_state": "active"},
            ],
        },
        "source_adapters": {
            "adapters": [
                {"source_type": "markdown", "enabled": True},
                {"source_type": "docx", "enabled": True},
            ],
        },
        "metadata_extraction": _pim_metadata_extraction(),
        "edge_inference": {
            "tier_assignments": [
                {
                    "edge_type": "supersedes",
                    "tier": 1,
                    "inference_rules": [{"method": "version_chain"}],
                },
            ],
        },
    }


@pytest.fixture
def pim_style_config(tmp_vault_dir):
    """Parsed VaultConfig with PIM-style filename_extraction."""
    return VaultConfig.model_validate(_pim_vault_config_dict(tmp_vault_dir))


@pytest.fixture
def pim_style_ingestion_service(
    graph_store,
    lock_manager,
    stub_content_store,
    stub_embedding_provider,
    stub_abstraction_provider,
    pim_style_config,
):
    """IngestionService with PIM-style metadata_extraction and both adapters."""
    return IngestionService(
        graph_store=graph_store,
        lock_manager=lock_manager,
        content_store=stub_content_store,
        embedding_provider=stub_embedding_provider,
        abstraction_provider=stub_abstraction_provider,
        config=pim_style_config,
        source_adapters={
            SourceType.MARKDOWN: MarkdownAdapter(),
            SourceType.DOCX: DocxAdapter(),
        },
    )


# ---------------------------------------------------------------------------
# File helpers
# ---------------------------------------------------------------------------


def _write_md(
    tmp_vault_dir: Path, relative_path: str, body: str = "# Default Heading\n\nBody.\n"
) -> Path:
    full_path = tmp_vault_dir / "sources" / relative_path
    full_path.parent.mkdir(parents=True, exist_ok=True)
    full_path.write_text(body)
    return full_path


def _write_docx(tmp_vault_dir: Path, relative_path: str) -> Path:
    """Write a minimal empty .docx at the given vault-relative path."""
    full_path = tmp_vault_dir / "sources" / relative_path
    full_path.parent.mkdir(parents=True, exist_ok=True)
    doc = _docx_pkg.Document()
    doc.save(str(full_path))
    return full_path


# ---------------------------------------------------------------------------
# TEST-SAGE-ME-001: Filename parse populates document record on direct ingest
# ---------------------------------------------------------------------------


async def test_me_001_filename_parse_populates_record(tmp_vault_dir, pim_style_ingestion_service):
    _write_md(
        tmp_vault_dir,
        "patents/2026-03-09_PIM_PV06_Claim-Set_v6.md",
        body="# A Heading\n\nBody.\n",
    )

    request = IngestRequest(
        source="patents/2026-03-09_PIM_PV06_Claim-Set_v6.md",
        adapter=SourceType.MARKDOWN,
        # ADR-021: filename inference runs only under needs_review=True.
        needs_review=True,
    )
    result = await pim_style_ingestion_service.ingest(request)
    doc = result.document

    assert doc.title == "Claim-Set"
    assert doc.document_date == "2026-03-09"
    assert doc.project == "PIM"
    assert doc.tags == ["PV06"]
    assert doc.version_label == "v6.0"
    assert doc.doc_type == "patent_draft"


# ---------------------------------------------------------------------------
# TEST-SAGE-ME-002: Caller-supplied metadata overrides filename parse
# ---------------------------------------------------------------------------


async def test_me_002_caller_metadata_overrides_filename_parse(
    tmp_vault_dir, pim_style_ingestion_service
):
    _write_md(
        tmp_vault_dir,
        "patents/2026-03-09_PIM_PV06_Claim-Set_v6.md",
        body="# A Heading\n\nBody.\n",
    )

    request = IngestRequest(
        source="patents/2026-03-09_PIM_PV06_Claim-Set_v6.md",
        adapter=SourceType.MARKDOWN,
        # ADR-021: filename inference runs only under needs_review=True.
        needs_review=True,
        metadata={
            "title": "Custom Title",
            "project": "OTHER",
            "version_label": "v99.0",
        },
    )
    result = await pim_style_ingestion_service.ingest(request)
    doc = result.document

    # Caller wins where specified
    assert doc.title == "Custom Title"
    assert doc.project == "OTHER"
    assert doc.version_label == "v99.0"

    # Filename parse fills unspecified fields
    assert doc.document_date == "2026-03-09"
    assert doc.tags == ["PV06"]
    assert doc.doc_type == "patent_draft"


# ---------------------------------------------------------------------------
# TEST-SAGE-ME-003: Filename-parsed title overrides adapter title (the Cowork case)
# ---------------------------------------------------------------------------


async def test_me_003_filename_title_overrides_adapter_title(
    tmp_vault_dir, pim_style_ingestion_service
):
    _write_md(
        tmp_vault_dir,
        "patents/2026-03-09_PIM_PV06_Claim-Set_v6.md",
        body="# A Long Rhetorical Title That Differs From The Filename\n\nBody.\n",
    )

    request = IngestRequest(
        source="patents/2026-03-09_PIM_PV06_Claim-Set_v6.md",
        adapter=SourceType.MARKDOWN,
        # ADR-021: filename inference runs only under needs_review=True.
        needs_review=True,
    )
    result = await pim_style_ingestion_service.ingest(request)

    # Filename parse wins when vault has a filename pattern configured.
    # Without ADR-015, the adapter's first-H1 title would win.
    assert result.document.title == "Claim-Set"


# ---------------------------------------------------------------------------
# TEST-SAGE-ME-004: No filename pattern -> adapter title is used
# ---------------------------------------------------------------------------


@pytest.fixture
def no_pattern_ingestion_service(
    graph_store,
    lock_manager,
    stub_content_store,
    stub_embedding_provider,
    stub_abstraction_provider,
    tmp_vault_dir,
):
    """IngestionService whose vault has NO filename_extraction block."""
    config_dict = _pim_vault_config_dict(tmp_vault_dir)
    # Strip filename_extraction: vault declares no filename convention.
    config_dict["metadata_extraction"] = {}
    config = VaultConfig.model_validate(config_dict)
    return IngestionService(
        graph_store=graph_store,
        lock_manager=lock_manager,
        content_store=stub_content_store,
        embedding_provider=stub_embedding_provider,
        abstraction_provider=stub_abstraction_provider,
        config=config,
        source_adapters={SourceType.MARKDOWN: MarkdownAdapter()},
    )


async def test_me_004_no_filename_pattern_preserves_adapter_title(
    tmp_vault_dir, no_pattern_ingestion_service
):
    _write_md(
        tmp_vault_dir,
        "notes/workflow_notes.md",
        body="# Session Handoff Notes\n\nBody.\n",
    )

    request = IngestRequest(
        source="notes/workflow_notes.md",
        adapter=SourceType.MARKDOWN,
    )
    result = await no_pattern_ingestion_service.ingest(request)
    doc = result.document

    # Adapter's H1 wins (no filename parse configured)
    assert doc.title == "Session Handoff Notes"

    # doc_type defaults to misc (no filename parse, no caller metadata)
    assert doc.doc_type == "misc"

    # No filename metadata populated
    assert doc.project is None
    assert doc.tags == []
    assert doc.version_label is None

    # document_date still derives from source_modified_at (BH-063 behavior)
    assert doc.document_date is not None


# ---------------------------------------------------------------------------
# TEST-SAGE-ME-005: doc_type does not default to "misc" when filename parse resolves one
# ---------------------------------------------------------------------------


async def test_me_005_filename_doc_type_is_not_overwritten_by_misc(
    tmp_vault_dir, pim_style_ingestion_service
):
    _write_md(
        tmp_vault_dir,
        "refs/2026-02-01_PIM_REF_Glossary_v2.md",
        body="# Glossary\n\nBody.\n",
    )

    request = IngestRequest(
        source="refs/2026-02-01_PIM_REF_Glossary_v2.md",
        adapter=SourceType.MARKDOWN,
        # ADR-021: filename inference runs only under needs_review=True.
        needs_review=True,
    )
    result = await pim_style_ingestion_service.ingest(request)

    # Compound rule (code=REF + title_contains=Glossary) resolves to glossary.
    # The default-to-misc fallback must NOT fire when a filename parse yielded
    # a doc_type.
    assert result.document.doc_type == "glossary"


# ---------------------------------------------------------------------------
# TEST-SAGE-ME-006: doc_type still defaults to "misc" when nothing yields one
# ---------------------------------------------------------------------------


async def test_me_006_doc_type_defaults_to_misc_when_unresolved(
    tmp_vault_dir, pim_style_ingestion_service
):
    # Filename has no code that matches any code_to_doc_type rule,
    # and "Untagged-Note" does not match any keyword_to_doc_type keyword.
    _write_md(
        tmp_vault_dir,
        "random/2026-03-01_PIM_Untagged-Note.md",
        body="# Some Heading\n\nBody.\n",
    )

    request = IngestRequest(
        source="random/2026-03-01_PIM_Untagged-Note.md",
        adapter=SourceType.MARKDOWN,
        # ADR-021: filename inference runs only under needs_review=True.
        needs_review=True,
    )
    result = await pim_style_ingestion_service.ingest(request)
    doc = result.document

    # Fallback still applies
    assert doc.doc_type == "misc"

    # But filename parse ran and populated what it could
    assert doc.document_date == "2026-03-01"
    assert doc.project == "PIM"


# ---------------------------------------------------------------------------
# TEST-SAGE-ME-007: Markdown and docx adapters share the same metadata path
# ---------------------------------------------------------------------------


@requires_docx
async def test_me_007_markdown_and_docx_produce_identical_metadata(
    tmp_vault_dir, pim_style_ingestion_service
):
    _write_md(
        tmp_vault_dir,
        "patents/2026-03-09_PIM_PV06_A_v1.md",
        body="# A\n\nBody.\n",
    )
    _write_docx(
        tmp_vault_dir,
        "patents/2026-03-09_PIM_PV06_A_v1.docx",
    )

    md_result = await pim_style_ingestion_service.ingest(
        IngestRequest(
            source="patents/2026-03-09_PIM_PV06_A_v1.md",
            adapter=SourceType.MARKDOWN,
            # ADR-021: filename inference runs only under needs_review=True.
            needs_review=True,
        )
    )
    docx_result = await pim_style_ingestion_service.ingest(
        IngestRequest(
            source="patents/2026-03-09_PIM_PV06_A_v1.docx",
            adapter=SourceType.DOCX,
            needs_review=True,
        )
    )

    md_doc = md_result.document
    dx_doc = docx_result.document

    # Filename-extracted fields must match across adapters.
    assert md_doc.title == dx_doc.title == "A"
    assert md_doc.project == dx_doc.project == "PIM"
    assert md_doc.tags == dx_doc.tags == ["PV06"]
    assert md_doc.version_label == dx_doc.version_label == "v1.0"
    assert md_doc.document_date == dx_doc.document_date == "2026-03-09"
    assert md_doc.doc_type == dx_doc.doc_type == "patent_draft"


# ---------------------------------------------------------------------------
# TEST-SAGE-ME-008: removed by CAS-ADR-021 implementation Chunk 2.
#
# The vault-config metadata_extraction.review_required flag no longer
# drives metadata_confirmed at ingest; behavior is gated on
# IngestRequest.needs_review (caller-authoritative). Coverage of the
# new contract lives in tests/sage/test_ad021_ingestion.py
# (TEST-AD021-001, TEST-AD021-002, TEST-AD021-003). The vault config
# field itself is removed in ADR-021 cleanup Phase B.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# TEST-SAGE-ME-009: App-backend regression
# ---------------------------------------------------------------------------
#
# The import relocation from app.backend.filename_parser to
# sage.services.filename_parser must not break the app backend's
# scan/ingest/edge_inference flow. Rather than duplicating the
# app backend tests here, we assert that the sage.services module
# exists and exposes the same public surface the app backend relies
# on. The existing tests/app/test_app_backend.py provides the
# behavioral coverage and must continue to pass after the import
# update; that is verified by running the full test suite.
# ---------------------------------------------------------------------------


def test_me_009_filename_parser_is_accessible_from_sage_services():
    from sage.services.filename_parser import (
        FilenameParser,
        ParsedMetadata,
        format_version,
        normalize_version,
    )

    # Sanity-check the public surface the app backend imports.
    assert callable(FilenameParser)
    assert callable(normalize_version)
    assert callable(format_version)
    assert ParsedMetadata.__dataclass_fields__  # is a dataclass
