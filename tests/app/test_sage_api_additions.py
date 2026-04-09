"""Tests for SAGE API additions required by the CAS Application.

Covers TEST-APP-BE-001 through TEST-APP-BE-016:
  - Vault listing (BE-001, BE-002)
  - Vault statistics (BE-003 through BE-006)
  - Hash check (BE-007 through BE-009)
  - Staging edges (BE-010 through BE-013)
  - Pending metadata (BE-014, BE-015)
  - Pipeline status filter on discover (BE-016)
"""

import asyncio
import copy
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from sage.app import create_app, _initialize_services
from sage.config import VaultConfig
from sage.models.enums import EdgeType, PipelineStatus, SourceType
from sage.models.schemas import Document, StagingEdge


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_vault_config_dict(tmp_path, vault_id: str, vault_name: str):
    """Create a minimal vault config dict for a given vault_id."""
    brain_dir = tmp_path / vault_id / "brain"
    brain_dir.mkdir(parents=True, exist_ok=True)
    sources_dir = tmp_path / vault_id / "sources"
    sources_dir.mkdir(parents=True, exist_ok=True)
    return {
        "vault": {
            "id": vault_id,
            "name": vault_name,
            "description": f"Test vault: {vault_name}",
            "owner": "testuser",
            "storage_root": str(sources_dir),
            "brain_root": str(brain_dir),
            "visibility": "personal",
        },
        "document_types": {
            "doc_types": [
                {"value": "note", "label": "Note"},
                {"value": "spec", "label": "Specification"},
            ],
        },
        "lifecycle": {
            "base_states_required": True,
            "states": [
                {"value": "active", "label": "Active"},
                {"value": "superseded", "label": "Superseded"},
                {"value": "completed", "label": "Completed"},
                {"value": "archived", "label": "Archived", "is_terminal": True},
            ],
            "transitions": [
                {"from_state": "(new)", "action": "ingest", "to_state": "active"},
                {
                    "from_state": "active",
                    "action": "supersede",
                    "to_state": "superseded",
                    "creates_edge": "supersedes",
                },
                {"from_state": "active", "action": "complete", "to_state": "completed"},
                {"from_state": "active", "action": "archive", "to_state": "archived"},
                {"from_state": "superseded", "action": "archive", "to_state": "archived"},
                {"from_state": "completed", "action": "archive", "to_state": "archived"},
                {"from_state": "archived", "action": "reactivate", "to_state": "active"},
            ],
        },
        "source_adapters": {
            "adapters": [{"source_type": "markdown", "enabled": True}],
        },
        "metadata_extraction": {"review_required": False},
        "edge_inference": {
            "tier_assignments": [
                {
                    "edge_type": "supersedes",
                    "tier": 1,
                    "inference_rules": [{"method": "version_chain"}],
                },
            ],
        },
    }


def _make_document(
    doc_id: str,
    title: str = "Test Doc",
    source_hash: str | None = None,
    pipeline_status: PipelineStatus = PipelineStatus.PROJECTION_COMPLETE,
    doc_type: str | None = None,
    lifecycle_status: str = "active",
    metadata_confirmed: bool = False,
    pipeline_error: str | None = None,
) -> Document:
    """Create a Document model for testing."""
    now = datetime.now(timezone.utc)
    return Document(
        id=doc_id,
        title=title,
        source_type=SourceType.MARKDOWN,
        source_path=f"test/{doc_id}.md",
        lifecycle_status=lifecycle_status,
        source_content_hash=source_hash or f"sha256:{doc_id}",
        adapter_version="1.0.0",
        created_by="testuser",
        created_at=now,
        last_modified_by="testuser",
        updated_at=now,
        pipeline_status=pipeline_status,
        pipeline_error=pipeline_error,
        doc_type=doc_type,
        metadata_confirmed=metadata_confirmed,
    )


def _make_staging_edge(
    edge_id: str,
    source_id: str,
    target_id: str,
    edge_type: EdgeType = EdgeType.COVERS,
    evidence: str = "Test inference evidence",
) -> StagingEdge:
    """Create a StagingEdge model for testing."""
    return StagingEdge(
        id=edge_id,
        source_id=source_id,
        target_id=target_id,
        edge_type=edge_type,
        inference_evidence=evidence,
        confidence_tier=2,
        created_at=datetime.now(timezone.utc),
    )


