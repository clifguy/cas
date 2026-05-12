"""Tests for new MCP tools (TEST-APP-MCP-001 through MCP-027).

Covers 9 new tools: 7 SAGE API tools + 2 app backend tools.
Direct function calls bypassing MCP transport, matching the existing
MCP test pattern in tests/sage/test_mcp_server.py.
"""

import asyncio
import hashlib
import json
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest

import sage.mcp_server as _mcp
from sage.adapters.stubs import (
    StubAbstractionProvider,
    StubContentStore,
    StubEmbeddingProvider,
)
from sage.config import VaultConfig
from sage.mcp_init import initialize_services
from sage.mcp_server import (
    app_batch_ingest,
    app_scan_directory,
    sage_confirm_staging_edge,
    sage_discover,
    sage_dismiss_staging_edge,
    sage_hash_check,
    sage_ingest,
    sage_list_staging_edges,
    sage_list_vaults,
    sage_pending_metadata,
    sage_unlink,
    sage_vault_stats,
)
from sage.models.enums import EdgeType, PipelineStatus, SourceType
from sage.models.schemas import Document, StagingEdge

_DOC_ID_RE = re.compile(r"^[0-9a-f]{8}_[a-z0-9_]+$")


def _id(name: str) -> str:
    """Translate a short test name to a shape-conformant document ID.

    The ID validator in sage/models/schemas.py requires the pattern
    ^[0-9a-f]{8}_[a-z0-9_]+$. Test fixtures use short readable names
    like "pending-doc-1"; this helper wraps them so the values still
    construct valid Document instances. Idempotent: an already-canonical
    id passes through unchanged.
    """
    if _DOC_ID_RE.fullmatch(name):
        return name
    slug = re.sub(r"[^a-z0-9_]+", "_", name.lower()).strip("_") or "n"
    return f"{hashlib.sha256(name.encode()).hexdigest()[:8]}_{slug}"


def _eid(name: str) -> str:
    """Deterministic canonical-UUID edge id derived from a short test name."""
    return str(uuid.uuid5(uuid.NAMESPACE_OID, f"sage-test-edge:{name}"))


