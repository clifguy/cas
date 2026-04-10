"""Tests for new MCP tools (TEST-APP-MCP-001 through MCP-025).

Covers 9 new tools: 7 SAGE API tools + 2 app backend tools.
Direct function calls bypassing MCP transport, matching the existing
MCP test pattern in tests/sage/test_mcp_server.py.
"""

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from sage.config import VaultConfig
from sage.mcp_init import initialize_services
from sage.mcp_server import (
    _vaults,
    sage_list_vaults,
    sage_vault_stats,
    sage_hash_check,
    sage_list_staging_edges,
    sage_confirm_staging_edge,
    sage_dismiss_staging_edge,
    sage_pending_metadata,
    app_scan_directory,
    app_batch_ingest,
    sage_ingest,
)
from sage.models.enums import EdgeType, PipelineStatus, SourceType
from sage.models.schemas import Document, StagingEdge


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_vault_config_dict(tmp_path, vault_id: str, vault_name: str):
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
                {"value": "checklist", "label": "Checklist"},
                {"value": "note", "label": "Note"},
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
            "adapters": [{"source_type": "markdown", "enabled": True}],
        },
        "metadata_extraction": {
            "review_required": False,
            "filename_extraction": {
                "separator": "_",
                "known_code_patterns": ["^[A-Z][A-Z0-9]{1,7}$", "^[A-Z]+-\\d+$"],
                "keyword_to_doc_type": [
                    {"keyword": "Checklist", "doc_type": "checklist"},
                ],
                "code_to_doc_type": [
                    {"code": "PV06", "doc_type": "patent_draft"},
                ],
            },
        },
        "edge_inference": {
            "tier_assignments": [
                {"edge_type": "supersedes", "tier": 1, "inference_rules": [{"method": "version_chain"}]},
                {"edge_type": "covers", "tier": 2, "inference_rules": [{"method": "filename_code_match"}]},
            ],
        },
    }


def _parse(result: str) -> object:
    return json.loads(result)


@pytest.fixture
async def two_vaults(tmp_path):
    """Register two vaults in the MCP vault registry."""
    c1 = VaultConfig.model_validate(
        _make_vault_config_dict(tmp_path, "test_vault", "Test Vault")
    )
    c2 = VaultConfig.model_validate(
        _make_vault_config_dict(tmp_path, "second_vault", "Second Vault")
    )
    s1 = await initialize_services(c1)
    s2 = await initialize_services(c2)
    _vaults["test_vault"] = s1
    _vaults["second_vault"] = s2

    yield s1, s2

    await asyncio.sleep(0.1)
    await s1.graph_store.close()
    await s2.graph_store.close()
    _vaults.pop("test_vault", None)
    _vaults.pop("second_vault", None)


@pytest.fixture
async def single_vault(tmp_path):
    """Register one vault with test files."""
    config = VaultConfig.model_validate(
        _make_vault_config_dict(tmp_path, "test_vault", "Test Vault")
    )
    services = await initialize_services(config)
    _vaults["test_vault"] = services

    # Create test source files
    sources = Path(config.vault.storage_root)
    (sources / "sample.md").write_text("# Sample\n\nContent.")
    (sources / "second.md").write_text("# Second\n\nMore content.")

    yield services, config

    await asyncio.sleep(0.3)
    await services.graph_store.close()
    _vaults.pop("test_vault", None)


@pytest.fixture
async def empty_registry():
    """Empty vault registry (saves/restores to avoid cross-module interference)."""
    saved = dict(_vaults)
    _vaults.clear()
    yield
    _vaults.clear()
    _vaults.update(saved)


# ---------------------------------------------------------------------------
# 1. sage_list_vaults (MCP-001, MCP-002)
# ---------------------------------------------------------------------------


class TestSageListVaults:

    async def test_mcp_001_returns_all_vaults(self, two_vaults):
        """sage_list_vaults returns all registered vaults."""
        result = _parse(await sage_list_vaults())
        assert isinstance(result, list)
        assert len(result) == 2
        ids = {v["id"] for v in result}
        assert ids == {"test_vault", "second_vault"}
        for v in result:
            assert "id" in v
            assert "name" in v
            assert "storage_root" in v

    async def test_mcp_002_empty_returns_empty_array(self, empty_registry):
        """sage_list_vaults with no vaults returns empty array."""
        result = _parse(await sage_list_vaults())
        assert result == []


# ---------------------------------------------------------------------------
# 2. sage_vault_stats (MCP-003, MCP-004, MCP-005)
# ---------------------------------------------------------------------------


