"""HTTP integration tests for the maintenance router.

POST /sage_vaults/{vault_id}/admin/migrate.
"""

from __future__ import annotations

import asyncio
import dataclasses
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from sage import mcp_server
from sage.adapters.stubs import StubContentStore
from sage.app import _initialize_services, create_app
from sage.config import VaultConfig
from sage.mcp_init import SAGEServices
from sage.models.schemas import MigrationReport
from sage.services.maintenance import MaintenanceService
from sage.storage.graph_store import GraphStore
from tests.sage.test_migrate_flag import _build_legacy_db


@pytest.fixture
async def legacy_db_app(minimal_vault_config_dict, monkeypatch):
    """Build a FastAPI app whose vault's graph_store points at a legacy DB.

    Mirrors the swap-in-legacy pattern from the service unit tests: the
    app boots normally (with the freshly-migrated schema), then we close
    the live graph_store, replace the file on disk with a legacy-shape
    one, and rewire the registry entry to point at a new uninitialized
    GraphStore handle bound to the legacy file. After this, a POST to
    /admin/migrate has real pending work.
    """
    monkeypatch.setenv("SAGE_TEST_STUB_PROVIDERS", "1")
    config = VaultConfig.model_validate(minimal_vault_config_dict)
    app = create_app(config=config)
    await _initialize_services(
        app,
        config,
        content_store_factory=lambda _brain: StubContentStore(),
    )

    vault_id = config.vault.id
    registry: dict[str, SAGEServices] = app.state.vault_registry
    services = registry[vault_id]
    db_path = Path(config.vault.brain_root) / "graph.db"

    await services.graph_store.close()
    db_path.unlink()
    _build_legacy_db(db_path)

    legacy_gs = GraphStore(db_path)
    registry_service = app.state.vault_registry_service
    new_maintenance = MaintenanceService(
        vault_id=vault_id,
        db_path=db_path,
        graph_store=legacy_gs,
        config=config,
        registry_service=registry_service,
    )
    registry[vault_id] = dataclasses.replace(
        services,
        graph_store=legacy_gs,
        maintenance_service=new_maintenance,
    )

    yield app, vault_id

    await asyncio.sleep(0.1)
    if vault_id in registry:
        await registry[vault_id].graph_store.close()
    mcp_server._vaults.clear()


async def test_post_admin_migrate_returns_200_with_report(legacy_db_app):
    """Happy path: 200 with a JSON body that round-trips as MigrationReport."""
    app, vault_id = legacy_db_app
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(f"/sage_vaults/{vault_id}/admin/migrate")

    assert resp.status_code == 200, resp.text
    report = MigrationReport.model_validate(resp.json())
    assert report.vault_id == vault_id
    assert len(report.columns_added) > 0


async def test_post_admin_migrate_unknown_vault_returns_404(legacy_db_app):
    """An unregistered vault id returns 404 via get_vault_id."""
    app, _vault_id = legacy_db_app
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post("/sage_vaults/ghost/admin/migrate")

    assert resp.status_code == 404, resp.text
    body = resp.json()
    assert body["code"] == "vault_not_found"


async def test_post_admin_migrate_is_idempotent_when_no_work_pending(
    minimal_vault_config_dict, monkeypatch
):
    """Two sequential POSTs against a current-schema vault: both 200,
    both report no work."""
    monkeypatch.setenv("SAGE_TEST_STUB_PROVIDERS", "1")
    config = VaultConfig.model_validate(minimal_vault_config_dict)
    app = create_app(config=config)
    await _initialize_services(
        app,
        config,
        content_store_factory=lambda _brain: StubContentStore(),
    )
    vault_id = config.vault.id

    transport = ASGITransport(app=app)
    try:
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            for _ in range(2):
                resp = await client.post(f"/sage_vaults/{vault_id}/admin/migrate")
                assert resp.status_code == 200, resp.text
                report = MigrationReport.model_validate(resp.json())
                assert report.columns_added == []
                assert report.backfills_applied == []
    finally:
        await asyncio.sleep(0.1)
        await app.state.vault_registry[vault_id].graph_store.close()
        mcp_server._vaults.clear()
