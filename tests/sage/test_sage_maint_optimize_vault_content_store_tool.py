"""MCP tool tests for optimize_vault_content_store (CAS-ADR-029).

Exercises the boundary contract: vault_id shape validation, registry
membership check, and successful round-trip of the
OptimizeContentStoreReport payload through the MCP serialize path. The
content store is a scripted snapshot stub — the tool-level contract is
report shaping around the ContentStore port; real reclamation is covered
by the content-store test modules.

Test-file naming matches the test_sage_maint_*_tool.py sibling pattern
(test_sage_maint_migrate_vault.py, test_sage_maint_detect_drift.py,
test_sage_maint_reabstract_deferred_tool.py).
"""

from __future__ import annotations

from sage import mcp_server
from sage.config import VaultConfig
from sage.mcp_init import initialize_services
from sage.models.schemas import OptimizeContentStoreReport
from sage.services.vault_registry import VaultRegistryService
from tests.sage.conftest import initialize_services_for_test
from tests.sage.test_maintenance_service import _SnapshotContentStore


async def test_optimize_vault_content_store_returns_report_dict(minimal_vault_config_dict):
    """Happy path: returns a dict that round-trips through
    OptimizeContentStoreReport, carrying the store's snapshot (the
    version shrinkage and byte delta come from the scripted snapshot, so
    a report built from constants rather than the store's observation
    fails here).
    """
    config = VaultConfig.model_validate(minimal_vault_config_dict)
    content_store = _SnapshotContentStore()
    registry_service = VaultRegistryService(mcp_server._vaults, initialize_services)
    async with initialize_services_for_test(
        config,
        registry_service=registry_service,
        content_store_factory=lambda _brain: content_store,
    ) as services:
        vault_id = config.vault.id
        mcp_server._vaults[vault_id] = services
        try:
            result = await mcp_server.optimize_vault_content_store(
                vault_id=vault_id, cleanup_older_than_days=0
            )

            report = OptimizeContentStoreReport.model_validate(result)
            assert report.vault_id == vault_id
            assert report.cleanup_older_than_days == 0
            assert report.pre_versions > report.post_versions, (
                "report must carry the store's snapshot"
            )
            assert report.bytes_reclaimed == report.pre_bytes - report.post_bytes
            # The threshold reached the store, not a tool-side default.
            assert len(content_store.optimize_calls) == 1
        finally:
            mcp_server._vaults.pop(vault_id, None)


async def test_optimize_vault_content_store_invalid_vault_id_shape_returns_error_envelope():
    """Whitespace + punctuation in vault_id fails the VaultIdStr adapter
    and surfaces as the structured invalid_vault_id (400) envelope carrying
    the offending value, not a raised exception."""
    result = await mcp_server.optimize_vault_content_store(vault_id="not a vault id!")

    assert isinstance(result, dict)
    assert "error" in result, f"expected error envelope, got {result!r}"
    assert result["error"] == "invalid_vault_id"
    assert result["detail"]["vault_id"] == "not a vault id!"


async def test_optimize_vault_content_store_unknown_vault_returns_error_envelope():
    """An unregistered vault_id returns the unknown_vault envelope."""
    result = await mcp_server.optimize_vault_content_store(vault_id="ghost")

    assert isinstance(result, dict)
    assert result.get("error") == "unknown_vault", (
        f"expected unknown_vault envelope, got {result!r}"
    )
