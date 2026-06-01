"""MCP tool tests for optimize_vault_content_store (CAS-ADR-029).

Exercises the boundary contract: vault_id shape validation, registry
membership check, and successful round-trip of the
OptimizeContentStoreReport payload through the MCP serialize path.

Test-file naming matches the existing test_sage_admin_*_tool.py
sibling pattern (test_sage_admin_migrate_vault.py,
test_sage_admin_detect_drift.py, test_sage_admin_reabstract_deferred_tool.py).
The test-file prefix is stale relative to the retired sage_admin_*
naming convention but renaming the four files is a separate hygiene
pass; the new test file matches the family for consistency.
"""

from __future__ import annotations

import pytest

from sage import mcp_server
from sage.models.schemas import OptimizeContentStoreReport
from tests.sage.test_maintenance_service import (
    _bootstrap_lancedb_vault,
    _churn_chunks,
    _close_registry_vault,
)


@pytest.fixture
def empty_registry(monkeypatch: pytest.MonkeyPatch) -> None:
    """Snapshot ``mcp_server._vaults`` before each test and restore after."""
    saved = dict(mcp_server._vaults)
    mcp_server._vaults.clear()
    try:
        yield
    finally:
        mcp_server._vaults.clear()
        mcp_server._vaults.update(saved)


async def test_optimize_vault_content_store_returns_report_dict(
    tmp_path, monkeypatch, empty_registry
):
    """Happy path: returns a dict that round-trips through
    OptimizeContentStoreReport."""
    async with _bootstrap_lancedb_vault(tmp_path, monkeypatch) as (
        registry,
        services,
        _vault_dir,
    ):
        vault_id = services.config.vault.id
        try:
            await _churn_chunks(services.content_store, cycles=15)
            mcp_server._vaults[vault_id] = registry[vault_id]

            result = await mcp_server.optimize_vault_content_store(
                vault_id=vault_id, cleanup_older_than_days=0
            )

            report = OptimizeContentStoreReport.model_validate(result)
            assert report.vault_id == vault_id
            assert report.cleanup_older_than_days == 0
            assert report.pre_versions > report.post_versions, "expected pruning on churned vault"
        finally:
            await _close_registry_vault(registry, vault_id)


async def test_optimize_vault_content_store_invalid_vault_id_shape_returns_error_envelope(
    empty_registry,
):
    """Whitespace + punctuation in vault_id fails the VaultIdStr adapter
    and surfaces as the structured invalid_vault_id (400) envelope carrying
    the offending value, not a raised exception."""
    result = await mcp_server.optimize_vault_content_store(vault_id="not a vault id!")

    assert isinstance(result, dict)
    assert "error" in result, f"expected error envelope, got {result!r}"
    assert result["error"] == "invalid_vault_id"
    assert result["detail"]["vault_id"] == "not a vault id!"


async def test_optimize_vault_content_store_unknown_vault_returns_error_envelope(
    empty_registry,
):
    """An unregistered vault_id returns the unknown_vault envelope."""
    result = await mcp_server.optimize_vault_content_store(vault_id="ghost")

    assert isinstance(result, dict)
    assert result.get("error") == "unknown_vault", (
        f"expected unknown_vault envelope, got {result!r}"
    )
