"""Tests for vault-management MCP tools (TEST-APP-MCP-030 through MCP-040).

Covers the three vault-management MCP tools:
  - create_vault (single config-dict argument)
  - get_vault_config
  - update_vault_config (with destructive-change force-gate)

Direct function calls bypassing MCP transport, matching the pattern in
tests/app/test_mcp_app_tools.py.
"""

import asyncio
from pathlib import Path

import pytest
import yaml

import sage.mcp_server as _mcp
from sage.config import VaultConfig
from sage.mcp_server import (
    create_vault,
    get_vault_config,
    ingest_document,
    list_vaults,
    update_vault_config,
)
from sage.mcp_server import (
    update_metadata as _update_metadata_bulk,
)
from sage.services.vault_registry import VaultRegistryService


async def update_metadata(vault_id, document_id, **kwargs):
    """Singleton-shaped shim around the post-CAS-ADR-029 consolidated tool.

    Wraps the call as a length-1 ``items`` collection and unwraps the
    per-item result envelope back to a singleton-shape response so
    existing test assertions continue to apply.
    """
    dry_run = kwargs.pop("dry_run", False)
    item = {"document_id": document_id, **kwargs}
    result = await _update_metadata_bulk(vault_id=vault_id, items=[item], dry_run=dry_run)
    if isinstance(result, dict) and "error" in result and "results" not in result:
        return result
    if isinstance(result, dict) and result.get("results"):
        per = result["results"][0]
        if per.get("status") == "error":
            err = per.get("error") or {}
            return {
                "error": err.get("error"),
                "message": err.get("message"),
                "detail": err.get("detail"),
            }
        return {"document": per.get("document"), "dry_run": dry_run}
    return result


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
            services.close_timing()
            await services.graph_store.close()
        except Exception:  # noqa: S110 -- teardown cleanup; close errors must not fail the test
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
        "metadata_extraction": {},
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
    """Pre-create a vault via create_vault and yield its config."""
    config = _make_full_config_dict(vaults_root, "test_vault", "Test Vault", "testuser")
    result = await create_vault(config=config)
    assert "vault_id" in result, f"setup failed: {result}"
    yield config


# ---------------------------------------------------------------------------
# 1. create_vault
# ---------------------------------------------------------------------------


