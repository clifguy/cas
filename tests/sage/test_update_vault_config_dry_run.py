"""T-0152: `update_vault_config` dry-run.

Four test categories:

A. Happy-path dry-run — response carries `status="previewed"`,
   `dry_run=True`, `preview.changed_sections`; yaml mtime + size
   unchanged.
B. Same-validator paired — VaultConfigValidationError raised by both
   paths with identical envelope (e.g., changing vault.id).
C. Side-effect-specific — yaml file unchanged; registry.reload not
   called.
H. Destructive-change case — dry-run on a destructive update NEVER
   raises DestructiveConfigChangeError; warnings are always populated
   in the response body. `force` is a no-op on dry-run.
"""

from __future__ import annotations

import asyncio

import pytest
from httpx import ASGITransport, AsyncClient

from sage.app import _initialize_services, create_app
from sage.config import VaultConfig
from sage.vault_management import config_path_for_vault


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


async def _ingest_note_doc(client, tmp_vault_dir):
    """Helper: ingest a doc so a doc_types removal becomes destructive."""
    src = tmp_vault_dir / "sources" / "test"
    src.mkdir(parents=True, exist_ok=True)
    (src / "sample.md").write_text("# Test\n\nBody.")
    resp = await client.post(
        "/sage_vaults/test_vault/documents",
        json={
            "source": "test/sample.md",
            "adapter": "markdown",
            "metadata": {"doc_type": "note"},
        },
    )
    assert resp.status_code == 201


# ---------------------------------------------------------------------------
# (A) Happy-path dry-run + (C) side-effect-specific (yaml unchanged)
# ---------------------------------------------------------------------------


async def test_dry_run_returns_preview_without_writing_yaml(app, client, tmp_vault_dir):
    """Dry-run on a non-destructive change returns status='previewed'
    with the section-level diff; in-memory config is unchanged and
    the yaml file is not materialized (no prior real-run wrote it)."""
    config_path = config_path_for_vault("test_vault")
    yaml_existed_before = config_path.exists()
    pre_get = await client.get("/sage_vaults/test_vault/config")
    pre_body = pre_get.json()

    resp = await client.put(
        "/sage_vaults/test_vault/config",
        json={
            "dry_run": True,
            "vault": {
                "id": "test_vault",
                "name": "Would Be Renamed",
                "owner": "testuser",
                "storage_root": str(tmp_vault_dir / "sources"),
                "brain_root": str(tmp_vault_dir / "brain"),
                "visibility": "personal",
            },
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "previewed"
    assert body["dry_run"] is True
    assert body["warnings"] == []  # non-destructive
    assert body["preview"]["changed_sections"] == ["vault"]

    # In-memory config unchanged: GET returns the same body as before.
    post_get = await client.get("/sage_vaults/test_vault/config")
    assert post_get.json() == pre_body
    # And dry-run did not materialize the yaml file (the fixture starts
    # with the config in memory only).
    assert config_path.exists() is yaml_existed_before


# ---------------------------------------------------------------------------
# (H) Destructive-change dry-run NEVER raises; warnings always populated
# ---------------------------------------------------------------------------


async def test_dry_run_on_destructive_change_returns_warnings_does_not_raise(
    app, client, tmp_vault_dir
):
    """Dry-run on a destructive doc_types removal must NOT raise 409.
    The response carries status='previewed', warnings non-empty, and
    preview.changed_sections=['document_types']. Yaml unchanged."""
    await _ingest_note_doc(client, tmp_vault_dir)
    pre_get = await client.get("/sage_vaults/test_vault/config")
    pre_body = pre_get.json()

    resp = await client.put(
        "/sage_vaults/test_vault/config",
        json={
            "dry_run": True,
            "document_types": {
                "doc_types": [{"value": "memo", "label": "Memo"}],
            },
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "previewed"
    assert body["dry_run"] is True
    assert any("note" in w for w in body["warnings"])
    assert body["preview"]["changed_sections"] == ["document_types"]

    # In-memory config unchanged: GET returns the same body as before
    # (still has "note" in doc_types).
    post_get = await client.get("/sage_vaults/test_vault/config")
    assert post_get.json() == pre_body
    dt_values = [dt["value"] for dt in post_get.json()["document_types"]["doc_types"]]
    assert "note" in dt_values


async def test_dry_run_with_force_true_is_no_op_for_force(app, client, tmp_vault_dir):
    """On dry-run, force is irrelevant — dry-run never raises and
    always returns warnings in the body. Identical response shape
    whether force is true or false."""
    await _ingest_note_doc(client, tmp_vault_dir)

    body_payload = {
        "dry_run": True,
        "document_types": {
            "doc_types": [{"value": "memo", "label": "Memo"}],
        },
    }

    resp_no_force = await client.put(
        "/sage_vaults/test_vault/config",
        json=body_payload,
    )
    resp_force = await client.put(
        "/sage_vaults/test_vault/config?force=true",
        json=body_payload,
    )
    assert resp_no_force.status_code == 200
    assert resp_force.status_code == 200
    j1, j2 = resp_no_force.json(), resp_force.json()
    assert j1["status"] == "previewed"
    assert j2["status"] == "previewed"
    assert j1["warnings"] == j2["warnings"]
    assert j1["preview"]["changed_sections"] == j2["preview"]["changed_sections"]


# ---------------------------------------------------------------------------
# (B) Same-validator paired — vault.id change rejected in both modes
# ---------------------------------------------------------------------------


async def test_vault_id_change_rejected_in_both_modes(app, client, tmp_vault_dir):
    """Changing vault.id is never permitted; both real-run and dry-run
    raise VaultConfigValidationError with the same envelope."""
    body_payload_real = {
        "vault": {
            "id": "renamed_vault",
            "name": "Renamed",
            "owner": "testuser",
            "storage_root": str(tmp_vault_dir / "sources"),
            "brain_root": str(tmp_vault_dir / "brain"),
            "visibility": "personal",
        }
    }
    body_payload_dry = {**body_payload_real, "dry_run": True}
    real_resp = await client.put("/sage_vaults/test_vault/config", json=body_payload_real)
    dry_resp = await client.put("/sage_vaults/test_vault/config", json=body_payload_dry)
    # Both should reject identically.
    assert real_resp.status_code == 400
    assert dry_resp.status_code == 400
    assert real_resp.json()["code"] == dry_resp.json()["code"]
    assert real_resp.json()["code"] == "vault_config_validation_error"


# ---------------------------------------------------------------------------
# (A) Real-run echo: dry_run=False on the response when not in dry-run.
# ---------------------------------------------------------------------------


async def test_real_run_carries_dry_run_false_and_status_updated(app, client, tmp_vault_dir):
    """Positive control: real-run returns status='updated', dry_run=False,
    preview=None."""
    resp = await client.put(
        "/sage_vaults/test_vault/config",
        json={
            "vault": {
                "id": "test_vault",
                "name": "Really Renamed",
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
    assert body["dry_run"] is False
    assert body.get("preview") is None
