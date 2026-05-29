"""HTTP integration tests for the bulk-link endpoint.

POST /sage_vaults/{vault_id}/edges.

The HTTP layer is exercised directly via an ASGI transport so the
FastAPI parameter binding, request-body parsing, response-model
serialization, status-code mapping, and the `get_vault_id` dependency
are all in scope. The MCP-tool-surface tests in test_sage_bulk_link.py
cover the full per-item / dry-run / response_mode behavioral matrix;
this file's job is the HTTP-specific concerns plus one round-trip per
response_mode branch.
"""

from __future__ import annotations

import asyncio

import pytest
from httpx import ASGITransport, AsyncClient

from sage import mcp_server
from sage.adapters.stubs import StubContentStore
from sage.app import _initialize_services, create_app
from sage.config import VaultConfig
from sage.models.schemas import BulkLinkResponse
from tests.sage.test_lifecycle import _id, _make_doc


@pytest.fixture
async def seeded_app(minimal_vault_config_dict, monkeypatch):
    """Boot an app with one vault and four active documents seeded.

    Four endpoints support multi-edge batches in a hub-and-spoke shape
    (each item's natural-key triple is distinct without colliding with
    its siblings) and one self-ref item for the per-item-error path.
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
    graph_store = app.state.vault_registry[vault_id].graph_store

    seeded_ids = [_id(f"doc_bulk_link_router_{n}") for n in range(4)]
    for doc_id in seeded_ids:
        await graph_store.insert_document(_make_doc(doc_id))

    yield app, vault_id, seeded_ids

    await asyncio.sleep(0.1)
    if vault_id in app.state.vault_registry:
        await app.state.vault_registry[vault_id].graph_store.close()
    mcp_server._vaults.clear()


def _ref_item(source: str, target: str) -> dict:
    """Well-formed references edge for the HTTP request body."""
    return {
        "source_id": source,
        "target_id": target,
        "edge_type": "references",
        "source_valid_from_version": source,
        "target_valid_from_version": target,
    }


async def test_bulk_endpoint_happy_path_returns_200_and_response_envelope(seeded_app):
    """All-success batch returns 200 with a BulkLinkResponse body."""
    app, vault_id, ids = seeded_app
    body = {"items": [_ref_item(ids[0], ids[1]), _ref_item(ids[0], ids[2])]}

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(f"/sage_vaults/{vault_id}/edges", json=body)

    assert resp.status_code == 200, resp.text
    response = BulkLinkResponse.model_validate(resp.json())
    assert response.total == 2
    assert response.success_count == 2
    assert response.error_count == 0
    assert all(r.status == "success" for r in response.results)


async def test_bulk_endpoint_unknown_vault_returns_404(seeded_app):
    """An unregistered vault_id is rejected by the get_vault_id dependency."""
    app, _vault_id, _ids = seeded_app
    body = {"items": []}

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post("/sage_vaults/ghost/edges", json=body)

    assert resp.status_code == 404, resp.text
    assert resp.json()["code"] == "vault_not_found"


async def test_bulk_endpoint_partial_failure_still_returns_200(seeded_app):
    """Per-item failure surfaces in the response envelope; the batch is
    a successful operation overall (200, not 500). Load-bearing contract
    test for non-atomic semantics at the HTTP layer."""
    app, vault_id, ids = seeded_app
    body = {
        "items": [
            _ref_item(ids[0], ids[1]),
            _ref_item(ids[2], ids[2]),  # self-ref
        ]
    }

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(f"/sage_vaults/{vault_id}/edges", json=body)

    assert resp.status_code == 200, resp.text
    response = BulkLinkResponse.model_validate(resp.json())
    assert response.success_count == 1
    assert response.error_count == 1
    assert response.results[1].status == "error"
    assert response.results[1].error["error"] == "self_referential_edge"


async def test_bulk_endpoint_empty_items_returns_200(seeded_app):
    """Empty items list is valid input."""
    app, vault_id, _ids = seeded_app
    body = {"items": []}

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(f"/sage_vaults/{vault_id}/edges", json=body)

    assert resp.status_code == 200, resp.text
    response = BulkLinkResponse.model_validate(resp.json())
    assert response.total == 0
    assert response.results == []


async def test_bulk_endpoint_malformed_body_returns_422(seeded_app):
    """FastAPI rejects a request whose items omit a required field
    (edge_type) at the request-body validation layer — 422, not 200
    with an item-error envelope. This is distinct from the MCP-tool
    contract (which routes shape failures through error_response and
    returns the same envelope as semantic errors) because the HTTP
    surface uses FastAPI's native 422 path."""
    app, vault_id, ids = seeded_app
    body = {
        "items": [
            {
                "source_id": ids[0],
                "target_id": ids[1],
                # Missing required edge_type
                "source_valid_from_version": ids[0],
                "target_valid_from_version": ids[1],
            }
        ]
    }

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(f"/sage_vaults/{vault_id}/edges", json=body)

    assert resp.status_code == 422, resp.text


async def test_bulk_endpoint_response_mode_light_strips_edge_body(seeded_app):
    """response_mode='light' at the HTTP layer drops the edge body from
    success entries; created and existing_rationale fields are
    preserved (the only natural-key idempotency signals callers have
    when the body is gone)."""
    app, vault_id, ids = seeded_app
    body = {
        "items": [_ref_item(ids[0], ids[1]), _ref_item(ids[0], ids[2])],
        "response_mode": "light",
    }

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(f"/sage_vaults/{vault_id}/edges", json=body)

    assert resp.status_code == 200, resp.text
    payload = resp.json()
    assert payload["success_count"] == 2
    for entry in payload["results"]:
        assert entry["status"] == "success"
        # FastAPI default serializer leaves None-valued Optional fields
        # either absent or null; both representations are acceptable.
        assert entry.get("edge") is None
        # signals preserved under light per the response_mode contract.
        assert entry["created"] is True


async def test_bulk_endpoint_response_mode_full_preserves_edge_body(seeded_app):
    """response_mode='full' at the HTTP layer preserves the edge body."""
    app, vault_id, ids = seeded_app
    body = {
        "items": [_ref_item(ids[0], ids[1])],
        "response_mode": "full",
    }

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(f"/sage_vaults/{vault_id}/edges", json=body)

    assert resp.status_code == 200, resp.text
    payload = resp.json()
    entry = payload["results"][0]
    assert entry["status"] == "success"
    assert entry["edge"]["edge_type"] == "references"
    assert entry["edge"]["id"]  # non-empty UUID string


async def test_bulk_endpoint_dry_run_carries_envelope_echo(seeded_app):
    """dry_run=True propagates through the HTTP layer and is echoed in
    the response envelope; no edges are persisted."""
    app, vault_id, ids = seeded_app
    body = {
        "items": [_ref_item(ids[0], ids[1])],
        "dry_run": True,
        "response_mode": "full",
    }

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(f"/sage_vaults/{vault_id}/edges", json=body)

    assert resp.status_code == 200, resp.text
    payload = resp.json()
    assert payload["dry_run"] is True
    # Sentinel id on the would-be edge.
    assert payload["results"][0]["edge"]["id"] == "00000000-0000-0000-0000-000000000000"
    # Anti-coincidental-pass: confirm no edge is persisted.
    graph_store = app.state.vault_registry[vault_id].graph_store
    persisted = await graph_store.get_edges_by_source(ids[0], "references")
    assert persisted == []
