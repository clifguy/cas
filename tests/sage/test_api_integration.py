"""API integration tests via FastAPI TestClient.

Verifies the HTTP layer: routing, status codes, error response format,
and end-to-end request/response contract.
"""

import asyncio
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from sage.adapters.stubs import (
    StubAbstractionProvider,
    StubContentStore,
    StubEmbeddingProvider,
)
from sage.app import _initialize_services, create_app
from sage.config import VaultConfig


@pytest.fixture
async def app(minimal_vault_config_dict, tmp_vault_dir):
    """Create a FastAPI app with test config, manually initializing services."""
    config = VaultConfig.model_validate(minimal_vault_config_dict)
    app = create_app(config=config)

    # Manually initialize services (lifespan not triggered by ASGITransport)
    await _initialize_services(
        app,
        config,
        content_store=StubContentStore(),
        embedding_provider=StubEmbeddingProvider(),
        abstraction_provider=StubAbstractionProvider(),
    )

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
        json={"display_name": "new_agent", "user_type": "agent"},
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["display_name"] == "new_agent"
    assert body["user_type"] == "agent"
    assert "id" in body


# ---------------------------------------------------------------------------
# Ingestion
# ---------------------------------------------------------------------------


async def test_ingest_201(client):
    resp = await client.post(
        "/sage_vaults/test_vault/documents",
        json={"source": "test/sample.md", "source_type": "markdown"},
    )
    assert resp.status_code == 201
    body = resp.json()
    assert "document" in body
    assert body["pipeline_status"] == "abstraction_complete"
    assert body["document"]["source_path"] == "test/sample.md"


async def test_ingest_duplicate_409(client):
    # First ingest
    resp1 = await client.post(
        "/sage_vaults/test_vault/documents",
        json={"source": "test/sample.md", "source_type": "markdown"},
    )
    assert resp1.status_code == 201

    await asyncio.sleep(0.1)

    # Duplicate ingest
    resp2 = await client.post(
        "/sage_vaults/test_vault/documents",
        json={"source": "test/sample.md", "source_type": "markdown"},
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
        json={"source": "test/sample.md", "source_type": "markdown"},
    )
    doc_id = resp.json()["document"]["id"]

    resp2 = await client.get(f"/sage_vaults/test_vault/documents/{doc_id}")
    assert resp2.status_code == 200
    assert resp2.json()["id"] == doc_id


async def test_get_document_404(client):
    resp = await client.get("/sage_vaults/test_vault/documents/deadbeef_nonexistent")
    assert resp.status_code == 404
    assert resp.json()["code"] == "document_not_found"


async def test_get_document_malformed_id_returns_invalid_document_id_400(client):
    """A malformed document_id on the FastAPI read path returns the structured
    invalid_document_id (400) envelope, not FastAPI's native 422 nor the 404
    not-found discriminator. The vault is registered, so binding reaches the
    document_id path-parameter validator; that rejection is translated into
    the typed SAGE 400 by translate_validation_error -- the HTTP-surface
    counterpart of the MCP boundary fix. Distinguishable from the native 422
    ``{"detail": [...]}`` array by both the status code and the ``code`` field.
    """
    resp = await client.get("/sage_vaults/test_vault/documents/not-a-doc-id")
    assert resp.status_code == 400, resp.text
    body = resp.json()
    assert body["code"] == "invalid_document_id", body
    assert body["detail"]["document_id"] == "not-a-doc-id", body


# ---------------------------------------------------------------------------
# Typed-alias structured-error family on the FastAPI transport.
#
# The generic RequestValidationError handler routes every translate-recognized
# alias rejection to its structured 400 (status_code=sage_err.status_code), so a
# malformed value on any alias-bearing endpoint returns 400 + code + detail
# rather than FastAPI's native 422. The detail key is the alias name. user_id is
# absent here: SetEditorsRequest (its only carrier) is wired to no route, so it
# has no FastAPI surface -- it is covered at the shared choke point in
# test_mcp_server.py::test_translate_validation_error_maps_typed_alias_family.
# ---------------------------------------------------------------------------


