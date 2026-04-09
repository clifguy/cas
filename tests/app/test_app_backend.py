"""Tests for CAS Application backend.

Covers:
  - Filename parser (EI-001 through EI-012)
  - Version chain inference (EI-013 through EI-018)
  - Filename code match inference (EI-019 through EI-024)
  - Two-phase orchestration (EI-025 through EI-030)
  - Scan endpoint (BE-017 through BE-021)
  - Batch ingest with SSE (BE-022 through BE-028, BE-031 through BE-033)
"""

import asyncio
import json
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from app.backend.filename_parser import FilenameParser, ParsedMetadata, normalize_version
from app.backend.edge_inference import (
    EdgeInferenceEngine,
    EdgePlan,
    InferenceItem,
    PlannedEdge,
    resolve_and_execute,
)
from app.backend.scan import ScanResult, scan_directory, EXTENSION_TO_ADAPTER
from sage.app import create_app, _initialize_services
from sage.config import VaultConfig
from sage.models.enums import EdgeType, PipelineStatus, SourceType
from sage.models.schemas import Document


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _pim_metadata_extraction():
    """PIM Health-like metadata extraction config."""
    return {
        "review_required": False,
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
            ],
            "keyword_to_doc_type": [
                {"keyword": "Checklist", "doc_type": "checklist"},
                {"keyword": "Work-Plan", "doc_type": "work_plan"},
                {"keyword": "Session-Context", "doc_type": "session_context"},
                {"keyword": "Template", "doc_type": "template"},
            ],
            "code_to_doc_type": [
                {"code": "REF", "title_contains": "Glossary", "doc_type": "glossary"},
                {"code": "REF", "title_contains": "FormattingStandards", "doc_type": "formatting_standards"},
                {"code": "REF", "title_contains": "IntegrationCatalog", "doc_type": "integration_catalog"},
                {"code": "REF", "doc_type": "reference_document"},
                {"code": "PVMaster", "doc_type": "patent_draft"},
                {"code": "PV", "doc_type": "patent_draft"},
                {"code": "TD", "doc_type": "technical_disclosure"},
            ],
        },
    }


def _make_vault_config_dict(tmp_path, vault_id: str, vault_name: str):
    """Create a vault config dict with PIM-like metadata extraction."""
    brain_dir = tmp_path / vault_id / "brain"
    brain_dir.mkdir(parents=True, exist_ok=True)
    sources_dir = tmp_path / vault_id / "sources"
    sources_dir.mkdir(parents=True, exist_ok=True)
    return {
        "vault": {
            "id": vault_id,
            "name": vault_name,
            "description": f"Test vault: {vault_name}",
            "owner": "testuser",
            "storage_root": str(sources_dir),
            "brain_root": str(brain_dir),
            "visibility": "personal",
        },
        "document_types": {
            "doc_types": [
                {"value": "patent_draft", "label": "Patent Draft"},
                {"value": "technical_disclosure", "label": "Technical Disclosure"},
                {"value": "glossary", "label": "Glossary"},
                {"value": "reference_document", "label": "Reference Document"},
                {"value": "checklist", "label": "Checklist"},
                {"value": "work_plan", "label": "Work Plan"},
                {"value": "session_context", "label": "Session Context"},
                {"value": "template", "label": "Template"},
                {"value": "integration_catalog", "label": "Integration Catalog"},
            ],
        },
        "lifecycle": {
            "base_states_required": True,
            "states": [
                {"value": "active", "label": "Active"},
                {"value": "superseded", "label": "Superseded"},
                {"value": "completed", "label": "Completed"},
                {"value": "archived", "label": "Archived", "is_terminal": True},
            ],
            "transitions": [
                {"from_state": "(new)", "action": "ingest", "to_state": "active"},
                {"from_state": "active", "action": "supersede", "to_state": "superseded", "creates_edge": "supersedes"},
                {"from_state": "active", "action": "complete", "to_state": "completed"},
                {"from_state": "active", "action": "archive", "to_state": "archived"},
                {"from_state": "superseded", "action": "archive", "to_state": "archived"},
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
                {
                    "edge_type": "covers",
                    "tier": 2,
                    "inference_rules": [{"method": "filename_code_match"}],
                },
            ],
        },
    }


# ---------------------------------------------------------------------------
# 1. Filename Parser: Segment Recognition (EI-001 through EI-008)
# ---------------------------------------------------------------------------


