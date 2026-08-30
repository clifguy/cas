"""Tests for sage/services/batch_inference.py.

Covers the batch-context edge inference service relocated from the app
layer per

  - Version chain inference (EI-013 through EI-018)
  - Filename code match inference (EI-019 through EI-024)
  - Two-phase orchestration (EI-025 through EI-039)
  - plan_batch_edges batch-context entry point (BI-001 through BI-004)
  - Migration boundary guard (BI-005)

The first three classes were relocated verbatim from
``tests/app/test_app_backend.py`` (predecessor location). The
fourth section is new: it covers ``plan_batch_edges``, the new public
entry point that absorbs the vault-querying logic previously in
``BatchIngestService._build_edge_plan``. The fifth section guards
against accidental re-introduction of the app-layer module.
"""

from __future__ import annotations

import importlib
import importlib.util
from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest

from sage.adapters.interfaces import Chunk
from sage.adapters.stubs import StubContentStore
from sage.config import LifecycleTransition, TransitionTable
from sage.models.enums import EdgeType, PipelineStatus, RationaleKind, SourceType
from sage.models.schemas import Document, Edge
from sage.services.batch_inference import (
    EdgeInferenceEngine,
    EdgePlan,
    InferenceItem,
    PlannedEdge,
    plan_batch_edges,
    resolve_and_execute,
)
from sage.services.filename_parser import (
    FilenameParser,
    ParsedMetadata,
    format_version,
    normalize_version,
)


def _table_with(*transitions: tuple[str, str, str]) -> TransitionTable:
    """Build a TransitionTable from (from_state, action, to_state) triples.

    Each supersede test states its own lifecycle contract inline rather
    than borrowing the base vault's. That matters here: under the base
    table (``active --supersede--> archived``) the state literals and the
    table-derived values coincide, so a test using only that table cannot
    tell a hardcoded transition from a derived one.
    """
    return TransitionTable(
        [
            LifecycleTransition(
                from_state=from_state,
                action=action,
                to_state=to_state,
                creates_edge="supersedes" if action == "supersede" else None,
            )
            for from_state, action, to_state in transitions
        ]
    )


# The base vault lifecycle (tests/sage/conftest.py::minimal_vault_config_dict).
BASE_TABLE = _table_with(("active", "supersede", "archived"))


class _SupersedeOps:
    """Records the Tier-1 link requests resolve_and_execute issues."""

    def __init__(self):
        self.linked = []

    async def _create_edge(self, request):
        self.linked.append(request)
        from unittest.mock import MagicMock

        return MagicMock(), True


class _SupersedeStore:
    """Graph store seeded with {document_id: lifecycle_status}.

    Records staging inserts and lifecycle writes, and applies those writes
    to the seeded state, so a test can assert on where documents ended up
    rather than on a count it fixed in advance. An id absent from the seed
    reads back as a missing document.
    """

    def __init__(self, docs: dict[str, str]):
        self.staged = []
        self.updated = []
        self._docs = dict(docs)

    def state_of(self, doc_id: str) -> str | None:
        return self._docs.get(doc_id)

    async def insert_staging_edge(self, edge, on_conflict="raise"):
        self.staged.append(edge)
        return edge, True

    async def get_document(self, doc_id):
        from unittest.mock import MagicMock

        status = self._docs.get(doc_id)
        if status is None:
            return None
        mock_doc = MagicMock()
        mock_doc.lifecycle_status = status
        return mock_doc

    async def update_document(self, doc_id, updates):
        self.updated.append((doc_id, updates))
        if "lifecycle_status" in updates:
            self._docs[doc_id] = updates["lifecycle_status"]
        return None


def _pim_metadata_extraction():
    """Example Portfolio-like metadata extraction config (copied from test_app_backend)."""
    return {
        "filename_extraction": {
            "pattern": "{date}_{project}_{code}_{title}_{version}",
            "separator": "_",
            "project_identifier": "EXAMPLE",
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
                {"keyword": "Session-Context", "doc_type": "session_context"},
                {"keyword": "Template", "doc_type": "template"},
            ],
            "code_to_doc_type": [
                {"code": "REF", "title_contains": "Glossary", "doc_type": "glossary"},
                {
                    "code": "REF",
                    "title_contains": "FormattingStandards",
                    "doc_type": "formatting_standards",
                },
                {
                    "code": "REF",
                    "title_contains": "IntegrationCatalog",
                    "doc_type": "integration_catalog",
                },
                {"code": "REF", "doc_type": "reference_document"},
                {"code": "PVMaster", "doc_type": "design_spec"},
                {"code": "PV", "doc_type": "design_spec"},
                {"code": "TDMaster", "doc_type": "technical_disclosure"},
                {"code": "TD", "doc_type": "technical_disclosure"},
            ],
        },
    }


# ---------------------------------------------------------------------------
# 1. Version Chain Inference (EI-013 through EI-018b)
# Relocated verbatim from tests/app/test_app_backend.py.
# ---------------------------------------------------------------------------


