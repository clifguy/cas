"""HTTP integration tests for the bulk lifecycle endpoint (T-0087).

POST /sage_vaults/{vault_id}/lifecycle/bulk.
"""

from __future__ import annotations

import asyncio

import pytest
from httpx import ASGITransport, AsyncClient

from sage import mcp_server
from sage.adapters.stubs import StubContentStore
from sage.app import _initialize_services, create_app
from sage.config import VaultConfig
from sage.models.schemas import BulkLifecycleResponse
from tests.sage.test_lifecycle import _id, _make_doc


@pytest.fixture
async def seeded_app(minimal_vault_config_dict, monkeypatch):
    """Boot an app with one vault and three active documents seeded."""
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

    yield app, vault_id, seeded_ids

    await asyncio.sleep(0.1)
    if vault_id in app.state.vault_registry:
        await app.state.vault_registry[vault_id].graph_store.close()
    mcp_server._vaults.clear()


async def test_bulk_endpoint_happy_path_returns_200_and_response_envelope(seeded_app):
    """All-success batch returns 200 with a BulkLifecycleResponse body."""
    app, vault_id, seeded_ids = seeded_app
    body = {
        "items": [
            {"document_id": seeded_ids[0], "action": "archive"},
            {"document_id": seeded_ids[1], "action": "archive"},
        ]
    }

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(f"/sage_vaults/{vault_id}/lifecycle/bulk", json=body)

    assert resp.status_code == 200, resp.text
    response = BulkLifecycleResponse.model_validate(resp.json())
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
        resp = await client.post("/sage_vaults/ghost/lifecycle/bulk", json=body)

    assert resp.status_code == 404, resp.text
    assert resp.json()["code"] == "vault_not_found"


async def test_bulk_endpoint_partial_failure_still_returns_200(seeded_app):
    """Per-item failure surfaces in the response envelope; the batch is a
    successful operation overall (200, not 500). This is the load-bearing
    contract test for the non-atomic semantics at the HTTP layer."""
    app, vault_id, seeded_ids = seeded_app
    body = {
        "items": [
            {"document_id": seeded_ids[0], "action": "archive"},
            {"document_id": _id("doc_ghost"), "action": "archive"},
        ]
    }

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(f"/sage_vaults/{vault_id}/lifecycle/bulk", json=body)

    assert resp.status_code == 200, resp.text
    response = BulkLifecycleResponse.model_validate(resp.json())
    assert response.success_count == 1
    assert response.error_count == 1
    assert response.results[1].status == "error"
    assert response.results[1].error["error"] == "document_not_found"


async def test_bulk_endpoint_empty_items_returns_200(seeded_app):
    """Empty items list is valid input."""
    app, vault_id, _ids = seeded_app
    body = {"items": []}

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(f"/sage_vaults/{vault_id}/lifecycle/bulk", json=body)

    assert resp.status_code == 200, resp.text
    response = BulkLifecycleResponse.model_validate(resp.json())
    assert response.total == 0
    assert response.results == []


# ---------------------------------------------------------------------------
# T-0153: response_mode parameter on the bulk lifecycle HTTP endpoint
# ---------------------------------------------------------------------------

_T0153_ABSTRACT = "Test abstract used as the bulk-mode bloat probe (T-0153)."


@pytest.fixture
async def seeded_six_app(minimal_vault_config_dict, monkeypatch):
    """T-0153 fixture for the HTTP router. Boot an app with six active
    documents each carrying a populated `semantic_abstract`. Six items so
    the threshold-default tests can cross the >5 boundary in either
    direction by slicing."""
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

    seeded_ids = [_id(f"doc_t0153_{n}") for n in range(6)]
    for doc_id in seeded_ids:
        await graph_store.insert_document(_make_doc(doc_id))
        await graph_store.update_document(doc_id, {"semantic_abstract": _T0153_ABSTRACT})

    yield app, vault_id, seeded_ids

    await asyncio.sleep(0.1)
    if vault_id in app.state.vault_registry:
        await app.state.vault_registry[vault_id].graph_store.close()
    mcp_server._vaults.clear()