class TestFilenameParserSegments:

    def _parser(self):
        return FilenameParser(_pim_metadata_extraction())

    def test_ei_001_standard_filename(self):
        """Parse standard filename with all segments."""
        p = self._parser()
        result = p.parse("2026-03-09_PIM_PV06_Claim-Set_v6")
        assert result.date == "2026-03-09"
        assert result.project == "PIM"
        assert "PV06" in result.codes
        assert result.title == "Claim-Set"
        assert result.version == "v6"

    def test_ei_002_missing_date(self):
        """Parse filename missing date."""
        p = self._parser()
        result = p.parse("PIM_REF_Glossary_v10")
        assert result.date is None
        assert result.project == "PIM"
        assert "REF" in result.codes
        assert result.title == "Glossary"
        assert result.version == "v10"

    def test_ei_003_missing_version(self):
        """Parse filename missing version."""
        p = self._parser()
        result = p.parse("2026-03-15_PIM_TD_Neural-Pathway-Analysis")
        assert result.date == "2026-03-15"
        assert result.project == "PIM"
        assert "TD" in result.codes
        assert "Neural-Pathway-Analysis" in result.title
        assert result.version is None

    def test_ei_004_multiple_codes(self):
        """Parse filename with multiple codes."""
        p = self._parser()
        result = p.parse("2026-03-20_PIM_PV06_CF-1_Integration-Catalog_v3")
        assert "PV06" in result.codes
        assert "CF-1" in result.codes
        assert len(result.codes) == 2
        assert result.title == "Integration-Catalog"

    def test_ei_005_only_title(self):
        """Parse filename with only title."""
        p = self._parser()
        result = p.parse("Meeting-Notes-March")
        assert result.date is None
        assert result.project is None
        assert result.codes == []
        assert result.version is None
        assert "Meeting-Notes-March" in result.title

    def test_ei_006_date_pattern_strict(self):
        """Only YYYY-MM-DD recognized as date."""
        p = self._parser()
        # Valid
        r1 = p.parse("2026-03-15_PIM_REF_Doc_v1")
        assert r1.date == "2026-03-15"
        # MM-DD-YYYY not recognized
        r2 = p.parse("03-15-2026_PIM_REF_Doc_v1")
        assert r2.date is None
        # Compact not recognized
        r3 = p.parse("20260315_PIM_REF_Doc_v1")
        assert r3.date is None

    def test_ei_007_version_from_right(self):
        """Version identified by v-prefix scanning from right."""
        p = self._parser()
        result = p.parse("2026-03-09_PIM_PV06_Validation-Report_v3")
        assert result.version == "v3"
        assert "Validation-Report" in result.title
        assert "PV06" in result.codes

    def test_ei_008_code_from_known_patterns(self):
        """Codes recognized via known_code_patterns from vault config."""
        p = self._parser()
        # PV06 matches ^[A-Z][A-Z0-9]{1,7}$
        r1 = p.parse("2026-03-09_PIM_PV06_Title_v1")
        assert "PV06" in r1.codes
        # CF-1 matches ^[A-Z]+-\d+$
        r2 = p.parse("2026-03-09_PIM_CF-1_Title_v1")
        assert "CF-1" in r2.codes
        # x99 matches neither (lowercase x, case-sensitive)
        r3 = p.parse("2026-03-09_PIM_x99_Title_v1")
        assert "x99" not in r3.codes


# ---------------------------------------------------------------------------
# 2. Filename Parser: Doc Type Resolution (EI-009 through EI-012)
# ---------------------------------------------------------------------------


class TestFilenameParserDocType:

    def _parser(self):
        return FilenameParser(_pim_metadata_extraction())

    def test_ei_009_keyword_before_code(self):
        """keyword_to_doc_type evaluated before code_to_doc_type."""
        p = self._parser()
        result = p.parse("2026-03-20_PIM_PV06_Checklist_v3")
        assert result.doc_type == "checklist"  # keyword match, not patent_draft

    def test_ei_010_compound_key_precedence(self):
        """code_to_doc_type compound key takes precedence."""
        p = self._parser()
        r1 = p.parse("2026-03-15_PIM_REF_Glossary_v10")
        assert r1.doc_type == "glossary"  # compound: REF + Glossary
        r2 = p.parse("2026-03-15_PIM_REF_Architecture-QA_v2")
        assert r2.doc_type == "reference_document"  # code-only: REF

    def test_ei_011_case_insensitive_keyword(self):
        """Keyword matching is case-insensitive."""
        p = self._parser()
        r1 = p.parse("PIM_PV06_Checklist_v3")
        assert r1.doc_type == "checklist"
        r2 = p.parse("PIM_PV06_CHECKLIST_v3")
        assert r2.doc_type == "checklist"
        r3 = p.parse("PIM_PV06_checklist_v3")
        assert r3.doc_type == "checklist"

    def test_ei_012_no_match_null_doc_type(self):
        """No rules match -> doc_type is null."""
        p = self._parser()
        result = p.parse("2026-03-15_PIM_UNKNOWN_Report_v1")
        # UNKNOWN matches a code pattern but has no code_to_doc_type mapping
        assert "UNKNOWN" in result.codes
        assert result.doc_type is None


# ---------------------------------------------------------------------------
# 2c. Filename Parser: Source Type Constraint on Doc Types
# ---------------------------------------------------------------------------


class TestFilenameParserSourceTypeConstraint:
    """Tests for source_types constraint on doc_type definitions.

    When a doc_type has a source_types list, it should only be resolved
    for files whose adapter matches. Non-matching adapters skip the rule.
    """

    def _parser(self):
        doc_types = [
            {"value": "patent_draft", "source_types": ["docx"]},
            {"value": "technical_disclosure", "source_types": ["docx"]},
            {"value": "checklist"},  # no constraint
            {"value": "report"},
            {"value": "glossary"},
        ]
        return FilenameParser(_pim_metadata_extraction(), doc_types=doc_types)

    def test_constrained_doc_type_matches_adapter(self):
        """PV code resolves to patent_draft when adapter is docx."""
        p = self._parser()
        result = p.parse("PIM_PV06_Claim-Set_v6", adapter="docx")
        assert result.doc_type == "patent_draft"

    def test_constrained_doc_type_skipped_for_wrong_adapter(self):
        """PV code does not resolve to patent_draft for markdown adapter."""
        p = self._parser()
        result = p.parse("PIM_PV06_Terminology_Audit_v1_0", adapter="markdown")
        assert result.doc_type != "patent_draft"

    def test_constrained_doc_type_skipped_for_no_adapter(self):
        """PV code does not resolve to patent_draft when adapter is None."""
        p = self._parser()
        result = p.parse("PIM_PV06_SomeFile_v1", adapter=None)
        assert result.doc_type != "patent_draft"

    def test_unconstrained_doc_type_any_adapter(self):
        """Doc type without source_types resolves for any adapter."""
        p = self._parser()
        r1 = p.parse("PIM_PV06_Checklist_v3", adapter="markdown")
        assert r1.doc_type == "checklist"
        r2 = p.parse("PIM_PV06_Checklist_v3", adapter="docx")
        assert r2.doc_type == "checklist"

    def test_no_doc_types_config_ignores_constraint(self):
        """Parser without doc_types config behaves as before (no constraint)."""
        p = FilenameParser(_pim_metadata_extraction())
        result = p.parse("PIM_PV06_Claim-Set_v6", adapter="markdown")
        assert result.doc_type == "patent_draft"  # no constraint applied

    def test_constrained_doc_type_matches_with_explicit_adapter(self):
        """Constraint only passes when adapter explicitly matches."""
        p = self._parser()
        r_docx = p.parse("PIM_TD08_Analysis_v1", adapter="docx")
        assert r_docx.doc_type == "technical_disclosure"
        r_md = p.parse("PIM_TD08_Analysis_v1", adapter="markdown")
        assert r_md.doc_type != "technical_disclosure"


