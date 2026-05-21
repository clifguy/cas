"""HTTP integration tests for the bulk metadata endpoint (T-0088).

POST /sage_vaults/{vault_id}/metadata/bulk.
"""

from __future__ import annotations

import asyncio

import pytest
from httpx import ASGITransport, AsyncClient

from sage import mcp_server
from sage.adapters.stubs import StubContentStore
from sage.app import _initialize_services, create_app
from sage.config import VaultConfig
from sage.models.schemas import BulkMetadataResponse
from tests.sage.test_lifecycle import _id, _make_doc


@pytest.fixture
async def seeded_app(minimal_vault_config_dict, monkeypatch):
    """Boot an app with one vault and three active documents seeded
    with tags=["a"]."""
    monkeypatch.setenv("SAGE_TEST_STUB_PROVIDERS", "1")
    config = VaultConfig.model_validate(minimal_vault_config_dict)
    app = create_app(config=config)
    await _initialize_services(
        app,
        config,
        content_store_factory=lambda _brain: StubContentStore(),
    )

    vault_id = config.vault.id
    graph_store = app.state.vault_registry[vault_id].graph_store

    seeded_ids = [_id("doc_r1"), _id("doc_r2"), _id("doc_r3")]
    for doc_id in seeded_ids:
        await graph_store.insert_document(_make_doc(doc_id))
        await graph_store.update_document(
            doc_id, {"doc_type": "note", "tags": ["a"], "metadata_confirmed": True}
        )

    yield app, vault_id, seeded_ids

    await asyncio.sleep(0.1)
    if vault_id in app.state.vault_registry:
        await app.state.vault_registry[vault_id].graph_store.close()
    mcp_server._vaults.clear()


async def test_bulk_metadata_endpoint_happy_path_returns_200_and_response_envelope(seeded_app):
    """All-success batch returns 200 with a BulkMetadataResponse body."""
    app, vault_id, seeded_ids = seeded_app
    body = {
        "items": [
            {"document_id": seeded_ids[0], "tags": {"add": ["b"]}},
            {"document_id": seeded_ids[1], "tags": {"add": ["b"]}},
        ]
    }

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(f"/sage_vaults/{vault_id}/metadata/bulk", json=body)

    assert resp.status_code == 200, resp.text
    response = BulkMetadataResponse.model_validate(resp.json())
    assert response.total == 2
    assert response.success_count == 2
    assert response.error_count == 0
    assert all(r.status == "success" for r in response.results)


async def test_bulk_metadata_endpoint_unknown_vault_returns_404(seeded_app):
    """An unregistered vault_id is rejected by the get_vault_id dependency."""
    app, _vault_id, _ids = seeded_app
    body = {"items": []}

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post("/sage_vaults/ghost/metadata/bulk", json=body)

    assert resp.status_code == 404, resp.text
    assert resp.json()["code"] == "vault_not_found"


async def test_bulk_metadata_endpoint_partial_failure_still_returns_200(seeded_app):
    """Per-item failure surfaces in the response envelope; the batch is a
    successful operation overall (200, not 500). Load-bearing contract test
    for the non-atomic semantics at the HTTP layer."""
    app, vault_id, seeded_ids = seeded_app
    body = {
        "items": [
            {"document_id": seeded_ids[0], "tags": {"add": ["b"]}},
            # 'a' is already present on seeded_ids[1]; tag_add_conflict.
            {"document_id": seeded_ids[1], "tags": {"add": ["a"]}},
        ]
    }

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(f"/sage_vaults/{vault_id}/metadata/bulk", json=body)

    assert resp.status_code == 200, resp.text
    response = BulkMetadataResponse.model_validate(resp.json())
    assert response.success_count == 1
    assert response.error_count == 1
    assert response.results[1].status == "error"
    assert response.results[1].error["error"] == "tag_add_conflict"


async def test_bulk_metadata_endpoint_empty_items_returns_200(seeded_app):
    """Empty items list is valid input."""
    app, vault_id, _ids = seeded_app
    body = {"items": []}

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(f"/sage_vaults/{vault_id}/metadata/bulk", json=body)

    assert resp.status_code == 200, resp.text
    response = BulkMetadataResponse.model_validate(resp.json())
    assert response.total == 0
    assert response.results == []
