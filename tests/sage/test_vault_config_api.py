"""API integration tests for vault configuration endpoints.

Tests the GET/PUT config and POST create-vault endpoints added to
the vaults router. Uses the same app/client fixture pattern as
test_api_integration.py.
"""

import asyncio

import pytest
from httpx import ASGITransport, AsyncClient

from sage.app import _initialize_services, create_app
from sage.config import VaultConfig
from sage.services.vault_registry import VaultRegistryService


@pytest.fixture
async def app(minimal_vault_config_dict, tmp_vault_dir):
    """Create a FastAPI app with test config."""
    config = VaultConfig.model_validate(minimal_vault_config_dict)
    app = create_app(config=config)
    await _initialize_services(app, config)
    yield app
    await asyncio.sleep(0.1)
    for services in app.state.vault_registry.values():
        await services.graph_store.close()


@pytest.fixture
async def client(app):
    """Async HTTP client for the SAGE API."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


# ---------------------------------------------------------------------------
# GET /sage_vaults/{vault_id}/config
# ---------------------------------------------------------------------------


async def test_get_config_200(client):
    """GET returns full config with all required sections."""
    resp = await client.get("/sage_vaults/test_vault/config")
    assert resp.status_code == 200
    body = resp.json()
    for section in (
        "vault",
        "document_types",
        "lifecycle",
        "source_adapters",
        "metadata_extraction",
        "edge_inference",
    ):
        assert section in body, f"Missing section: {section}"
    assert body["vault"]["id"] == "test_vault"


async def test_get_config_404_unknown_vault(client):
    """GET with unknown vault_id returns 404."""
    resp = await client.get("/sage_vaults/nonexistent/config")
    assert resp.status_code == 404
    assert resp.json()["code"] == "vault_not_found"


# ---------------------------------------------------------------------------
# PUT /sage_vaults/{vault_id}/config
# ---------------------------------------------------------------------------


async def test_update_config_identity_200(client, tmp_vault_dir):
    """Update vault name via identity section."""
    resp = await client.put(
        "/sage_vaults/test_vault/config",
        json={
            "vault": {
                "id": "test_vault",
                "name": "Renamed Vault",
                "owner": "testuser",
                "storage_root": str(tmp_vault_dir / "sources"),
                "brain_root": str(tmp_vault_dir / "brain"),
                "visibility": "personal",
            }
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "updated"
    assert body["vault_id"] == "test_vault"

    # Verify config was updated in registry
    get_resp = await client.get("/sage_vaults/test_vault/config")
    assert get_resp.json()["vault"]["name"] == "Renamed Vault"


async def test_update_config_doc_types_200(client):
    """Add a new doc_type to the existing list."""
    resp = await client.put(
        "/sage_vaults/test_vault/config",
        json={
            "document_types": {
                "doc_types": [
                    {"value": "note", "label": "Note"},
                    {"value": "memo", "label": "Memo"},
                    {"value": "report", "label": "Report"},
                ],
            }
        },
    )
    assert resp.status_code == 200

    get_resp = await client.get("/sage_vaults/test_vault/config")
    dt_values = [dt["value"] for dt in get_resp.json()["document_types"]["doc_types"]]
    assert "report" in dt_values


async def test_update_config_400_invalid(client):
    """Malformed config section returns 400 with validation errors."""
    resp = await client.put(
        "/sage_vaults/test_vault/config",
        json={"lifecycle": {"states": "not_a_list"}},
    )
    assert resp.status_code == 400
    assert resp.json()["code"] == "vault_config_validation_error"
    assert len(resp.json()["detail"]["errors"]) > 0


async def test_update_config_400_id_change_rejected(client):
    """Changing vault.id returns 400."""
    resp = await client.put(
        "/sage_vaults/test_vault/config",
        json={
            "vault": {
                "id": "different_id",
                "name": "Test",
                "owner": "testuser",
                "storage_root": "/tmp/x",
                "brain_root": "/tmp/x",
                "visibility": "personal",
            }
        },
    )
    assert resp.status_code == 400
    assert (
        "vault.id" in resp.json()["detail"]["errors"][0].lower()
        or "vault_config_validation_error" == resp.json()["code"]
    )


async def _ingest_note_doc(client, tmp_vault_dir):
    """Helper: ingest a document with doc_type 'note'."""
    sources = tmp_vault_dir / "sources"
    test_dir = sources / "test"
    test_dir.mkdir(parents=True, exist_ok=True)
    (test_dir / "sample.md").write_text("# Test\n\nContent.")
    ingest_resp = await client.post(
        "/sage_vaults/test_vault/documents",
        json={
            "source": "test/sample.md",
            "source_type": "markdown",
            "metadata": {"doc_type": "note"},
        },
    )
    assert ingest_resp.status_code == 201


# TEST-SAGE-VM-REST-001
async def test_update_config_destructive_without_force_returns_409(client, tmp_vault_dir):
    """Removing an in-use doc_type without force returns 409 + error."""
    await _ingest_note_doc(client, tmp_vault_dir)

    resp = await client.put(
        "/sage_vaults/test_vault/config",
        json={
            "document_types": {
                "doc_types": [
                    {"value": "memo", "label": "Memo"},
                ],
            }
        },
    )
    assert resp.status_code == 409
    body = resp.json()
    assert body["code"] == "destructive_config_change"
    warnings = body.get("detail", {}).get("warnings", [])
    assert any("note" in w for w in warnings)

    # Config unchanged on disk / registry
    get_resp = await client.get("/sage_vaults/test_vault/config")
    dt_values = [dt["value"] for dt in get_resp.json()["document_types"]["doc_types"]]
    assert "note" in dt_values


# TEST-SAGE-VM-REST-002
async def test_update_config_destructive_with_force_returns_200(client, tmp_vault_dir):
    """force=true allows the destructive update; warnings returned."""
    await _ingest_note_doc(client, tmp_vault_dir)

    resp = await client.put(
        "/sage_vaults/test_vault/config?force=true",
        json={
            "document_types": {
                "doc_types": [
                    {"value": "memo", "label": "Memo"},
                ],
            }
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "updated"
    assert any("note" in w for w in body["warnings"])

    get_resp = await client.get("/sage_vaults/test_vault/config")
    dt_values = [dt["value"] for dt in get_resp.json()["document_types"]["doc_types"]]
    assert "note" not in dt_values
    assert "memo" in dt_values


# TEST-SAGE-VM-REST-003
async def test_update_config_non_destructive_ignores_force(client, tmp_vault_dir):
    """A benign update returns 200 with empty warnings whether or not force is set."""
    # Without force
    resp1 = await client.put(
        "/sage_vaults/test_vault/config",
        json={
            "vault": {
                "id": "test_vault",
                "name": "Renamed Once",
                "owner": "testuser",
                "storage_root": str(tmp_vault_dir / "sources"),
                "brain_root": str(tmp_vault_dir / "brain"),
                "visibility": "personal",
            }
        },
    )
    assert resp1.status_code == 200
    assert resp1.json()["warnings"] == []

    # With force=true
    resp2 = await client.put(
        "/sage_vaults/test_vault/config?force=true",
        json={
            "vault": {
                "id": "test_vault",
                "name": "Renamed Twice",
                "owner": "testuser",
                "storage_root": str(tmp_vault_dir / "sources"),
                "brain_root": str(tmp_vault_dir / "brain"),
                "visibility": "personal",
            }
        },
    )
    assert resp2.status_code == 200
    assert resp2.json()["warnings"] == []


async def test_update_config_preserves_other_sections(client):
    """Updating one section does not alter other sections."""
    # Get original config
    original = (await client.get("/sage_vaults/test_vault/config")).json()

    # Update only document_types
    await client.put(
        "/sage_vaults/test_vault/config",
        json={
            "document_types": {
                "doc_types": [
                    {"value": "note", "label": "Note"},
                    {"value": "memo", "label": "Memo"},
                    {"value": "extra", "label": "Extra"},
                ],
            }
        },
    )

    updated = (await client.get("/sage_vaults/test_vault/config")).json()
    # Lifecycle should be unchanged
    assert updated["lifecycle"] == original["lifecycle"]
    # source_adapters should be unchanged
    assert updated["source_adapters"] == original["source_adapters"]


async def test_update_config_failed_reload_keeps_old_services_in_registry(
    app, client, tmp_vault_dir, monkeypatch
):
    """T-0183 FastAPI atomicity: a failed PUT-config reload leaves
    ``app.state.vault_registry[vault_id]`` pointing at the still-functional
    old services.

    The PUT-config path writes the new YAML to disk, then calls
    ``VaultRegistryService.reload`` → ``reload_vault_in_registry`` →
    ``initialize_services``. With T-0183's build-new-first ordering inside
    ``reload_vault_in_registry``, a failure in ``initialize_services``
    propagates with the registry untouched. The old graph store stays open;
    subsequent reads work; the caller can retry the PUT after fixing the
    underlying cause.

    Trap (anti-coincidental, parallel to N1 in tests/sage/test_mcp_server.py
    for the MCP path): a literal try/restore wrap around the OLD close-old-
    first ordering would re-install the (closed) old reference, passing the
    identity check but failing the still-functional check. Both must hold.
    """
    import sage.mcp_init as _mcp_init
    from sage.api.errors import SAGEError

    # Capture the live services bundle BEFORE the failed PUT
    old = app.state.vault_registry["test_vault"]

    async def failing_initialize_services(*args, **kwargs):
        raise SAGEError(
            code="schema_migration_required",
            message="simulated reload failure for T-0183 FastAPI atomicity test",
            status_code=409,
        )

    monkeypatch.setattr(_mcp_init, "initialize_services", failing_initialize_services)

    resp = await client.put(
        "/sage_vaults/test_vault/config",
        json={
            "vault": {
                "id": "test_vault",
                "name": "Reload-Failure Sentinel",
                "owner": "testuser",
                "storage_root": str(tmp_vault_dir / "sources"),
                "brain_root": str(tmp_vault_dir / "brain"),
                "visibility": "personal",
            }
        },
    )

    # (a) Error response, not 200
    assert resp.status_code == 409, resp.text
    assert resp.json()["code"] == "schema_migration_required"

    # (b) Registry slot still points at the SAME object (identity check)
    assert app.state.vault_registry["test_vault"] is old

    # (c) Old services are still FUNCTIONAL — graph store is not closed.
    # Use the same closure-detection idiom as
    # tests/sage/test_mcp_server.py::test_reload_vault_closes_old_graph_store
    # (which inverts these assertions for the success path). A behavioural
    # check like `await old.graph_store.list_all_documents()` would pass
    # coincidentally because close() does not break read paths — fresh
    # threads transparently open new SQLite connections.
    assert old.graph_store._executor is not None, (
        "old graph_store was closed; build-new-first ordering not enforced"
    )
    assert old.graph_store._all_connections, (
        "old graph_store has no live connections; close() was called"
    )


# ---------------------------------------------------------------------------
# POST /sage_vaults (create)
# ---------------------------------------------------------------------------


async def test_create_vault_201(client, tmp_path):
    """Create a new vault with default config."""
    config = VaultRegistryService.get_default_config("new_vault", "New Vault", "testuser")
    # Override paths to use tmp_path
    config["vault"]["storage_root"] = str(tmp_path / "new_vault" / "sources")
    config["vault"]["brain_root"] = str(tmp_path / "new_vault" / "brain")

    resp = await client.post("/sage_vaults", json={"config": config})
    assert resp.status_code == 201
    body = resp.json()
    assert body["id"] == "new_vault"
    assert body["name"] == "New Vault"

    # Verify it appears in the vault list
    list_resp = await client.get("/sage_vaults")
    vault_ids = [v["id"] for v in list_resp.json()]
    assert "new_vault" in vault_ids


async def test_create_vault_409_exists(client, tmp_path):
    """Duplicate vault_id returns 409."""
    config = VaultRegistryService.get_default_config("test_vault", "Dup", "testuser")
    config["vault"]["storage_root"] = str(tmp_path / "dup" / "sources")
    config["vault"]["brain_root"] = str(tmp_path / "dup" / "brain")

    resp = await client.post("/sage_vaults", json={"config": config})
    assert resp.status_code == 409
    assert resp.json()["code"] == "vault_already_exists"


async def test_create_vault_400_invalid(client):
    """Invalid config dict returns 400."""
    resp = await client.post(
        "/sage_vaults",
        json={"config": {"vault": {"id": "bad"}}},
    )
    assert resp.status_code == 400
    assert resp.json()["code"] == "vault_config_validation_error"


# ---------------------------------------------------------------------------
# Default config validation
# ---------------------------------------------------------------------------


def test_default_config_validates():
    """The generated default config passes VaultConfig validation."""
    config_dict = VaultRegistryService.get_default_config(
        "test_default", "Test Default", "testuser"
    )
    config = VaultConfig.model_validate(config_dict)
    assert config.vault.id == "test_default"
    assert len(config.document_types.doc_types) == 2
    assert config.abstraction.enabled is False