async def test_malformed_vault_id_returns_invalid_vault_id_400(client):
    """A malformed vault_id path segment returns the structured invalid_vault_id
    (400) from binding validation, not FastAPI's native 422."""
    resp = await client.get("/sage_vaults/NOT-A-VAULT/documents/deadbeef_a")
    assert resp.status_code == 400, resp.text
    body = resp.json()
    assert body["code"] == "invalid_vault_id", body
    assert body["detail"]["vault_id"] == "NOT-A-VAULT", body


async def test_malformed_edge_id_returns_invalid_edge_id_400(client):
    """A malformed edge_id path segment on DELETE /edges/{edge_id} returns the
    structured invalid_edge_id (400)."""
    resp = await client.delete("/sage_vaults/test_vault/edges/not-a-uuid")
    assert resp.status_code == 400, resp.text
    body = resp.json()
    assert body["code"] == "invalid_edge_id", body
    assert body["detail"]["edge_id"] == "not-a-uuid", body


async def test_malformed_function_id_returns_invalid_function_id_400(client):
    """A malformed function_id path segment on GET /preconditions/{function_id}
    returns the structured invalid_function_id (400)."""
    resp = await client.get("/sage_vaults/test_vault/preconditions/not-a-fn")
    assert resp.status_code == 400, resp.text
    body = resp.json()
    assert body["code"] == "invalid_function_id", body
    assert body["detail"]["function_id"] == "not-a-fn", body


async def test_malformed_document_date_returns_invalid_document_date_400(client):
    """A malformed document_date in the metadata-patch body returns the
    structured invalid_document_date (400)."""
    resp = await client.post(
        "/sage_vaults/test_vault/metadata",
        json={"items": [{"document_id": "deadbeef_a", "document_date": "2026-13-99"}]},
    )
    assert resp.status_code == 400, resp.text
    body = resp.json()
    assert body["code"] == "invalid_document_date", body
    assert body["detail"]["document_date"] == "2026-13-99", body


async def test_malformed_sha256_returns_invalid_sha256_400(client):
    """A malformed synced_from_content_hash in the create-edges body returns the
    structured invalid_sha256 (400)."""
    resp = await client.post(
        "/sage_vaults/test_vault/edges",
        json={
            "items": [
                {
                    "source_id": "deadbeef_a",
                    "target_id": "deadbeef_b",
                    "edge_type": "supersedes",
                    "synced_from_content_hash": "deadbeef",
                }
            ]
        },
    )
    assert resp.status_code == 400, resp.text
    body = resp.json()
    assert body["code"] == "invalid_sha256", body
    assert body["detail"]["sha256"] == "deadbeef", body


# ---------------------------------------------------------------------------
# Lifecycle transition
# ---------------------------------------------------------------------------


async def test_lifecycle_transition_200(client):
    """POST /lifecycles with one item (CAS-ADR-029 v4 plural-noun)."""
    # Ingest
    resp = await client.post(
        "/sage_vaults/test_vault/documents",
        json={"source": "test/sample.md", "source_type": "markdown"},
    )
    doc_id = resp.json()["document"]["id"]

    # Archive via the consolidated plural-noun endpoint.
    resp2 = await client.post(
        "/sage_vaults/test_vault/lifecycles",
        json={"items": [{"document_id": doc_id, "action": "archive"}]},
    )
    assert resp2.status_code == 200
    body = resp2.json()
    assert body["success_count"] == 1 and body["error_count"] == 0
    per = body["results"][0]
    assert per["status"] == "success"
    assert per["document"]["lifecycle_status"] == "archived"


