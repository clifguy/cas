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
        services.close_timing()
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
    # edge_inference should be unchanged
    assert updated["edge_inference"] == original["edge_inference"]


async def test_update_config_failed_reload_keeps_old_services_in_registry(
    app, client, tmp_vault_dir, monkeypatch
):
    """FastAPI atomicity: a failed PUT-config reload leaves
    ``app.state.vault_registry[vault_id]`` pointing at the still-functional
    old services.

    The PUT-config path writes the new YAML to disk, then calls
    ``VaultRegistryService.reload`` → ``reload_vault_in_registry`` →
    ``initialize_services``. With build-new-first ordering inside
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

    # (c) Old services are still FUNCTIONAL — the graph store was not closed.
    # Behavioural assertion per TEST-SAGE-BH-137: the CAS-ADR-036 close
    # barrier makes every post-close dispatch raise (see
    # tests/sage/test_mcp_server.py::test_reload_vault_closes_old_graph_store,
    # which asserts the raise for the success path's old store), so a
    # successful list_all_documents() is the contrapositive of close()
    # having run. A literal try/restore around a close-old-first ordering
    # would re-install the closed reference — passing the identity check —
    # and fail here.
    live_docs = await old.graph_store.list_all_documents()
    assert isinstance(live_docs, list)


# ---------------------------------------------------------------------------
# Outer-sequence atomicity: yaml-write + reload rolls back on reload failure
# so the full ``update_config`` call is transactional w.r.t. both the on-disk
# yaml and the in-memory registry slot.
# ---------------------------------------------------------------------------


@pytest.fixture
async def isolated_vault_client(monkeypatch, tmp_path, minimal_vault_config_dict, tmp_vault_dir):
    """Isolated FastAPI client whose vault_config.yaml lives under tmp_path.

    Redirects the module-level ``_VAULTS_ROOT`` in ``sage.vault_management``
    to a temp dir so atomicity assertions inspect the test's own yaml file
    rather than the user's real ``~/sage_vaults/test_vault/vault_config.yaml``.
    Yields ``(client, app, isolated_root)``.
    """
    isolated_root = tmp_path / "sage_vaults"
    monkeypatch.setattr("sage.vault_management._VAULTS_ROOT", isolated_root)

    config = VaultConfig.model_validate(minimal_vault_config_dict)
    app = create_app(config=config)
    await _initialize_services(app, config)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client, app, isolated_root

    await asyncio.sleep(0.1)
    for services in app.state.vault_registry.values():
        services.close_timing()
        await services.graph_store.close()


async def _seed_initial_yaml(client, tmp_vault_dir, name: str = "Initial Name") -> None:
    """PUT a benign config update so ``vault_config.yaml`` exists on disk
    with a known shape before the atomicity test fires its failure-inducing PUT.
    """
    resp = await client.put(
        "/sage_vaults/test_vault/config",
        json={
            "vault": {
                "id": "test_vault",
                "name": name,
                "owner": "testuser",
                "storage_root": str(tmp_vault_dir / "sources"),
                "brain_root": str(tmp_vault_dir / "brain"),
                "visibility": "personal",
            }
        },
    )
    assert resp.status_code == 200, resp.text


async def test_update_config_rolls_back_yaml_on_reload_failure(
    isolated_vault_client, tmp_vault_dir, monkeypatch
):
    """A1: when ``_registry_service.reload`` raises, the on-disk
    ``vault_config.yaml`` is restored to its pre-PUT state.

    Trap (anti-coincidental): a yaml-write-then-reload implementation
    persists the new bytes regardless of whether reload succeeds. The
    post-call dict-equality assertion is the trap — it must fail against
    a write-first implementation and pass only when the reload call is
    wrapped in a rollback handler that restores the pre-call bytes.
    """
    import yaml as _yaml

    import sage.mcp_init as _mcp_init
    from sage.api.errors import SAGEError

    client, app, isolated_root = isolated_vault_client

    await _seed_initial_yaml(client, tmp_vault_dir, name="Initial Name")

    config_path = isolated_root / "test_vault" / "vault_config.yaml"
    pre_call_dict = _yaml.safe_load(config_path.read_text())
    assert pre_call_dict["vault"]["name"] == "Initial Name"

    async def failing_initialize_services(*args, **kwargs):
        raise SAGEError(
            code="schema_migration_required",
            message="simulated reload failure for outer-sequence atomicity test",
            status_code=409,
        )

    monkeypatch.setattr(_mcp_init, "initialize_services", failing_initialize_services)

    resp = await client.put(
        "/sage_vaults/test_vault/config",
        json={
            "vault": {
                "id": "test_vault",
                "name": "Should Not Persist",
                "owner": "testuser",
                "storage_root": str(tmp_vault_dir / "sources"),
                "brain_root": str(tmp_vault_dir / "brain"),
                "visibility": "personal",
            }
        },
    )

    assert resp.status_code == 409, resp.text
    assert resp.json()["code"] == "schema_migration_required"

    post_call_dict = _yaml.safe_load(config_path.read_text())
    assert post_call_dict == pre_call_dict, (
        "yaml-rollback failed: on-disk yaml does not match the pre-PUT state. "
        f"Expected name={pre_call_dict['vault']['name']!r}, "
        f"got name={post_call_dict['vault']['name']!r}."
    )


async def test_update_config_rolls_back_yaml_when_inner_initialize_raises_late(
    isolated_vault_client, tmp_vault_dir, monkeypatch
):
    """A2: yaml rollback fires even when the inner allocator (i.e.
    ``UserService.bootstrap_owner``, which runs late inside
    ``initialize_services``) raises.

    Distinguishes "rollback wired only to outer reload surface" from
    "rollback wired to anything that can raise post-yaml-write."

    Trap (anti-coincidental): a rollback that catches only the surface
    ``reload`` call (and not the underlying allocator) would let the
    late-stage failure leak through with the new yaml on disk.
    """
    import yaml as _yaml

    from sage.api.errors import SAGEError
    from sage.services.user_service import UserService

    client, app, isolated_root = isolated_vault_client

    await _seed_initial_yaml(client, tmp_vault_dir, name="Pre Late Failure")

    config_path = isolated_root / "test_vault" / "vault_config.yaml"
    pre_call_dict = _yaml.safe_load(config_path.read_text())
    assert pre_call_dict["vault"]["name"] == "Pre Late Failure"

    async def raising_bootstrap(self):
        raise SAGEError(
            code="schema_migration_required",
            message="simulated late-stage failure inside initialize_services",
            status_code=409,
        )

    monkeypatch.setattr(UserService, "bootstrap_owner", raising_bootstrap)

    resp = await client.put(
        "/sage_vaults/test_vault/config",
        json={
            "vault": {
                "id": "test_vault",
                "name": "Late Failure Should Not Persist",
                "owner": "testuser",
                "storage_root": str(tmp_vault_dir / "sources"),
                "brain_root": str(tmp_vault_dir / "brain"),
                "visibility": "personal",
            }
        },
    )

    assert resp.status_code == 409, resp.text
    assert resp.json()["code"] == "schema_migration_required"

    post_call_dict = _yaml.safe_load(config_path.read_text())
    assert post_call_dict == pre_call_dict, (
        "yaml-rollback did not fire on late-stage allocator failure. "
        f"Expected name={pre_call_dict['vault']['name']!r}, "
        f"got name={post_call_dict['vault']['name']!r}."
    )


async def test_update_config_happy_path_writes_new_yaml_and_swaps_registry(
    isolated_vault_client, tmp_vault_dir
):
    """A3: when the reload succeeds, the new yaml IS persisted and the
    registry slot is a fresh services instance.

    Paired with A1/A2 to prove the rollback path fires on failure and only
    on failure. Without this guard a buggy "always rollback" implementation
    would pass A1/A2 but break the happy path.
    """
    import yaml as _yaml

    client, app, isolated_root = isolated_vault_client

    await _seed_initial_yaml(client, tmp_vault_dir, name="Before Success")

    config_path = isolated_root / "test_vault" / "vault_config.yaml"
    pre_call_services = app.state.vault_registry["test_vault"]

    resp = await client.put(
        "/sage_vaults/test_vault/config",
        json={
            "vault": {
                "id": "test_vault",
                "name": "After Success",
                "owner": "testuser",
                "storage_root": str(tmp_vault_dir / "sources"),
                "brain_root": str(tmp_vault_dir / "brain"),
                "visibility": "personal",
            }
        },
    )
    assert resp.status_code == 200, resp.text

    post_call_dict = _yaml.safe_load(config_path.read_text())
    assert post_call_dict["vault"]["name"] == "After Success", (
        "happy-path yaml not persisted; a buggy always-rollback would surface here"
    )

    # Registry slot replaced with a freshly-initialized services bundle.
    assert app.state.vault_registry["test_vault"] is not pre_call_services


async def test_update_config_rollback_failure_does_not_mask_original_exception(
    isolated_vault_client, tmp_vault_dir, monkeypatch
):
    """A4: if the rollback write itself fails, the ORIGINAL reload
    exception is the one that propagates (mirrors the
    initialize_services "cleanup does not mask original exception"
    discipline).

    Trap (anti-coincidental): a naive ``raise rollback_exc from original``
    surfaces the rollback exception (OSError) instead of the original
    (SAGEError). The status-code/code assertion is the trap.
    """
    import sage.mcp_init as _mcp_init
    from sage.api.errors import SAGEError

    client, app, isolated_root = isolated_vault_client

    await _seed_initial_yaml(client, tmp_vault_dir, name="Pre Rollback Failure")

    # First, force the reload step to fail with the ORIGINAL code we want
    # surfaced.
    async def failing_initialize_services(*args, **kwargs):
        raise SAGEError(
            code="schema_migration_required",
            message="ORIGINAL: reload failed for outer-sequence atomicity test",
            status_code=409,
        )

    monkeypatch.setattr(_mcp_init, "initialize_services", failing_initialize_services)

    # Second, force the rollback's ``_atomic_write_bytes`` call to fail.
    # The rollback path writes the snapshotted old bytes via this helper;
    # making it raise simulates an I/O error during rollback (disk full,
    # permission revoked, etc.). The initial-write call uses
    # ``_write_config_yaml`` and is untouched by this monkeypatch.
    import sage.services.vault_config as _vc_module

    rollback_calls = {"n": 0}

    def failing_atomic_write_bytes(path, data):
        rollback_calls["n"] += 1
        raise OSError("ROLLBACK FAILURE: simulated I/O error during yaml rollback")

    monkeypatch.setattr(_vc_module, "_atomic_write_bytes", failing_atomic_write_bytes)

    resp = await client.put(
        "/sage_vaults/test_vault/config",
        json={
            "vault": {
                "id": "test_vault",
                "name": "Triggers Rollback Failure",
                "owner": "testuser",
                "storage_root": str(tmp_vault_dir / "sources"),
                "brain_root": str(tmp_vault_dir / "brain"),
                "visibility": "personal",
            }
        },
    )

    # The ORIGINAL SAGEError (reload failure) must be the one surfaced —
    # not the rollback's OSError. Status code 409 and the SAGEError code
    # field carry that signal.
    assert resp.status_code == 409, (
        f"expected original SAGEError to propagate as 409; got {resp.status_code}. "
        f"Response: {resp.text}"
    )
    assert resp.json()["code"] == "schema_migration_required", (
        "original SAGEError was masked by the rollback OSError; the "
        "rollback exception-handler must log and swallow, not re-raise"
    )

    # The rollback should have been attempted exactly once (so the
    # exception net actually hit it, not skipped past it).
    assert rollback_calls["n"] == 1, (
        f"expected exactly 1 _atomic_write_bytes rollback call; got {rollback_calls['n']}"
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
# Outer-sequence atomicity for create_vault: yaml-write + service allocation
# rolls back so a failed creation leaves no orphan yaml on disk and no half-
# registered vault in memory. Same shape of guarantee as update_config's
# rollback wrap, applied to the create path.
# ---------------------------------------------------------------------------


async def test_create_vault_rolls_back_yaml_on_initialize_failure(
    app, client, tmp_path, monkeypatch
):
    """D1: when ``_initialize_services`` raises after ``_write_config_yaml``
    has written the new vault's yaml, the on-disk yaml is unlinked and the
    vault is NOT registered.

    Trap (anti-coincidental): a write-first, then-allocate-then-register
    sequence leaves orphan yaml on disk when the allocator raises. The
    not-on-disk assertion is the trap.
    """
    from sage.api.errors import SAGEError

    # Isolate the vault-yaml path to tmp_path so the assertion does not
    # depend on the user's real ~/sage_vaults/ tree.
    isolated_root = tmp_path / "sage_vaults"
    monkeypatch.setattr("sage.vault_management._VAULTS_ROOT", isolated_root)

    new_vault_id = "atomicity_create_target"

    async def failing_initialize_services(*args, **kwargs):
        raise SAGEError(
            code="schema_migration_required",
            message="simulated allocator failure for create_vault atomicity",
            status_code=409,
        )

    monkeypatch.setattr(
        app.state.vault_registry_service,
        "_initialize_services",
        failing_initialize_services,
    )

    config = VaultRegistryService.get_default_config(
        new_vault_id, "Atomicity Create Target", "testuser"
    )
    config["vault"]["storage_root"] = str(tmp_path / new_vault_id / "sources")
    config["vault"]["brain_root"] = str(tmp_path / new_vault_id / "brain")

    resp = await client.post("/sage_vaults", json={"config": config})

    # (a) Error surfaces with the original allocator code.
    assert resp.status_code == 409, resp.text
    assert resp.json()["code"] == "schema_migration_required"

    # (b) No orphan yaml on disk at the would-be vault path.
    expected_yaml = isolated_root / new_vault_id / "vault_config.yaml"
    assert not expected_yaml.exists(), (
        f"create_vault left orphan yaml at {expected_yaml} after a failed "
        "allocator call; the outer-sequence rollback did not fire"
    )

    # (c) Vault not registered.
    assert new_vault_id not in app.state.vault_registry


async def test_create_vault_rolls_back_when_bootstrap_owner_fails_post_register(
    app, client, tmp_path, monkeypatch
):
    """D2: when ``bootstrap_owner`` raises after the new services are
    already in the registry, both the registry entry and the on-disk yaml
    are rolled back; the user can re-issue create_vault cleanly.

    Trap (anti-coincidental): a write-then-allocate-then-register-then-
    bootstrap sequence with no rollback leaves the vault half-registered
    (services live in the registry, but no owner bootstrapped) and the
    yaml on disk. Both asserts must hold.
    """
    from sage.api.errors import SAGEError
    from sage.services.user_service import UserService

    isolated_root = tmp_path / "sage_vaults"
    monkeypatch.setattr("sage.vault_management._VAULTS_ROOT", isolated_root)

    new_vault_id = "atomicity_bootstrap_target"

    async def raising_bootstrap(self):
        raise SAGEError(
            code="schema_migration_required",
            message="simulated late-stage bootstrap_owner failure",
            status_code=409,
        )

    monkeypatch.setattr(UserService, "bootstrap_owner", raising_bootstrap)

    config = VaultRegistryService.get_default_config(
        new_vault_id, "Atomicity Bootstrap Target", "testuser"
    )
    config["vault"]["storage_root"] = str(tmp_path / new_vault_id / "sources")
    config["vault"]["brain_root"] = str(tmp_path / new_vault_id / "brain")

    resp = await client.post("/sage_vaults", json={"config": config})

    assert resp.status_code == 409, resp.text
    assert resp.json()["code"] == "schema_migration_required"

    expected_yaml = isolated_root / new_vault_id / "vault_config.yaml"
    assert not expected_yaml.exists(), (
        "create_vault left orphan yaml after bootstrap_owner failed; "
        "rollback must fire on post-register failures too"
    )

    assert new_vault_id not in app.state.vault_registry, (
        "create_vault left a half-registered vault in the registry after "
        "bootstrap_owner failed; the registry entry must be removed alongside "
        "the yaml rollback"
    )


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


# ---------------------------------------------------------------------------
# GET /sage_vaults/default-config
#
# The scaffold a new vault would get, served rather than restated by every
# caller that wants one. The web client posts what this returns; the
# expectations below compare against the live builder rather than a config
# transcribed into the test, so a scaffold change moves one copy, not three.
# ---------------------------------------------------------------------------


async def test_default_config_endpoint_serves_the_registry_scaffold(client):
    """The route returns exactly what the registry builder returns.

    Compared against the builder itself, not a literal: a literal here
    would be the third copy of the scaffold, which is the thing this
    endpoint exists to prevent.
    """
    resp = await client.get("/sage_vaults/default-config", params={"vault_id": "brand_new"})

    assert resp.status_code == 200, resp.text
    assert resp.json() == VaultRegistryService.get_default_config("brand_new")


async def test_default_config_endpoint_leaves_name_and_owner_to_the_caller(client):
    """Identity fields the server cannot know come back empty.

    The vault id shapes the storage roots, so the server derives those;
    the display name and owner are the caller's to supply.

    The root assertions pin that the supplied id reaches the derived paths.
    They do not pin the prefix ahead of it: that is the server's vault root,
    and a suffix match holds against a correct one and a stale one alike.
    Nothing here distinguishes the two, and this is not the test that would.
    """
    body = (
        await client.get("/sage_vaults/default-config", params={"vault_id": "brand_new"})
    ).json()

    assert body["vault"]["id"] == "brand_new"
    assert body["vault"]["name"] == ""
    assert body["vault"]["owner"] == ""
    assert body["vault"]["storage_root"].endswith("/brand_new/sources")
    assert body["vault"]["brain_root"].endswith("/brand_new/brain")


async def test_default_config_endpoint_does_not_require_the_vault_to_exist(app, client):
    """An unregistered vault id is served, not 404'd.

    The scaffold precedes creation by construction, so the route must not
    resolve its argument through the vault registry. Anchored on an id the
    registry demonstrably lacks, so a route wired to ``get_vault_id`` fails
    here rather than passing on an incidentally-registered id.
    """
    assert "brand_new" not in app.state.vault_registry

    resp = await client.get("/sage_vaults/default-config", params={"vault_id": "brand_new"})

    assert resp.status_code == 200, resp.text


async def test_default_config_endpoint_rejects_a_malformed_vault_id(client):
    """A vault id that violates the shape is rejected at request binding.

    Pins that ``VaultIdStr`` sits on the handler's own annotation. A
    ``Query(...)`` factory default would strip the validator and this
    would come back 200. The typed code separates a rejected shape from
    an absent argument, which the second assertion holds apart: without
    it, renaming the parameter away would leave this test green.
    """
    resp = await client.get("/sage_vaults/default-config", params={"vault_id": "Not-A-Vault!"})

    assert resp.status_code == 400, resp.text
    assert resp.json()["code"] == "invalid_vault_id"

    omitted = await client.get("/sage_vaults/default-config")
    assert omitted.json().get("code") != "invalid_vault_id"


async def test_the_served_scaffold_creates_a_vault_verbatim(client):
    """The served document is directly postable to the create endpoint.

    The round trip the web client depends on: fetch, fill the two identity
    fields, post. Nothing else is substituted, so a scaffold the create
    endpoint would reject fails here rather than in the browser.
    """
    config = (
        await client.get("/sage_vaults/default-config", params={"vault_id": "round_trip"})
    ).json()
    config["vault"]["name"] = "Round Trip"
    config["vault"]["owner"] = "testuser"

    created = await client.post("/sage_vaults", json={"config": config})
    assert created.status_code == 201, created.text
    assert created.json()["name"] == "Round Trip"

    # The stored config round-trips through VaultConfig, which materializes
    # the optional transition fields the scaffold omits. Compare the keys the
    # scaffold actually carried, positionally, so a reordered or rewritten
    # table fails while an added default does not.
    stored = (await client.get("/sage_vaults/round_trip/config")).json()
    served_rows = config["lifecycle"]["transitions"]
    stored_rows = stored["lifecycle"]["transitions"]
    assert len(stored_rows) == len(served_rows)
    for served_row, stored_row in zip(served_rows, stored_rows, strict=True):
        assert {key: stored_row[key] for key in served_row} == served_row
