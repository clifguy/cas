"""HTTP integration tests for the maintenance router.

POST /sage_vaults/{vault_id}/admin/migrate.
POST /sage_vaults/{vault_id}/admin/optimize-content-store.

The app fixture builds its vault through the production (uninjected
graph-store) path, so the migrate endpoint exercises the Postgres-backed
no-op contract end-to-end (CAS-ADR-042). The content store is a scripted
stub: the optimize route's contract is report shaping and audit logging
around the ContentStore port; real reclamation is covered by the
content-store test modules.
"""

from __future__ import annotations

import asyncio
import json

import pytest
from httpx import ASGITransport, AsyncClient

from sage.app import _initialize_services, create_app
from sage.config import VaultConfig
from sage.models.schemas import MigrationReport, OptimizeContentStoreReport
from tests.sage.test_maintenance_service import _SnapshotContentStore


@pytest.fixture
async def maintenance_app(minimal_vault_config_dict, tmp_path):
    """FastAPI app with one vault built through the uninjected service path.

    The graph store is the real Postgres binding under the test-harness
    stack-config pin; the content store is a ``_SnapshotContentStore`` so the
    optimize route has a deterministic snapshot to shape. The vault's
    MaintenanceService is rewired so its audit log lands under ``tmp_path``.

    Yields ``(app, vault_id, content_store, vault_dir)``.
    """
    config = VaultConfig.model_validate(minimal_vault_config_dict)
    app = create_app(config=config)
    content_store = _SnapshotContentStore()
    await _initialize_services(
        app,
        config,
        content_store_factory=lambda _brain: content_store,
    )

    vault_id = config.vault.id
    registry = app.state.vault_registry
    registry[vault_id].maintenance_service._vault_dir = tmp_path

    try:
        yield app, vault_id, content_store, tmp_path
    finally:
        await asyncio.sleep(0.1)
        current = registry.pop(vault_id, None)
        if current is not None:
            current.close_timing()
            await current.close_storage()


async def test_post_admin_migrate_returns_200_noop_report_and_is_idempotent(
    maintenance_app,
):
    """Two sequential POSTs: both 200, both round-trip as a MigrationReport
    reporting no schema work, with both tier3 keys present in the body.

    On the Postgres backend the vault schema is provisioned externally, so
    the migrate contract that survives is the no-op report shape; a response
    claiming column or backfill work would mean a lingering embedded-backend
    detect path ran against a store that is not SQLite.
    """
    app, vault_id, _content_store, _vault_dir = maintenance_app
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        for _ in range(2):
            resp = await client.post(f"/sage_vaults/{vault_id}/admin/migrate")

            assert resp.status_code == 200, resp.text
            body = resp.json()
            report = MigrationReport.model_validate(body)
            assert report.vault_id == vault_id
            assert report.columns_added == []
            assert report.backfills_applied == []
            # The tier3 keys are present even on the no-op path -- callers
            # must be able to inspect both on every call.
            assert "tier3_uniqueness_activations" in body
            assert "tier3_uniqueness_collisions" in body


async def test_post_admin_migrate_unknown_vault_returns_404(maintenance_app):
    """An unregistered vault id returns 404 via get_vault_id."""
    app, _vault_id, _content_store, _vault_dir = maintenance_app
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post("/sage_vaults/ghost/admin/migrate")

    assert resp.status_code == 404, resp.text
    body = resp.json()
    assert body["code"] == "vault_not_found"


# ============================================================================
# optimize_vault_content_store route tests
# ============================================================================


async def test_post_admin_optimize_content_store_returns_200_with_report(
    maintenance_app,
):
    """200 with a JSON body that round-trips as OptimizeContentStoreReport,
    reflects the requested threshold, and carries the store's snapshot
    (bytes_reclaimed is the pre/post delta, not a constant).
    """
    from datetime import timedelta

    app, vault_id, content_store, _vault_dir = maintenance_app

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
    assert report.bytes_reclaimed == report.pre_bytes - report.post_bytes
    # The request-body threshold reached the store, not a route-side default.
    assert content_store.optimize_calls == [timedelta(days=0)]


async def test_post_admin_optimize_content_store_unknown_vault_returns_404(
    maintenance_app,
):
    """An unregistered vault id returns 404 via the shared
    get_vault_id dependency, with the standard error envelope.
    """
    app, _vault_id, _content_store, _vault_dir = maintenance_app
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/sage_vaults/ghost/admin/optimize-content-store",
            json={"cleanup_older_than_days": 0},
        )

    assert resp.status_code == 404, resp.text
    body = resp.json()
    assert body["code"] == "vault_not_found"


async def test_post_admin_optimize_content_store_default_days_is_7(maintenance_app):
    """An empty body falls through to the Pydantic default of
    cleanup_older_than_days=7. The audit log records the resolved
    threshold (not just any default), pinning the contract end-to-end.
    """
    app, vault_id, _content_store, vault_dir = maintenance_app

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