# ---------------------------------------------------------------------------
# 2b. Filename Parser: Pre-split Date/Version Extraction
# ---------------------------------------------------------------------------


class TestFilenameParserPreSplit:
    """Tests for pre-split extraction of date and version from full stem.

    These cover the two bugs found in bulk ingest:
      1. Space-separated date/title boundary (date fuses with next segment)
      2. Underscore-delimited version components (v2_3 split into v2 + orphan 3)
    """

    def _parser(self):
        return FilenameParser(_pim_metadata_extraction())

    # -- Bug 1: Mixed-delimiter date boundary --

    def test_space_separated_date(self):
        """Date followed by space instead of underscore is still extracted."""
        p = self._parser()
        # Note: the space is in the stem because Path.stem preserves it
        result = p.parse("2025-12-20 PIM_Portfolio_Refactoring_Checklist_v2")
        assert result.date == "2025-12-20"
        assert result.project == "PIM"
        assert result.title == "Portfolio_Refactoring_Checklist"
        assert result.version == "v2"

    def test_underscore_separated_date(self):
        """Date followed by underscore still works (no regression)."""
        p = self._parser()
        result = p.parse("2025-12-20_PIM_Portfolio_Refactoring_Checklist_v2")
        assert result.date == "2025-12-20"
        assert result.project == "PIM"
        assert result.title == "Portfolio_Refactoring_Checklist"
        assert result.version == "v2"

    # -- Bug 2: Multi-part version with underscore sub-separator --

    def test_version_underscore_minor(self):
        """Version with underscore minor component: v2_3 parsed as single version."""
        p = self._parser()
        result = p.parse("2025-12-20_PIM_Portfolio_Refactoring_Checklist_v2_3")
        assert result.version == "v2_3"
        assert result.title == "Portfolio_Refactoring_Checklist"
        assert result.date == "2025-12-20"
        assert result.project == "PIM"

    def test_version_underscore_two_parts(self):
        """Version v2_4 captured intact, not split."""
        p = self._parser()
        result = p.parse("2025-12-20_PIM_Portfolio_Refactoring_Checklist_v2_4")
        assert result.version == "v2_4"
        assert result.title == "Portfolio_Refactoring_Checklist"

    def test_version_dot_minor(self):
        """Version with dot minor component: v2.1 parsed correctly."""
        p = self._parser()
        result = p.parse("2025-12-20_PIM_Portfolio_Refactoring_Checklist_v2.1")
        assert result.version == "v2.1"
        assert result.title == "Portfolio_Refactoring_Checklist"
        assert result.date == "2025-12-20"
        assert result.project == "PIM"

    def test_version_three_part_underscore(self):
        """Three-part version with underscores: v8_4_1."""
        p = self._parser()
        result = p.parse("PIM_REF_Doc_v8_4_1")
        assert result.version == "v8_4_1"
        assert result.title == "Doc"
        assert "REF" in result.codes

    # -- Version chain grouping: all five test files produce same title --

    def test_bulk_ingest_version_family(self):
        """All five test files produce identical (title, project) for grouping."""
        p = self._parser()
        stems = [
            "2025-12-20 PIM_Portfolio_Refactoring_Checklist_v2",
            "2025-12-20_PIM_Portfolio_Refactoring_Checklist_v2_3",
            "2025-12-20_PIM_Portfolio_Refactoring_Checklist_v2_4",
            "2025-12-20_PIM_Portfolio_Refactoring_Checklist_v2_5",
            "2025-12-20_PIM_Portfolio_Refactoring_Checklist_v2.1",
        ]
        results = [p.parse(s) for s in stems]

        # All titles identical
        titles = {r.title for r in results}
        assert titles == {"Portfolio_Refactoring_Checklist"}, f"Got titles: {titles}"

        # All projects identical
        projects = {r.project for r in results}
        assert projects == {"PIM"}, f"Got projects: {projects}"

        # All dates identical
        dates = {r.date for r in results}
        assert dates == {"2025-12-20"}, f"Got dates: {dates}"

        # Versions are distinct and normalize in correct order
        versions = [r.version for r in results]
        assert len(set(versions)) == 5, f"Expected 5 distinct versions, got: {versions}"

        normalized = sorted(
            [(r.version, normalize_version(r.version)) for r in results],
            key=lambda t: t[1],
        )
        version_order = [v for v, _ in normalized]
        assert version_order == ["v2", "v2.1", "v2_3", "v2_4", "v2_5"]

    # -- Regression guards --

    def test_standard_filename_unchanged(self):
        """Standard underscore-only filename still parses correctly."""
        p = self._parser()
        result = p.parse("2026-03-09_PIM_PV06_Claim-Set_v6")
        assert result.date == "2026-03-09"
        assert result.project == "PIM"
        assert "PV06" in result.codes
        assert result.title == "Claim-Set"
        assert result.version == "v6"

    def test_no_version_no_date(self):
        """Filename with neither date nor version still parses title."""
        p = self._parser()
        result = p.parse("PIM_REF_Glossary")
        assert result.date is None
        assert result.version is None
        assert result.project == "PIM"
        assert "REF" in result.codes
        assert result.title == "Glossary"

    def test_version_case_insensitive(self):
        """Uppercase V prefix is recognized."""
        p = self._parser()
        result = p.parse("2025-12-20_PIM_Doc_V3_2")
        assert result.version == "V3_2"
        assert normalize_version(result.version) == (3, 2, 0)