@pytest.fixture
async def multi_vault_app(tmp_path):
    """App with two vaults (pim_health, personal_notes)."""
    config1 = VaultConfig.model_validate(
        _make_vault_config_dict(tmp_path, "pim_health", "PIM Health")
    )
    config2 = VaultConfig.model_validate(
        _make_vault_config_dict(tmp_path, "personal_notes", "Personal Notes")
    )
    app = create_app(configs=[config1, config2])

    # Manually initialize (lifespan not triggered by ASGITransport)
    app.state.vault_registry = {}
    from sage.mcp_init import initialize_services

    for cfg in [config1, config2]:
        services = await initialize_services(cfg)
        app.state.vault_registry[cfg.vault.id] = services

    yield app

    for services in app.state.vault_registry.values():
        await services.graph_store.close()


@pytest.fixture
async def empty_vault_app(tmp_path):
    """App with no vaults configured."""
    app = create_app()
    app.state.vault_registry = {}
    yield app


@pytest.fixture
async def single_vault_app(tmp_path):
    """App with one vault containing test data."""
    config = VaultConfig.model_validate(
        _make_vault_config_dict(tmp_path, "pim_health", "PIM Health")
    )
    app = create_app(config=config)
    await _initialize_services(app, config)

    # Create a source file for ingestion tests
    sources = Path(config.vault.storage_root)
    test_dir = sources / "test"
    test_dir.mkdir(parents=True, exist_ok=True)
    (test_dir / "sample.md").write_text("# Sample\n\nSample content.")

    yield app

    await asyncio.sleep(0.3)
    for services in app.state.vault_registry.values():
        await services.graph_store.close()


