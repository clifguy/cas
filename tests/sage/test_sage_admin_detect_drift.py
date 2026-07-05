"""MCP tool tests for verify_vault_drift.

Exercises the boundary contract: vault_id shape validation, registry
membership check, and successful round-trip of the DriftReport payload
through the MCP serialize path. Service-layer detection semantics are
covered by tests/sage/test_maintenance_service.py.
"""

from __future__ import annotations

from sage import mcp_server
from sage.config import VaultConfig
from sage.mcp_init import initialize_services
from sage.models.schemas import DriftReport
from sage.services.vault_registry import VaultRegistryService
from tests.sage.conftest import initialize_services_for_test


async def test_sage_admin_detect_drift_returns_report_dict(minimal_vault_config_dict):
    """Happy path: an empty vault returns a DriftReport with zero entries
    that round-trips through the Pydantic model.

    The vault registers through the uninjected production path (real
    Postgres storage under the test-harness stack-config pin), with the
    registry service wired over ``mcp_server._vaults`` — the registry the
    MCP tool's get_vault reads — so the maintenance service the tool
    dispatches to is the one production construction builds.
    """
    config = VaultConfig.model_validate(minimal_vault_config_dict)
    registry_service = VaultRegistryService(mcp_server._vaults, initialize_services)
    async with initialize_services_for_test(config, registry_service=registry_service) as services:
        vault_id = config.vault.id
        mcp_server._vaults[vault_id] = services
        try:
            result = await mcp_server.verify_vault_drift(vault_id=vault_id)

            report = DriftReport.model_validate(result)
            assert report.vault_id == vault_id
            assert report.total_edges_walked == 0
            assert report.entries == []
            assert report.summary == {
                "content_drift": 0,
                "chain_advanced_no_content_change": 0,
                "recorded_null": 0,
                "chain_nonlinear": 0,
            }
        finally:
            mcp_server._vaults.pop(vault_id, None)


async def test_sage_admin_detect_drift_invalid_vault_id_shape_returns_error_envelope():
    """Whitespace + punctuation in vault_id fails the VaultIdStr adapter
    and surfaces as the structured invalid_vault_id (400) envelope carrying
    the offending value, not a raised exception."""
    result = await mcp_server.verify_vault_drift(vault_id="not a vault id!")

    assert isinstance(result, dict)
    assert "error" in result, f"expected error envelope, got {result!r}"
    assert result["error"] == "invalid_vault_id"
    assert result["detail"]["vault_id"] == "not a vault id!"


async def test_sage_admin_detect_drift_unknown_vault_returns_error_envelope():
    """An unregistered vault_id returns the unknown_vault envelope."""
    result = await mcp_server.verify_vault_drift(vault_id="ghost")

    assert isinstance(result, dict)
    assert result.get("error") == "unknown_vault", (
        f"expected unknown_vault envelope, got {result!r}"
    )