# ---------------------------------------------------------------------------
# 3. Version Chain Inference (EI-013 through EI-018)
# ---------------------------------------------------------------------------


class TestVersionChain:

    def test_ei_013_version_normalization(self):
        """Version normalization to (major, minor, patch) tuple."""
        assert normalize_version("v7") == (7, 0, 0)
        assert normalize_version("v10_2") == (10, 2, 0)
        assert normalize_version("v8_4_1") == (8, 4, 1)
        assert normalize_version("v1.3") == (1, 3, 0)
        assert normalize_version("v12") == (12, 0, 0)

    def test_ei_014_linear_chain(self):
        """Linear chain: each version supersedes immediate predecessor."""
        engine = EdgeInferenceEngine()
        items = [
            InferenceItem("f1", False, ParsedMetadata("Claim-Set", codes=["PV06"], version="v1", doc_type="patent_draft")),
            InferenceItem("f3", False, ParsedMetadata("Claim-Set", codes=["PV06"], version="v3", doc_type="patent_draft")),
            InferenceItem("f7", False, ParsedMetadata("Claim-Set", codes=["PV06"], version="v7", doc_type="patent_draft")),
        ]
        plan = engine.build_edge_plan(items, [])
        supersedes = [e for e in plan.edges if e.edge_type == EdgeType.SUPERSEDES]
        assert len(supersedes) == 2
        # v7 supersedes v3
        assert any(e.source_ref == "f7" and e.target_ref == "f3" for e in supersedes)
        # v3 supersedes v1
        assert any(e.source_ref == "f3" and e.target_ref == "f1" for e in supersedes)
        # No v7 -> v1 edge
        assert not any(e.source_ref == "f7" and e.target_ref == "f1" for e in supersedes)

    def test_ei_015_groups_by_title(self):
        """Version chains scoped to documents sharing the same title."""
        engine = EdgeInferenceEngine()
        items = [
            InferenceItem("a1", False, ParsedMetadata("Claim-Set", version="v1", doc_type="patent_draft")),
            InferenceItem("a2", False, ParsedMetadata("Claim-Set", version="v2", doc_type="patent_draft")),
            InferenceItem("b1", False, ParsedMetadata("Neural-Analysis", version="v1", doc_type="technical_disclosure")),
            InferenceItem("b2", False, ParsedMetadata("Neural-Analysis", version="v2", doc_type="technical_disclosure")),
        ]
        plan = engine.build_edge_plan(items, [])
        supersedes = [e for e in plan.edges if e.edge_type == EdgeType.SUPERSEDES]
        assert len(supersedes) == 2
        assert any(e.source_ref == "a2" and e.target_ref == "a1" for e in supersedes)
        assert any(e.source_ref == "b2" and e.target_ref == "b1" for e in supersedes)

    def test_ei_016_includes_existing_vault_docs(self):
        """Version chain includes existing vault documents."""
        engine = EdgeInferenceEngine()
        scan_items = [
            InferenceItem("f6", False, ParsedMetadata("Claim-Set", version="v6", doc_type="patent_draft")),
            InferenceItem("f7", False, ParsedMetadata("Claim-Set", version="v7", doc_type="patent_draft")),
        ]
        existing = [
            InferenceItem("existing-v5", True, ParsedMetadata("Claim-Set", version="v5", doc_type="patent_draft")),
        ]
        plan = engine.build_edge_plan(scan_items, existing)
        supersedes = [e for e in plan.edges if e.edge_type == EdgeType.SUPERSEDES]
        assert len(supersedes) == 2
        assert any(e.source_ref == "f7" and e.target_ref == "f6" for e in supersedes)
        assert any(e.source_ref == "f6" and e.target_ref == "existing-v5" for e in supersedes)

    def test_ei_017_single_version_no_edge(self):
        """Single version produces no supersedes edge."""
        engine = EdgeInferenceEngine()
        items = [
            InferenceItem("f1", False, ParsedMetadata("Claim-Set", version="v1", doc_type="patent_draft")),
        ]
        plan = engine.build_edge_plan(items, [])
        supersedes = [e for e in plan.edges if e.edge_type == EdgeType.SUPERSEDES]
        assert len(supersedes) == 0

    def test_ei_018_null_version_is_original(self):
        """Versionless file treated as original, sorts before all versions."""
        engine = EdgeInferenceEngine()
        items = [
            InferenceItem("f0", False, ParsedMetadata("Neural-Analysis", version=None, doc_type="technical_disclosure")),
            InferenceItem("f1", False, ParsedMetadata("Neural-Analysis", version="v1", doc_type="technical_disclosure")),
            InferenceItem("f2", False, ParsedMetadata("Neural-Analysis", version="v2", doc_type="technical_disclosure")),
        ]
        plan = engine.build_edge_plan(items, [])
        supersedes = [e for e in plan.edges if e.edge_type == EdgeType.SUPERSEDES]
        assert len(supersedes) == 2  # v2 -> v1 -> original
        assert any(e.source_ref == "f2" and e.target_ref == "f1" for e in supersedes)
        assert any(e.source_ref == "f1" and e.target_ref == "f0" for e in supersedes)

    def test_ei_018b_all_versionless_no_chain(self):
        """All-versionless group produces no edges."""
        engine = EdgeInferenceEngine()
        items = [
            InferenceItem("f0", False, ParsedMetadata("Report", version=None, doc_type="report")),
            InferenceItem("f1", False, ParsedMetadata("Report", version=None, doc_type="report")),
        ]
        plan = engine.build_edge_plan(items, [])
        supersedes = [e for e in plan.edges if e.edge_type == EdgeType.SUPERSEDES]
        assert len(supersedes) == 0