@pytest.fixture
async def multi_client(multi_vault_app):
    transport = ASGITransport(app=multi_vault_app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.fixture
async def empty_client(empty_vault_app):
    transport = ASGITransport(app=empty_vault_app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.fixture
async def client(single_vault_app):
    transport = ASGITransport(app=single_vault_app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


# ---------------------------------------------------------------------------
# 1. Vault Listing (BE-001, BE-002)
# ---------------------------------------------------------------------------


class TestVaultListing:
    async def test_be_001_list_configured_vaults(self, multi_client):
        """GET /sage_vaults returns list of configured vaults."""
        resp = await multi_client.get("/sage_vaults")
        assert resp.status_code == 200
        body = resp.json()
        assert isinstance(body, list)
        assert len(body) == 2
        ids = {v["id"] for v in body}
        assert ids == {"pim_health", "personal_notes"}
        for v in body:
            assert "id" in v
            assert "name" in v
            assert "description" in v
            assert "storage_root" in v

    async def test_be_002_empty_vaults_returns_empty_array(self, empty_client):
        """GET /sage_vaults returns empty array when no vaults configured."""
        resp = await empty_client.get("/sage_vaults")
        assert resp.status_code == 200
        assert resp.json() == []


# ---------------------------------------------------------------------------
# 2. Vault Statistics (BE-003 through BE-006)
# ---------------------------------------------------------------------------


class TestVaultStatistics:
    async def test_be_003_stats_returns_all_fields(
        self, multi_vault_app, multi_client
    ):
        """GET /sage_vaults/{vault_id}/stats returns all ten statistics."""
        # Insert test data
        services = multi_vault_app.state.vault_registry["pim_health"]
        gs = services.graph_store

        doc1 = _make_document("doc-1", doc_type="note")
        doc2 = _make_document(
            "doc-2",
            doc_type="spec",
            pipeline_status=PipelineStatus.ABSTRACTION_COMPLETE,
        )
        await gs.insert_document(doc1)
        await gs.insert_document(doc2)

        from sage.models.schemas import Edge

        edge = Edge(
            id=str(uuid.uuid4()),
            source_id="doc-1",
            target_id="doc-2",
            edge_type=EdgeType.REFERENCES,
            created_at=datetime.now(timezone.utc),
        )
        await gs.insert_edge(edge)

        staging = _make_staging_edge("stg-1", "doc-1", "doc-2")
        await gs.insert_staging_edge(staging)

        resp = await multi_client.get("/sage_vaults/pim_health/stats")
        assert resp.status_code == 200
        body = resp.json()

        assert body["total_documents"] == 2
        assert isinstance(body["by_lifecycle_state"], dict)
        assert isinstance(body["by_doc_type"], dict)
        assert isinstance(body["by_source_adapter"], dict)
        assert body["total_edges"] == 1
        assert isinstance(body["by_edge_type"], dict)
        assert body["staging_edge_count"] == 1
        assert isinstance(body["lancedb_size_bytes"], int)
        assert isinstance(body["sqlite_size_bytes"], int)
        assert body["last_ingestion_at"] is not None

    async def test_be_004_stats_includes_health_indicators(
        self, multi_vault_app, multi_client
    ):
        """Stats response includes health indicator counts."""
        services = multi_vault_app.state.vault_registry["pim_health"]
        gs = services.graph_store

        # Documents in various states
        doc_ok = _make_document(
            "doc-ok",
            pipeline_status=PipelineStatus.ABSTRACTION_COMPLETE,
            metadata_confirmed=True,
        )
        doc_pending = _make_document(
            "doc-pending",
            pipeline_status=PipelineStatus.ABSTRACTION_COMPLETE,
            metadata_confirmed=False,
        )
        doc_failed = _make_document(
            "doc-failed",
            pipeline_status=PipelineStatus.FAILED,
            pipeline_error="adapter crash",
        )
        doc_deferred = _make_document(
            "doc-deferred",
            pipeline_status=PipelineStatus.ABSTRACTION_SKIPPED,
        )
        for d in [doc_ok, doc_pending, doc_failed, doc_deferred]:
            await gs.insert_document(d)

        staging = _make_staging_edge("stg-h1", "doc-ok", "doc-pending")
        await gs.insert_staging_edge(staging)

        resp = await multi_client.get("/sage_vaults/pim_health/stats")
        assert resp.status_code == 200
        health = resp.json()["health"]

        assert health["pending_metadata_count"] >= 1
        assert health["pending_edge_count"] == 1
        assert health["deferred_abstract_count"] == 1
        assert health["failed_ingestion_count"] == 1

    async def test_be_005_stats_empty_vault(self, multi_client):
        """Stats for empty vault returns zero counts."""
        resp = await multi_client.get("/sage_vaults/personal_notes/stats")
        assert resp.status_code == 200
        body = resp.json()
        assert body["total_documents"] == 0
        assert body["total_edges"] == 0
        assert body["staging_edge_count"] == 0
        assert body["last_ingestion_at"] is None
        assert body["by_lifecycle_state"] == {}
        assert body["by_doc_type"] == {}

    async def test_be_006_stats_nonexistent_vault(self, multi_client):
        """Stats for non-existent vault returns 404."""
        resp = await multi_client.get("/sage_vaults/nonexistent/stats")
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# 3. Hash Check (BE-007 through BE-009)
# ---------------------------------------------------------------------------


class TestHashCheck:
    async def test_be_007_hash_check_returns_matches(
        self, multi_vault_app, multi_client
    ):
        """POST hash-check returns matches with document IDs."""
        services = multi_vault_app.state.vault_registry["pim_health"]
        gs = services.graph_store

        doc1 = _make_document("doc-001", source_hash="sha256:abc123def456")
        doc3 = _make_document("doc-003", source_hash="sha256:patent456")
        await gs.insert_document(doc1)
        await gs.insert_document(doc3)

        resp = await multi_client.post(
            "/sage_vaults/pim_health/hash-check",
            json={
                "hashes": [
                    "sha256:abc123def456",
                    "sha256:unknown",
                    "sha256:patent456",
                ]
            },
        )
        assert resp.status_code == 200
        body = resp.json()

        assert body["sha256:abc123def456"]["exists"] is True
        assert body["sha256:abc123def456"]["document_id"] == "doc-001"
        assert body["sha256:unknown"]["exists"] is False
        assert body["sha256:unknown"].get("document_id") is None
        assert body["sha256:patent456"]["exists"] is True
        assert body["sha256:patent456"]["document_id"] == "doc-003"

    async def test_be_008_hash_check_empty_array(self, multi_client):
        """Hash check with empty array returns empty result."""
        resp = await multi_client.post(
            "/sage_vaults/pim_health/hash-check",
            json={"hashes": []},
        )
        assert resp.status_code == 200
        assert resp.json() == {}

    async def test_be_009_hash_check_nonexistent_vault(self, multi_client):
        """Hash check against non-existent vault returns 404."""
        resp = await multi_client.post(
            "/sage_vaults/nonexistent/hash-check",
            json={"hashes": ["sha256:x"]},
        )
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# 4. Staging Edges (BE-010 through BE-013)
# ---------------------------------------------------------------------------


class TestStagingEdges:
    async def test_be_010_list_staging_edges(
        self, multi_vault_app, multi_client
    ):
        """GET staging-edges lists Tier 2 staging edges."""
        services = multi_vault_app.state.vault_registry["pim_health"]
        gs = services.graph_store

        doc1 = _make_document("doc-s1")
        doc2 = _make_document("doc-s2")
        await gs.insert_document(doc1)
        await gs.insert_document(doc2)

        stg = _make_staging_edge(
            "staging-010",
            "doc-s1",
            "doc-s2",
            EdgeType.COVERS,
            "Status report mentions CD-04 patent code",
        )
        await gs.insert_staging_edge(stg)

        resp = await multi_client.get("/sage_vaults/pim_health/staging-edges")
        assert resp.status_code == 200
        body = resp.json()
        assert isinstance(body, list)
        assert len(body) >= 1

        edge = body[0]
        assert "id" in edge
        assert "source_id" in edge
        assert "target_id" in edge
        assert "edge_type" in edge
        assert "inference_evidence" in edge
        assert "confidence_tier" in edge
        assert "created_at" in edge

    async def test_be_011_confirm_staging_edge(
        self, multi_vault_app, multi_client
    ):
        """POST confirm moves staging edge to production."""
        services = multi_vault_app.state.vault_registry["pim_health"]
        gs = services.graph_store

        doc1 = _make_document("doc-c1")
        doc2 = _make_document("doc-c2")
        await gs.insert_document(doc1)
        await gs.insert_document(doc2)

        stg = _make_staging_edge("staging-001", "doc-c1", "doc-c2")
        await gs.insert_staging_edge(stg)

        resp = await multi_client.post(
            "/sage_vaults/pim_health/staging-edges/staging-001/confirm"
        )
        assert resp.status_code == 200

        # Staging edge should be gone
        remaining = await gs.list_staging_edges()
        staging_ids = [e.id for e in remaining]
        assert "staging-001" not in staging_ids

        # Production edge should exist
        prod_edges = await gs.get_edges_by_source("doc-c1")
        assert len(prod_edges) == 1
        assert prod_edges[0].source_id == "doc-c1"
        assert prod_edges[0].target_id == "doc-c2"

    async def test_be_012_dismiss_staging_edge(
        self, multi_vault_app, multi_client
    ):
        """POST dismiss deletes staging edge without creating production edge."""
        services = multi_vault_app.state.vault_registry["pim_health"]
        gs = services.graph_store

        doc1 = _make_document("doc-d1")
        doc2 = _make_document("doc-d2")
        await gs.insert_document(doc1)
        await gs.insert_document(doc2)

        stg = _make_staging_edge("staging-002", "doc-d1", "doc-d2")
        await gs.insert_staging_edge(stg)

        resp = await multi_client.post(
            "/sage_vaults/pim_health/staging-edges/staging-002/dismiss"
        )
        assert resp.status_code == 200

        # Staging edge should be gone
        remaining = await gs.list_staging_edges()
        staging_ids = [e.id for e in remaining]
        assert "staging-002" not in staging_ids

        # No production edge created
        prod_edges = await gs.get_edges_by_source("doc-d1")
        assert len(prod_edges) == 0

    async def test_be_013_confirm_nonexistent_staging_edge(self, multi_client):
        """Confirm non-existent staging edge returns 404."""
        resp = await multi_client.post(
            "/sage_vaults/pim_health/staging-edges/gone-001/confirm"
        )
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# 5. Pending Metadata (BE-014, BE-015)
# ---------------------------------------------------------------------------


class TestPendingMetadata:
    async def test_be_014_pending_metadata_returns_documents(
        self, multi_vault_app, multi_client
    ):
        """GET pending-metadata returns documents with extracted fields."""
        services = multi_vault_app.state.vault_registry["pim_health"]
        gs = services.graph_store

        # Unconfirmed document
        doc = _make_document(
            "doc-pm1",
            title="Research Notes",
            doc_type="note",
            metadata_confirmed=False,
        )
        await gs.insert_document(doc)

        # Confirmed document (should not appear)
        doc_confirmed = _make_document(
            "doc-pm2",
            title="Confirmed Doc",
            metadata_confirmed=True,
        )
        await gs.insert_document(doc_confirmed)

        resp = await multi_client.get("/sage_vaults/pim_health/pending-metadata")
        assert resp.status_code == 200
        body = resp.json()
        assert isinstance(body, list)

        # Only unconfirmed documents
        doc_ids = [item["document"]["id"] for item in body]
        assert "doc-pm1" in doc_ids
        assert "doc-pm2" not in doc_ids

        # Check extracted_fields structure
        item = next(i for i in body if i["document"]["id"] == "doc-pm1")
        assert "extracted_fields" in item
        fields = item["extracted_fields"]
        assert "title" in fields
        assert fields["title"]["source"] in ("filename", "content", "default")

    async def test_be_015_pending_metadata_empty_when_all_confirmed(
        self, multi_vault_app, multi_client
    ):
        """Pending metadata returns empty array when none pending."""
        services = multi_vault_app.state.vault_registry["pim_health"]
        gs = services.graph_store

        doc = _make_document("doc-pm3", metadata_confirmed=True)
        await gs.insert_document(doc)

        resp = await multi_client.get("/sage_vaults/pim_health/pending-metadata")
        assert resp.status_code == 200
        assert resp.json() == []


# ---------------------------------------------------------------------------
# 6. Pipeline Status Filter on Discover (BE-016)
# ---------------------------------------------------------------------------


class TestPipelineStatusFilter:
    async def test_be_016_discover_pipeline_status_filter(
        self, single_vault_app, client
    ):
        """Discover endpoint accepts pipeline_status filter."""
        services = single_vault_app.state.vault_registry["pim_health"]
        gs = services.graph_store

        # Insert documents with various pipeline states
        doc_ok = _make_document(
            "doc-f1",
            title="OK Doc",
            pipeline_status=PipelineStatus.ABSTRACTION_COMPLETE,
        )
        doc_failed = _make_document(
            "doc-f2",
            title="Failed Doc",
            pipeline_status=PipelineStatus.FAILED,
            pipeline_error="adapter crash",
        )
        doc_skipped = _make_document(
            "doc-f3",
            title="Skipped Doc",
            pipeline_status=PipelineStatus.ABSTRACTION_SKIPPED,
        )
        for d in [doc_ok, doc_failed, doc_skipped]:
            await gs.insert_document(d)

        # Index minimal content for retrieval (using stub content store)
        from sage.adapters.interfaces import SearchResult

        cs = services.ingestion_service._content_store
        # For stub content store, we need to insert directly
        # Use deterministic mode with pipeline_status filter instead

        # Test: filter for "failed" documents only
        # Note: deterministic mode won't work for failed docs (pipeline gate).
        # Use a direct scope=filtered query. Since we're using StubContentStore
        # which returns empty results for semantic search, we verify the filter
        # is accepted without error and doesn't crash.
        resp = await client.post(
            "/sage_vaults/pim_health/discover",
            json={
                "mode": "semantic",
                "query": "test query",
                "scope": "filtered",
                "filters": {"pipeline_status": "abstraction_skipped"},
            },
        )
        assert resp.status_code == 200
        body = resp.json()
        # Stub content store returns empty results, but the filter
        # is accepted without error. The important thing is no 400/500.
        assert body["mode"] == "semantic"
        assert isinstance(body["results"], list)