class TestSageCreateVault:
    # TEST-APP-MCP-030
    async def test_mcp_030_default_config_creates_vault(self, vaults_root, empty_registry):
        """``VaultRegistryService.get_default_config`` produces a config dict
        that ``create_vault`` accepts without further hand-tuning."""
        default_cfg = VaultRegistryService.get_default_config("new_vault", "New Vault", "testuser")
        result = _parse(await create_vault(config=default_cfg))

        assert result["vault_id"] == "new_vault"
        assert result["name"] == "New Vault"
        assert "config" in result

        # Echoed config must be a full, valid VaultConfig
        VaultConfig.model_validate(result["config"])

        # Registry contains the new vault
        vaults_list = _parse(await list_vaults())
        ids = {v["id"] for v in vaults_list["vaults"]}
        assert "new_vault" in ids

        # YAML file written on disk
        yaml_path = vaults_root / "new_vault" / "vault_config.yaml"
        assert yaml_path.exists()
        on_disk = yaml.safe_load(yaml_path.read_text())
        assert on_disk["vault"]["id"] == "new_vault"
        assert on_disk["vault"]["name"] == "New Vault"
        # The reported storage root must match the config the vault actually
        # uses -- the YAML the create wrote -- not the dict the tool echoes
        # (which is the caller's own object, so equality with it holds for
        # any wiring) nor the dict the test sent (same object).
        assert result["storage_root"] == on_disk["vault"]["storage_root"]

    # TEST-APP-MCP-031
    async def test_mcp_031_rejects_duplicate(self, vaults_root, empty_registry):
        """Creating an already-registered vault id returns vault_already_exists."""
        cfg = VaultRegistryService.get_default_config("existing", "Existing", "testuser")
        await create_vault(config=cfg)
        cfg_again = VaultRegistryService.get_default_config("existing", "Existing Again", "x")
        result = _parse(await create_vault(config=cfg_again))
        assert result.get("error") == "vault_already_exists"

    # TEST-APP-MCP-033
    async def test_mcp_033_full_config_mode_creates_vault(self, vaults_root, empty_registry):
        """A complete config dict creates a vault matching the dict's identity."""
        cfg = _make_full_config_dict(vaults_root, "full_cfg", "Full Cfg", "testuser")
        result = _parse(await create_vault(config=cfg))

        assert result["vault_id"] == "full_cfg"
        assert result["config"]["vault"]["id"] == "full_cfg"
        vaults_list = _parse(await list_vaults())
        assert "full_cfg" in {v["id"] for v in vaults_list["vaults"]}

    # TEST-APP-MCP-034
    async def test_mcp_034_full_config_rejects_invalid(self, vaults_root, empty_registry):
        """Invalid config dict returns vault_config_validation_error."""
        result = _parse(await create_vault(config={"vault": {"id": "bad"}}))
        assert result.get("error") == "vault_config_validation_error"
        assert "errors" in result.get("detail", {})
        assert len(result["detail"]["errors"]) > 0
        # No vault directory created
        assert not (vaults_root / "bad").exists()

    async def test_create_vault_populates_config_path_on_services(
        self, vaults_root, empty_registry
    ):
        """/F10 regression — create_vault must populate
        ``SAGEServices.config_path`` so a subsequent ``reload_vault`` after
        an on-disk YAML edit re-reads from disk instead of silently no-op'ing
        on the in-memory ``VaultConfig``.
        """
        cfg = _make_full_config_dict(vaults_root, "cp_vault", "CP Vault", "testuser")
        result = await create_vault(config=cfg)
        assert "vault_id" in result, f"setup failed: {result}"

        services = _mcp._vaults["cp_vault"]
        expected_path = vaults_root / "cp_vault" / "vault_config.yaml"
        assert services.config_path == expected_path


# ---------------------------------------------------------------------------
# 2. get_vault_config
# ---------------------------------------------------------------------------