# ---------------------------------------------------------------------------
# 4. Filename Code Match Inference (EI-019 through EI-024)
# ---------------------------------------------------------------------------


class TestFilenameCodeMatch:

    def test_ei_019_workflow_covers_content(self):
        """Workflow artifact covers content artifact sharing a code."""
        engine = EdgeInferenceEngine()
        items = [
            InferenceItem("chk", False, ParsedMetadata("Checklist", codes=["PV06"], version="v3", doc_type="checklist")),
            InferenceItem("pat", False, ParsedMetadata("Claim-Set", codes=["PV06"], version="v7", doc_type="patent_draft")),
        ]
        plan = engine.build_edge_plan(items, [])
        covers = [e for e in plan.edges if e.edge_type == EdgeType.COVERS]
        assert len(covers) == 1
        assert covers[0].source_ref == "chk"
        assert covers[0].target_ref == "pat"

    def test_ei_020_direction_workflow_source(self):
        """Direction: workflow is source, content is target."""
        engine = EdgeInferenceEngine()
        items = [
            InferenceItem("pat", False, ParsedMetadata("Claim-Set", codes=["PV06"], doc_type="patent_draft")),
            InferenceItem("chk", False, ParsedMetadata("Checklist", codes=["PV06"], doc_type="checklist")),
        ]
        plan = engine.build_edge_plan(items, [])
        covers = [e for e in plan.edges if e.edge_type == EdgeType.COVERS]
        assert len(covers) == 1
        # Workflow (checklist) is source, content (patent_draft) is target
        assert covers[0].source_ref == "chk"
        assert covers[0].target_ref == "pat"

    def test_ei_021_workflow_to_workflow_no_edge(self):
        """Workflow-to-workflow pairs get no automatic edge."""
        engine = EdgeInferenceEngine()
        items = [
            InferenceItem("chk", False, ParsedMetadata("Checklist", codes=["PV06"], doc_type="checklist")),
            InferenceItem("wp", False, ParsedMetadata("Work-Plan", codes=["PV06"], doc_type="work_plan")),
        ]
        plan = engine.build_edge_plan(items, [])
        covers = [e for e in plan.edges if e.edge_type == EdgeType.COVERS]
        assert len(covers) == 0

    def test_ei_022_content_to_content_no_edge(self):
        """Content-to-content pairs get no automatic edge."""
        engine = EdgeInferenceEngine()
        items = [
            InferenceItem("p1", False, ParsedMetadata("Claim-Set", codes=["PV06"], doc_type="patent_draft")),
            InferenceItem("p2", False, ParsedMetadata("Specification", codes=["PV06"], doc_type="patent_draft")),
        ]
        plan = engine.build_edge_plan(items, [])
        covers = [e for e in plan.edges if e.edge_type == EdgeType.COVERS]
        assert len(covers) == 0

    def test_ei_023_match_across_new_and_existing(self):
        """Code match across new files and existing vault documents."""
        engine = EdgeInferenceEngine()
        scan_items = [
            InferenceItem("chk", False, ParsedMetadata("Checklist", codes=["PV06"], doc_type="checklist")),
        ]
        existing = [
            InferenceItem("existing-patent", True, ParsedMetadata("Claim-Set", codes=["PV06"], doc_type="patent_draft")),
        ]
        plan = engine.build_edge_plan(scan_items, existing)
        covers = [e for e in plan.edges if e.edge_type == EdgeType.COVERS]
        assert len(covers) == 1
        assert covers[0].source_ref == "chk"
        assert covers[0].target_ref == "existing-patent"

    def test_ei_024_multiple_codes_multiple_edges(self):
        """Multiple codes produce multiple edges."""
        engine = EdgeInferenceEngine()
        items = [
            InferenceItem("chk", False, ParsedMetadata("Checklist", codes=["PV06", "CF-1"], doc_type="checklist")),
            InferenceItem("pat", False, ParsedMetadata("Claim-Set", codes=["PV06"], doc_type="patent_draft")),
            InferenceItem("ic", False, ParsedMetadata("Integration-Catalog", codes=["CF-1"], doc_type="integration_catalog")),
        ]
        plan = engine.build_edge_plan(items, [])
        covers = [e for e in plan.edges if e.edge_type == EdgeType.COVERS]
        assert len(covers) == 2
        targets = {e.target_ref for e in covers}
        assert targets == {"pat", "ic"}


# ---------------------------------------------------------------------------
# 5. Two-Phase Orchestration (EI-025 through EI-030)
# ---------------------------------------------------------------------------


