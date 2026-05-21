"""MCP tool tests for sage_bulk_set_lifecycle (T-0087).

Exercises the boundary contract: vault_id and per-item shape validation,
registry membership check, and round-trip of the BulkLifecycleResponse
payload through the MCP serialize path.
"""

from __future__ import annotations

import pytest

from sage import mcp_server
from sage.adapters.stubs import StubContentStore
from sage.app import _initialize_services, create_app
from sage.config import VaultConfig
from sage.models.schemas import BulkLifecycleResponse
from tests.sage.test_lifecycle import _id, _make_doc


@pytest.fixture
def empty_registry(monkeypatch: pytest.MonkeyPatch):
    """Snapshot mcp_server._vaults before each test and restore after."""
    saved = dict(mcp_server._vaults)
    mcp_server._vaults.clear()
    try:
        yield
    finally:
        mcp_server._vaults.clear()
        mcp_server._vaults.update(saved)


@pytest.fixture
async def seeded_mcp_vault(minimal_vault_config_dict, monkeypatch, empty_registry):
    """Boot a vault, seed two documents, publish on mcp_server._vaults."""
    monkeypatch.setenv("SAGE_TEST_STUB_PROVIDERS", "1")
    config = VaultConfig.model_validate(minimal_vault_config_dict)
    app = create_app(config=config)
    await _initialize_services(
        app,
        config,
        content_store_factory=lambda _brain: StubContentStore(),
    )
    vault_id = config.vault.id
    services = app.state.vault_registry[vault_id]
    mcp_server._vaults[vault_id] = services

    seeded_ids = [_id("doc_mcp1"), _id("doc_mcp2")]
    for doc_id in seeded_ids:
        await services.graph_store.insert_document(_make_doc(doc_id))

    yield vault_id, seeded_ids

    await services.graph_store.close()


async def test_mcp_tool_round_trip_returns_dict_matching_response_model(seeded_mcp_vault):
    """The returned dict must deserialize cleanly as BulkLifecycleResponse."""
    vault_id, seeded_ids = seeded_mcp_vault
    items = [{"document_id": d, "action": "archive"} for d in seeded_ids]

    result = await mcp_server.sage_bulk_set_lifecycle(vault_id=vault_id, items=items)

    assert isinstance(result, dict)
    assert "error" not in result, f"unexpected error envelope: {result!r}"
    response = BulkLifecycleResponse.model_validate(result)
    assert response.total == 2
    assert response.success_count == 2


async def test_mcp_tool_invalid_vault_id_returns_error_envelope(empty_registry):
    """A vault_id that fails the VaultIdStr adapter surfaces as the error envelope."""
    result = await mcp_server.sage_bulk_set_lifecycle(vault_id="not a vault id!", items=[])

    assert isinstance(result, dict)
    assert "error" in result
    assert result["error"] == "internal_error"


async def test_mcp_tool_unknown_vault_returns_error_envelope(empty_registry):
    """A syntactically valid but unregistered vault_id surfaces unknown_vault."""
    result = await mcp_server.sage_bulk_set_lifecycle(vault_id="ghost", items=[])

    assert isinstance(result, dict)
    assert result.get("error") == "unknown_vault"


async def test_mcp_tool_items_validation_rejects_bad_shape(seeded_mcp_vault):
    """Malformed items (missing document_id) fail shape validation BEFORE
    any per-item work runs. No partial state changes occur."""
    vault_id, seeded_ids = seeded_mcp_vault
    bad_items = [
        {"document_id": seeded_ids[0], "action": "archive"},
        {"action": "archive"},  # missing document_id
    ]

    result = await mcp_server.sage_bulk_set_lifecycle(vault_id=vault_id, items=bad_items)

    assert isinstance(result, dict)
    assert "error" in result, f"expected validation error envelope, got {result!r}"

    # Anti-coincidental-pass: the well-formed item must NOT have been processed.
    # If shape validation had run lazily inside the loop, item 0 would have
    # committed before item 1 raised.
    services = mcp_server._vaults[vault_id]
    stored = await services.graph_store.get_document(seeded_ids[0])
    assert stored.lifecycle_status == "active", (
        "Up-front shape validation must reject the entire batch before any "
        "per-item work runs; item 0 must remain in its seeded state."
    )