async def test_lifecycle_409_invalid_transition(client):
    """A per-item invalid transition surfaces in the response envelope,
    not as a batch-level HTTP 409 (CAS-ADR-029 v4 not-atomic batch).
    """
    # Ingest
    resp = await client.post(
        "/sage_vaults/test_vault/documents",
        json={"source": "test/sample.md", "source_type": "markdown"},
    )
    doc_id = resp.json()["document"]["id"]

    # Try reactivate on active doc (invalid). Endpoint returns 200 with
    # a per-item error envelope (the batch is not atomic).
    resp2 = await client.post(
        "/sage_vaults/test_vault/lifecycles",
        json={"items": [{"document_id": doc_id, "action": "reactivate"}]},
    )
    assert resp2.status_code == 200
    body = resp2.json()
    assert body["success_count"] == 0 and body["error_count"] == 1
    per = body["results"][0]
    assert per["status"] == "error"
    err = per["error"]
    assert err["error"] == "invalid_lifecycle_transition"
    assert "valid_actions" in err["detail"]


# ---------------------------------------------------------------------------
# Vault scoping
# ---------------------------------------------------------------------------


async def test_wrong_vault_404(client):
    resp = await client.get("/sage_vaults/wrong_vault/documents/anything")
    assert resp.status_code == 404
    assert resp.json()["code"] == "vault_not_found"


# ---------------------------------------------------------------------------
# Graph operations (Slice 2)
# ---------------------------------------------------------------------------


async def test_link_201(app, client):
    """POST /edges with one item returns 200 + per-item success envelope
    (CAS-ADR-029 v4 plural-noun convention).
    """
    resp1 = await client.post(
        "/sage_vaults/test_vault/documents",
        json={"source": "test/sample.md", "source_type": "markdown"},
    )
    assert resp1.status_code == 201
    doc_id_a = resp1.json()["document"]["id"]

    # Create a second source file for a distinct document

    storage_root = Path(app.state.config.vault.storage_root).expanduser()
    (storage_root / "test" / "sample2.md").write_text("# Second Document\n\nDifferent content.")

    resp2 = await client.post(
        "/sage_vaults/test_vault/documents",
        json={"source": "test/sample2.md", "source_type": "markdown"},
    )
    assert resp2.status_code == 201
    doc_id_b = resp2.json()["document"]["id"]

    resp3 = await client.post(
        "/sage_vaults/test_vault/edges",
        json={
            "items": [
                {
                    "source_id": doc_id_a,
                    "target_id": doc_id_b,
                    "edge_type": "references",
                    "source_valid_from_version": doc_id_a,
                    "target_valid_from_version": doc_id_b,
                    "rationale": "test link",
                },
            ],
        },
    )
    assert resp3.status_code == 200
    body = resp3.json()
    assert body["success_count"] == 1 and body["error_count"] == 0
    per = body["results"][0]
    assert per["status"] == "success"
    assert per["created"] is True
    edge = per["edge"]
    assert edge["source_id"] == doc_id_a
    assert edge["target_id"] == doc_id_b
    assert edge["edge_type"] == "references"
    assert "id" in edge


async def test_link_self_referential_400(client):
    """A per-item self-referential edge surfaces in the response
    envelope, not as a batch-level 400.
    """
    resp1 = await client.post(
        "/sage_vaults/test_vault/documents",
        json={"source": "test/sample.md", "source_type": "markdown"},
    )
    doc_id = resp1.json()["document"]["id"]

    resp2 = await client.post(
        "/sage_vaults/test_vault/edges",
        json={
            "items": [
                {
                    "source_id": doc_id,
                    "target_id": doc_id,
                    "edge_type": "references",
                },
            ],
        },
    )
    assert resp2.status_code == 200
    body = resp2.json()
    assert body["success_count"] == 0 and body["error_count"] == 1
    per = body["results"][0]
    assert per["status"] == "error"
    assert per["error"]["error"] == "self_referential_edge"


async def test_check_preconditions_200(client):
    """GET /preconditions/{id} returns precondition result."""
    resp1 = await client.post(
        "/sage_vaults/test_vault/documents",
        json={"source": "test/sample.md", "source_type": "markdown"},
    )
    doc_id = resp1.json()["document"]["id"]

    resp2 = await client.get(f"/sage_vaults/test_vault/preconditions/{doc_id}")
    assert resp2.status_code == 200
    body = resp2.json()
    assert body["function_id"] == doc_id
    assert body["satisfied"] is True  # No dependencies = vacuously satisfied
    assert body["checks"] == []


