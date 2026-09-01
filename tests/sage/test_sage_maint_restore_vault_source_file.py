"""MCP tool tests for restore_vault_source_file.

Exercises the boundary contract: vault_id and document_id shape validation,
registry membership check, and successful round-trip of the
SourceFileRestoreReport payload through the MCP serialize path. Service-layer
repair semantics (drift detection, the already-intact short-circuit, target
resolution and its refusals) are covered by
tests/sage/test_maintenance_service.py.
"""

from __future__ import annotations

from pathlib import Path

from sage import mcp_server
from sage.config import VaultConfig
from sage.mcp_init import initialize_services
from sage.models.schemas import SourceFileRestoreReport
from sage.services.vault_registry import VaultRegistryService
from tests.sage.conftest import initialize_services_for_test


async def test_restore_vault_source_file_returns_report_dict(minimal_vault_config_dict, tmp_path):
    """Happy path: a drifted retained copy is repaired and the report
    round-trips through the Pydantic model.

    The vault registers through the uninjected production path, with the
    registry service wired over ``mcp_server._vaults`` — the registry the MCP
    tool's get_vault reads — so the maintenance service the tool dispatches to
    is the one production construction builds.

    Anti-coincidental-pass: the copy is genuinely drifted before the call
    (asserted), so this exercises the repair branch rather than the
    already-intact short-circuit, and the bytes on disk are asserted afterwards
    — a tool that returned a well-formed report without writing would pass a
    schema-only check.
    """
    config = VaultConfig.model_validate(minimal_vault_config_dict)
    registry_service = VaultRegistryService(mcp_server._vaults, initialize_services)
    async with initialize_services_for_test(config, registry_service=registry_service) as services:
        vault_id = config.vault.id
        mcp_server._vaults[vault_id] = services
        try:
            original = b"# Restore probe\n\nOriginal body.\n"
            delivered = tmp_path / "operator_copy" / "restore_probe.md"
            delivered.parent.mkdir(parents=True, exist_ok=True)
            delivered.write_bytes(original)

            ingested = await mcp_server.ingest_document(
                vault_id=vault_id, source=str(delivered), source_type="markdown"
            )
            assert "error" not in ingested, ingested
            retained = Path(config.vault.storage_root) / ingested["source_path"]
            retained.write_bytes(b"something else wrote here")
            assert retained.read_bytes() != original

            result = await mcp_server.restore_vault_source_file(
                vault_id=vault_id, source=str(delivered)
            )

            report = SourceFileRestoreReport.model_validate(result)
            assert report.vault_id == vault_id
            assert report.document_id == ingested["id"]
            assert report.source_path == ingested["source_path"]
            assert report.restored is True
            assert report.status == "restored"
            assert retained.read_bytes() == original
        finally:
            mcp_server._vaults.pop(vault_id, None)


async def test_restore_vault_source_file_invalid_vault_id_shape_returns_error_envelope(tmp_path):
    """Whitespace + punctuation in vault_id fails the VaultIdStr adapter and
    surfaces as the structured invalid_vault_id (400) envelope carrying the
    offending value, not a raised exception."""
    result = await mcp_server.restore_vault_source_file(
        vault_id="not a vault id!", source=str(tmp_path / "absent.md")
    )

    assert isinstance(result, dict)
    assert "error" in result, f"expected error envelope, got {result!r}"
    assert result["error"] == "invalid_vault_id"
    assert result["detail"]["vault_id"] == "not a vault id!"


async def test_restore_vault_source_file_invalid_document_id_shape_returns_error_envelope(
    tmp_path,
):
    """A malformed document_id pin fails the DocumentIdStr adapter and surfaces
    as a structured envelope rather than reaching the service.

    Validated at the boundary before the vault is resolved, so a bad pin cannot
    reach a repair that would then have to decide what to do with it.
    """
    result = await mcp_server.restore_vault_source_file(
        vault_id="ghost", source=str(tmp_path / "absent.md"), document_id="not a doc id!"
    )

    assert isinstance(result, dict)
    assert "error" in result, f"expected error envelope, got {result!r}"
    assert result["error"] == "invalid_document_id"


async def test_restore_vault_source_file_unknown_vault_returns_error_envelope(tmp_path):
    """An unregistered vault_id returns the unknown_vault envelope."""
    result = await mcp_server.restore_vault_source_file(
        vault_id="ghost", source=str(tmp_path / "absent.md")
    )

    assert isinstance(result, dict)
    assert result.get("error") == "unknown_vault", (
        f"expected unknown_vault envelope, got {result!r}"
    )