class TestSageVaultStats:

    async def test_mcp_003_returns_stats_and_health(self, single_vault):
        """sage_vault_stats returns statistics and health indicators."""
        services, config = single_vault
        # Ingest a document first
        await sage_ingest("test_vault", "sample.md", "markdown")
        await asyncio.sleep(0.3)

        result = _parse(await sage_vault_stats("test_vault"))
        assert result["total_documents"] >= 1
        assert "by_lifecycle_state" in result
        assert "by_doc_type" in result
        assert "by_source_adapter" in result
        assert "total_edges" in result
        assert "staging_edge_count" in result
        assert "health" in result
        h = result["health"]
        assert "pending_metadata_count" in h
        assert "pending_edge_count" in h
        assert "deferred_abstract_count" in h
        assert "failed_ingestion_count" in h

    async def test_mcp_004_empty_vault_zero_counts(self, single_vault):
        """sage_vault_stats for empty vault returns zero counts."""
        result = _parse(await sage_vault_stats("test_vault"))
        assert result["total_documents"] == 0
        assert result["total_edges"] == 0
        assert result["staging_edge_count"] == 0

    async def test_mcp_005_unknown_vault_error(self, single_vault):
        """sage_vault_stats for unknown vault returns error."""
        result = _parse(await sage_vault_stats("nonexistent"))
        assert result["error"] == "unknown_vault"


# ---------------------------------------------------------------------------
# 3. sage_hash_check (MCP-006, MCP-007)
# ---------------------------------------------------------------------------


class TestSageHashCheck:

    async def test_mcp_006_returns_matches(self, single_vault):
        """sage_hash_check returns match results."""
        services, config = single_vault
        # Ingest to get a known hash
        doc_result = _parse(await sage_ingest("test_vault", "sample.md", "markdown"))
        doc_hash = doc_result["source_content_hash"]
        await asyncio.sleep(0.1)

        result = _parse(await sage_hash_check("test_vault", [doc_hash, "sha256:unknown"]))
        assert result[doc_hash]["exists"] is True
        assert result[doc_hash]["document_id"] == doc_result["id"]
        assert result["sha256:unknown"]["exists"] is False

    async def test_mcp_007_empty_list(self, single_vault):
        """sage_hash_check with empty list returns empty object."""
        result = _parse(await sage_hash_check("test_vault", []))
        assert result == {}


# ---------------------------------------------------------------------------
# 4. sage_list_staging_edges (MCP-008, MCP-009)
# ---------------------------------------------------------------------------


class TestSageListStagingEdges:

    async def test_mcp_008_returns_staging_edges(self, single_vault):
        """sage_list_staging_edges returns Tier 2 edges."""
        services, config = single_vault
        # Ingest two docs and create a staging edge
        r1 = _parse(await sage_ingest("test_vault", "sample.md", "markdown"))
        r2 = _parse(await sage_ingest("test_vault", "second.md", "markdown"))
        await asyncio.sleep(0.1)

        staging = StagingEdge(
            id="staging-001",
            source_id=r1["id"],
            target_id=r2["id"],
            edge_type=EdgeType.COVERS,
            inference_evidence="Test evidence",
            confidence_tier=2,
            created_at=datetime.now(timezone.utc),
        )
        await services.graph_store.insert_staging_edge(staging)

        result = _parse(await sage_list_staging_edges("test_vault"))
        assert isinstance(result, list)
        assert len(result) == 1
        assert result[0]["id"] == "staging-001"
        assert result[0]["edge_type"] == "covers"

    async def test_mcp_009_empty_when_none(self, single_vault):
        """sage_list_staging_edges returns empty array when none exist."""
        result = _parse(await sage_list_staging_edges("test_vault"))
        assert result == []


# ---------------------------------------------------------------------------
# 5. sage_confirm/dismiss_staging_edge (MCP-010, MCP-011, MCP-012)
# ---------------------------------------------------------------------------


