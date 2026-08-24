"""MCP tool tests for migrate_vault (CAS-ADR-029).

Exercises the boundary contract: vault_id shape validation, registry
membership check, and successful round-trip of the MigrationReport
payload through the MCP serialize path.
"""

from __future__ import annotations

from sage import mcp_server
from sage.config import VaultConfig
from sage.mcp_init import initialize_services
from sage.models.schemas import MigrationReport
from sage.services.vault_registry import VaultRegistryService
from tests.sage.conftest import initialize_services_for_test


async def test_sage_maint_migrate_vault_returns_report_dict(minimal_vault_config_dict):
    """Happy path: returns a dict that round-trips through MigrationReport
    as the Postgres no-op report, with both tier3 keys present.

    The vault registers through the uninjected production path, so the
    maintenance service resolves ``storage_backend`` from the pinned stack
    config (Postgres) exactly as the running server does — a report claiming
    column or backfill work here would mean the embedded-backend detect path
    ran against a store that is not SQLite.
    """
    config = VaultConfig.model_validate(minimal_vault_config_dict)
    # mcp_server._vaults IS the registry that mcp_server's get_vault reads;
    # wire the registry service over it so the tool-side maintenance service
    # is constructed (it requires a registry service).
    registry_service = VaultRegistryService(mcp_server._vaults, initialize_services)
    async with initialize_services_for_test(config, registry_service=registry_service) as services:
        vault_id = config.vault.id
        mcp_server._vaults[vault_id] = services
        try:
            result = await mcp_server.migrate_vault(vault_id=vault_id)

            assert isinstance(result, dict)
            assert "error" not in result, f"expected report, got {result!r}"
            # The tool returns a dict that must validate cleanly as a
            # MigrationReport.
            report = MigrationReport.model_validate(result)
            assert report.vault_id == vault_id
            assert report.columns_added == []
            assert report.backfills_applied == []
            # Both tier3 keys are present even on the no-op path.
            assert "tier3_uniqueness_activations" in result
            assert "tier3_uniqueness_collisions" in result
        finally:
            mcp_server._vaults.pop(vault_id, None)


async def test_sage_maint_migrate_vault_invalid_vault_id_shape_returns_error_envelope():
    """Whitespace + punctuation in vault_id fails the VaultIdStr adapter
    and surfaces as the structured invalid_vault_id (400) envelope carrying
    the offending value, not a raised exception."""
    result = await mcp_server.migrate_vault(vault_id="not a vault id!")

    assert isinstance(result, dict)
    assert "error" in result, f"expected error envelope, got {result!r}"
    # The VaultIdStr ValidationError funnels through `_error_response` and is
    # relabeled to the structured invalid_vault_id (400) family code.
    assert result["error"] == "invalid_vault_id"
    assert result["detail"]["vault_id"] == "not a vault id!"


async def test_sage_maint_migrate_vault_unknown_vault_returns_error_envelope():
    """An unregistered vault_id returns the unknown_vault envelope."""
    result = await mcp_server.migrate_vault(vault_id="ghost")

    assert isinstance(result, dict)
    assert result.get("error") == "unknown_vault", (
        f"expected unknown_vault envelope, got {result!r}"
    )