async def test_traverse_200(client):
    """POST /traverse returns traversal result."""
    resp1 = await client.post(
        "/sage_vaults/test_vault/documents",
        json={"source": "test/sample.md", "source_type": "markdown"},
    )
    doc_id = resp1.json()["document"]["id"]

    resp2 = await client.post(
        "/sage_vaults/test_vault/traverse",
        json={"start_id": doc_id},
    )
    assert resp2.status_code == 200
    body = resp2.json()
    assert body["start_id"] == doc_id
    assert isinstance(body["nodes"], list)


# ---------------------------------------------------------------------------
# Retrieval (Slice 3)
# ---------------------------------------------------------------------------


async def test_discover_semantic_200(client):
    """POST /discover with semantic mode returns 200."""
    # Ingest a document first
    resp1 = await client.post(
        "/sage_vaults/test_vault/documents",
        json={"source": "test/sample.md", "source_type": "markdown"},
    )
    assert resp1.status_code == 201

    # Wait for background pipeline
    await asyncio.sleep(0.5)

    resp2 = await client.post(
        "/sage_vaults/test_vault/discover",
        json={"mode": "semantic", "query": "sample content"},
    )
    assert resp2.status_code == 200
    body = resp2.json()
    assert body["mode"] == "semantic"
    assert isinstance(body["results"], list)


async def test_discover_deterministic_200(app, client):
    """POST /discover with deterministic mode returns matching chunks."""
    resp1 = await client.post(
        "/sage_vaults/test_vault/documents",
        json={"source": "test/sample.md", "source_type": "markdown"},
    )
    assert resp1.status_code == 201
    doc_id = resp1.json()["document"]["id"]

    # Wait for background pipeline to index chunks
    await asyncio.sleep(0.5)

    resp2 = await client.post(
        "/sage_vaults/test_vault/discover",
        json={
            "mode": "deterministic",
            "document_id": doc_id,
            "heading_path": "Sample Document",
        },
    )
    assert resp2.status_code == 200
    body = resp2.json()
    assert body["mode"] == "deterministic"
    assert len(body["results"]) > 0


async def test_discover_semantic_missing_query_422(client):
    """POST /discover semantic mode without query returns validation error."""
    resp = await client.post(
        "/sage_vaults/test_vault/discover",
        json={"mode": "semantic"},
    )
    # Missing query for semantic mode = 400
    assert resp.status_code == 400


# ---------------------------------------------------------------------------
# Retrieval: discover — ADR-028 error envelope on parameter validation
# ---------------------------------------------------------------------------


async def test_discover_invalid_mode_http(client):
    """POST /discover with unknown mode returns 400 + ADR-028 envelope, not FastAPI 422."""
    resp = await client.post(
        "/sage_vaults/test_vault/discover",
        json={"mode": "bogus"},
    )
    assert resp.status_code == 400
    body = resp.json()
    assert body["code"] == "invalid_mode"
    assert body["detail"]["mode"] == "bogus"
    assert set(body["detail"]["valid_modes"]) == {
        "semantic",
        "keyword",
        "catalog",
        "deterministic",
    }


async def test_discover_unknown_filter_key_http(client):
    """POST /discover with unknown filter key returns 400 + unknown_filter_key envelope."""
    resp = await client.post(
        "/sage_vaults/test_vault/discover",
        json={"mode": "catalog", "filters": {"tickett_id": "T-0001"}},
    )
    assert resp.status_code == 400
    body = resp.json()
    assert body["code"] == "unknown_filter_key"
    assert body["detail"]["key"] == "tickett_id"


async def test_discover_invalid_filter_shape_http(client):
    """POST /discover with wrong-typed filter value returns 400 + invalid_filter_shape envelope."""
    resp = await client.post(
        "/sage_vaults/test_vault/discover",
        json={"mode": "catalog", "filters": {"tags": 42}},
    )
    assert resp.status_code == 400
    body = resp.json()
    assert body["code"] == "invalid_filter_shape"
    assert body["detail"]["field"] == "tags"


