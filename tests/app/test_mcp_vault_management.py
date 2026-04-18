"""Tests for vault-management MCP tools (TEST-APP-MCP-030 through MCP-040).

Covers the three new MCP tools:
  - sage_create_vault (convenience + full-config modes)
  - sage_get_vault_config
  - sage_update_vault_config (with destructive-change force-gate)

Direct function calls bypassing MCP transport, matching the pattern in
tests/app/test_mcp_app_tools.py.
"""

import asyncio
from pathlib import Path

import pytest
import yaml

from sage.adapters.stubs import (
    StubAbstractionProvider,
    StubContentStore,
    StubEmbeddingProvider,
)
from sage.config import VaultConfig
from sage.mcp_init import initialize_services
import sage.mcp_server as _mcp

from sage.mcp_server import (
    sage_create_vault,
    sage_get_vault_config,
    sage_update_vault_config,
    sage_list_vaults,
    sage_ingest,
    sage_update_metadata,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _parse(result):
    return result


@pytest.fixture
def vaults_root(tmp_path, monkeypatch):
    """Redirect _VAULTS_ROOT in vault_management so tests don't touch ~/sage_vaults/."""
    root = tmp_path / "sage_vaults"
    root.mkdir()
    from sage import vault_management

    monkeypatch.setattr(vault_management, "_VAULTS_ROOT", root)
    return root


@pytest.fixture
async def empty_registry():
    """Empty vault registry for clean create/update tests."""
    saved = dict(_mcp._vaults)
    _mcp._vaults.clear()
    yield
    # Close anything left behind by the test
    for services in list(_mcp._vaults.values()):
        try:
            await services.graph_store.close()
        except Exception:
            pass
    _mcp._vaults.clear()
    _mcp._vaults.update(saved)


def _make_full_config_dict(vaults_root: Path, vault_id: str, name: str, owner: str) -> dict:
    """Build a full valid config dict anchored in vaults_root."""
    vault_dir = vaults_root / vault_id
    return {
        "vault": {
            "id": vault_id,
            "name": name,
            "owner": owner,
            "storage_root": str(vault_dir / "sources"),
            "brain_root": str(vault_dir / "brain"),
            "visibility": "personal",
        },
        "document_types": {
            "doc_types": [
                {"value": "note", "label": "Note"},
                {"value": "memo", "label": "Memo"},
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
        "metadata_extraction": {"review_required": False},
        "edge_inference": {
            "tier_assignments": [
                {
                    "edge_type": "supersedes",
                    "tier": 1,
                    "inference_rules": [{"method": "version_chain"}],
                },
            ],
        },
        "abstraction": {"enabled": False},
    }


@pytest.fixture
async def registered_vault(vaults_root, empty_registry, tmp_path):
    """Pre-create a vault via sage_create_vault and yield its config."""
    config = _make_full_config_dict(vaults_root, "test_vault", "Test Vault", "testuser")
    result = await sage_create_vault(config=config)
    assert "vault_id" in result, f"setup failed: {result}"
    yield config


# ---------------------------------------------------------------------------
# 1. sage_create_vault
# ---------------------------------------------------------------------------


class TestSageCreateVault:

    # TEST-APP-MCP-030
    async def test_mcp_030_convenience_mode_creates_vault_with_default_config(
        self, vaults_root, empty_registry
    ):
        """Convenience mode: vault_id + name + owner produces default config."""
        result = _parse(
            await sage_create_vault(
                vault_id="new_vault", name="New Vault", owner="testuser"
            )
        )

        assert result["vault_id"] == "new_vault"
        assert result["name"] == "New Vault"
        assert "storage_root" in result
        assert "config" in result

        # Echoed config must be a full, valid VaultConfig
        VaultConfig.model_validate(result["config"])

        # Registry contains the new vault
        vaults_list = _parse(await sage_list_vaults())
        ids = {v["id"] for v in vaults_list["vaults"]}
        assert "new_vault" in ids

        # YAML file written on disk
        yaml_path = vaults_root / "new_vault" / "vault_config.yaml"
        assert yaml_path.exists()
        on_disk = yaml.safe_load(yaml_path.read_text())
        assert on_disk["vault"]["id"] == "new_vault"
        assert on_disk["vault"]["name"] == "New Vault"

    # TEST-APP-MCP-031
    async def test_mcp_031_convenience_rejects_duplicate(
        self, vaults_root, empty_registry
    ):
        """Creating an already-registered vault id returns vault_already_exists."""
        await sage_create_vault(
            vault_id="existing", name="Existing", owner="testuser"
        )
        result = _parse(
            await sage_create_vault(vault_id="existing", name="x", owner="x")
        )
        assert result.get("error") == "vault_already_exists"

    # TEST-APP-MCP-032
    async def test_mcp_032_rejects_mixed_mode(self, vaults_root, empty_registry):
        """Passing both config and convenience args returns validation error."""
        cfg = _make_full_config_dict(vaults_root, "mixed", "Mixed", "testuser")
        result = _parse(
            await sage_create_vault(
                vault_id="mixed", name="Mixed", owner="testuser", config=cfg
            )
        )
        assert "error" in result
        # Not strict on exact code -- just that it's a validation-style error,
        # not a successful creation.
        assert "vault_id" not in result or result.get("error")
        # No vault actually created
        vaults_list = _parse(await sage_list_vaults())
        ids = {v["id"] for v in vaults_list["vaults"]}
        assert "mixed" not in ids

    # TEST-APP-MCP-033
    async def test_mcp_033_full_config_mode_creates_vault(
        self, vaults_root, empty_registry
    ):
        """Full-config mode accepts a complete dict."""
        cfg = _make_full_config_dict(vaults_root, "full_cfg", "Full Cfg", "testuser")
        result = _parse(await sage_create_vault(config=cfg))

        assert result["vault_id"] == "full_cfg"
        assert result["config"]["vault"]["id"] == "full_cfg"
        vaults_list = _parse(await sage_list_vaults())
        assert "full_cfg" in {v["id"] for v in vaults_list["vaults"]}

    # TEST-APP-MCP-034
    async def test_mcp_034_full_config_rejects_invalid(
        self, vaults_root, empty_registry
    ):
        """Invalid config dict returns vault_config_validation_error."""
        result = _parse(
            await sage_create_vault(config={"vault": {"id": "bad"}})
        )
        assert result.get("error") == "vault_config_validation_error"
        assert "errors" in result.get("detail", {})
        assert len(result["detail"]["errors"]) > 0
        # No vault directory created
        assert not (vaults_root / "bad").exists()


# ---------------------------------------------------------------------------
# 2. sage_get_vault_config
# ---------------------------------------------------------------------------


class TestSageGetVaultConfig:

    # TEST-APP-MCP-035
    async def test_mcp_035_returns_full_config_and_errors_on_unknown(
        self, registered_vault
    ):
        """Returns full config for known vault; error for unknown."""
        result = _parse(await sage_get_vault_config("test_vault"))
        for section in (
            "vault",
            "document_types",
            "lifecycle",
            "source_adapters",
            "metadata_extraction",
            "edge_inference",
        ):
            assert section in result
        assert result["vault"]["id"] == "test_vault"

        unknown = _parse(await sage_get_vault_config("does_not_exist"))
        assert unknown.get("error") == "unknown_vault"


# ---------------------------------------------------------------------------
# 3. sage_update_vault_config
# ---------------------------------------------------------------------------


class TestSageUpdateVaultConfig:

    # TEST-APP-MCP-036
    async def test_mcp_036_updates_section_preserves_others(self, registered_vault):
        """Section replacement leaves other sections byte-equal."""
        original = _parse(await sage_get_vault_config("test_vault"))

        result = _parse(
            await sage_update_vault_config(
                "test_vault",
                sections={
                    "document_types": {
                        "doc_types": [
                            {"value": "note", "label": "Note"},
                            {"value": "memo", "label": "Memo"},
                            {"value": "extra", "label": "Extra"},
                        ],
                    }
                },
            )
        )
        assert result["status"] == "updated"
        assert result["vault_id"] == "test_vault"
        assert result["warnings"] == []

        updated = _parse(await sage_get_vault_config("test_vault"))
        dt_values = [dt["value"] for dt in updated["document_types"]["doc_types"]]
        assert "extra" in dt_values
        # Other sections byte-equal
        for section in ("lifecycle", "source_adapters", "metadata_extraction", "edge_inference"):
            assert updated[section] == original[section]

    # TEST-APP-MCP-037
    async def test_mcp_037_blocks_destructive_change_without_force(
        self, registered_vault
    ):
        """Removing an in-use doc_type without force returns destructive_config_change."""
        # Ingest a document and set doc_type='note' via update_metadata
        sources = Path(registered_vault["vault"]["storage_root"])
        sources.mkdir(parents=True, exist_ok=True)
        (sources / "sample.md").write_text("# Sample\n\nContent.")
        ingest_result = await sage_ingest("test_vault", "sample.md", "markdown")
        await asyncio.sleep(0.2)
        await sage_update_metadata(
            "test_vault", ingest_result["id"], doc_type="note"
        )

        # Attempt destructive update
        result = _parse(
            await sage_update_vault_config(
                "test_vault",
                sections={
                    "document_types": {
                        "doc_types": [{"value": "memo", "label": "Memo"}],
                    }
                },
            )
        )
        assert result.get("error") == "destructive_config_change"
        warnings = result.get("detail", {}).get("warnings", [])
        assert any("note" in w for w in warnings)

        # On-disk config unchanged
        current = _parse(await sage_get_vault_config("test_vault"))
        dt_values = [dt["value"] for dt in current["document_types"]["doc_types"]]
        assert "note" in dt_values

    # TEST-APP-MCP-038
    async def test_mcp_038_force_true_proceeds_with_warnings(self, registered_vault):
        """force=True allows the destructive change; warnings returned."""
        sources = Path(registered_vault["vault"]["storage_root"])
        sources.mkdir(parents=True, exist_ok=True)
        (sources / "sample.md").write_text("# Sample\n\nContent.")
        ingest_result = await sage_ingest("test_vault", "sample.md", "markdown")
        await asyncio.sleep(0.2)
        await sage_update_metadata(
            "test_vault", ingest_result["id"], doc_type="note"
        )

        result = _parse(
            await sage_update_vault_config(
                "test_vault",
                sections={
                    "document_types": {
                        "doc_types": [{"value": "memo", "label": "Memo"}],
                    }
                },
                force=True,
            )
        )
        assert result["status"] == "updated"
        assert len(result["warnings"]) >= 1
        assert any("note" in w for w in result["warnings"])

        updated = _parse(await sage_get_vault_config("test_vault"))
        dt_values = [dt["value"] for dt in updated["document_types"]["doc_types"]]
        assert "note" not in dt_values
        assert "memo" in dt_values

    # TEST-APP-MCP-039
    async def test_mcp_039_rejects_vault_id_change_even_with_force(
        self, registered_vault
    ):
        """Changing vault.id is never allowed, regardless of force."""
        result = _parse(
            await sage_update_vault_config(
                "test_vault",
                sections={
                    "vault": {
                        "id": "different_id",
                        "name": "X",
                        "owner": "testuser",
                        "storage_root": "/tmp/x",
                        "brain_root": "/tmp/x",
                        "visibility": "personal",
                    }
                },
                force=True,
            )
        )
        assert result.get("error") == "vault_config_validation_error"
        assert any(
            "vault.id" in e.lower() or "vault_id" in e.lower()
            for e in result.get("detail", {}).get("errors", [])
        )

    # TEST-APP-MCP-040
    async def test_mcp_040_invalid_section_rejected(self, registered_vault):
        """Malformed section fails validation; no YAML write."""
        result = _parse(
            await sage_update_vault_config(
                "test_vault",
                sections={"lifecycle": {"states": "not_a_list"}},
            )
        )
        assert result.get("error") == "vault_config_validation_error"
        assert len(result.get("detail", {}).get("errors", [])) > 0