class TestTwoPhaseOrchestration:

    def test_ei_025_pre_ingest_edge_plan(self):
        """Pre-ingest analysis builds edge plan from file manifest."""
        engine = EdgeInferenceEngine()
        scan_items = [
            InferenceItem("f_v6", False, ParsedMetadata("Claim-Set", codes=["PV06"], version="v6", doc_type="patent_draft")),
            InferenceItem("f_v7", False, ParsedMetadata("Claim-Set", codes=["PV06"], version="v7", doc_type="patent_draft")),
            InferenceItem("f_chk", False, ParsedMetadata("Checklist", codes=["PV06"], version="v3", doc_type="checklist")),
        ]
        existing = [
            InferenceItem("existing-v5", True, ParsedMetadata("Claim-Set", codes=["PV06"], version="v5", doc_type="patent_draft")),
        ]
        plan = engine.build_edge_plan(scan_items, existing)

        supersedes = [e for e in plan.edges if e.edge_type == EdgeType.SUPERSEDES]
        covers = [e for e in plan.edges if e.edge_type == EdgeType.COVERS]

        # 2 supersedes: v7->v6, v6->existing-v5
        assert len(supersedes) == 2
        # 3 covers: checklist -> v7, v6, existing-v5
        assert len(covers) == 3

    @pytest.mark.asyncio
    async def test_ei_026_post_ingest_resolves_ids(self):
        """Post-ingest creation resolves file paths to document IDs."""
        plan = EdgePlan(edges=[
            PlannedEdge("f_v7", "f_v6", EdgeType.SUPERSEDES, 1, "version_chain", "v7 > v6"),
        ])
        path_to_id = {"f_v7": "doc-v7", "f_v6": "doc-v6"}

        # Use mock objects for graph_store and graph_ops_service
        class MockGraphOps:
            def __init__(self):
                self.linked = []
            async def link(self, request):
                self.linked.append(request)

        class MockGraphStore:
            def __init__(self):
                self.staged = []
            async def insert_staging_edge(self, edge):
                self.staged.append(edge)

        mock_ops = MockGraphOps()
        mock_store = MockGraphStore()
        result = await resolve_and_execute(plan, path_to_id, mock_store, mock_ops)

        assert result.edges_created == {"supersedes": 1}
        assert len(mock_ops.linked) == 1
        assert mock_ops.linked[0].source_id == "doc-v7"
        assert mock_ops.linked[0].target_id == "doc-v6"

    @pytest.mark.asyncio
    async def test_ei_027_tier_routing(self):
        """Tier 1 via link(), Tier 2 via staging."""
        plan = EdgePlan(edges=[
            PlannedEdge("doc-a", "doc-b", EdgeType.SUPERSEDES, 1, "version_chain", "test"),
            PlannedEdge("doc-c", "doc-d", EdgeType.COVERS, 2, "filename_code_match", "test"),
        ])

        class MockGraphOps:
            def __init__(self):
                self.linked = []
            async def link(self, request):
                self.linked.append(request)

        class MockGraphStore:
            def __init__(self):
                self.staged = []
            async def insert_staging_edge(self, edge):
                self.staged.append(edge)

        mock_ops = MockGraphOps()
        mock_store = MockGraphStore()
        result = await resolve_and_execute(plan, {}, mock_store, mock_ops)

        assert result.edges_created == {"supersedes": 1}
        assert result.edges_staged == {"covers": 1}
        assert len(mock_ops.linked) == 1
        assert len(mock_store.staged) == 1

    @pytest.mark.asyncio
    async def test_ei_028_failed_ingestion_drops_edges(self):
        """Edges involving failed ingestions are dropped."""
        plan = EdgePlan(edges=[
            PlannedEdge("/path/to/failed.docx", "doc-b", EdgeType.SUPERSEDES, 1, "version_chain", "test"),
            PlannedEdge("doc-a", "doc-b", EdgeType.SUPERSEDES, 1, "version_chain", "test"),
        ])
        # Only doc-a and doc-b resolved; /path/to/failed.docx not in map
        path_to_id = {}

        class MockGraphOps:
            def __init__(self):
                self.linked = []
            async def link(self, request):
                self.linked.append(request)

        class MockGraphStore:
            def __init__(self):
                self.staged = []
            async def insert_staging_edge(self, edge):
                self.staged.append(edge)

        mock_ops = MockGraphOps()
        mock_store = MockGraphStore()
        result = await resolve_and_execute(plan, path_to_id, mock_store, mock_ops)

        assert result.edges_dropped == 1
        assert result.edges_created == {"supersedes": 1}

    def test_ei_029_empty_manifest_empty_plan(self):
        """Empty manifest produces empty edge plan."""
        engine = EdgeInferenceEngine()
        plan = engine.build_edge_plan([], [])
        assert plan.edges == []

    def test_ei_030_single_file_no_matches(self):
        """Single file with no matches produces empty edge plan."""
        engine = EdgeInferenceEngine()
        items = [
            InferenceItem("f1", False, ParsedMetadata("Neural-Analysis", codes=["TD"], version="v1", doc_type="technical_disclosure")),
        ]
        plan = engine.build_edge_plan(items, [])
        assert plan.edges == []


# ---------------------------------------------------------------------------
# 6. Scan Endpoint (BE-017 through BE-021)
# ---------------------------------------------------------------------------


@pytest.fixture
async def scan_app(tmp_path):
    """App with a vault and test files for scanning."""
    config = VaultConfig.model_validate(
        _make_vault_config_dict(tmp_path, "pim_health", "PIM Health")
    )
    app = create_app(config=config)
    await _initialize_services(app, config)

    yield app, config

    await asyncio.sleep(0.1)
    for services in app.state.vault_registry.values():
        await services.graph_store.close()


@pytest.fixture
async def scan_client(scan_app):
    app, config = scan_app
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c, config