async def test_discover_semantic_missing_query_http_unchanged(client):
    """Regression guard: existing missing_query envelope path preserved (still 400)."""
    resp = await client.post(
        "/sage_vaults/test_vault/discover",
        json={"mode": "semantic"},
    )
    assert resp.status_code == 400
    body = resp.json()
    assert body["code"] == "missing_query"


# ---------------------------------------------------------------------------
# Utilities (Slice 4)
# ---------------------------------------------------------------------------


async def test_export_projection_200(app, client):
    """POST /documents/{id}/export writes projection and returns 200."""
    # Ingest a document
    resp1 = await client.post(
        "/sage_vaults/test_vault/documents",
        json={"source": "test/sample.md", "source_type": "markdown"},
    )
    assert resp1.status_code == 201
    doc_id = resp1.json()["document"]["id"]

    # Wait for pipeline to index chunks
    await asyncio.sleep(0.5)

    resp2 = await client.post(
        f"/sage_vaults/test_vault/documents/{doc_id}/export",
        json={"output_path": "exports/test_export.md"},
    )
    assert resp2.status_code == 200
    body = resp2.json()
    assert body["document_id"] == doc_id
    assert "exports/test_export.md" in body["output_path"]


async def test_export_projection_path_traversal_400(client):
    """POST /documents/{id}/export with../ path returns 400."""
    # Ingest first
    resp1 = await client.post(
        "/sage_vaults/test_vault/documents",
        json={"source": "test/sample.md", "source_type": "markdown"},
    )
    doc_id = resp1.json()["document"]["id"]

    await asyncio.sleep(0.5)

    resp2 = await client.post(
        f"/sage_vaults/test_vault/documents/{doc_id}/export",
        json={"output_path": "../../etc/passwd"},
    )
    assert resp2.status_code == 400
    assert resp2.json()["code"] == "path_traversal_denied"


async def test_read_projection_200(app, client):
    """GET /documents/{id}/projection returns full text and metadata."""
    resp1 = await client.post(
        "/sage_vaults/test_vault/documents",
        json={"source": "test/sample.md", "source_type": "markdown"},
    )
    assert resp1.status_code == 201
    doc_id = resp1.json()["document"]["id"]

    await asyncio.sleep(0.5)

    resp2 = await client.get(
        f"/sage_vaults/test_vault/documents/{doc_id}/projection",
    )
    assert resp2.status_code == 200
    body = resp2.json()
    assert body["document_id"] == doc_id
    assert "projection_text" in body
    assert len(body["projection_text"]) > 0
    assert "title" in body


async def test_read_projection_404(client):
    """GET /documents/{id}/projection with nonexistent id returns 404."""
    resp = await client.get(
        "/sage_vaults/test_vault/documents/deadbeef_nonexistent/projection",
    )
    assert resp.status_code == 404
    assert resp.json()["code"] == "document_not_found"


# ---------------------------------------------------------------------------
# Open-with-local-app
# ---------------------------------------------------------------------------


async def test_open_document_200(client, monkeypatch, tmp_vault_dir):
    """POST /documents/{id}/open invokes the OS opener and returns 200."""
    calls = []

    def fake_popen(args, *a, **kw):
        calls.append(args)

        class _Dummy:
            pass

        return _Dummy()

    monkeypatch.setattr("subprocess.Popen", fake_popen)
    monkeypatch.setattr("sys.platform", "darwin")

    resp1 = await client.post(
        "/sage_vaults/test_vault/documents",
        json={"source": "test/sample.md", "source_type": "markdown"},
    )
    doc_id = resp1.json()["document"]["id"]

    resp2 = await client.post(
        f"/sage_vaults/test_vault/documents/{doc_id}/open",
    )
    assert resp2.status_code == 200
    body = resp2.json()
    assert body["opened"] is True
    assert body["path"].endswith("test/sample.md")

    # Subprocess got invoked with the macOS `open` command and the resolved path.
    assert len(calls) == 1
    invoked = calls[0]
    assert invoked[0] == "open"
    assert invoked[1].endswith("test/sample.md")


