"""MCP tool tests for verify_vault_source_files.

Exercises the boundary contract: vault_id shape validation, registry
membership check, and successful round-trip of the
SourceFileIntegrityReport payload through the MCP serialize path.
Service-layer detection semantics (missing files, hash mismatch, scope)
are covered by tests/sage/test_maintenance_service.py.
"""

from __future__ import annotations

from sage import mcp_server
from sage.config import VaultConfig
from sage.mcp_init import initialize_services
from sage.models.schemas import SourceFileIntegrityReport
from sage.services.vault_registry import VaultRegistryService
from tests.sage.conftest import initialize_services_for_test


async def test_verify_vault_source_files_returns_report_dict(minimal_vault_config_dict):
    """Happy path: an empty vault returns a SourceFileIntegrityReport with
    zero entries that round-trips through the Pydantic model.

    The vault registers through the uninjected production path (real
    Postgres storage under the test-harness stack-config pin), with the
    registry service wired over ``mcp_server._vaults`` — the registry the
    MCP tool's get_vault reads — so the maintenance service the tool
    dispatches to is the one production construction builds. The audit
    itself resolves the vault-source store from the active profile, which
    stays filesystem-backed against the config's storage_root.
    """
    config = VaultConfig.model_validate(minimal_vault_config_dict)
    registry_service = VaultRegistryService(mcp_server._vaults, initialize_services)
    async with initialize_services_for_test(config, registry_service=registry_service) as services:
        vault_id = config.vault.id
        mcp_server._vaults[vault_id] = services
        try:
            result = await mcp_server.verify_vault_source_files(vault_id=vault_id)

            report = SourceFileIntegrityReport.model_validate(result)
            assert report.vault_id == vault_id
            assert report.total_documents_checked == 0
            assert report.check_hashes is False
            assert report.entries == []
            assert report.summary == {
                "healthy": 0,
                "missing": 0,
                "hash_mismatch": 0,
                "symlinked": 0,
                "out_of_root": 0,
            }
        finally:
            mcp_server._vaults.pop(vault_id, None)


async def test_verify_vault_source_files_invalid_vault_id_shape_returns_error_envelope():
    """Whitespace + punctuation in vault_id fails the VaultIdStr adapter
    and surfaces as the structured invalid_vault_id (400) envelope carrying
    the offending value, not a raised exception."""
    result = await mcp_server.verify_vault_source_files(vault_id="not a vault id!")

    assert isinstance(result, dict)
    assert "error" in result, f"expected error envelope, got {result!r}"
    assert result["error"] == "invalid_vault_id"
    assert result["detail"]["vault_id"] == "not a vault id!"


async def test_verify_vault_source_files_unknown_vault_returns_error_envelope():
    """An unregistered vault_id returns the unknown_vault envelope."""
    result = await mcp_server.verify_vault_source_files(vault_id="ghost")

    assert isinstance(result, dict)
    assert result.get("error") == "unknown_vault", (
        f"expected unknown_vault envelope, got {result!r}"
    )


async def test_verify_vault_source_files_malformed_scope_id_returns_invalid_document_id():
    """A malformed id in ``document_ids`` fails the typed alias at the
    boundary and surfaces as ``invalid_document_id`` -- not a report, and not
    the unmatched-scope refusal, which is reserved for well-formed ids that
    name no document."""
    result = await mcp_server.verify_vault_source_files(
        vault_id="ghost", document_ids=["not a document id!"]
    )

    assert isinstance(result, dict)
    assert result.get("error") == "invalid_document_id", f"got {result!r}"


async def test_verify_vault_source_files_empty_scope_is_refused():
    """An empty ``document_ids`` is refused rather than audited: it would
    otherwise return a clean report over no documents at all."""
    result = await mcp_server.verify_vault_source_files(vault_id="ghost", document_ids=[])

    assert isinstance(result, dict)
    assert result.get("error") == "invalid_parameter", f"got {result!r}"
    assert result["detail"]["parameter"] == "document_ids"


async def test_verify_vault_source_files_unmatched_scope_returns_typed_refusal(
    minimal_vault_config_dict,
):
    """A well-formed id naming no document reaches the service through the
    tool and comes back as the typed refusal naming it.

    Anti-coincidental-pass: a tool that dropped ``document_ids`` on the way to
    the service would audit the (empty) vault and return a report, not this
    envelope.
    """
    config = VaultConfig.model_validate(minimal_vault_config_dict)
    registry_service = VaultRegistryService(mcp_server._vaults, initialize_services)
    async with initialize_services_for_test(config, registry_service=registry_service) as services:
        vault_id = config.vault.id
        mcp_server._vaults[vault_id] = services
        try:
            result = await mcp_server.verify_vault_source_files(
                vault_id=vault_id, document_ids=["deadbeef_absent"]
            )

            assert result.get("error") == "document_scope_unmatched", f"got {result!r}"
            assert result["detail"] == {"unmatched_ids": ["deadbeef_absent"]}
        finally:
            mcp_server._vaults.pop(vault_id, None)
