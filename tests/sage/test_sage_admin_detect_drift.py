"""MCP tool tests for sage_admin_detect_drift (T-0111).

Exercises the boundary contract: vault_id shape validation, registry
membership check, and successful round-trip of the DriftReport payload
through the MCP serialize path. Service-layer detection semantics are
covered by tests/sage/test_maintenance_service.py.
"""

from __future__ import annotations

import pytest

from sage import mcp_server
from sage.models.schemas import DriftReport
from tests.sage.test_maintenance_service import (
    _bootstrap_post_migration_vault,
    _close_registry_vault,
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


async def test_sage_admin_detect_drift_returns_report_dict(tmp_path, monkeypatch, empty_registry):
    """Happy path: an empty vault returns a DriftReport with zero entries
    that round-trips through the Pydantic model."""
    registry, services, registry_service = await _bootstrap_post_migration_vault(
        tmp_path, monkeypatch
    )
    vault_id = services.config.vault.id
    try:
        mcp_server._vaults[vault_id] = registry[vault_id]

        result = await mcp_server.sage_admin_detect_drift(vault_id=vault_id)

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
        await _close_registry_vault(registry, vault_id)


async def test_sage_admin_detect_drift_invalid_vault_id_shape_returns_error_envelope(
    empty_registry,
):
    """Whitespace + punctuation in vault_id fails the VaultIdStr adapter
    and surfaces as a standard error envelope, not a raised exception."""
    result = await mcp_server.sage_admin_detect_drift(vault_id="not a vault id!")

    assert isinstance(result, dict)
    assert "error" in result, f"expected error envelope, got {result!r}"
    assert result["error"] == "internal_error"


async def test_sage_admin_detect_drift_unknown_vault_returns_error_envelope(
    empty_registry,
):
    """An unregistered vault_id returns the unknown_vault envelope."""
    result = await mcp_server.sage_admin_detect_drift(vault_id="ghost")

    assert isinstance(result, dict)
    assert result.get("error") == "unknown_vault", (
        f"expected unknown_vault envelope, got {result!r}"
    )