async def test_open_document_uses_xdg_open_on_linux(client, monkeypatch):
    """On Linux, the endpoint invokes xdg-open."""
    calls = []

    def fake_popen(args, *a, **kw):
        calls.append(args)

        class _Dummy:
            pass

        return _Dummy()

    monkeypatch.setattr("subprocess.Popen", fake_popen)
    monkeypatch.setattr("sys.platform", "linux")

    resp1 = await client.post(
        "/sage_vaults/test_vault/documents",
        json={"source": "test/sample.md", "source_type": "markdown"},
    )
    doc_id = resp1.json()["document"]["id"]

    resp2 = await client.post(
        f"/sage_vaults/test_vault/documents/{doc_id}/open",
    )
    assert resp2.status_code == 200
    assert calls[0][0] == "xdg-open"


async def test_open_document_404_unknown_id(client):
    """Unknown document id returns 404 with document_not_found."""
    resp = await client.post(
        "/sage_vaults/test_vault/documents/deadbeef_nonexistent/open",
    )
    assert resp.status_code == 404
    assert resp.json()["code"] == "document_not_found"


async def test_open_document_missing_file_404(client, tmp_vault_dir):
    """If the source file is missing on disk, return content_file_missing (404)."""
    resp1 = await client.post(
        "/sage_vaults/test_vault/documents",
        json={"source": "test/sample.md", "source_type": "markdown"},
    )
    doc_id = resp1.json()["document"]["id"]

    # Remove the file from disk after ingestion.
    (tmp_vault_dir / "sources" / "test" / "sample.md").unlink()

    resp2 = await client.post(
        f"/sage_vaults/test_vault/documents/{doc_id}/open",
    )
    assert resp2.status_code == 404
    assert resp2.json()["code"] == "content_file_missing"


async def test_eval_retrieval_not_configured_400(client):
    """POST /eval-retrieval without assertions config returns 400."""
    resp = await client.post("/sage_vaults/test_vault/eval-retrieval")
    assert resp.status_code == 400
    assert resp.json()["code"] == "assertions_not_configured"


async def test_refresh_views_200(client):
    """POST /refresh-views returns 200 with views_generated count."""
    # Ingest a document so there's something to generate views for
    resp1 = await client.post(
        "/sage_vaults/test_vault/documents",
        json={"source": "test/sample.md", "source_type": "markdown"},
    )
    assert resp1.status_code == 201

    resp2 = await client.post("/sage_vaults/test_vault/refresh-views")
    assert resp2.status_code == 200
    body = resp2.json()
    assert body["vault_id"] == "test_vault"
    assert isinstance(body["views_generated"], int)
    assert body["views_generated"] >= 1  # at least by_lifecycle/active


# ---------------------------------------------------------------------------
# Edge_id path-parameter validation on routes that take edge_id
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "method,path",
    [
        ("DELETE", "/sage_vaults/test_vault/edges/{edge_id}"),
        ("POST", "/sage_vaults/test_vault/staging-edges/{edge_id}/confirm"),
        ("POST", "/sage_vaults/test_vault/staging-edges/{edge_id}/dismiss"),
    ],
    ids=["unlink", "confirm_staging_edge", "dismiss_staging_edge"],
)
@pytest.mark.parametrize(
    "bad_input",
    ["not-a-uuid", "12345", "deadbeef-dead-beef"],
    ids=["random_text", "digits", "truncated_uuid"],
)
async def test_edge_id_route_rejects_non_uuid_400(client, method, path, bad_input):
    """Non-UUID edge_id path params surface as the structured invalid_edge_id
    (400): the typed-alias family translates the binding rejection into a typed
    SAGE 400 instead of FastAPI's native 422."""
    resp = await client.request(method, path.format(edge_id=bad_input))
    assert resp.status_code == 400, resp.text
    body = resp.json()
    assert body["code"] == "invalid_edge_id", body
    assert body["detail"]["edge_id"] == bad_input, body
