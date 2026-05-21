"""HTTP integration tests for the reabstract-deferred router (T-0089).

POST /sage_vaults/{vault_id}/admin/reabstract-deferred.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest
from httpx import ASGITransport, AsyncClient

from sage import mcp_server
from sage.adapters.interfaces import AbstractionProvider, Chunk
from sage.adapters.stubs import StubContentStore
from sage.app import _initialize_services, create_app
from sage.config import VaultConfig
from sage.mcp_init import SAGEServices
from sage.models.enums import PipelineStatus
from sage.models.schemas import ReabstractReport
from tests.sage.test_lifecycle import _id
from tests.sage.test_reabstract_deferred_service import (
    _GatedAbstractionProvider,
    _make_skipped_doc,
)


async def _seed_one_skipped(
    services: SAGEServices,
    *,
    doc_id_label: str = "router_skipped",
) -> str:
    """Insert one abstraction_skipped markdown doc and a body chunk for it."""
    doc = _make_skipped_doc(_id(doc_id_label))
    await services.graph_store.insert_document(doc)
    chunk = Chunk(
        document_id=doc.id,
        heading_path="Body",
        content="Body content for projection.",
        chunk_index=0,
    )
    await services.content_store.index_chunks(doc.id, [chunk])
    return doc.id


@pytest.fixture
async def maintenance_app(minimal_vault_config_dict, monkeypatch):
    """Build a FastAPI app with one vault wired through the normal
    initialization path so MaintenanceService.ingestion_service is
    populated and reabstract-deferred is reachable end-to-end."""
    monkeypatch.setenv("SAGE_TEST_STUB_PROVIDERS", "1")
    config = VaultConfig.model_validate(minimal_vault_config_dict)
    app = create_app(config=config)
    await _initialize_services(
        app,
        config,
        content_store_factory=lambda _brain: StubContentStore(),
    )
    vault_id = config.vault.id
    yield app, vault_id, config

    await asyncio.sleep(0.1)
    registry: dict[str, SAGEServices] = app.state.vault_registry
    if vault_id in registry:
        await registry[vault_id].graph_store.close()
    mcp_server._vaults.clear()


async def test_post_reabstract_deferred_returns_report(maintenance_app):
    """200 with a JSON body that round-trips as ReabstractReport;
    reabstracted_count reflects the seeded worklist."""
    app, vault_id, _config = maintenance_app
    services: SAGEServices = app.state.vault_registry[vault_id]
    await _seed_one_skipped(services, doc_id_label="router_happy")

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            f"/sage_vaults/{vault_id}/admin/reabstract-deferred",
            json={"include_pdf": False},
        )

    assert resp.status_code == 200, resp.text
    report = ReabstractReport.model_validate(resp.json())
    assert report.vault_id == vault_id
    assert report.reabstracted_count == 1
    assert report.failed_count == 0
    assert len(report.entries) == 1


async def test_post_reabstract_deferred_404_for_unknown_vault(maintenance_app):
    """An unregistered vault id returns 404 via get_vault_id."""
    app, _vault_id, _config = maintenance_app
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post("/sage_vaults/ghost/admin/reabstract-deferred")

    assert resp.status_code == 404, resp.text
    body = resp.json()
    assert body["code"] == "vault_not_found"


async def test_post_reabstract_deferred_409_when_already_in_flight(
    minimal_vault_config_dict,
    monkeypatch,
):
    """Two concurrent POSTs against the same vault: one 200, one 409 with
    a structured reabstract_already_in_flight payload that includes
    start_time. Built with a gated abstraction provider so the first
    call blocks until the test releases it."""
    monkeypatch.setenv("SAGE_TEST_STUB_PROVIDERS", "1")
    config = VaultConfig.model_validate(minimal_vault_config_dict)
    app = create_app(config=config)

    # Construct services with a gated provider so we can hold call A
    # mid-flight while call B attempts to enter.
    gated: AbstractionProvider = _GatedAbstractionProvider()
    await _initialize_services(
        app,
        config,
        abstraction_provider=gated,
        content_store_factory=lambda _brain: StubContentStore(),
    )
    vault_id = config.vault.id
    services: SAGEServices = app.state.vault_registry[vault_id]
    await _seed_one_skipped(services, doc_id_label="router_gated")

    transport = ASGITransport(app=app)
    try:
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            before = datetime.now(timezone.utc)
            task_a = asyncio.create_task(
                client.post(f"/sage_vaults/{vault_id}/admin/reabstract-deferred")
            )
            # Wait for the background reabstract to hit the gate, by
            # which time the lock is held inside MaintenanceService.
            await asyncio.wait_for(gated.entered.wait(), timeout=5.0)
            after = datetime.now(timezone.utc)

            resp_b = await client.post(f"/sage_vaults/{vault_id}/admin/reabstract-deferred")
            assert resp_b.status_code == 409, resp_b.text
            body_b = resp_b.json()
            assert body_b["code"] == "reabstract_already_in_flight"
            assert body_b["detail"]["vault_id"] == vault_id
            start_time = datetime.fromisoformat(body_b["detail"]["start_time"])
            assert before <= start_time <= after

            # Release the gate; call A should complete with 200 and a
            # single-success report.
            gated.gate.set()
            resp_a = await asyncio.wait_for(task_a, timeout=5.0)
            assert resp_a.status_code == 200, resp_a.text
            report_a = ReabstractReport.model_validate(resp_a.json())
            assert report_a.reabstracted_count == 1

            # Confirm the on-disk pipeline_status transitioned (defense
            # against the report-without-effect coincidental pass).
            doc = await services.graph_store.get_document(
                _id("router_gated"),
            )
            assert doc.pipeline_status == PipelineStatus.ABSTRACTION_COMPLETE.value
    finally:
        await asyncio.sleep(0.1)
        await app.state.vault_registry[vault_id].graph_store.close()
        mcp_server._vaults.clear()