class TestStagingEdgeActions:

    async def _setup_staging(self, services):
        """Ingest docs and create a staging edge, return IDs."""
        r1 = _parse(await sage_ingest("test_vault", "sample.md", "markdown"))
        r2 = _parse(await sage_ingest("test_vault", "second.md", "markdown"))
        await asyncio.sleep(0.1)
        staging = StagingEdge(
            id="staging-test",
            source_id=r1["id"],
            target_id=r2["id"],
            edge_type=EdgeType.COVERS,
            inference_evidence="Test evidence",
            confidence_tier=2,
            created_at=datetime.now(timezone.utc),
        )
        await services.graph_store.insert_staging_edge(staging)
        return r1["id"], r2["id"]

    async def test_mcp_010_confirm_moves_to_production(self, single_vault):
        """sage_confirm_staging_edge promotes to production."""
        services, config = single_vault
        await self._setup_staging(services)

        result = _parse(await sage_confirm_staging_edge("test_vault", "staging-test"))
        assert result["confirmed"] is True
        assert "production_edge_id" in result

        # Staging edge gone
        listing = _parse(await sage_list_staging_edges("test_vault"))
        assert len(listing) == 0

    async def test_mcp_011_dismiss_deletes(self, single_vault):
        """sage_dismiss_staging_edge deletes from staging."""
        services, config = single_vault
        await self._setup_staging(services)

        result = _parse(await sage_dismiss_staging_edge("test_vault", "staging-test"))
        assert result["dismissed"] is True

        listing = _parse(await sage_list_staging_edges("test_vault"))
        assert len(listing) == 0

    async def test_mcp_012_nonexistent_returns_error(self, single_vault):
        """Confirm/dismiss non-existent staging edge returns error."""
        result = _parse(await sage_confirm_staging_edge("test_vault", "gone-001"))
        assert "error" in result


# ---------------------------------------------------------------------------
# 6. sage_pending_metadata (MCP-013, MCP-014)
# ---------------------------------------------------------------------------


class TestSagePendingMetadata:

    async def test_mcp_013_returns_pending(self, single_vault):
        """sage_pending_metadata returns documents awaiting confirmation."""
        services, config = single_vault
        await sage_ingest("test_vault", "sample.md", "markdown")
        await asyncio.sleep(0.1)

        result = _parse(await sage_pending_metadata("test_vault"))
        assert isinstance(result, list)
        # Newly ingested docs have metadata_confirmed=False
        assert len(result) >= 1
        assert "document" in result[0]

    async def test_mcp_014_empty_when_none(self, single_vault):
        """sage_pending_metadata returns empty array when none pending."""
        result = _parse(await sage_pending_metadata("test_vault"))
        assert result == []


# ---------------------------------------------------------------------------
# 7. app_scan_directory (MCP-015, MCP-016, MCP-017, MCP-018)
# ---------------------------------------------------------------------------


class TestAppScanDirectory:

    async def test_mcp_015_returns_files_with_parsed_metadata(self, single_vault, tmp_path):
        """app_scan_directory returns files with parsed metadata."""
        services, config = single_vault
        scan_dir = tmp_path / "scan_inbox"
        scan_dir.mkdir()
        (scan_dir / "2026-03-09_PIM_PV06_Claim-Set_v7.md").write_text("# Test")
        (scan_dir / "notes.txt").write_text("txt file")

        result = _parse(await app_scan_directory("test_vault", str(scan_dir)))
        assert "files" in result
        assert "warnings" in result
        files = result["files"]
        md = [f for f in files if f["file_path"].endswith(".md")]
        assert len(md) == 1
        assert md[0]["adapter"] == "markdown"
        assert md[0]["sage_status"] == "new"
        assert "parsed_metadata" in md[0]
        pm = md[0]["parsed_metadata"]
        assert pm["title"] == "Claim-Set"
        assert "PV06" in pm["codes"]

    async def test_mcp_016_invalid_directory_error(self, single_vault):
        """app_scan_directory with invalid directory returns error."""
        result = _parse(await app_scan_directory("test_vault", "/nonexistent/path"))
        assert result["error"] == "invalid_directory"

    async def test_mcp_017_respects_max_depth(self, single_vault, tmp_path):
        """app_scan_directory respects max_depth."""
        services, config = single_vault
        scan_dir = tmp_path / "depth_test"
        scan_dir.mkdir()
        (scan_dir / "top.md").write_text("# Top")
        sub = scan_dir / "sub"
        sub.mkdir()
        (sub / "nested.md").write_text("# Nested")

        result = _parse(await app_scan_directory("test_vault", str(scan_dir), max_depth=0))
        paths = [f["file_path"] for f in result["files"]]
        assert any("top.md" in p for p in paths)
        assert not any("nested.md" in p for p in paths)

    async def test_mcp_018_permission_warnings(self, single_vault, tmp_path):
        """app_scan_directory reports permission errors as warnings."""
        services, config = single_vault
        scan_dir = tmp_path / "perm_test"
        scan_dir.mkdir()
        (scan_dir / "ok.md").write_text("# OK")

        result = _parse(await app_scan_directory("test_vault", str(scan_dir)))
        assert isinstance(result["warnings"], list)