class TestVersionChain:
    def test_ei_013_version_normalization(self):
        """Version normalization to (major, minor, patch) tuple."""
        assert normalize_version("v7") == (7, 0, 0)
        assert normalize_version("v10_2") == (10, 2, 0)
        assert normalize_version("v8_4_1") == (8, 4, 1)
        assert normalize_version("v1.3") == (1, 3, 0)
        assert normalize_version("v12") == (12, 0, 0)
        # Alpha suffixes stripped from version parts
        assert normalize_version("v6a") == (6, 0, 0)
        assert normalize_version("v3_1b") == (3, 1, 0)
        assert normalize_version("v2a.4") == (2, 4, 0)

    def test_format_version_preserves_trailing_zero_patch(self):
        """A 3-component input keeps its patch component even when zero."""
        assert format_version("v8.2.0") == "v8.2.0"
        assert format_version("v9.1.0") == "v9.1.0"
        assert format_version("v3.0.0") == "v3.0.0"
        # Underscore separator and uppercase prefix behave the same.
        assert format_version("v8_2_0") == "v8.2.0"
        assert format_version("V1_0_0") == "v1.0.0"

    def test_format_version_existing_canonical_forms_unchanged(self):
        """Single- and two-component inputs continue to canonicalize as before."""
        assert format_version("v7") == "v7.0"
        assert format_version("v10_2") == "v10.2"
        assert format_version("v8_4_1") == "v8.4.1"
        assert format_version("V3_2") == "v3.2"
        assert format_version("v6a") == "v6.0"
        assert format_version("v3_1b") == "v3.1"

    def test_filename_parser_preserves_trailing_zero_patch_end_to_end(self):
        """Full parse path round-trips a vN.M.0 version without truncation."""
        p = FilenameParser(_pim_metadata_extraction())
        assert p.parse("2026-04-28_EXAMPLE_Doc_v8.2.0").version == "v8.2.0"
        assert p.parse("2026-04-28_EXAMPLE_Doc_v9.1.0").version == "v9.1.0"
        assert p.parse("2026-04-28_EXAMPLE_Doc_v3.0.0").version == "v3.0.0"

    def test_ei_014_linear_chain(self):
        """Linear chain: each version supersedes immediate predecessor."""
        engine = EdgeInferenceEngine()
        items = [
            InferenceItem(
                "f1",
                False,
                ParsedMetadata("Claim-Set", codes=["PV06"], version="v1", doc_type="design_spec"),
            ),
            InferenceItem(
                "f3",
                False,
                ParsedMetadata("Claim-Set", codes=["PV06"], version="v3", doc_type="design_spec"),
            ),
            InferenceItem(
                "f7",
                False,
                ParsedMetadata("Claim-Set", codes=["PV06"], version="v7", doc_type="design_spec"),
            ),
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
        """Version chains scoped to documents sharing title, project, and doc_type."""
        engine = EdgeInferenceEngine()
        items = [
            InferenceItem(
                "a1", False, ParsedMetadata("Claim-Set", version="v1", doc_type="design_spec")
            ),
            InferenceItem(
                "a2", False, ParsedMetadata("Claim-Set", version="v2", doc_type="design_spec")
            ),
            InferenceItem(
                "b1",
                False,
                ParsedMetadata("Neural-Analysis", version="v1", doc_type="technical_disclosure"),
            ),
            InferenceItem(
                "b2",
                False,
                ParsedMetadata("Neural-Analysis", version="v2", doc_type="technical_disclosure"),
            ),
        ]
        plan = engine.build_edge_plan(items, [])
        supersedes = [e for e in plan.edges if e.edge_type == EdgeType.SUPERSEDES]
        assert len(supersedes) == 2
        assert any(e.source_ref == "a2" and e.target_ref == "a1" for e in supersedes)
        assert any(e.source_ref == "b2" and e.target_ref == "b1" for e in supersedes)

    def test_ei_015b_doc_type_mismatch_no_chain(self):
        """Version chain does not cross doc_type boundary."""
        engine = EdgeInferenceEngine()
        items = [
            InferenceItem(
                "p1", False, ParsedMetadata("Claim-Set", version="v1", doc_type="design_spec")
            ),
            InferenceItem(
                "w2", False, ParsedMetadata("Claim-Set", version="v2", doc_type="work_plan")
            ),
            InferenceItem(
                "p3", False, ParsedMetadata("Claim-Set", version="v3", doc_type="design_spec")
            ),
        ]
        plan = engine.build_edge_plan(items, [])
        supersedes = [e for e in plan.edges if e.edge_type == EdgeType.SUPERSEDES]
        assert len(supersedes) == 1
        # Only the two design_spec items chain (v3 supersedes v1)
        assert supersedes[0].source_ref == "p3"
        assert supersedes[0].target_ref == "p1"
        # The work_plan v2 does not participate in any edge
        assert not any("w2" in (e.source_ref, e.target_ref) for e in supersedes)

    def test_ei_016_includes_existing_vault_docs(self):
        """Version chain includes existing vault documents."""
        engine = EdgeInferenceEngine()
        scan_items = [
            InferenceItem(
                "f6", False, ParsedMetadata("Claim-Set", version="v6", doc_type="design_spec")
            ),
            InferenceItem(
                "f7", False, ParsedMetadata("Claim-Set", version="v7", doc_type="design_spec")
            ),
        ]
        existing = [
            InferenceItem(
                "existing-v5",
                True,
                ParsedMetadata("Claim-Set", version="v5", doc_type="design_spec"),
            ),
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
            InferenceItem(
                "f1", False, ParsedMetadata("Claim-Set", version="v1", doc_type="design_spec")
            ),
        ]
        plan = engine.build_edge_plan(items, [])
        supersedes = [e for e in plan.edges if e.edge_type == EdgeType.SUPERSEDES]
        assert len(supersedes) == 0

    def test_ei_018_null_version_is_original(self):
        """Versionless file treated as original, sorts before all versions."""
        engine = EdgeInferenceEngine()
        items = [
            InferenceItem(
                "f0",
                False,
                ParsedMetadata("Neural-Analysis", version=None, doc_type="technical_disclosure"),
            ),
            InferenceItem(
                "f1",
                False,
                ParsedMetadata("Neural-Analysis", version="v1", doc_type="technical_disclosure"),
            ),
            InferenceItem(
                "f2",
                False,
                ParsedMetadata("Neural-Analysis", version="v2", doc_type="technical_disclosure"),
            ),
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
# 2. Filename Code Match Inference (EI-019 through EI-024)
# Relocated verbatim from tests/app/test_app_backend.py.
# ---------------------------------------------------------------------------


class TestFilenameCodeMatch:
    def test_ei_019_workflow_covers_content(self):
        """Workflow artifact covers content artifact sharing a code."""
        engine = EdgeInferenceEngine()
        items = [
            InferenceItem(
                "chk",
                False,
                ParsedMetadata("Checklist", codes=["PV06"], version="v3", doc_type="checklist"),
            ),
            InferenceItem(
                "pat",
                False,
                ParsedMetadata("Claim-Set", codes=["PV06"], version="v7", doc_type="design_spec"),
            ),
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
            InferenceItem(
                "pat", False, ParsedMetadata("Claim-Set", codes=["PV06"], doc_type="design_spec")
            ),
            InferenceItem(
                "chk", False, ParsedMetadata("Checklist", codes=["PV06"], doc_type="checklist")
            ),
        ]
        plan = engine.build_edge_plan(items, [])
        covers = [e for e in plan.edges if e.edge_type == EdgeType.COVERS]
        assert len(covers) == 1
        # Workflow (checklist) is source, content (design_spec) is target
        assert covers[0].source_ref == "chk"
        assert covers[0].target_ref == "pat"

    def test_ei_021_workflow_to_workflow_no_edge(self):
        """Workflow-to-workflow pairs get no automatic edge."""
        engine = EdgeInferenceEngine()
        items = [
            InferenceItem(
                "chk", False, ParsedMetadata("Checklist", codes=["PV06"], doc_type="checklist")
            ),
            InferenceItem(
                "wp", False, ParsedMetadata("Work-Plan", codes=["PV06"], doc_type="work_plan")
            ),
        ]
        plan = engine.build_edge_plan(items, [])
        covers = [e for e in plan.edges if e.edge_type == EdgeType.COVERS]
        assert len(covers) == 0

    def test_ei_022_content_to_content_no_edge(self):
        """Content-to-content pairs get no automatic edge."""
        engine = EdgeInferenceEngine()
        items = [
            InferenceItem(
                "p1", False, ParsedMetadata("Claim-Set", codes=["PV06"], doc_type="design_spec")
            ),
            InferenceItem(
                "p2",
                False,
                ParsedMetadata("Specification", codes=["PV06"], doc_type="design_spec"),
            ),
        ]
        plan = engine.build_edge_plan(items, [])
        covers = [e for e in plan.edges if e.edge_type == EdgeType.COVERS]
        assert len(covers) == 0

    def test_ei_023_match_across_new_and_existing(self):
        """Code match across new files and existing vault documents."""
        engine = EdgeInferenceEngine()
        scan_items = [
            InferenceItem(
                "chk", False, ParsedMetadata("Checklist", codes=["PV06"], doc_type="checklist")
            ),
        ]
        existing = [
            InferenceItem(
                "existing-report",
                True,
                ParsedMetadata("Claim-Set", codes=["PV06"], doc_type="design_spec"),
            ),
        ]
        plan = engine.build_edge_plan(scan_items, existing)
        covers = [e for e in plan.edges if e.edge_type == EdgeType.COVERS]
        assert len(covers) == 1
        assert covers[0].source_ref == "chk"
        assert covers[0].target_ref == "existing-report"

    def test_ei_024_multiple_codes_multiple_edges(self):
        """Multiple codes produce multiple edges."""
        engine = EdgeInferenceEngine()
        items = [
            InferenceItem(
                "chk",
                False,
                ParsedMetadata("Checklist", codes=["PV06", "CF-1"], doc_type="checklist"),
            ),
            InferenceItem(
                "pat", False, ParsedMetadata("Claim-Set", codes=["PV06"], doc_type="design_spec")
            ),
            InferenceItem(
                "ic",
                False,
                ParsedMetadata(
                    "Integration-Catalog", codes=["CF-1"], doc_type="integration_catalog"
                ),
            ),
        ]
        plan = engine.build_edge_plan(items, [])
        covers = [e for e in plan.edges if e.edge_type == EdgeType.COVERS]
        assert len(covers) == 2
        targets = {e.target_ref for e in covers}
        assert targets == {"pat", "ic"}


# ---------------------------------------------------------------------------
# 3. Two-Phase Orchestration (EI-025 through EI-039)
# Relocated verbatim from tests/app/test_app_backend.py.
# ---------------------------------------------------------------------------


class TestTwoPhaseOrchestration:
    def test_ei_025_pre_ingest_edge_plan(self):
        """Pre-ingest analysis builds edge plan from file manifest."""
        engine = EdgeInferenceEngine()
        scan_items = [
            InferenceItem(
                "f_v6",
                False,
                ParsedMetadata("Claim-Set", codes=["PV06"], version="v6", doc_type="design_spec"),
            ),
            InferenceItem(
                "f_v7",
                False,
                ParsedMetadata("Claim-Set", codes=["PV06"], version="v7", doc_type="design_spec"),
            ),
            InferenceItem(
                "f_chk",
                False,
                ParsedMetadata("Checklist", codes=["PV06"], version="v3", doc_type="checklist"),
            ),
        ]
        existing = [
            InferenceItem(
                "existing-v5",
                True,
                ParsedMetadata("Claim-Set", codes=["PV06"], version="v5", doc_type="design_spec"),
            ),
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
        DOC_V7 = "aaaaaaa7_doc_v7"
        DOC_V6 = "aaaaaaa6_doc_v6"
        plan = EdgePlan(
            edges=[
                PlannedEdge("f_v7", "f_v6", EdgeType.SUPERSEDES, 1, "version_chain", "v7 > v6"),
            ]
        )
        path_to_id = {"f_v7": DOC_V7, "f_v6": DOC_V6}

        # Use mock objects for graph_store and graph_ops_service
        class MockGraphOps:
            def __init__(self):
                self.linked = []

            async def _create_edge(self, request):
                self.linked.append(request)
                # Stub returns a placeholder edge and created=True so
                # resolve_and_execute counts it under edges_created.
                from unittest.mock import MagicMock

                return MagicMock(), True

        class MockGraphStore:
            def __init__(self):
                self.staged = []
                self.updated = []

            async def insert_staging_edge(self, edge, on_conflict="raise"):
                self.staged.append(edge)
                return edge, True

            async def get_document(self, doc_id):
                # These tests exercise id resolution and tier routing, not
                # the supersede lifecycle gate: every target is supersedable
                # so the gate lets the planned edges through.
                from unittest.mock import MagicMock

                mock_doc = MagicMock()
                mock_doc.lifecycle_status = "active"
                return mock_doc

            async def update_document(self, doc_id, updates):
                self.updated.append((doc_id, updates))
                return None

        mock_ops = MockGraphOps()
        mock_store = MockGraphStore()
        result = await resolve_and_execute(plan, path_to_id, mock_store, mock_ops, BASE_TABLE)

        assert result.edges_created == {"supersedes": 1}
        assert len(mock_ops.linked) == 1
        assert mock_ops.linked[0].source_id == DOC_V7
        assert mock_ops.linked[0].target_id == DOC_V6

    @pytest.mark.asyncio
    async def test_ei_027_tier_routing(self):
        """Tier 1 via link(), Tier 2 via staging."""
        plan = EdgePlan(
            edges=[
                PlannedEdge(
                    "aaaaaaaa_doc_a",
                    "bbbbbbbb_doc_b",
                    EdgeType.SUPERSEDES,
                    1,
                    "version_chain",
                    "test",
                ),
                PlannedEdge(
                    "cccccccc_doc_c",
                    "dddddddd_doc_d",
                    EdgeType.COVERS,
                    2,
                    "filename_code_match",
                    "test",
                ),
            ]
        )

        class MockGraphOps:
            def __init__(self):
                self.linked = []

            async def _create_edge(self, request):
                self.linked.append(request)
                from unittest.mock import MagicMock

                return MagicMock(), True

        class MockGraphStore:
            def __init__(self):
                self.staged = []
                self.updated = []

            async def insert_staging_edge(self, edge, on_conflict="raise"):
                self.staged.append(edge)
                return edge, True

            async def get_document(self, doc_id):
                # These tests exercise id resolution and tier routing, not
                # the supersede lifecycle gate: every target is supersedable
                # so the gate lets the planned edges through.
                from unittest.mock import MagicMock

                mock_doc = MagicMock()
                mock_doc.lifecycle_status = "active"
                return mock_doc

            async def update_document(self, doc_id, updates):
                self.updated.append((doc_id, updates))
                return None

        mock_ops = MockGraphOps()
        mock_store = MockGraphStore()
        result = await resolve_and_execute(plan, {}, mock_store, mock_ops, BASE_TABLE)

        assert result.edges_created == {"supersedes": 1}
        assert result.edges_staged == {"covers": 1}
        assert len(mock_ops.linked) == 1
        assert len(mock_store.staged) == 1

    @pytest.mark.asyncio
    async def test_ei_028_failed_ingestion_drops_edges(self):
        """Edges involving failed ingestions are dropped."""
        plan = EdgePlan(
            edges=[
                PlannedEdge(
                    "/path/to/failed.docx",
                    "bbbbbbbb_doc_b",
                    EdgeType.SUPERSEDES,
                    1,
                    "version_chain",
                    "test",
                ),
                PlannedEdge(
                    "aaaaaaaa_doc_a",
                    "bbbbbbbb_doc_b",
                    EdgeType.SUPERSEDES,
                    1,
                    "version_chain",
                    "test",
                ),
            ]
        )
        # Only doc-a and doc-b resolved; /path/to/failed.docx not in map
        path_to_id = {}

        class MockGraphOps:
            def __init__(self):
                self.linked = []

            async def _create_edge(self, request):
                self.linked.append(request)
                from unittest.mock import MagicMock

                return MagicMock(), True

        class MockGraphStore:
            def __init__(self):
                self.staged = []
                self.updated = []

            async def insert_staging_edge(self, edge, on_conflict="raise"):
                self.staged.append(edge)
                return edge, True

            async def get_document(self, doc_id):
                # These tests exercise id resolution and tier routing, not
                # the supersede lifecycle gate: every target is supersedable
                # so the gate lets the planned edges through.
                from unittest.mock import MagicMock

                mock_doc = MagicMock()
                mock_doc.lifecycle_status = "active"
                return mock_doc

            async def update_document(self, doc_id, updates):
                self.updated.append((doc_id, updates))
                return None

        mock_ops = MockGraphOps()
        mock_store = MockGraphStore()
        result = await resolve_and_execute(plan, path_to_id, mock_store, mock_ops, BASE_TABLE)

        assert result.edges_dropped == 1
        assert result.edges_created == {"supersedes": 1}

    @pytest.mark.asyncio
    async def test_ei_031_supersedes_transitions_target_to_archived(self):
        """Supersedes edge transitions target document to 'archived'."""
        DOC_V2 = "aaaaaaa2_doc_v2"
        DOC_V1 = "aaaaaaa1_doc_v1"
        plan = EdgePlan(
            edges=[
                PlannedEdge(DOC_V2, DOC_V1, EdgeType.SUPERSEDES, 1, "version_chain", "v2 > v1"),
            ]
        )

        mock_ops = _SupersedeOps()
        mock_store = _SupersedeStore({DOC_V1: "active"})
        result = await resolve_and_execute(plan, {}, mock_store, mock_ops, BASE_TABLE)

        assert result.edges_created == {"supersedes": 1}
        # Target document must have been transitioned to "archived"
        assert len(mock_store.updated) == 1
        updated_id, updates = mock_store.updated[0]
        assert updated_id == DOC_V1
        assert updates["lifecycle_status"] == "archived"
        assert "updated_at" in updates

    @pytest.mark.asyncio
    async def test_ei_032_supersede_gated_when_target_state_forbids_it(self):
        """A target whose state forbids supersede yields no edge and a warning.

        The edge and the transition are two halves of one supersession. The
        gate runs before the write, so a forbidden target produces neither
        half -- asserted on ``linked`` as well as ``edges_created``, since
        a gate that ran after the write would leave the edge behind while
        still reporting the warning.

        The target is ``completed``: a state the table neither permits
        supersede from nor lands a supersession in, so it is distinct from
        the already-superseded case covered by EI-038.
        """
        DOC_V3 = "aaaaaaa3_doc_v3"
        DOC_V2 = "aaaaaaa2_doc_v2"
        plan = EdgePlan(
            edges=[
                PlannedEdge(DOC_V3, DOC_V2, EdgeType.SUPERSEDES, 1, "version_chain", "v3 > v2"),
            ]
        )

        mock_ops = _SupersedeOps()
        mock_store = _SupersedeStore({DOC_V2: "completed"})
        result = await resolve_and_execute(plan, {}, mock_store, mock_ops, BASE_TABLE)

        assert result.edges_created == {}
        assert mock_ops.linked == []
        assert result.edges_dropped == 1
        assert len(mock_store.updated) == 0

        assert len(result.warnings) == 1
        warning = result.warnings[0]
        # IngestSummary.edge_warnings is typed dict[str, str]; the gated
        # entry must carry the same keys as every other warning reason.
        assert set(warning) == {"source", "target", "edge_type", "reason", "detail"}
        assert all(isinstance(value, str) for value in warning.values())
        assert warning["reason"] == "supersede_target_not_transitionable"
        assert warning["source"] == DOC_V3
        assert warning["target"] == DOC_V2
        # The detail names the observed state and the permitted ones, both
        # read off the table rather than restated.
        assert "completed" in warning["detail"]
        assert "active" in warning["detail"]

    @pytest.mark.asyncio
    async def test_ei_034_supersede_target_state_comes_from_the_table(self):
        """The state a superseded target lands in is the table's to_state.

        The table here declares a to_state that is not ``archived``, so a
        hardcoded landing state fails rather than coinciding with the
        derived one as it does under the base lifecycle.
        """
        DOC_V2 = "aaaaaaa2_doc_v2"
        DOC_V1 = "aaaaaaa1_doc_v1"
        plan = EdgePlan(
            edges=[
                PlannedEdge(DOC_V2, DOC_V1, EdgeType.SUPERSEDES, 1, "version_chain", "v2 > v1"),
            ]
        )

        mock_ops = _SupersedeOps()
        mock_store = _SupersedeStore({DOC_V1: "active"})
        result = await resolve_and_execute(
            plan,
            {},
            mock_store,
            mock_ops,
            _table_with(("active", "supersede", "retired")),
        )

        assert result.edges_created == {"supersedes": 1}
        assert len(mock_store.updated) == 1
        updated_id, updates = mock_store.updated[0]
        assert updated_id == DOC_V1
        assert updates["lifecycle_status"] == "retired"
        assert "updated_at" in updates

    @pytest.mark.asyncio
    async def test_ei_035_supersede_permitted_from_a_configured_non_active_state(self):
        """A vault may declare supersede from a state other than 'active'.

        The permissive direction of the same property: a target in a state
        the table declares is superseded, where a hardcoded ``active``
        precondition would skip it.
        """
        DOC_V2 = "aaaaaaa2_doc_v2"
        DOC_V1 = "aaaaaaa1_doc_v1"
        plan = EdgePlan(
            edges=[
                PlannedEdge(DOC_V2, DOC_V1, EdgeType.SUPERSEDES, 1, "version_chain", "v2 > v1"),
            ]
        )

        mock_ops = _SupersedeOps()
        mock_store = _SupersedeStore({DOC_V1: "completed"})
        result = await resolve_and_execute(
            plan,
            {},
            mock_store,
            mock_ops,
            _table_with(
                ("active", "supersede", "archived"),
                ("completed", "supersede", "archived"),
            ),
        )

        assert result.edges_created == {"supersedes": 1}
        assert len(mock_store.updated) == 1
        updated_id, updates = mock_store.updated[0]
        assert updated_id == DOC_V1
        assert updates["lifecycle_status"] == "archived"

    @pytest.mark.asyncio
    async def test_ei_036_no_supersedes_edge_outlives_an_unsuperseded_target(self):
        """Every supersedes edge written leaves its target in a superseded state.

        The invariant the gate exists to hold, over a mixed batch: a
        permitted supersede, a target already superseded, a forbidden one,
        and an unrelated Tier-2 edge. Asserted over the final states of the
        targets actually linked -- derived from what the run did rather
        than from counts fixed in advance, so it keeps its meaning if the
        batch composition changes, and so it covers the transitioned and
        already-transitioned cases with one statement.
        """
        DOC_V2 = "aaaaaaa2_doc_v2"
        DOC_V1 = "aaaaaaa1_doc_v1"
        DOC_B2 = "bbbbbbb2_doc_b2"
        DOC_B1 = "bbbbbbb1_doc_b1"
        DOC_E2 = "eeeeeee2_doc_e2"
        DOC_E1 = "eeeeeee1_doc_e1"
        DOC_C = "cccccccc_doc_c"
        DOC_D = "dddddddd_doc_d"
        plan = EdgePlan(
            edges=[
                PlannedEdge(DOC_V2, DOC_V1, EdgeType.SUPERSEDES, 1, "version_chain", "v2 > v1"),
                PlannedEdge(DOC_E2, DOC_E1, EdgeType.SUPERSEDES, 1, "version_chain", "e2 > e1"),
                PlannedEdge(DOC_B2, DOC_B1, EdgeType.SUPERSEDES, 1, "version_chain", "b2 > b1"),
                PlannedEdge(DOC_C, DOC_D, EdgeType.COVERS, 2, "filename_code_match", "c covers d"),
            ]
        )

        mock_ops = _SupersedeOps()
        mock_store = _SupersedeStore(
            {
                DOC_V1: "active",  # permitted: transitions
                DOC_E1: "archived",  # already superseded: no write needed
                DOC_B1: "completed",  # forbidden: gated
            }
        )
        result = await resolve_and_execute(plan, {}, mock_store, mock_ops, BASE_TABLE)

        superseded = {
            request.target_id
            for request in mock_ops.linked
            if request.edge_type == EdgeType.SUPERSEDES
        }
        stranded = {target for target in superseded if mock_store.state_of(target) != "archived"}
        assert not stranded, f"supersedes edges point at unsuperseded targets: {stranded}"
        # Both permitted shapes really did land; the forbidden one did not.
        assert superseded == {DOC_V1, DOC_E1}

        # The gate is scoped to Tier-1 supersedes; Tier-2 is untouched.
        assert result.edges_staged == {"covers": 1}
        assert result.edges_dropped == 1

    @pytest.mark.asyncio
    async def test_ei_040_supersede_target_read_failure_has_its_own_reason(self):
        """A failed target read refuses the edge under a distinct reason.

        Separate from ``lifecycle_transition_failed``, which means the edge
        landed and only the transition failed. Here no edge exists at all,
        so a caller triaging warnings must be able to tell the two apart.
        """
        DOC_V2 = "aaaaaaa2_doc_v2"
        DOC_V1 = "aaaaaaa1_doc_v1"
        plan = EdgePlan(
            edges=[
                PlannedEdge(DOC_V2, DOC_V1, EdgeType.SUPERSEDES, 1, "version_chain", "v2 > v1"),
            ]
        )

        class _ExplodingStore(_SupersedeStore):
            async def get_document(self, doc_id):
                raise RuntimeError("storage unavailable")

        mock_ops = _SupersedeOps()
        mock_store = _ExplodingStore({DOC_V1: "active"})
        result = await resolve_and_execute(plan, {}, mock_store, mock_ops, BASE_TABLE)

        assert result.edges_created == {}
        assert mock_ops.linked == []
        assert result.edges_dropped == 1
        assert len(result.warnings) == 1
        assert result.warnings[0]["reason"] == "supersede_target_read_failed"
        assert "storage unavailable" in result.warnings[0]["detail"]

    @pytest.mark.asyncio
    async def test_ei_038_supersede_of_an_already_superseded_target_needs_no_write(self):
        """A target already in a supersede landing state is linked, not gated.

        Chain repair reaches this routinely: inserting an intermediate
        version re-points a supersedes edge at a predecessor the
        supersession being replaced already archived. The edge is sound --
        the predecessor holds the state the edge asserts of it -- so it is
        created, with no lifecycle write and no warning.
        """
        DOC_V2 = "aaaaaaa2_doc_v2"
        DOC_V1 = "aaaaaaa1_doc_v1"
        plan = EdgePlan(
            edges=[
                PlannedEdge(DOC_V2, DOC_V1, EdgeType.SUPERSEDES, 1, "version_chain", "v2 > v1"),
            ]
        )

        mock_ops = _SupersedeOps()
        mock_store = _SupersedeStore({DOC_V1: "archived"})
        result = await resolve_and_execute(plan, {}, mock_store, mock_ops, BASE_TABLE)

        assert result.edges_created == {"supersedes": 1}
        assert result.edges_dropped == 0
        assert mock_store.updated == []
        assert result.warnings == []

    @pytest.mark.asyncio
    async def test_ei_039_landing_state_recognition_is_table_derived(self):
        """The 'already superseded' state is read off the table, not assumed.

        Same shape as EI-038 against a table that lands supersessions in
        ``retired``: a target already ``retired`` is linked without a
        write, while ``archived`` -- inert under this table -- is gated.
        """
        DOC_V2 = "aaaaaaa2_doc_v2"
        DOC_V1 = "aaaaaaa1_doc_v1"
        DOC_B2 = "bbbbbbb2_doc_b2"
        DOC_B1 = "bbbbbbb1_doc_b1"
        plan = EdgePlan(
            edges=[
                PlannedEdge(DOC_V2, DOC_V1, EdgeType.SUPERSEDES, 1, "version_chain", "v2 > v1"),
                PlannedEdge(DOC_B2, DOC_B1, EdgeType.SUPERSEDES, 1, "version_chain", "b2 > b1"),
            ]
        )

        mock_ops = _SupersedeOps()
        mock_store = _SupersedeStore({DOC_V1: "retired", DOC_B1: "archived"})
        result = await resolve_and_execute(
            plan,
            {},
            mock_store,
            mock_ops,
            _table_with(("active", "supersede", "retired")),
        )

        assert result.edges_created == {"supersedes": 1}
        assert [request.target_id for request in mock_ops.linked] == [DOC_V1]
        assert mock_store.updated == []
        assert result.edges_dropped == 1
        assert result.warnings[0]["target"] == DOC_B1

    @pytest.mark.asyncio
    async def test_ei_037_supersede_dropped_when_target_document_is_missing(self):
        """An absent target has no state to check, so no edge is created."""
        DOC_V2 = "aaaaaaa2_doc_v2"
        DOC_V1 = "aaaaaaa1_doc_v1"
        plan = EdgePlan(
            edges=[
                PlannedEdge(DOC_V2, DOC_V1, EdgeType.SUPERSEDES, 1, "version_chain", "v2 > v1"),
            ]
        )

        mock_ops = _SupersedeOps()
        mock_store = _SupersedeStore({})  # DOC_V1 never ingested
        result = await resolve_and_execute(plan, {}, mock_store, mock_ops, BASE_TABLE)

        assert result.edges_created == {}
        assert mock_ops.linked == []
        assert result.edges_dropped == 1
        assert len(mock_store.updated) == 0
        assert len(result.warnings) == 1
        # Its own reason, not the not-transitionable one: an absent document
        # has no state to report against the table's permitted set.
        assert result.warnings[0]["reason"] == "supersede_target_missing"
        assert DOC_V1 in result.warnings[0]["detail"]

    @pytest.mark.asyncio
    async def test_ei_041_supersede_syncs_chunk_lifecycle_to_landing_state(self):
        """The lifecycle write carries its chunk-store sync.

        Pre-filter pushdown reads lifecycle_status off the chunk row, so a
        supersession that moves only the document leaves the predecessor's
        chunks answering active-filtered searches indefinitely. The sync
        mirrors what the explicit lifecycle path does after its own write.

        Runs against a table landing supersessions in ``retired`` rather
        than the base table's ``archived`` (see ``_table_with``): a sync
        that hardcoded the base landing state instead of mirroring the
        settled one would pass under the base table and fails here.
        """
        DOC_V2 = "aaaaaaa2_doc_v2"
        DOC_V1 = "aaaaaaa1_doc_v1"
        plan = EdgePlan(
            edges=[
                PlannedEdge(DOC_V2, DOC_V1, EdgeType.SUPERSEDES, 1, "version_chain", "v2 > v1"),
            ]
        )

        content_store = StubContentStore()
        await content_store.index_chunks(
            DOC_V1,
            [
                Chunk(
                    document_id=DOC_V1,
                    heading_path="Claims",
                    content="claim set details",
                    chunk_index=0,
                    lifecycle_status="active",
                ),
                Chunk(
                    document_id=DOC_V1,
                    heading_path="Notes",
                    content="claim revision notes",
                    chunk_index=1,
                    lifecycle_status="active",
                ),
            ],
        )
        # Positive control: the chunks are present and answer an
        # active-filtered search before the supersession runs, so the
        # post-run emptiness below can only come from the sync.
        before = await content_store.search_bm25("claim", filters={"lifecycle_status": "active"})
        assert {r.document_id for r in before} == {DOC_V1}

        mock_ops = _SupersedeOps()
        mock_store = _SupersedeStore({DOC_V1: "active"})
        result = await resolve_and_execute(
            plan,
            {},
            mock_store,
            mock_ops,
            _table_with(("active", "supersede", "retired")),
            content_store=content_store,
        )

        assert result.edges_created == {"supersedes": 1}
        assert mock_store.state_of(DOC_V1) == "retired"
        assert result.warnings == []
        # Every chunk moved to the landing state the table names -- not the
        # base table's, so a hardcoded state cannot coincide with it.
        chunks = await content_store.get_chunks_by_heading_prefix(DOC_V1, "Claims")
        assert all(c.lifecycle_status == "retired" for c in chunks)
        after = await content_store.search_bm25("claim", filters={"lifecycle_status": "active"})
        assert after == []

    @pytest.mark.asyncio
    async def test_ei_042_chunk_sync_failure_warns_and_never_raises(self):
        """A failed chunk sync is a warning, not a batch failure.

        The edge landed and the document moved; only the mirror into the
        content store failed. That is triage information, not grounds to
        abort the batch -- the same best-effort contract the lifecycle
        write itself holds under ``lifecycle_transition_failed``.
        """
        DOC_V2 = "aaaaaaa2_doc_v2"
        DOC_V1 = "aaaaaaa1_doc_v1"
        plan = EdgePlan(
            edges=[
                PlannedEdge(DOC_V2, DOC_V1, EdgeType.SUPERSEDES, 1, "version_chain", "v2 > v1"),
            ]
        )

        class _ExplodingContentStore(StubContentStore):
            async def update_chunk_metadata(self, document_id, metadata):
                raise RuntimeError("chunk store unavailable")

        mock_ops = _SupersedeOps()
        mock_store = _SupersedeStore({DOC_V1: "active"})
        result = await resolve_and_execute(
            plan, {}, mock_store, mock_ops, BASE_TABLE, content_store=_ExplodingContentStore()
        )

        # Edge and document write both succeeded and still count.
        assert result.edges_created == {"supersedes": 1}
        assert mock_store.state_of(DOC_V1) == "archived"
        assert len(result.warnings) == 1
        assert result.warnings[0]["reason"] == "chunk_lifecycle_sync_failed"
        assert result.warnings[0]["source"] == DOC_V2
        assert result.warnings[0]["target"] == DOC_V1
        assert "chunk store unavailable" in result.warnings[0]["detail"]

    @pytest.mark.asyncio
    async def test_ei_043_no_chunk_sync_when_lifecycle_write_fails(self):
        """No sync when the document write fails.

        The sync mirrors a transition that happened. If the document did
        not move, pushing the landing state onto its chunks would create
        the very divergence the sync exists to prevent, in the opposite
        direction.
        """
        DOC_V2 = "aaaaaaa2_doc_v2"
        DOC_V1 = "aaaaaaa1_doc_v1"
        plan = EdgePlan(
            edges=[
                PlannedEdge(DOC_V2, DOC_V1, EdgeType.SUPERSEDES, 1, "version_chain", "v2 > v1"),
            ]
        )

        class _WriteFailingStore(_SupersedeStore):
            async def update_document(self, doc_id, updates):
                raise RuntimeError("document write failed")

        class _RecordingContentStore(StubContentStore):
            def __init__(self):
                super().__init__()
                self.sync_calls = []

            async def update_chunk_metadata(self, document_id, metadata):
                self.sync_calls.append((document_id, metadata))

        content_store = _RecordingContentStore()
        mock_ops = _SupersedeOps()
        mock_store = _WriteFailingStore({DOC_V1: "active"})
        result = await resolve_and_execute(
            plan, {}, mock_store, mock_ops, BASE_TABLE, content_store=content_store
        )

        assert len(result.warnings) == 1
        assert result.warnings[0]["reason"] == "lifecycle_transition_failed"
        assert content_store.sync_calls == []

    @pytest.mark.asyncio
    async def test_ei_044_no_chunk_sync_on_landing_state_no_write(self):
        """The chain-repair no-write case performs no sync either.

        A target already in a landing state gets the edge and no document
        write (EI-038); there is no transition for the chunks to mirror,
        so the content store is not touched.
        """
        DOC_V2 = "aaaaaaa2_doc_v2"
        DOC_V1 = "aaaaaaa1_doc_v1"
        plan = EdgePlan(
            edges=[
                PlannedEdge(DOC_V2, DOC_V1, EdgeType.SUPERSEDES, 1, "version_chain", "v2 > v1"),
            ]
        )

        class _RecordingContentStore(StubContentStore):
            def __init__(self):
                super().__init__()
                self.sync_calls = []

            async def update_chunk_metadata(self, document_id, metadata):
                self.sync_calls.append((document_id, metadata))

        content_store = _RecordingContentStore()
        mock_ops = _SupersedeOps()
        mock_store = _SupersedeStore({DOC_V1: "archived"})
        result = await resolve_and_execute(
            plan, {}, mock_store, mock_ops, BASE_TABLE, content_store=content_store
        )

        assert result.edges_created == {"supersedes": 1}
        assert mock_store.updated == []
        assert result.warnings == []
        assert content_store.sync_calls == []

    def test_ei_029_empty_manifest_empty_plan(self):
        """Empty manifest produces empty edge plan."""
        engine = EdgeInferenceEngine()
        plan = engine.build_edge_plan([], [])
        assert plan.edges == []

    def test_ei_030_single_file_no_matches(self):
        """Single file with no matches produces empty edge plan."""
        engine = EdgeInferenceEngine()
        items = [
            InferenceItem(
                "f1",
                False,
                ParsedMetadata(
                    "Neural-Analysis", codes=["TD"], version="v1", doc_type="technical_disclosure"
                ),
            ),
        ]
        plan = engine.build_edge_plan(items, [])
        assert plan.edges == []


# ---------------------------------------------------------------------------
# 4. plan_batch_edges (BI-001 through BI-004)
# New tests covering the batch-context entry point introduced by.
# ---------------------------------------------------------------------------


def _doc_id(short: str) -> str:
    """Build a shape-conformant document id from a short test handle.

    The DocumentIdStr validator requires ``^[0-9a-f]{8}_[a-z0-9_]+$``; the
    test helpers in tests/sage/test_graph_ops.py hash the short name to
    produce the 8-char prefix. We inline a deterministic version here so
    the assertions can address documents by readable handle while still
    constructing valid Document instances.
    """
    import hashlib

    prefix = hashlib.sha256(short.encode()).hexdigest()[:8]
    return f"{prefix}_{short}"


def _make_doc(
    short: str,
    *,
    title: str,
    project: str | None,
    doc_type: str | None,
    version_label: str | None,
    tags: list[str] | None = None,
    lifecycle_status: str = "active",
    pipeline_status: PipelineStatus = PipelineStatus.ABSTRACTION_COMPLETE,
) -> Document:
    """Construct a Document with the fields plan_batch_edges reads off."""
    import hashlib

    doc_id = _doc_id(short)
    now = datetime.now(timezone.utc)
    return Document(
        id=doc_id,
        title=title,
        source_type=SourceType.MARKDOWN,
        source_path=f"test/{short}.md",
        lifecycle_status=lifecycle_status,
        version_label=version_label,
        project=project,
        tags=tags or [],
        doc_type=doc_type,
        source_content_hash="sha256:" + hashlib.sha256(f"plan-batch-{short}".encode()).hexdigest(),
        adapter_version="0.1.0",
        created_by="testuser",
        created_at=now,
        last_modified_by="testuser",
        updated_at=now,
        projected_at=now,
        pipeline_status=pipeline_status,
    )


def _make_supersedes_edge(
    *,
    source_id: str,
    target_id: str,
    rationale_kind: RationaleKind = RationaleKind.VERSION_CHAIN,
) -> Edge:
    """Construct a supersedes Edge ready for graph_store.insert_edge."""
    return Edge(
        id=str(uuid4()),
        source_id=source_id,
        target_id=target_id,
        edge_type=EdgeType.SUPERSEDES,
        created_at=datetime.now(timezone.utc),
        rationale="seeded by test",
        rationale_kind=rationale_kind,
    )


class _RecordingGraphStore:
    """Thin proxy that records the method calls plan_batch_edges makes.

    Wraps the real ``graph_store`` fixture so the underlying SQLite-backed
    queries return the same data; the proxy only adds visibility into
    which methods plan_batch_edges actually invokes (used by the BI-003
    and BI-004 spy tests).
    """

    def __init__(self, inner):
        self._inner = inner
        self.query_documents_calls: list[dict] = []
        self.get_edges_by_source_calls: list[tuple[str, str | None]] = []

    async def query_documents(
        self,
        *,
        filters=None,
        limit=None,
        offset=None,
        default_exclude_failed: bool = True,
    ):
        self.query_documents_calls.append(
            {
                "filters": filters,
                "limit": limit,
                "offset": offset,
                "default_exclude_failed": default_exclude_failed,
            }
        )
        return await self._inner.query_documents(
            filters=filters,
            limit=limit,
            offset=offset,
            default_exclude_failed=default_exclude_failed,
        )

    async def get_edges_by_source(self, source_id, edge_type=None):
        self.get_edges_by_source_calls.append((source_id, edge_type))
        return await self._inner.get_edges_by_source(source_id, edge_type)


@pytest.mark.asyncio
async def test_bi_001_plan_batch_edges_active_only(graph_store):
    """Active-only vault: planner returns chain edges including the active head.

    Precondition: vault has one v5 active claim-set; scan batch arrives
    with v6 and v7 of the same chain identity. The planner must produce
    the same EdgePlan shape as the engine would when handed the same
    InferenceItems directly.
    """
    v5 = _make_doc(
        "claim_set_v5",
        title="Claim-Set",
        project="PV06",
        doc_type="design_spec",
        version_label="v5",
        tags=["PV06"],
        lifecycle_status="active",
    )
    await graph_store.insert_document(v5)

    scan_items = [
        InferenceItem(
            "/scan/f6",
            False,
            ParsedMetadata(
                "Claim-Set",
                codes=["PV06"],
                version="v6",
                doc_type="design_spec",
                project="PV06",
            ),
        ),
        InferenceItem(
            "/scan/f7",
            False,
            ParsedMetadata(
                "Claim-Set",
                codes=["PV06"],
                version="v7",
                doc_type="design_spec",
                project="PV06",
            ),
        ),
    ]
    vault_services = SimpleNamespace(graph_store=graph_store)

    plan = await plan_batch_edges(scan_items=scan_items, vault_services=vault_services)

    supersedes = [e for e in plan.edges if e.edge_type == EdgeType.SUPERSEDES]
    assert len(supersedes) == 2
    # v7 supersedes v6 (both new)
    assert any(e.source_ref == "/scan/f7" and e.target_ref == "/scan/f6" for e in supersedes)
    # v6 supersedes existing v5 (file_path -> existing doc id)
    assert any(e.source_ref == "/scan/f6" and e.target_ref == v5.id for e in supersedes)


@pytest.mark.asyncio
async def test_bi_002_plan_batch_edges_includes_archived_chain_predecessors(graph_store):
    """Archived predecessors reachable via (project, doc_type) targeted query.

    Precondition: vault has v1 archived and v2 active for the same chain
    identity. Scan batch has v3. The targeted chain-candidate query (Pass
    B) must surface v1 (it is not in the ``lifecycle_status=active`` Pass
    A result), so the engine sees the full v1 < v2 chain when planning v3.

    Verifies the optimization in [ingest_service.py:281-304] -- the
    targeted query catches archived predecessors that Pass A misses.
    """
    v1 = _make_doc(
        "claim_set_v1",
        title="Claim-Set",
        project="PV06",
        doc_type="design_spec",
        version_label="v1",
        tags=["PV06"],
        lifecycle_status="archived",
    )
    v2 = _make_doc(
        "claim_set_v2",
        title="Claim-Set",
        project="PV06",
        doc_type="design_spec",
        version_label="v2",
        tags=["PV06"],
        lifecycle_status="active",
    )
    await graph_store.insert_document(v1)
    await graph_store.insert_document(v2)
    # Seed the existing v2 -> v1 supersedes edge so the chain is already
    # well-formed; the planner should NOT propose a removal or a duplicate
    # add for that segment.
    seeded = _make_supersedes_edge(source_id=v2.id, target_id=v1.id)
    await graph_store.insert_edge(seeded)

    scan_items = [
        InferenceItem(
            "/scan/f3",
            False,
            ParsedMetadata(
                "Claim-Set",
                codes=["PV06"],
                version="v3",
                doc_type="design_spec",
                project="PV06",
            ),
        ),
    ]
    vault_services = SimpleNamespace(graph_store=graph_store)

    plan = await plan_batch_edges(scan_items=scan_items, vault_services=vault_services)

    supersedes = [e for e in plan.edges if e.edge_type == EdgeType.SUPERSEDES]
    # Only v3 -> v2 should be planned (new). v2 -> v1 already exists and
    # is preserved.
    assert len(supersedes) == 1
    edge = supersedes[0]
    assert edge.source_ref == "/scan/f3"
    assert edge.target_ref == v2.id
    # No removals: chain is in correct shape after adding v3 -> v2.
    assert plan.removals == []


@pytest.mark.asyncio
async def test_bi_003_plan_batch_edges_fetches_existing_supersedes_edges(graph_store):
    """The planner calls get_edges_by_source for each chain-scope member.

    Spy on the graph store; confirm that ``get_edges_by_source(doc_id,
    "supersedes")`` is invoked for the active vault docs that share the
    chain identity of any scan-batch arrival. This is the seam the
    provenance gate (CAS-ADR-019) depends on -- without these edges the
    diff cannot detect that a manual edge is about to be replaced.
    """
    v5 = _make_doc(
        "claim_set_v5_spy",
        title="Claim-Set-Spy",
        project="PV06",
        doc_type="design_spec",
        version_label="v5",
        tags=["PV06"],
        lifecycle_status="active",
    )
    await graph_store.insert_document(v5)

    scan_items = [
        InferenceItem(
            "/scan/spy_v6",
            False,
            ParsedMetadata(
                "Claim-Set-Spy",
                codes=["PV06"],
                version="v6",
                doc_type="design_spec",
                project="PV06",
            ),
        ),
    ]
    recording = _RecordingGraphStore(graph_store)
    vault_services = SimpleNamespace(graph_store=recording)

    await plan_batch_edges(scan_items=scan_items, vault_services=vault_services)

    # The existing v5 doc lands in chain scope (matches the scan batch's
    # chain identity), so plan_batch_edges must have queried its edges.
    called_ids = {source for source, _ in recording.get_edges_by_source_calls}
    assert v5.id in called_ids
    # Each call asks for the supersedes edge type specifically.
    assert all(edge_type == "supersedes" for _, edge_type in recording.get_edges_by_source_calls)


@pytest.mark.asyncio
async def test_bi_004_plan_batch_edges_no_versioned_items_skips_chain_candidate_query(
    graph_store,
):
    """Optimization: no versioned scan items => no per-(project, doc_type) Pass B.

    Preserves the optimization in [ingest_service.py:281-304]: when every
    scan item lacks a ``version``, ``chain_dim_pairs`` is empty and only
    the Pass-A active-docs query fires. A regression here would cause
    unnecessary catalog queries on every code-match-only batch.
    """
    # Seed one unrelated active doc so the active-docs Pass A has at least
    # one row to return; the test is about whether Pass B fires, not the
    # contents of Pass A.
    seed = _make_doc(
        "unrelated_seed",
        title="Some-Note",
        project=None,
        doc_type="note",
        version_label=None,
        tags=[],
        lifecycle_status="active",
    )
    await graph_store.insert_document(seed)

    scan_items = [
        InferenceItem(
            "/scan/note1",
            False,
            ParsedMetadata("Note-One", version=None, doc_type="note"),
        ),
        InferenceItem(
            "/scan/note2",
            False,
            ParsedMetadata("Note-Two", version=None, doc_type="note"),
        ),
    ]
    recording = _RecordingGraphStore(graph_store)
    vault_services = SimpleNamespace(graph_store=recording)

    await plan_batch_edges(scan_items=scan_items, vault_services=vault_services)

    # Pass A: active-docs query (filters={"lifecycle_status": "active"}).
    # Pass B: would be per-(project, doc_type) queries. With no versioned
    # scan items, Pass B should fire zero times. Total queries == 1.
    assert len(recording.query_documents_calls) == 1
    assert recording.query_documents_calls[0]["filters"] == {"lifecycle_status": "active"}


@pytest.mark.asyncio
async def test_bi_006_plan_batch_edges_includes_failed_active_predecessor(graph_store):
    """A failed+active predecessor must surface as a chain candidate.

    The chain-identity fields are set at ingest time before abstraction,
    so a pipeline_status=FAILED document still carries valid chain
    identity. Pre-fix, both query_documents passes inherit the BH-020
    default-exclude and silently drop the failed predecessor; the engine
    then writes the new arrival's supersedes edge to the wrong target.

    Precondition: vault has v1 (active, abstraction_complete) and v2
    (active, FAILED) sharing chain identity, plus the existing v2 -> v1
    supersedes edge. Scan batch has v3.

    Expected: plan.edges contains exactly one new supersedes edge from
    v3 to *v2* (not v1). plan.removals stays empty.
    """
    v1 = _make_doc(
        "claim_set_v1_t0150",
        title="Claim-Set-T0150",
        project="PV06",
        doc_type="design_spec",
        version_label="v1",
        tags=["PV06"],
        lifecycle_status="active",
    )
    v2 = _make_doc(
        "claim_set_v2_t0150",
        title="Claim-Set-T0150",
        project="PV06",
        doc_type="design_spec",
        version_label="v2",
        tags=["PV06"],
        lifecycle_status="active",
        pipeline_status=PipelineStatus.FAILED,
    )
    await graph_store.insert_document(v1)
    await graph_store.insert_document(v2)
    seeded = _make_supersedes_edge(source_id=v2.id, target_id=v1.id)
    await graph_store.insert_edge(seeded)

    scan_items = [
        InferenceItem(
            "/scan/f3",
            False,
            ParsedMetadata(
                "Claim-Set-T0150",
                codes=["PV06"],
                version="v3",
                doc_type="design_spec",
                project="PV06",
            ),
        ),
    ]
    vault_services = SimpleNamespace(graph_store=graph_store)

    plan = await plan_batch_edges(scan_items=scan_items, vault_services=vault_services)

    supersedes = [e for e in plan.edges if e.edge_type == EdgeType.SUPERSEDES]
    assert len(supersedes) == 1
    edge = supersedes[0]
    assert edge.source_ref == "/scan/f3"
    # Anti-coincidental-pass: must target v2 (failed predecessor), not v1.
    # Pre-fix, this assertion fails with target_ref == v1.id, confirming
    # the silent chain corruption.
    assert edge.target_ref == v2.id, (
        f"Expected v3 to supersede v2 (the failed predecessor), got target_ref="
        f"{edge.target_ref!r}. v1={v1.id!r} v2={v2.id!r}."
    )
    assert plan.removals == []


@pytest.mark.asyncio
async def test_bi_007_plan_batch_edges_passes_default_exclude_failed_false_on_both_passes(
    graph_store,
):
    """Both query_documents passes must opt out of BH-020.

    Structural spy assertion complementing BI-006. BI-006's behavioral
    output (an active+failed predecessor surfacing) passes if *either*
    Pass A or Pass B is individually fixed, because the post-fix
    candidate surfaces through whichever pass got the kwarg. This test
    enforces the two-site discipline by spying on every
    query_documents call plan_batch_edges issues and asserting
    default_exclude_failed=False is passed on each.

    A scan with a versioned item ensures both Pass A and Pass B fire
    (per BI-004's optimization characterization).
    """
    seed = _make_doc(
        "spy_seed",
        title="Spy-Chain",
        project="PV06",
        doc_type="design_spec",
        version_label="v1",
        tags=["PV06"],
        lifecycle_status="active",
    )
    await graph_store.insert_document(seed)

    scan_items = [
        InferenceItem(
            "/scan/spy_v2",
            False,
            ParsedMetadata(
                "Spy-Chain",
                codes=["PV06"],
                version="v2",
                doc_type="design_spec",
                project="PV06",
            ),
        ),
    ]
    recording = _RecordingGraphStore(graph_store)
    vault_services = SimpleNamespace(graph_store=recording)

    await plan_batch_edges(scan_items=scan_items, vault_services=vault_services)

    # Expect 2 calls: Pass A (lifecycle_status=active) and Pass B
    # (project + doc_type targeted). Both must opt out of BH-020.
    assert len(recording.query_documents_calls) == 2
    for i, call in enumerate(recording.query_documents_calls):
        assert call["default_exclude_failed"] is False, (
            f"query_documents call #{i} (filters={call['filters']!r}) did not pass "
            f"default_exclude_failed=False; got {call['default_exclude_failed']!r}. "
            f"plan_batch_edges must opt out of BH-020 at both call sites so "
            f"failed-but-chain-identity-valid predecessors reach the Python gate."
        )


# ---------------------------------------------------------------------------
# 5. Migration boundary guard (BI-005)
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    importlib.util.find_spec("app.backend.edge_inference") is not None,
    reason=(
        "T-0138 boundary guard: only meaningful once Phase C deletes "
        "app/backend/edge_inference.py. Skipped while the legacy module "
        "still exists on the import path."
    ),
)
def test_bi_005_app_layer_does_not_define_edge_inference():
    """The app-layer edge_inference module is gone for good.

    The relocation is complete only when ``app.backend.edge_inference``
    no longer exists. Re-introducing the module (intentionally or as a
    merge mishap) would split the inference surface across the SAGE/app
    boundary again and re-open the principle-5 smell the relocation
    closes.
    """
    with pytest.raises(ImportError):
        importlib.import_module("app.backend.edge_inference")
