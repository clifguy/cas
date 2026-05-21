"""MCP tool tests for sage_admin_migrate_vault (T-0086, CAS-ADR-029).

Exercises the boundary contract: vault_id shape validation, registry
membership check, and successful round-trip of the MigrationReport
payload through the MCP serialize path.
"""

from __future__ import annotations

import pytest

from sage import mcp_server
from sage.models.schemas import MigrationReport
from tests.sage.test_maintenance_service import (
    _bootstrap_post_migration_vault,
    _close_registry_vault,
    _swap_in_legacy_db,
)


@pytest.fixture
def empty_registry(monkeypatch: pytest.MonkeyPatch) -> None:
    """Snapshot _vaults before each test and restore after."""
    saved = dict(mcp_server._vaults)
    mcp_server._vaults.clear()
    try:
        yield
    finally:
        mcp_server._vaults.clear()
        mcp_server._vaults.update(saved)


async def test_sage_admin_migrate_vault_returns_report_dict(tmp_path, monkeypatch, empty_registry):
    """Happy path: returns a dict that round-trips through MigrationReport."""
    # Build the same registry/services structure as the service tests, then
    # publish it on mcp_server._vaults so the MCP tool's get_vault finds it.
    registry, services, registry_service = await _bootstrap_post_migration_vault(
        tmp_path, monkeypatch
    )
    vault_id = services.config.vault.id
    try:
        _db_path, _maintenance = await _swap_in_legacy_db(registry, services, registry_service)
        # mcp_server._vaults IS the registry that mcp_server._get_vault reads.
        # Mirror the entry we just built so the tool sees it.
        mcp_server._vaults[vault_id] = registry[vault_id]

        result = await mcp_server.sage_admin_migrate_vault(vault_id=vault_id)

        # The tool returns a dict that must validate cleanly as a MigrationReport.
        report = MigrationReport.model_validate(result)
        assert report.vault_id == vault_id
        assert len(report.columns_added) > 0, "expected pending alters on legacy DB"
    finally:
        # T-0135: close the post-migration graph_store currently bound to
        # the local registry; the in-tool reload swapped a fresh
        # SAGEServices in here whose graph_store would otherwise leak.
        await _close_registry_vault(registry, vault_id)


async def test_sage_admin_migrate_vault_invalid_vault_id_shape_returns_error_envelope(
    empty_registry,
):
    """Whitespace + punctuation in vault_id fails the VaultIdStr adapter
    and surfaces as a standard error envelope, not a raised exception."""
    result = await mcp_server.sage_admin_migrate_vault(vault_id="not a vault id!")

    assert isinstance(result, dict)
    assert "error" in result, f"expected error envelope, got {result!r}"
    # ValidationError funnels through `_error_response` -> internal_error.
    assert result["error"] == "internal_error"


async def test_sage_admin_migrate_vault_unknown_vault_returns_error_envelope(
    empty_registry,
):
    """An unregistered vault_id returns the unknown_vault envelope."""
    result = await mcp_server.sage_admin_migrate_vault(vault_id="ghost")

    assert isinstance(result, dict)
    assert result.get("error") == "unknown_vault", (
        f"expected unknown_vault envelope, got {result!r}"
    )