class TestScanEndpoint:

    async def test_be_017_validates_directory(self, scan_client):
        """POST /app/scan validates directory existence."""
        client, config = scan_client
        resp = await client.post("/app/scan", json={
            "vault_id": "pim_health",
            "directory": "/nonexistent/path",
        })
        assert resp.status_code == 400
        assert "Directory not found" in resp.json()["detail"]

    async def test_be_018_returns_files_with_parsed_metadata(self, scan_client, tmp_path):
        """POST /app/scan returns file list with status and parsed metadata."""
        client, config = scan_client

        # Create test files in a scan directory
        scan_dir = tmp_path / "scan_inbox"
        scan_dir.mkdir()
        (scan_dir / "2026-03-09_PIM_PV06_Claim-Set_v7.md").write_text("# Claim Set v7")
        (scan_dir / "notes.txt").write_text("Just notes")  # no adapter

        resp = await client.post("/app/scan", json={
            "vault_id": "pim_health",
            "directory": str(scan_dir),
        })
        assert resp.status_code == 200
        body = resp.json()
        files = body["files"]

        # Find the .md file
        md_files = [f for f in files if f["file_path"].endswith(".md")]
        assert len(md_files) == 1
        md = md_files[0]
        assert md["adapter"] == "markdown"
        assert md["sage_status"] == "new"
        assert md["parsed_metadata"]["title"] == "Claim-Set"
        assert md["parsed_metadata"]["date"] == "2026-03-09"
        assert "PV06" in md["parsed_metadata"]["codes"]
        assert md["parsed_metadata"]["version"] == "v7"

        # .txt file has no adapter
        txt_files = [f for f in files if f["file_path"].endswith(".txt")]
        assert len(txt_files) == 1
        assert txt_files[0]["adapter"] is None
        assert txt_files[0]["sage_status"] == "no_adapter"

    async def test_be_019_respects_depth_limit(self, scan_client, tmp_path):
        """POST /app/scan respects optional depth limit."""
        client, config = scan_client

        scan_dir = tmp_path / "depth_test"
        scan_dir.mkdir()
        (scan_dir / "top.md").write_text("# Top")
        sub = scan_dir / "sub"
        sub.mkdir()
        (sub / "nested.md").write_text("# Nested")

        # max_depth=0 should only return files in directory itself
        resp = await client.post("/app/scan", json={
            "vault_id": "pim_health",
            "directory": str(scan_dir),
            "max_depth": 0,
        })
        assert resp.status_code == 200
        files = resp.json()["files"]
        paths = [f["file_path"] for f in files]
        assert any("top.md" in p for p in paths)
        assert not any("nested.md" in p for p in paths)

    async def test_be_020_computes_hash(self, scan_client, tmp_path):
        """POST /app/scan computes SHA-256 content hash."""
        client, config = scan_client

        scan_dir = tmp_path / "hash_test"
        scan_dir.mkdir()
        (scan_dir / "doc.md").write_text("# Hash Test")

        resp = await client.post("/app/scan", json={
            "vault_id": "pim_health",
            "directory": str(scan_dir),
        })
        files = resp.json()["files"]
        md_file = [f for f in files if f["file_path"].endswith(".md")][0]
        assert md_file["file_hash"].startswith("sha256:")

    async def test_be_021_permission_warnings(self, scan_client, tmp_path):
        """Scan handles permission errors as warnings."""
        client, config = scan_client

        scan_dir = tmp_path / "perm_test"
        scan_dir.mkdir()
        (scan_dir / "readable.md").write_text("# OK")

        # The warnings array should exist even if empty
        resp = await client.post("/app/scan", json={
            "vault_id": "pim_health",
            "directory": str(scan_dir),
        })
        assert resp.status_code == 200
        assert "warnings" in resp.json()
        assert isinstance(resp.json()["warnings"], list)


# ---------------------------------------------------------------------------
# 7. Batch Ingest with SSE (BE-022 through BE-028, BE-031 through BE-033)
# ---------------------------------------------------------------------------


@pytest.fixture
async def ingest_app(tmp_path):
    """App with vault and test files for ingest."""
    config = VaultConfig.model_validate(
        _make_vault_config_dict(tmp_path, "pim_health", "PIM Health")
    )
    app = create_app(config=config)
    await _initialize_services(app, config)

    yield app, config

    await asyncio.sleep(0.3)
    for services in app.state.vault_registry.values():
        await services.graph_store.close()


@pytest.fixture
async def ingest_client(ingest_app):
    app, config = ingest_app
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c, config