def _entry_lacks_document(entry: dict) -> bool:
    """Light-mode predicate: the `document` field carries no body
    content on the wire. FastAPI's default serializer emits
    `"document": null` for an unset Optional; some configurations
    strip the key entirely via response_model_exclude_none=True.
    Both representations satisfy the contract."""
    return entry.get("document") is None


async def test_t0153_t1_http_light_strips_document_and_semantic_abstract(
    seeded_six_app,
):
    """T1 — Explicit response_mode='light' over HTTP drops the per-item
    `document` body. Batch size 3 is below the >5 threshold so this
    exercises the explicit-override path."""
    app, vault_id, seeded_ids = seeded_six_app
    body = {
        "response_mode": "light",
        "items": [{"document_id": d, "action": "archive"} for d in seeded_ids[:3]],
    }

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(f"/sage_vaults/{vault_id}/lifecycle/bulk", json=body)

    assert resp.status_code == 200, resp.text
    raw = resp.json()
    assert raw["success_count"] == 3
    for entry in raw["results"]:
        assert entry["status"] == "success"
        assert _entry_lacks_document(entry), (
            f"light mode must strip per-item document body; got {entry!r}"
        )
    assert _T0153_ABSTRACT not in resp.text, (
        "light mode must not leak the semantic_abstract probe string"
    )


async def test_t0153_t2_http_full_preserves_document_with_semantic_abstract(
    seeded_six_app,
):
    """T2 — Explicit response_mode='full' over HTTP preserves the
    current per-item shape, including semantic_abstract."""
    app, vault_id, seeded_ids = seeded_six_app
    body = {
        "response_mode": "full",
        "items": [{"document_id": d, "action": "archive"} for d in seeded_ids[:3]],
    }

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(f"/sage_vaults/{vault_id}/lifecycle/bulk", json=body)

    assert resp.status_code == 200, resp.text
    raw = resp.json()
    for entry in raw["results"]:
        assert entry["status"] == "success"
        assert entry["document"]["semantic_abstract"] == _T0153_ABSTRACT


async def test_t0153_t3_http_default_above_threshold_returns_light(seeded_six_app):
    """T3 — Default (no response_mode in body) with 6 items returns
    light. Anti-coincidental: `response_mode` is NOT in the body."""
    app, vault_id, seeded_ids = seeded_six_app
    body = {"items": [{"document_id": d, "action": "archive"} for d in seeded_ids[:6]]}

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(f"/sage_vaults/{vault_id}/lifecycle/bulk", json=body)

    assert resp.status_code == 200, resp.text
    raw = resp.json()
    assert raw["success_count"] == 6
    for entry in raw["results"]:
        assert _entry_lacks_document(entry), (
            f"default above threshold (6>5) must be light; got {entry!r}"
        )


async def test_t0153_t4_http_default_at_or_below_threshold_returns_full(
    seeded_six_app,
):
    """T4 — Default with 3 items returns full."""
    app, vault_id, seeded_ids = seeded_six_app
    body = {"items": [{"document_id": d, "action": "archive"} for d in seeded_ids[:3]]}

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(f"/sage_vaults/{vault_id}/lifecycle/bulk", json=body)

    assert resp.status_code == 200, resp.text
    raw = resp.json()
    for entry in raw["results"]:
        assert entry["document"]["semantic_abstract"] == _T0153_ABSTRACT


