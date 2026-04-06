"""API integration tests via FastAPI TestClient.

Verifies the HTTP layer: routing, status codes, error response format,
and end-to-end request/response contract.
"""

import asyncio
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from sage.app import create_app, _initialize_services
from sage.config import VaultConfig


@pytest.fixture
async def app(minimal_vault_config_dict, tmp_vault_dir):
    """Create a FastAPI app with test config, manually initializing services."""
    config = VaultConfig.model_validate(minimal_vault_config_dict)
    app = create_app(config=config)

    # Manually initialize services (lifespan not triggered by ASGITransport)
    await _initialize_services(app, config)

    # Create a test source file
    sources = tmp_vault_dir / "sources"
    test_dir = sources / "test"
    test_dir.mkdir(parents=True, exist_ok=True)
    (test_dir / "sample.md").write_text("# Sample Document\n\nSample content.")

    yield app
    # Wait for any background pipeline tasks to finish before closing
    await asyncio.sleep(0.5)
    await app.state.graph_store.close()


@pytest.fixture
async def client(app):
    """Async HTTP client for the SAGE API."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


# ---------------------------------------------------------------------------
# User registration
# ---------------------------------------------------------------------------

async def test_register_user_201(client):
    resp = await client.post(
        "/sage_vaults/test_vault/users",
        json={"display_name": "new_agent", "type": "agent"},
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["display_name"] == "new_agent"
    assert body["type"] == "agent"
    assert "id" in body


# ---------------------------------------------------------------------------
# Ingestion
# ---------------------------------------------------------------------------

async def test_ingest_201(client):
    resp = await client.post(
        "/sage_vaults/test_vault/documents",
        json={"source": "test/sample.md", "adapter": "markdown"},
    )
    assert resp.status_code == 201
    body = resp.json()
    assert "document" in body
    assert body["pipeline_status"] == "projection_complete"
    assert body["document"]["source_path"] == "test/sample.md"


async def test_ingest_duplicate_409(client):
    # First ingest
    resp1 = await client.post(
        "/sage_vaults/test_vault/documents",
        json={"source": "test/sample.md", "adapter": "markdown"},
    )
    assert resp1.status_code == 201

    await asyncio.sleep(0.1)

    # Duplicate ingest
    resp2 = await client.post(
        "/sage_vaults/test_vault/documents",
        json={"source": "test/sample.md", "adapter": "markdown"},
    )
    assert resp2.status_code == 409
    body = resp2.json()
    assert body["code"] == "duplicate_content"


# ---------------------------------------------------------------------------
# Get document
# ---------------------------------------------------------------------------

async def test_get_document_200(client):
    # Ingest first
    resp = await client.post(
        "/sage_vaults/test_vault/documents",
        json={"source": "test/sample.md", "adapter": "markdown"},
    )
    doc_id = resp.json()["document"]["id"]

    resp2 = await client.get(f"/sage_vaults/test_vault/documents/{doc_id}")
    assert resp2.status_code == 200
    assert resp2.json()["id"] == doc_id


async def test_get_document_404(client):
    resp = await client.get("/sage_vaults/test_vault/documents/nonexistent")
    assert resp.status_code == 404
    assert resp.json()["code"] == "document_not_found"


# ---------------------------------------------------------------------------
# Lifecycle transition
# ---------------------------------------------------------------------------

async def test_lifecycle_transition_200(client):
    # Ingest
    resp = await client.post(
        "/sage_vaults/test_vault/documents",
        json={"source": "test/sample.md", "adapter": "markdown"},
    )
    doc_id = resp.json()["document"]["id"]

    # Archive
    resp2 = await client.post(
        f"/sage_vaults/test_vault/documents/{doc_id}/lifecycle",
        json={"action": "archive"},
    )
    assert resp2.status_code == 200
    assert resp2.json()["document"]["lifecycle_status"] == "archived"


async def test_lifecycle_409_invalid_transition(client):
    # Ingest
    resp = await client.post(
        "/sage_vaults/test_vault/documents",
        json={"source": "test/sample.md", "adapter": "markdown"},
    )
    doc_id = resp.json()["document"]["id"]

    # Try reactivate on active doc (invalid)
    resp2 = await client.post(
        f"/sage_vaults/test_vault/documents/{doc_id}/lifecycle",
        json={"action": "reactivate"},
    )
    assert resp2.status_code == 409
    body = resp2.json()
    assert body["code"] == "invalid_lifecycle_transition"
    assert "valid_actions" in body["detail"]


# ---------------------------------------------------------------------------
# Vault scoping
# ---------------------------------------------------------------------------

async def test_wrong_vault_404(client):
    resp = await client.get("/sage_vaults/wrong_vault/documents/anything")
    assert resp.status_code == 404
    assert resp.json()["code"] == "vault_not_found"