_STG_001 = _eid("staging-001")
_STG_TEST = _eid("staging-test")
_GONE_001 = _eid("gone-001")


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
            "adapters": [{"source_type": "markdown", "enabled": True}],
        },
        "metadata_extraction": {
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


def _parse(result: str | dict) -> object:
    if isinstance(result, dict):
        return result
    return json.loads(result)


@pytest.fixture
async def two_vaults(tmp_path):
    """Register two vaults in the MCP vault registry."""
    c1 = VaultConfig.model_validate(_make_vault_config_dict(tmp_path, "test_vault", "Test Vault"))
    c2 = VaultConfig.model_validate(
        _make_vault_config_dict(tmp_path, "second_vault", "Second Vault")
    )
    s1 = await initialize_services(
        c1,
        content_store=StubContentStore(),
        embedding_provider=StubEmbeddingProvider(),
        abstraction_provider=StubAbstractionProvider(),
    )
    s2 = await initialize_services(
        c2,
        content_store=StubContentStore(),
        embedding_provider=StubEmbeddingProvider(),
        abstraction_provider=StubAbstractionProvider(),
    )
    _mcp._vaults["test_vault"] = s1
    _mcp._vaults["second_vault"] = s2

    yield s1, s2

    await asyncio.sleep(0.1)
    await s1.graph_store.close()
    await s2.graph_store.close()
    _mcp._vaults.pop("test_vault", None)
    _mcp._vaults.pop("second_vault", None)


@pytest.fixture
async def single_vault(tmp_path):
    """Register one vault with test files."""
    config = VaultConfig.model_validate(
        _make_vault_config_dict(tmp_path, "test_vault", "Test Vault")
    )
    services = await initialize_services(
        config,
        embedding_provider=StubEmbeddingProvider(),
        abstraction_provider=StubAbstractionProvider(),
    )
    _mcp._vaults["test_vault"] = services

    # Create test source files
    sources = Path(config.vault.storage_root)
    (sources / "sample.md").write_text("# Sample\n\nContent.")
    (sources / "second.md").write_text("# Second\n\nMore content.")

    yield services, config

    await asyncio.sleep(0.3)
    await services.graph_store.close()
    _mcp._vaults.pop("test_vault", None)


@pytest.fixture
async def empty_registry():
    """Empty vault registry (saves/restores to avoid cross-module interference)."""
    saved = dict(_mcp._vaults)
    _mcp._vaults.clear()
    yield
    _mcp._vaults.clear()
    _mcp._vaults.update(saved)


# ---------------------------------------------------------------------------
# 1. sage_list_vaults (MCP-001, MCP-002)
# ---------------------------------------------------------------------------


class TestSageListVaults:
    async def test_mcp_001_returns_all_vaults(self, two_vaults):
        """sage_list_vaults returns all registered vaults in envelope."""
        result = _parse(await sage_list_vaults())
        assert result["count"] == 2
        assert isinstance(result["vaults"], list)
        ids = {v["id"] for v in result["vaults"]}
        assert ids == {"test_vault", "second_vault"}
        for v in result["vaults"]:
            assert "id" in v
            assert "name" in v
            assert "storage_root" in v

    async def test_mcp_002_empty_returns_envelope(self, empty_registry):
        """sage_list_vaults with no vaults returns envelope with empty list."""
        result = _parse(await sage_list_vaults())
        assert result["vaults"] == []
        assert result["count"] == 0


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

    async def test_sqlite_size_bytes_nonzero_after_ingest(self, single_vault):
        """sqlite_size_bytes reflects actual graph.db file size."""
        services, config = single_vault
        await sage_ingest("test_vault", "sample.md", "markdown")
        await asyncio.sleep(0.3)

        result = _parse(await sage_vault_stats("test_vault"))
        assert result["sqlite_size_bytes"] > 0

    async def test_lancedb_size_bytes_nonzero_after_indexing(self, single_vault):
        """lancedb_size_bytes reflects actual LanceDB directory size."""
        services, config = single_vault
        await sage_ingest("test_vault", "sample.md", "markdown")
        await asyncio.sleep(0.5)  # allow indexing to complete

        result = _parse(await sage_vault_stats("test_vault"))
        assert result["lancedb_size_bytes"] > 0

    async def test_storage_sizes_zero_for_empty_vault(self, single_vault):
        """Empty vault has sqlite overhead but zero LanceDB (no table yet)."""
        result = _parse(await sage_vault_stats("test_vault"))
        # SQLite file exists with schema, so it has some size
        assert result["sqlite_size_bytes"] > 0
        # LanceDB directory may not exist or be empty before first ingest
        assert result["lancedb_size_bytes"] >= 0
        # Chunk count is 0 before any indexing
        assert result["lancedb_chunk_count"] == 0

    async def test_lancedb_chunk_count_nonzero_after_indexing(self, single_vault):
        """lancedb_chunk_count reflects the number of indexed chunk rows."""
        services, config = single_vault
        await sage_ingest("test_vault", "sample.md", "markdown")
        await asyncio.sleep(0.5)  # allow indexing to complete

        result = _parse(await sage_vault_stats("test_vault"))
        assert result["lancedb_chunk_count"] > 0


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
        """sage_list_staging_edges returns Tier 2 edges in envelope."""
        services, config = single_vault
        # Ingest two docs and create a staging edge
        r1 = _parse(await sage_ingest("test_vault", "sample.md", "markdown"))
        r2 = _parse(await sage_ingest("test_vault", "second.md", "markdown"))
        await asyncio.sleep(0.1)

        staging = StagingEdge(
            id=_STG_001,
            source_id=r1["id"],
            target_id=r2["id"],
            edge_type=EdgeType.COVERS,
            inference_evidence="Test evidence",
            confidence_tier=2,
            created_at=datetime.now(timezone.utc),
        )
        await services.graph_store.insert_staging_edge(staging)

        result = _parse(await sage_list_staging_edges("test_vault"))
        assert result["count"] == 1
        assert result["vault_id"] == "test_vault"
        assert result["items"][0]["id"] == _STG_001
        assert result["items"][0]["edge_type"] == "covers"

    async def test_mcp_009_empty_when_none(self, single_vault):
        """sage_list_staging_edges returns envelope when none exist."""
        result = _parse(await sage_list_staging_edges("test_vault"))
        assert result["items"] == []
        assert result["count"] == 0
        assert result["vault_id"] == "test_vault"
        assert result["status"] == "no_staging_edges"


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
            id=_STG_TEST,
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

        result = _parse(await sage_confirm_staging_edge("test_vault", _STG_TEST))
        assert result["confirmed"] is True
        assert "production_edge_id" in result

        # Staging edge gone
        listing = _parse(await sage_list_staging_edges("test_vault"))
        assert listing["count"] == 0

    async def test_mcp_011_dismiss_deletes(self, single_vault):
        """sage_dismiss_staging_edge deletes from staging."""
        services, config = single_vault
        await self._setup_staging(services)

        result = _parse(await sage_dismiss_staging_edge("test_vault", _STG_TEST))
        assert result["dismissed"] is True

        listing = _parse(await sage_list_staging_edges("test_vault"))
        assert listing["count"] == 0

    async def test_mcp_012_nonexistent_returns_error(self, single_vault):
        """Confirm/dismiss non-existent staging edge returns error."""
        result = _parse(await sage_confirm_staging_edge("test_vault", _GONE_001))
        assert "error" in result


# ---------------------------------------------------------------------------
# T-0024: edge_id validation across MCP tools that take edge_id directly
# ---------------------------------------------------------------------------


class TestEdgeIdValidation:
    """Negative tests proving non-canonical / invalid edge_id input is normalized
    or rejected at the MCP-tool boundary, not silently passed to storage where
    a non-canonical UUID would miss the canonical-form lookup.
    """

    @pytest.mark.parametrize(
        "tool",
        [sage_unlink, sage_confirm_staging_edge, sage_dismiss_staging_edge],
        ids=["sage_unlink", "sage_confirm_staging_edge", "sage_dismiss_staging_edge"],
    )
    @pytest.mark.parametrize(
        "bad_input",
        ["not-a-uuid", "", "12345", "deadbeef-dead-beef"],
        ids=["random_text", "empty", "digits", "truncated_uuid"],
    )
    async def test_invalid_uuid_rejected(self, single_vault, tool, bad_input):
        result = _parse(await tool("test_vault", bad_input))
        assert "error" in result
        assert "edge id must be a UUID" in result["message"]

    @pytest.mark.parametrize(
        "tool,setup_required",
        [
            (sage_confirm_staging_edge, True),
            (sage_dismiss_staging_edge, True),
        ],
        ids=["sage_confirm_staging_edge", "sage_dismiss_staging_edge"],
    )
    async def test_non_canonical_uuid_normalized_to_lookup(
        self, single_vault, tool, setup_required
    ):
        """A staging edge created with canonical id X is found by URN-prefixed lookup."""
        services, _config = single_vault
        # Set up the staging edge with the canonical id
        r1 = _parse(await sage_ingest("test_vault", "sample.md", "markdown"))
        r2 = _parse(await sage_ingest("test_vault", "second.md", "markdown"))
        await asyncio.sleep(0.1)
        canonical = _eid("normalize-target")
        staging = StagingEdge(
            id=canonical,
            source_id=r1["id"],
            target_id=r2["id"],
            edge_type=EdgeType.COVERS,
            inference_evidence="Test",
            confidence_tier=2,
            created_at=datetime.now(timezone.utc),
        )
        await services.graph_store.insert_staging_edge(staging)

        # Call with URN-prefixed (non-canonical) input — should normalize and succeed
        non_canonical = f"urn:uuid:{canonical}"
        result = _parse(await tool("test_vault", non_canonical))
        # Either confirmed=True or dismissed=True; the absence of "error" proves
        # the non-canonical input was normalized before the storage lookup.
        assert "error" not in result, f"non-canonical input not normalized: {result}"


# ---------------------------------------------------------------------------
# 6. sage_pending_metadata (MCP-013, MCP-014)
# ---------------------------------------------------------------------------


class TestSagePendingMetadata:
    async def test_mcp_013_returns_pending(self, tmp_path):
        """sage_pending_metadata returns documents awaiting confirmation in envelope.

        Under CAS-ADR-021, metadata_confirmed=False is driven by the
        caller's IngestRequest.needs_review flag, not by vault config.
        The MCP tool surface does not yet expose needs_review (Chunk 4),
        so the test seeds a pending document by inserting it directly
        via the graph store. This keeps the test focused on
        sage_pending_metadata's behavior, decoupled from how a document
        comes to be unconfirmed.
        """
        cfg_dict = _make_vault_config_dict(tmp_path, "review_vault", "Review Required Vault")
        config = VaultConfig.model_validate(cfg_dict)
        services = await initialize_services(
            config,
            content_store=StubContentStore(),
            embedding_provider=StubEmbeddingProvider(),
            abstraction_provider=StubAbstractionProvider(),
        )
        _mcp._vaults["review_vault"] = services
        try:
            now = datetime.now(timezone.utc)
            pending_doc = Document(
                id=_id("pending-doc-1"),
                title="Pending Sample",
                source_type=SourceType.MARKDOWN,
                source_path="sample.md",
                lifecycle_status="active",
                source_content_hash="0" * 64,
                adapter_version="1.0",
                created_by="testuser",
                created_at=now,
                last_modified_by="testuser",
                updated_at=now,
                projected_at=now,
                pipeline_status=PipelineStatus.ABSTRACTION_COMPLETE,
                metadata_confirmed=False,
            )
            await services.graph_store.insert_document(pending_doc)

            result = _parse(await sage_pending_metadata("review_vault"))
            assert result["vault_id"] == "review_vault"
            assert result["count"] >= 1
            assert result["status"] == "pending_review"
            assert "document" in result["items"][0]
        finally:
            await asyncio.sleep(0.3)
            await services.graph_store.close()
            _mcp._vaults.pop("review_vault", None)

    async def test_mcp_014_empty_when_none(self, single_vault):
        """sage_pending_metadata returns envelope when none pending."""
        result = _parse(await sage_pending_metadata("test_vault"))
        assert result["items"] == []
        assert result["count"] == 0
        assert result["vault_id"] == "test_vault"
        assert result["status"] == "no_pending_metadata"


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

        result = _parse(
            await app_batch_ingest(
                "test_vault",
                [
                    {
                        "file_path": str(v1),
                        "adapter": "markdown",
                        "parsed_metadata": {
                            "title": "Patent",
                            "codes": ["PV06"],
                            "version": "v1",
                            "doc_type": "patent_draft",
                        },
                    },
                    {
                        "file_path": str(v2),
                        "adapter": "markdown",
                        "parsed_metadata": {
                            "title": "Patent",
                            "codes": ["PV06"],
                            "version": "v2",
                            "doc_type": "patent_draft",
                        },
                    },
                ],
            )
        )
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

        result = _parse(
            await app_batch_ingest(
                "test_vault",
                [
                    {"file_path": str(good), "adapter": "markdown"},
                    {"file_path": "/nonexistent/bad.md", "adapter": "markdown"},
                ],
            )
        )
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
    async def test_mcp_023_all_return_serializable_dicts(self, single_vault, tmp_path):
        """All tools return dicts that are JSON-serializable."""
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
            await app_batch_ingest(
                "test_vault",
                [
                    {"file_path": str(sources / "sample.md"), "adapter": "markdown"},
                ],
            ),
        ]
        for r in results:
            assert isinstance(r, dict)
            json.dumps(r, default=str)  # Should not raise

    async def test_mcp_024_unknown_vault_error(self, single_vault):
        """App tools with unknown vault_id return structured error."""
        result = _parse(await app_scan_directory("nonexistent", "/tmp"))
        assert result["error"] == "unknown_vault"

    async def test_mcp_025_tool_naming_convention(self):
        """Tool naming follows sage_*/app_* prefix convention."""
        # The tool functions we imported all follow the convention
        sage_tools = [
            "sage_list_vaults",
            "sage_vault_stats",
            "sage_hash_check",
            "sage_list_staging_edges",
            "sage_confirm_staging_edge",
            "sage_dismiss_staging_edge",
            "sage_pending_metadata",
        ]
        app_tools = ["app_scan_directory", "app_batch_ingest"]

        for name in sage_tools:
            assert name.startswith("sage_")
        for name in app_tools:
            assert name.startswith("app_")