class TestBatchIngest:

    async def test_be_022_sse_content_type(self, ingest_client, tmp_path):
        """POST /app/ingest returns text/event-stream."""
        client, config = ingest_client
        sources = Path(config.vault.storage_root)
        doc = sources / "test.md"
        doc.write_text("# Test\n\nContent.")

        resp = await client.post("/app/ingest", json={
            "vault_id": "pim_health",
            "files": [{"file_path": str(doc), "adapter": "markdown"}],
        })
        assert resp.status_code == 200
        assert "text/event-stream" in resp.headers.get("content-type", "")

    async def test_be_023_sse_event_format(self, ingest_client, tmp_path):
        """SSE events have correct JSON format."""
        client, config = ingest_client
        sources = Path(config.vault.storage_root)
        doc = sources / "event_test.md"
        doc.write_text("# Event Test\n\nContent.")

        resp = await client.post("/app/ingest", json={
            "vault_id": "pim_health",
            "files": [{"file_path": str(doc), "adapter": "markdown"}],
        })
        text = resp.text
        events = [
            json.loads(line.replace("data: ", ""))
            for line in text.strip().split("\n")
            if line.startswith("data: ")
        ]
        assert len(events) >= 2  # at least started + completed or summary

        # Check progress events
        progress = [e for e in events if e["event_type"] == "progress"]
        assert len(progress) >= 1
        for pe in progress:
            assert "file_index" in pe
            assert "total_files" in pe
            assert "filename" in pe
            assert "stage" in pe
            assert "status" in pe

    async def test_be_024_summary_event(self, ingest_client, tmp_path):
        """SSE emits summary event on completion."""
        client, config = ingest_client
        sources = Path(config.vault.storage_root)
        doc = sources / "summary_test.md"
        doc.write_text("# Summary Test\n\nContent.")

        resp = await client.post("/app/ingest", json={
            "vault_id": "pim_health",
            "files": [{"file_path": str(doc), "adapter": "markdown"}],
        })
        events = [
            json.loads(line.replace("data: ", ""))
            for line in resp.text.strip().split("\n")
            if line.startswith("data: ")
        ]
        summaries = [e for e in events if e["event_type"] == "summary"]
        assert len(summaries) == 1
        s = summaries[0]
        assert "documents_created" in s
        assert "edges_created" in s
        assert "edges_staged" in s
        assert "error_count" in s

    async def test_be_025_ingest_with_metadata(self, ingest_client, tmp_path):
        """Ingestion passes metadata dict to SAGE."""
        client, config = ingest_client
        sources = Path(config.vault.storage_root)
        doc = sources / "meta_test.md"
        doc.write_text("# Meta Test\n\nContent.")

        resp = await client.post("/app/ingest", json={
            "vault_id": "pim_health",
            "files": [{
                "file_path": str(doc),
                "adapter": "markdown",
                "parsed_metadata": {
                    "title": "Meta Test",
                    "project": "PIM",
                    "codes": ["PV06"],
                    "version": "v1",
                    "doc_type": "patent_draft",
                },
            }],
        })
        events = [
            json.loads(line.replace("data: ", ""))
            for line in resp.text.strip().split("\n")
            if line.startswith("data: ")
        ]
        # Should have a completed progress event with document_id
        completed = [e for e in events if e.get("status") == "completed"]
        assert len(completed) >= 1
        assert "document_id" in completed[0]

    async def test_be_027_per_file_error_isolation(self, ingest_client, tmp_path):
        """Per-file SAGE errors don't abort the batch."""
        client, config = ingest_client
        sources = Path(config.vault.storage_root)
        good = sources / "good.md"
        good.write_text("# Good\n\nContent.")

        resp = await client.post("/app/ingest", json={
            "vault_id": "pim_health",
            "files": [
                {"file_path": str(good), "adapter": "markdown"},
                {"file_path": "/nonexistent/bad.md", "adapter": "markdown"},
            ],
        })
        events = [
            json.loads(line.replace("data: ", ""))
            for line in resp.text.strip().split("\n")
            if line.startswith("data: ")
        ]
        # Should have events for both files
        progress = [e for e in events if e["event_type"] == "progress"]
        assert len(progress) >= 2  # at least started for each

        # Summary should show error_count
        summary = [e for e in events if e["event_type"] == "summary"][0]
        assert summary["error_count"] >= 1
        assert summary["documents_created"]["new"] >= 1

    async def test_be_028_empty_file_list_rejected(self, ingest_client):
        """Empty file list returns 400."""
        client, config = ingest_client
        resp = await client.post("/app/ingest", json={
            "vault_id": "pim_health",
            "files": [],
        })
        assert resp.status_code == 400

    async def test_be_032_two_phase_edge_inference(self, ingest_client, tmp_path):
        """Ingest runs two-phase edge inference producing edges."""
        client, config = ingest_client
        sources = Path(config.vault.storage_root)

        # Create two versions of the same document
        v1 = sources / "2026-03-09_PIM_PV06_Claim-Set_v1.md"
        v1.write_text("# Claim Set v1\n\nFirst version.")
        v2 = sources / "2026-03-09_PIM_PV06_Claim-Set_v2.md"
        v2.write_text("# Claim Set v2\n\nSecond version.")

        resp = await client.post("/app/ingest", json={
            "vault_id": "pim_health",
            "files": [
                {
                    "file_path": str(v1),
                    "adapter": "markdown",
                    "parsed_metadata": {"title": "Claim-Set", "codes": ["PV06"], "version": "v1", "doc_type": "patent_draft"},
                },
                {
                    "file_path": str(v2),
                    "adapter": "markdown",
                    "parsed_metadata": {"title": "Claim-Set", "codes": ["PV06"], "version": "v2", "doc_type": "patent_draft"},
                },
            ],
        })
        events = [
            json.loads(line.replace("data: ", ""))
            for line in resp.text.strip().split("\n")
            if line.startswith("data: ")
        ]
        summary = [e for e in events if e["event_type"] == "summary"][0]
        # Should have at least one supersedes edge created
        assert summary["edges_created"].get("supersedes", 0) >= 1

    async def test_be_033_summary_edge_counts_by_type(self, ingest_client, tmp_path):
        """Summary event includes edge counts broken down by type."""
        client, config = ingest_client
        sources = Path(config.vault.storage_root)

        # Patent + checklist (code match) + versions (version chain)
        v1 = sources / "patent_v1.md"
        v1.write_text("# Patent v1")
        v2 = sources / "patent_v2.md"
        v2.write_text("# Patent v2")
        chk = sources / "checklist.md"
        chk.write_text("# Checklist")

        resp = await client.post("/app/ingest", json={
            "vault_id": "pim_health",
            "files": [
                {"file_path": str(v1), "adapter": "markdown",
                 "parsed_metadata": {"title": "Patent", "codes": ["PV06"], "version": "v1", "doc_type": "patent_draft"}},
                {"file_path": str(v2), "adapter": "markdown",
                 "parsed_metadata": {"title": "Patent", "codes": ["PV06"], "version": "v2", "doc_type": "patent_draft"}},
                {"file_path": str(chk), "adapter": "markdown",
                 "parsed_metadata": {"title": "Checklist", "codes": ["PV06"], "doc_type": "checklist"}},
            ],
        })
        events = [
            json.loads(line.replace("data: ", ""))
            for line in resp.text.strip().split("\n")
            if line.startswith("data: ")
        ]
        summary = [e for e in events if e["event_type"] == "summary"][0]
        assert "edges_created" in summary
        assert "edges_staged" in summary
        # supersedes (Tier 1) in edges_created, covers (Tier 2) in edges_staged
        assert summary["edges_created"].get("supersedes", 0) >= 1
        assert summary["edges_staged"].get("covers", 0) >= 1
