"""Integration tests for app.backend.dependencies (T-0049).

These tests exercise the new FastAPI ``Depends`` factories that resolve
``ScanService`` and ``IngestStreamingService`` from the request body's
``vault_id``. The pattern is the body-scoped analogue of SAGE's
path-scoped ``Depends(get_vault_id)`` chain.

The factories must:
  - Resolve services correctly when the registered vault matches.
  - Raise ``VaultNotFoundError`` (404 ``vault_not_found``) when the body
    points at an unregistered vault, BEFORE any response body is
    started (critical for ``/ingest`` which is otherwise a streaming
    200).
"""

from __future__ import annotations

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
from tests.app.test_app_backend import _make_vault_config_dict


@pytest.fixture
async def app_with_vault(tmp_path):
    """FastAPI app with a single vault registered under id 'pim_health'."""
    config = VaultConfig.model_validate(
        _make_vault_config_dict(tmp_path, "pim_health", "PIM Health")
    )
    app = create_app(config=config)
    await _initialize_services(
        app,
        config,
        content_store=StubContentStore(),
        embedding_provider=StubEmbeddingProvider(),
        abstraction_provider=StubAbstractionProvider(),
    )
    yield app, config
    await asyncio.sleep(0.2)
    for services in app.state.vault_registry.values():
        await services.graph_store.close()


@pytest.fixture
async def http_client(app_with_vault):
    app, config = app_with_vault
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client, config


class TestDependenciesModuleSurface:
    """T-0049 requires app.backend.dependencies to host the new
    Depends factories. These are the architectural commitments that
    the migration must satisfy; the integration tests below verify
    they wire correctly end-to-end."""

    def test_dependencies_module_exports_get_scan_service(self) -> None:
        from app.backend.dependencies import get_scan_service

        assert callable(get_scan_service)

    def test_dependencies_module_exports_get_ingest_streaming_service(self) -> None:
        from app.backend.dependencies import get_ingest_streaming_service

        assert callable(get_ingest_streaming_service)


class TestGetScanService:
    async def test_resolves_service_for_registered_vault(self, http_client, tmp_path):
        """The Depends factory must wire ScanService against the
        registered vault's services. We verify end-to-end by issuing
        a real /scan against an empty directory and asserting the
        canonical empty-success response shape."""
        client, _config = http_client
        scan_dir = tmp_path / "scan_inbox"
        scan_dir.mkdir()
        resp = await client.post(
            "/app/scan",
            json={"vault_id": "pim_health", "directory": str(scan_dir)},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["files"] == []
        assert body["warnings"] == []

    async def test_unknown_vault_returns_404_vault_not_found(self, http_client, tmp_path):
        """An unregistered vault_id must surface as 404 vault_not_found
        from the Depends factory BEFORE handler logic runs."""
        client, _config = http_client
        scan_dir = tmp_path / "scan_inbox"
        scan_dir.mkdir()
        resp = await client.post(
            "/app/scan",
            json={"vault_id": "does_not_exist", "directory": str(scan_dir)},
        )
        assert resp.status_code == 404, resp.text
        body = resp.json()
        assert body["code"] == "vault_not_found"


class TestGetIngestStreamingService:
    async def test_resolves_service_for_registered_vault(self, http_client, tmp_path):
        """The Depends factory must wire IngestStreamingService against
        the registered vault. We verify by issuing a real /ingest and
        confirming an SSE response with at least one progress event."""
        client, config = http_client
        sources = Path(config.vault.storage_root)
        doc = sources / "probe.md"
        doc.write_text("# Probe\n\nContent.")
        resp = await client.post(
            "/app/ingest",
            json={
                "vault_id": "pim_health",
                "files": [{"file_path": str(doc), "adapter": "markdown"}],
            },
        )
        assert resp.status_code == 200, resp.text
        assert "text/event-stream" in resp.headers.get("content-type", "")
        # Confirm there is at least one progress event on the wire.
        assert "event_type" in resp.text
        assert "progress" in resp.text

    async def test_unknown_vault_returns_404_vault_not_found_before_stream(
        self, http_client, tmp_path
    ):
        """When vault_id is unknown, /ingest must respond 404 (not a
        started 200 stream). This is the load-bearing invariant: the
        Depends factory raises before StreamingResponse is constructed.
        """
        client, _config = http_client
        resp = await client.post(
            "/app/ingest",
            json={
                "vault_id": "does_not_exist",
                "files": [{"file_path": "/tmp/anything.md", "adapter": "markdown"}],
            },
        )
        assert resp.status_code == 404, resp.text
        # The error envelope, NOT an SSE event stream.
        assert "text/event-stream" not in resp.headers.get("content-type", "")
        body = resp.json()
        assert body["code"] == "vault_not_found"

    async def test_empty_files_returns_400_empty_file_list_before_stream(self, http_client):
        """The empty-files validation now lives inside
        IngestStreamingService.stream() and must still surface as a 400
        (not a started 200 stream)."""
        client, _config = http_client
        resp = await client.post(
            "/app/ingest",
            json={"vault_id": "pim_health", "files": []},
        )
        assert resp.status_code == 400, resp.text
        assert "text/event-stream" not in resp.headers.get("content-type", "")
        body = resp.json()
        assert body["code"] == "empty_file_list"