class TestSageGetVaultConfig:
    # TEST-APP-MCP-035
    async def test_mcp_035_returns_full_config_and_errors_on_unknown(self, registered_vault):
        """Returns full config for known vault; error for unknown.

        Also asserts (CAS-ADR-030 /) that the vault-config response
        no longer carries `abstraction.provider` or `abstraction.model`;
        those moved to stack scope. The `get_stack_config` MCP tool
        is the canonical surface for them.
        """
        result = _parse(await get_vault_config("test_vault"))
        for section in (
            "vault",
            "document_types",
            "lifecycle",
            "metadata_extraction",
            "edge_inference",
        ):
            assert section in result
        assert result["vault"]["id"] == "test_vault"

        abstraction = result.get("abstraction", {})
        assert "provider" not in abstraction
        assert "model" not in abstraction

        unknown = _parse(await get_vault_config("does_not_exist"))
        assert unknown.get("error") == "unknown_vault"

    async def test_mcp_get_stack_config_returns_provider_and_model(self, registered_vault):
        """`get_stack_config` surfaces the stack-wide abstraction config
        (CAS-ADR-030). Shape: `{"abstraction": {"provider":..., "model":...}}`.
        """
        from sage.mcp_server import get_stack_config

        result = _parse(await get_stack_config())
        assert "abstraction" in result
        assert "provider" in result["abstraction"]
        assert "model" in result["abstraction"]
        # The provider must be one of the documented enum values.
        assert result["abstraction"]["provider"] in {"local-mlx", "anthropic", "stub"}

    async def test_mcp_document_responses_do_not_leak_abstraction_provider_or_model(
        self, registered_vault
    ):
        """No document-path response leaks the abstraction provider or model
        identity (CAS-ADR-030). The provider/model are operator-facing config,
        surfaced only by ``get_stack_config`` -- callers triaging documents
        must never see which producer made an abstract (producers are
        fungible; provenance carries no caller-relevant signal).

        Anti-coincidental-pass: the stack config is set to sentinel values
        (``provider="anthropic"``, ``model="claude-haiku-4-5"``) and the
        POSITIVE CONTROL asserts ``get_stack_config`` *does* surface both --
        proving the tokens are configured and detectable. The four
        document-path responses (``ingest_document``, ``get_document``,
        ``search``, ``get_vault_config``) must then contain neither token, so
        the absence assertions cannot pass merely because the tokens were
        never present.
        """
        import json as _json

        import sage.mcp_init as _mcp_init
        from sage.config import SageCoreConfig, StackAbstractionConfig
        from sage.mcp_server import get_document, get_stack_config, search

        PROVIDER_TOKEN = "anthropic"
        MODEL_TOKEN = "claude-haiku-4-5"

        saved = _mcp_init.get_stack_config()
        _mcp_init.set_stack_config(
            SageCoreConfig(
                abstraction=StackAbstractionConfig(provider=PROVIDER_TOKEN, model=MODEL_TOKEN)
            )
        )
        try:
            # Positive control: get_stack_config surfaces both tokens.
            stack = _parse(await get_stack_config())
            stack_blob = _json.dumps(stack)
            assert PROVIDER_TOKEN in stack_blob
            assert MODEL_TOKEN in stack_blob

            # Ingest a document, then exercise the four document-path tools.
            sources = Path(registered_vault["vault"]["storage_root"])
            sources.mkdir(parents=True, exist_ok=True)
            (sources / "sample.md").write_text("# Sample\n\nContent for triage.")
            ingest_result = _parse(await ingest_document("test_vault", "sample.md", "markdown"))
            await asyncio.sleep(0.2)
            doc_id = ingest_result["id"]

            doc_result = _parse(await get_document("test_vault", doc_id))
            search_result = _parse(await search("test_vault", query="Content", mode="keyword"))
            vault_cfg_result = _parse(await get_vault_config("test_vault"))

            for label, response in (
                ("ingest_document", ingest_result),
                ("get_document", doc_result),
                ("search", search_result),
                ("get_vault_config", vault_cfg_result),
            ):
                blob = _json.dumps(response)
                assert PROVIDER_TOKEN not in blob, f"{label} response leaks the provider identity"
                assert MODEL_TOKEN not in blob, f"{label} response leaks the model identity"
        finally:
            _mcp_init.set_stack_config(saved)

    async def test_mcp_get_stack_config_surfaces_profile(self, registered_vault):
        """`get_stack_config` surfaces the active deployment-profile marker
        (CAS-ADR-042) additively, alongside the abstraction section.

        Additive only — does not assert provider/model absent, so it stays
        compatible with the abstraction config that ships beside it.
        """
        from sage.mcp_server import get_stack_config

        result = _parse(await get_stack_config())
        assert "profile" in result
        assert result["profile"] == "local"


# ---------------------------------------------------------------------------
# 3. update_vault_config
# ---------------------------------------------------------------------------