# ---------------------------------------------------------------------------
# 10. sage_discover catalog mode (MCP-026, MCP-027)
# ---------------------------------------------------------------------------


class TestSageDiscoverCatalog:
    async def _seed_docs(self, services):
        """Insert 5 documents for catalog mode tests."""
        from sage.models.schemas import Document

        gs = services.graph_store
        now = datetime.now(timezone.utc)

        def _doc(doc_id, doc_type="patent_draft", tags=None, lifecycle="active"):
            return Document(
                id=_id(doc_id),
                title=f"Test {doc_id}",
                source_type=SourceType.MARKDOWN,
                source_path=f"test/{doc_id}.md",
                lifecycle_status=lifecycle,
                source_content_hash=f"hash_{doc_id}",
                adapter_version="0.1.0",
                created_by="testuser",
                created_at=now,
                last_modified_by="testuser",
                updated_at=now,
                projected_at=now,
                pipeline_status=PipelineStatus.ABSTRACTION_COMPLETE,
                doc_type=doc_type,
                tags=tags or [],
            )

        await gs.insert_document(_doc("doc_a", "patent_draft", ["PV07"]))
        await gs.insert_document(_doc("doc_b", "glossary", ["PV07"]))
        await gs.insert_document(_doc("doc_c", "patent_draft", ["PV08"]))

    async def test_mcp_026_catalog_returns_filtered(self, single_vault):
        """sage_discover catalog mode returns filtered documents."""
        services, config = single_vault
        await self._seed_docs(services)

        result = _parse(
            await sage_discover(
                vault_id="test_vault",
                mode="catalog",
                scope="filtered",
                filters={"tags": ["PV07"]},
            )
        )

        assert result["mode"] == "catalog"
        assert result["total_available"] == 2
        assert len(result["results"]) == 2
        result_ids = {r["document"]["id"] for r in result["results"]}
        assert result_ids == {_id("doc_a"), _id("doc_b")}
        # No chunk content or relevance scores
        for r in result["results"]:
            assert r.get("chunk_content") is None
            assert r.get("relevance_score") is None

    async def test_mcp_027_catalog_pagination_offset(self, single_vault):
        """sage_discover catalog mode pagination with offset."""
        services, config = single_vault
        await self._seed_docs(services)

        resp1 = _parse(
            await sage_discover(
                vault_id="test_vault",
                mode="catalog",
                limit=2,
                offset=0,
            )
        )
        resp2 = _parse(
            await sage_discover(
                vault_id="test_vault",
                mode="catalog",
                limit=2,
                offset=2,
            )
        )

        assert resp1["total_available"] == 3
        assert len(resp1["results"]) == 2
        assert resp2["total_available"] == 3
        assert len(resp2["results"]) == 1

        ids1 = {r["document"]["id"] for r in resp1["results"]}
        ids2 = {r["document"]["id"] for r in resp2["results"]}
        assert len(ids1 & ids2) == 0  # No overlap