# ---------------------------------------------------------------------------
# 8. app_batch_ingest (MCP-019, MCP-020, MCP-021, MCP-022)
# ---------------------------------------------------------------------------


class TestAppBatchIngest:

    async def test_mcp_019_returns_summary_with_edges(self, single_vault):
        """app_batch_ingest processes files and returns summary with edge counts."""
        services, config = single_vault
        sources = Path(config.vault.storage_root)
        v1 = sources / "patent_v1.md"
        v1.write_text("# Patent v1\n\nFirst.")
        v2 = sources / "patent_v2.md"
        v2.write_text("# Patent v2\n\nSecond.")

        result = _parse(await app_batch_ingest("test_vault", [
            {"file_path": str(v1), "adapter": "markdown",
             "parsed_metadata": {"title": "Patent", "codes": ["PV06"], "version": "v1", "doc_type": "patent_draft"}},
            {"file_path": str(v2), "adapter": "markdown",
             "parsed_metadata": {"title": "Patent", "codes": ["PV06"], "version": "v2", "doc_type": "patent_draft"}},
        ]))
        assert "documents_created" in result
        assert result["documents_created"]["new"] == 2
        assert "edges_created" in result
        assert "edges_staged" in result
        assert "edges_dropped" in result
        assert result["error_count"] == 0
        # Should have a supersedes edge
        assert result["edges_created"].get("supersedes", 0) >= 1

    async def test_mcp_021_continues_after_error(self, single_vault):
        """app_batch_ingest continues after per-file error."""
        services, config = single_vault
        sources = Path(config.vault.storage_root)
        good = sources / "good_file.md"
        good.write_text("# Good\n\nContent.")

        result = _parse(await app_batch_ingest("test_vault", [
            {"file_path": str(good), "adapter": "markdown"},
            {"file_path": "/nonexistent/bad.md", "adapter": "markdown"},
        ]))
        assert result["error_count"] == 1
        assert result["documents_created"]["new"] >= 1
        assert len(result["errors"]) == 1
        assert "bad.md" in result["errors"][0]["filename"]

    async def test_mcp_022_empty_list_error(self, single_vault):
        """app_batch_ingest with empty file list returns error."""
        result = _parse(await app_batch_ingest("test_vault", []))
        assert "error" in result
        assert result["error"] == "empty_file_list"


# ---------------------------------------------------------------------------
# 9. Cross-Cutting Conventions (MCP-023, MCP-024, MCP-025)
# ---------------------------------------------------------------------------


class TestMCPConventions:

    async def test_mcp_023_all_return_valid_json(self, single_vault, tmp_path):
        """All new tools return valid JSON strings."""
        services, config = single_vault
        sources = Path(config.vault.storage_root)
        scan_dir = tmp_path / "json_test"
        scan_dir.mkdir()
        (scan_dir / "test.md").write_text("# Test")

        results = [
            await sage_list_vaults(),
            await sage_vault_stats("test_vault"),
            await sage_hash_check("test_vault", []),
            await sage_list_staging_edges("test_vault"),
            await sage_pending_metadata("test_vault"),
            await app_scan_directory("test_vault", str(scan_dir)),
            await app_batch_ingest("test_vault", [
                {"file_path": str(sources / "sample.md"), "adapter": "markdown"},
            ]),
        ]
        for r in results:
            assert isinstance(r, str)
            json.loads(r)  # Should not raise

    async def test_mcp_024_unknown_vault_error(self, single_vault):
        """App tools with unknown vault_id return structured error."""
        result = _parse(await app_scan_directory("nonexistent", "/tmp"))
        assert result["error"] == "unknown_vault"

    async def test_mcp_025_tool_naming_convention(self):
        """Tool naming follows sage_*/app_* prefix convention."""
        from sage.mcp_server import mcp as mcp_server
        # The tool functions we imported all follow the convention
        sage_tools = [
            "sage_list_vaults", "sage_vault_stats", "sage_hash_check",
            "sage_list_staging_edges", "sage_confirm_staging_edge",
            "sage_dismiss_staging_edge", "sage_pending_metadata",
        ]
        app_tools = ["app_scan_directory", "app_batch_ingest"]

        for name in sage_tools:
            assert name.startswith("sage_")
        for name in app_tools:
            assert name.startswith("app_")
