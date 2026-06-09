"""HTTP integration tests for the maintenance router.

POST /sage_vaults/{vault_id}/admin/migrate.
POST /sage_vaults/{vault_id}/admin/optimize-content-store.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from sage import mcp_server
from sage.adapters.content_store_lancedb import LanceDBContentStore
from sage.adapters.stubs import StubContentStore
from sage.app import _initialize_services, create_app
from sage.config import VaultConfig
from sage.mcp_init import SAGEServices
from sage.models.schemas import MigrationReport, OptimizeContentStoreReport
from sage.services.maintenance import MaintenanceService
from sage.storage.graph_store import SqliteGraphStore
from tests.sage.test_maintenance_service import _churn_chunks
from tests.sage.test_migrate_flag import _build_legacy_db


@pytest.fixture
async def legacy_db_app(minimal_vault_config_dict, monkeypatch):
    """Build a FastAPI app whose vault's graph_store points at a legacy DB.

    Mirrors the swap-in-legacy pattern from the service unit tests: the
    app boots normally (with the freshly-migrated schema), then we close
    the live graph_store, replace the file on disk with a legacy-shape
    one, and rewire the registry entry to point at a new uninitialized
    SqliteGraphStore handle bound to the legacy file. After this, a POST to
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

    legacy_gs = SqliteGraphStore(db_path)
    registry_service = app.state.vault_registry_service
    new_maintenance = MaintenanceService(
        vault_id=vault_id,
        db_path=db_path,
        graph_store=legacy_gs,
        config=config,
        registry_service=registry_service,
        content_store=services.content_store,
    )
    registry[vault_id] = dataclasses.replace(
        services,
        graph_store=legacy_gs,
        maintenance_service=new_maintenance,
    )

    yield app, vault_id

    await asyncio.sleep(0.1)
    if vault_id in registry:
        registry[vault_id].close_timing()
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
        app.state.vault_registry[vault_id].close_timing()
        await app.state.vault_registry[vault_id].graph_store.close()
        mcp_server._vaults.clear()


# ============================================================================
# optimize_vault_content_store route tests
# ============================================================================


@pytest.fixture
async def lancedb_app(minimal_vault_config_dict, tmp_path, monkeypatch):
    """Build a FastAPI app whose vault uses a real LanceDB content store.

    Distinct from ``legacy_db_app``: the optimize route must observe
    real on-disk reclamation, which the stub cannot provide. The
    vault's MaintenanceService is rewired so its audit log lands
    under ``tmp_path`` (where the test brain_root lives) rather than
    the canonical ``~/sage_vaults/<vault_id>/`` location.
    """
    monkeypatch.setenv("SAGE_TEST_STUB_PROVIDERS", "1")
    config = VaultConfig.model_validate(minimal_vault_config_dict)
    app = create_app(config=config)
    await _initialize_services(
        app,
        config,
        content_store_factory=lambda brain: LanceDBContentStore(brain),
    )

    vault_id = config.vault.id
    registry: dict[str, SAGEServices] = app.state.vault_registry
    services = registry[vault_id]
    # brain_root lives at tmp_path/brain in test conftest; the vault
    # dir for audit-log placement is its parent.
    vault_dir = Path(config.vault.brain_root).parent
    services.maintenance_service._vault_dir = vault_dir

    try:
        yield app, vault_id, services, vault_dir
    finally:
        await asyncio.sleep(0.1)
        if vault_id in registry:
            registry[vault_id].close_timing()
            await registry[vault_id].graph_store.close()
        mcp_server._vaults.clear()


async def test_post_admin_optimize_content_store_returns_200_with_report(
    lancedb_app,
):
    """200 with a JSON body that round-trips as
    OptimizeContentStoreReport and reflects the requested threshold.
    """
    app, vault_id, services, _vault_dir = lancedb_app
    await _churn_chunks(services.content_store, cycles=20)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            f"/sage_vaults/{vault_id}/admin/optimize-content-store",
            json={"cleanup_older_than_days": 0},
        )

    assert resp.status_code == 200, resp.text
    report = OptimizeContentStoreReport.model_validate(resp.json())
    assert report.vault_id == vault_id
    assert report.cleanup_older_than_days == 0
    assert report.pre_versions > report.post_versions
    assert report.post_versions >= 1


async def test_post_admin_optimize_content_store_unknown_vault_returns_404(
    lancedb_app,
):
    """An unregistered vault id returns 404 via the shared
    get_vault_id dependency, with the standard error envelope.
    """
    app, _vault_id, _services, _vault_dir = lancedb_app
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/sage_vaults/ghost/admin/optimize-content-store",
            json={"cleanup_older_than_days": 0},
        )

    assert resp.status_code == 404, resp.text
    body = resp.json()
    assert body["code"] == "vault_not_found"


async def test_post_admin_optimize_content_store_default_days_is_7(lancedb_app):
    """An empty body falls through to the Pydantic default of
    cleanup_older_than_days=7. The audit log records the resolved
    threshold (not just any default), pinning the contract end-to-end.
    """
    app, vault_id, _services, vault_dir = lancedb_app

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            f"/sage_vaults/{vault_id}/admin/optimize-content-store",
            json={},
        )

    assert resp.status_code == 200, resp.text
    report = OptimizeContentStoreReport.model_validate(resp.json())
    assert report.cleanup_older_than_days == 7

    audit_path = vault_dir / ".maintenance_log.jsonl"
    lines = audit_path.read_text().strip().splitlines()
    entry = json.loads(lines[-1])
    assert entry["operation"] == "optimize_vault_content_store"
    assert entry["cleanup_older_than_days"] == 7
