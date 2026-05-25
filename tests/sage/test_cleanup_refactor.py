"""Tests for the SAGE/MCP cleanup refactor (TEST-CLN-001 through CLN-012).

Verifies type consistency, import hygiene, schema annotations, naming,
and the IngestResult dataclass after cleanup.
"""

import asyncio
import json
from pathlib import Path

import pytest

import sage.mcp_server as _mcp
from sage.config import VaultConfig
from sage.mcp_init import initialize_services
from sage.mcp_server import app_batch_ingest
from sage.models.enums import SourceType
from sage.models.schemas import IngestRequest

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_vault_config(tmp_path, vault_id: str = "test_vault"):
    brain_dir = tmp_path / vault_id / "brain"
    brain_dir.mkdir(parents=True, exist_ok=True)
    sources_dir = tmp_path / vault_id / "sources"
    sources_dir.mkdir(parents=True, exist_ok=True)
    return {
        "vault": {
            "id": vault_id,
            "name": "Test Vault",
            "description": "Cleanup test vault",
            "owner": "testuser",
            "storage_root": str(sources_dir),
            "brain_root": str(brain_dir),
            "visibility": "personal",
        },
        "document_types": {
            "doc_types": [
                {"value": "patent_draft", "label": "Patent Draft"},
                {"value": "checklist", "label": "Checklist"},
                {"value": "note", "label": "Note"},
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
            ],
        },
        "source_adapters": {
            "adapters": [{"source_type": "markdown", "enabled": True}],
        },
        "metadata_extraction": {
            "filename_extraction": {
                "separator": "_",
                "known_code_patterns": ["^[A-Z][A-Z0-9]{1,7}$"],
                "keyword_to_doc_type": [],
                "code_to_doc_type": [
                    {"code": "PV06", "doc_type": "patent_draft"},
                ],
            },
        },
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


def _parse(result: str | dict) -> dict:
    if isinstance(result, dict):
        return result
    return json.loads(result)


@pytest.fixture
async def vault(tmp_path):
    """Register a single vault in the MCP vault registry."""
    config = VaultConfig.model_validate(_make_vault_config(tmp_path))
    services = await initialize_services(config)
    _mcp._vaults["test_vault"] = services

    # Create test files
    sources = Path(config.vault.storage_root)
    (sources / "simple.md").write_text("# Simple\n\nBasic content.")
    (sources / "patent_v1.md").write_text("# Patent v1\n\nFirst version.")
    (sources / "patent_v2.md").write_text("# Patent v2\n\nSecond version.")

    yield services, config

    await asyncio.sleep(0.3)
    await services.graph_store.close()
    _mcp._vaults.pop("test_vault", None)


# ---------------------------------------------------------------------------
# TEST-CLN-001: EdgeResult type consistency in batch ingest summary
# ---------------------------------------------------------------------------


class TestEdgeResultTypeConsistency:
    """edges_created and edges_staged are always dict[str, int], never int."""

    async def test_cln_001a_no_edge_inference(self, vault):
        """With infer_edges=False, edges_created and edges_staged are empty dicts."""
        services, config = vault
        sources = Path(config.vault.storage_root)

        result = _parse(
            await app_batch_ingest(
                "test_vault",
                [
                    {"file_path": str(sources / "simple.md"), "source_type": "markdown"},
                ],
                infer_edges=False,
            )
        )

        assert isinstance(result["edges_created"], dict)
        assert isinstance(result["edges_staged"], dict)
        assert result["edges_created"] == {}
        assert result["edges_staged"] == {}

    async def test_cln_001b_with_inference_no_edges(self, vault):
        """With infer_edges=True but no matching versions, still returns dicts."""
        services, config = vault
        sources = Path(config.vault.storage_root)

        result = _parse(
            await app_batch_ingest(
                "test_vault",
                [
                    {
                        "file_path": str(sources / "simple.md"),
                        "source_type": "markdown",
                        "parsed_metadata": {"title": "Simple", "doc_type": "note"},
                    },
                ],
                infer_edges=True,
            )
        )

        assert isinstance(result["edges_created"], dict)
        assert isinstance(result["edges_staged"], dict)

    async def test_cln_001c_with_inference_edges_produced(self, vault):
        """Versioned files produce edges_created as dict with edge type keys."""
        services, config = vault
        sources = Path(config.vault.storage_root)

        result = _parse(
            await app_batch_ingest(
                "test_vault",
                [
                    {
                        "file_path": str(sources / "patent_v1.md"),
                        "source_type": "markdown",
                        "parsed_metadata": {
                            "title": "Patent",
                            "codes": ["PV06"],
                            "version": "v1",
                            "doc_type": "patent_draft",
                        },
                    },
                    {
                        "file_path": str(sources / "patent_v2.md"),
                        "source_type": "markdown",
                        "parsed_metadata": {
                            "title": "Patent",
                            "codes": ["PV06"],
                            "version": "v2",
                            "doc_type": "patent_draft",
                        },
                    },
                ],
                infer_edges=True,
            )
        )

        assert isinstance(result["edges_created"], dict)
        assert isinstance(result["edges_staged"], dict)
        # Version chain should produce a supersedes edge
        assert result["edges_created"].get("supersedes", 0) >= 1


# ---------------------------------------------------------------------------
# TEST-CLN-003: Module-level imports compile without errors
# ---------------------------------------------------------------------------


class TestModuleLevelImports:
    """MCP server module imports all error classes at top level."""

    def test_cln_003_import_succeeds(self):
        """sage.mcp_server imports without circular import errors."""
        import importlib

        import sage.mcp_server as mod

        # importlib.reload re-executes the module body, which rebinds both
        # `_vaults` and `_vault_registry_service`. Other modules and the
        # registry service singleton both hold references to the originals,
        # so the new bindings must be replaced with the originals after
        # reload to keep the registry dict and its wrapping service paired.
        original_vaults = mod._vaults
        original_service = mod._vault_registry_service
        importlib.reload(mod)
        mod._vaults = original_vaults
        mod._vault_registry_service = original_service
        # If we get here, no ImportError occurred

    def test_cln_003_error_classes_at_module_level(self):
        """Error classes are importable from sage.mcp_server's namespace."""
        # These should be in the module's import section, not local
        # We verify by checking the module's source for local imports
        import inspect

        import sage.mcp_server as m

        source = inspect.getsource(m.sage_get_document)
        assert "from sage.api.errors import" not in source


# ---------------------------------------------------------------------------
# TEST-CLN-009: Type annotation on _passes_scope
# ---------------------------------------------------------------------------


class TestTypeAnnotations:
    """Critical missing type annotations are added."""

    def test_cln_009_passes_scope_doc_annotated(self):
        """_passes_scope has type annotation on doc parameter."""
        import inspect

        from sage.services.retrieval import RetrievalService

        sig = inspect.signature(RetrievalService._passes_scope)
        params = sig.parameters
        assert params["doc"].annotation is not inspect.Parameter.empty


# ---------------------------------------------------------------------------
# TEST-CLN-011: Graph traversal helper naming
# ---------------------------------------------------------------------------


class TestTraversalNaming:
    """Traversal helpers use clear parameter names (match_col, follow_col)."""

    def test_cln_011_traversal_still_works(self, vault):
        """Traversal produces correct results after rename (regression)."""
        # This is a regression test -- actual logic tested in test_graph_ops.py
        # We just verify the rename didn't break anything by importing
        import inspect

        from sage.storage.graph_store import GraphStore

        source = inspect.getsource(GraphStore._traverse_sync)
        assert "match_col" in source
        assert "follow_col" in source
        # Old names should be gone
        assert "from_col" not in source
        assert "to_col" not in source


# ---------------------------------------------------------------------------
# TEST-CLN-012: IngestResult dataclass
# ---------------------------------------------------------------------------


class TestIngestResult:
    """Ingestion service returns IngestResult instead of tuple."""

    async def test_cln_012a_new_document(self, vault):
        """New document ingest returns IngestResult with is_new=True."""
        services, config = vault
        from sage.services.ingestion import IngestResult

        request = IngestRequest(source="simple.md", source_type=SourceType.MARKDOWN)
        result = await services.ingestion_service.ingest(request)

        assert isinstance(result, IngestResult)
        assert result.is_new is True
        assert result.document is not None
        assert result.document.id

    async def test_cln_012b_force_reingest(self, vault):
        """Force re-ingestion returns IngestResult with is_new=False."""
        services, config = vault
        from sage.services.ingestion import IngestResult

        request = IngestRequest(source="simple.md", source_type=SourceType.MARKDOWN)
        # First ingest
        await services.ingestion_service.ingest(request)
        await asyncio.sleep(0.2)
        # Force re-ingest
        force_request = IngestRequest(
            source="simple.md", source_type=SourceType.MARKDOWN, force=True
        )
        result = await services.ingestion_service.ingest(force_request)

        assert isinstance(result, IngestResult)
        assert result.is_new is False