async def test_t0153_t5_http_error_envelope_intact_in_light_mode(seeded_six_app):
    """T5 — Error item in light mode carries the full error envelope
    (round-trip against full mode)."""
    app, vault_id, seeded_ids = seeded_six_app
    ghost_id = _id("doc_ghost_t0153_t5_router")
    light_body = {
        "response_mode": "light",
        "items": [
            {"document_id": seeded_ids[0], "action": "archive"},
            {"document_id": ghost_id, "action": "archive"},
        ],
    }
    full_body = {
        "response_mode": "full",
        "items": [
            {"document_id": seeded_ids[1], "action": "archive"},
            {"document_id": ghost_id, "action": "archive"},
        ],
    }

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        light_resp = await client.post(f"/sage_vaults/{vault_id}/lifecycle/bulk", json=light_body)
        full_resp = await client.post(f"/sage_vaults/{vault_id}/lifecycle/bulk", json=full_body)

    light_err = light_resp.json()["results"][1]
    full_err = full_resp.json()["results"][1]
    assert light_err["status"] == "error"
    assert full_err["status"] == "error"
    assert light_err["error"] == full_err["error"], (
        f"light mode must not strip error envelope; light={light_err!r} full={full_err!r}"
    )


async def test_t0153_t6_http_error_envelope_intact_in_full_mode(seeded_six_app):
    """T6 — Error item in full mode (regression guard)."""
    app, vault_id, seeded_ids = seeded_six_app
    ghost_id = _id("doc_ghost_t0153_t6_router")
    body = {
        "response_mode": "full",
        "items": [
            {"document_id": seeded_ids[0], "action": "archive"},
            {"document_id": ghost_id, "action": "archive"},
        ],
    }

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(f"/sage_vaults/{vault_id}/lifecycle/bulk", json=body)

    raw = resp.json()
    assert raw["results"][1]["status"] == "error"
    assert raw["results"][1]["error"]["error"] == "document_not_found"


async def test_t0153_t7_http_mixed_batch_in_light_mode(seeded_six_app):
    """T7 — Mixed-result batch in light mode."""
    app, vault_id, seeded_ids = seeded_six_app
    ghost_id = _id("doc_ghost_t0153_t7_router")
    body = {
        "response_mode": "light",
        "items": [
            {"document_id": seeded_ids[0], "action": "archive"},
            {"document_id": ghost_id, "action": "archive"},
            {"document_id": seeded_ids[1], "action": "archive"},
        ],
    }

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(f"/sage_vaults/{vault_id}/lifecycle/bulk", json=body)

    raw = resp.json()
    assert raw["success_count"] == 2
    assert raw["error_count"] == 1
    assert raw["total"] == 3
    assert raw["results"][0]["status"] == "success"
    assert _entry_lacks_document(raw["results"][0])
    assert raw["results"][1]["status"] == "error"
    assert raw["results"][1]["error"]["error"] == "document_not_found"
    assert raw["results"][2]["status"] == "success"
    assert _entry_lacks_document(raw["results"][2])


async def test_t0153_t8_http_invalid_response_mode_rejected_with_422(
    seeded_six_app,
):
    """T8 — Invalid response_mode in the request body is rejected by
    FastAPI's request validation (422). No per-item state changes."""
    app, vault_id, seeded_ids = seeded_six_app
    body = {
        "response_mode": "verbose",
        "items": [{"document_id": d, "action": "archive"} for d in seeded_ids[:3]],
    }

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(f"/sage_vaults/{vault_id}/lifecycle/bulk", json=body)

    assert resp.status_code == 422, resp.text
    # Anti-coincidental: no per-item state was committed.
    graph_store = app.state.vault_registry[vault_id].graph_store
    for doc_id in seeded_ids[:3]:
        stored = await graph_store.get_document(doc_id)
        assert stored.lifecycle_status == "active", (
            f"invalid response_mode must abort batch BEFORE per-item work; "
            f"{doc_id} should still be active"
        )


async def test_t0153_t9_http_empty_batch_with_explicit_light(seeded_six_app):
    """T9 — Empty items with response_mode='light' returns 200 cleanly."""
    app, vault_id, _ = seeded_six_app
    body = {"response_mode": "light", "items": []}

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(f"/sage_vaults/{vault_id}/lifecycle/bulk", json=body)

    assert resp.status_code == 200, resp.text
    raw = resp.json()
    assert raw["results"] == []
    assert raw["total"] == 0