class TestSageUpdateVaultConfig:
    # TEST-APP-MCP-036
    async def test_mcp_036_updates_section_preserves_others(self, registered_vault):
        """Section replacement leaves other sections byte-equal."""
        original = _parse(await get_vault_config("test_vault"))

        result = _parse(
            await update_vault_config(
                "test_vault",
                document_types={
                    "doc_types": [
                        {"value": "note", "label": "Note"},
                        {"value": "memo", "label": "Memo"},
                        {"value": "extra", "label": "Extra"},
                    ],
                },
            )
        )
        assert result["status"] == "updated"
        assert result["vault_id"] == "test_vault"
        assert result["warnings"] == []

        updated = _parse(await get_vault_config("test_vault"))
        dt_values = [dt["value"] for dt in updated["document_types"]["doc_types"]]
        assert "extra" in dt_values
        # Other sections byte-equal
        for section in ("lifecycle", "metadata_extraction", "edge_inference"):
            assert updated[section] == original[section]

    async def test_update_rewrite_drops_an_ignored_legacy_section(
        self, registered_vault, vaults_root
    ):
        """A stale ``source_adapters`` section disappears on the first write.

        ``update_config`` rebuilds the persisted YAML from the validated
        model, so the section a pre-CAS-ADR-046 config still carries is
        dropped by any update rather than needing a hand edit of a file
        inside the vault tree. That is what makes the operational
        migration a single tool call per vault.
        """
        config_path = Path(vaults_root) / "test_vault" / "vault_config.yaml"
        raw = yaml.safe_load(config_path.read_text())
        raw["source_adapters"] = {
            "adapters": [{"source_type": "markdown", "enabled": False}],
        }
        config_path.write_text(yaml.dump(raw, sort_keys=False))
        assert "source_adapters" in yaml.safe_load(config_path.read_text())

        result = _parse(
            await update_vault_config(
                "test_vault",
                adapter_defaults={"docx": {"heading_style_map": {"Custom Section": 1}}},
            )
        )
        assert result["status"] == "updated"

        written = yaml.safe_load(config_path.read_text())
        assert "source_adapters" not in written
        assert written["adapter_defaults"] == {"docx": {"heading_style_map": {"Custom Section": 1}}}

    # TEST-APP-MCP-037
    async def test_mcp_037_blocks_destructive_change_without_force(self, registered_vault):
        """Removing an in-use doc_type without force returns destructive_config_change."""
        # Ingest a document and set doc_type='note' via update_metadata
        sources = Path(registered_vault["vault"]["storage_root"])
        sources.mkdir(parents=True, exist_ok=True)
        (sources / "sample.md").write_text("# Sample\n\nContent.")
        ingest_result = await ingest_document("test_vault", "sample.md", "markdown")
        await asyncio.sleep(0.2)
        await update_metadata("test_vault", ingest_result["id"], doc_type="note")

        # Attempt destructive update
        result = _parse(
            await update_vault_config(
                "test_vault",
                document_types={
                    "doc_types": [{"value": "memo", "label": "Memo"}],
                },
            )
        )
        assert result.get("error") == "destructive_config_change"
        warnings = result.get("detail", {}).get("warnings", [])
        assert any("note" in w for w in warnings)

        # On-disk config unchanged
        current = _parse(await get_vault_config("test_vault"))
        dt_values = [dt["value"] for dt in current["document_types"]["doc_types"]]
        assert "note" in dt_values

    # TEST-APP-MCP-038
    async def test_mcp_038_force_true_proceeds_with_warnings(self, registered_vault):
        """force=True allows the destructive change; warnings returned."""
        sources = Path(registered_vault["vault"]["storage_root"])
        sources.mkdir(parents=True, exist_ok=True)
        (sources / "sample.md").write_text("# Sample\n\nContent.")
        ingest_result = await ingest_document("test_vault", "sample.md", "markdown")
        await asyncio.sleep(0.2)
        await update_metadata("test_vault", ingest_result["id"], doc_type="note")

        result = _parse(
            await update_vault_config(
                "test_vault",
                document_types={
                    "doc_types": [{"value": "memo", "label": "Memo"}],
                },
                force=True,
            )
        )
        assert result["status"] == "updated"
        assert len(result["warnings"]) >= 1
        assert any("note" in w for w in result["warnings"])

        updated = _parse(await get_vault_config("test_vault"))
        dt_values = [dt["value"] for dt in updated["document_types"]["doc_types"]]
        assert "note" not in dt_values
        assert "memo" in dt_values

    # TEST-APP-MCP-039
    async def test_mcp_039_rejects_vault_id_change_even_with_force(self, registered_vault):
        """Changing vault.id is never allowed, regardless of force."""
        result = _parse(
            await update_vault_config(
                "test_vault",
                vault={
                    "id": "different_id",
                    "name": "X",
                    "owner": "testuser",
                    "storage_root": "/tmp/x",
                    "brain_root": "/tmp/x",
                    "visibility": "personal",
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
            await update_vault_config(
                "test_vault",
                lifecycle={"states": "not_a_list"},
            )
        )
        assert result.get("error") == "vault_config_validation_error"
        assert len(result.get("detail", {}).get("errors", [])) > 0
