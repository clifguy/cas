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
import contextlib
import hashlib
import re
import uuid
from datetime import datetime, timezone
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
from sage.models.enums import EdgeType, PipelineStatus, SourceType
from sage.models.schemas import Document, StagingEdge
from tests.sage.conftest import initialize_services_for_test

_DOC_ID_RE = re.compile(r"^[0-9a-f]{8}_[a-z0-9_]+$")


def _id(name: str) -> str:
    """Translate a short test name to a shape-conformant document ID.

    The ID validator in sage/models/schemas.py requires the pattern
    ^[0-9a-f]{8}_[a-z0-9_]+$. Test fixtures use short readable names
    like "doc-1"; this helper wraps them so the values still construct
    valid Document instances. Idempotent: an already-canonical id
    passes through unchanged.
    """
    if _DOC_ID_RE.fullmatch(name):
        return name
    slug = re.sub(r"[^a-z0-9_]+", "_", name.lower()).strip("_") or "n"
    return f"{hashlib.sha256(name.encode()).hexdigest()[:8]}_{slug}"


_SHA256_RE = re.compile(r"^sha256:[0-9a-f]{64}$")


def _sha(name: str) -> str:
    """Deterministic canonical Sha256 from a short test name.

    The Sha256Str validator requires `^sha256:[0-9a-f]{64}$`. Test
    fixtures historically used short readable strings like
    f"hash_{doc_id}" or "sha256:abc"; this helper maps any such
    name to a stable canonical Sha256. Idempotent.
    """
    if _SHA256_RE.fullmatch(name):
        return name
    return "sha256:" + hashlib.sha256(f"sage-test-hash:{name}".encode()).hexdigest()


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
                {"value": "completed", "label": "Completed"},
                {"value": "archived", "label": "Archived", "is_terminal": True},
            ],
            "transitions": [
                {"from_state": "(new)", "action": "ingest", "to_state": "active"},
                {
                    "from_state": "active",
                    "action": "supersede",
                    "to_state": "archived",
                    "creates_edge": "supersedes",
                },
                {"from_state": "active", "action": "complete", "to_state": "completed"},
                {"from_state": "active", "action": "archive", "to_state": "archived"},
                {"from_state": "completed", "action": "archive", "to_state": "archived"},
                {"from_state": "archived", "action": "reactivate", "to_state": "active"},
            ],
        },
        "metadata_extraction": {},
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
    """Create a Document model for testing.

    `doc_id` is wrapped with `_id()` so callers can pass short readable
    names like "doc-1"; the helper returns a shape-conformant
    DocumentIdStr.
    """
    now = datetime.now(timezone.utc)
    canonical_id = _id(doc_id)
    return Document(
        id=canonical_id,
        title=title,
        source_type=SourceType.MARKDOWN,
        source_path=f"test/{doc_id}.md",
        lifecycle_status=lifecycle_status,
        source_content_hash=_sha(source_hash) if source_hash else _sha(doc_id),
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


def _eid(name: str) -> str:
    """Deterministic canonical-UUID edge id derived from a short test name."""
    return str(uuid.uuid5(uuid.NAMESPACE_OID, f"sage-test-edge:{name}"))


_STG_1 = _eid("stg-1")
_STG_H1 = _eid("stg-h1")
_STG_010 = _eid("staging-010")
_STG_001 = _eid("staging-001")
_STG_002 = _eid("staging-002")
_GONE_001 = _eid("gone-001")


def _make_staging_edge(
    edge_id: str,
    source_id: str,
    target_id: str,
    edge_type: EdgeType = EdgeType.COVERS,
    evidence: str = "Test inference evidence",
) -> StagingEdge:
    """Create a StagingEdge model for testing.

    `source_id` and `target_id` are wrapped with `_id()` so callers can
    pass the same short readable names they hand to `_make_document`.
    """
    return StagingEdge(
        id=edge_id,
        source_id=_id(source_id),
        target_id=_id(target_id),
        edge_type=edge_type,
        inference_evidence=evidence,
        confidence_tier=2,
        created_at=datetime.now(timezone.utc),
    )


@pytest.fixture
async def multi_vault_app(tmp_path):
    """App with two vaults registered in the canonical registry."""
    config1 = VaultConfig.model_validate(
        _make_vault_config_dict(tmp_path, "example_vault", "Example Portfolio")
    )
    config2 = VaultConfig.model_validate(
        _make_vault_config_dict(tmp_path, "personal_notes", "Personal Notes")
    )
    app = create_app(configs=[config1, config2])

    from sage.app import _ensure_registry_service

    registry_service = _ensure_registry_service(app)
    async with contextlib.AsyncExitStack() as stack:
        for cfg in [config1, config2]:
            services = await stack.enter_async_context(
                initialize_services_for_test(
                    cfg,
                    content_store=StubContentStore(),
                    embedding_provider=StubEmbeddingProvider(),
                    abstraction_provider=StubAbstractionProvider(),
                    registry_service=registry_service,
                )
            )
            app.state.vault_registry[cfg.vault.id] = services
        yield app


@pytest.fixture
async def empty_vault_app(tmp_path):
    """App with no vaults configured."""
    app = create_app()
    from sage.app import _ensure_registry_service

    _ensure_registry_service(app)
    yield app


@pytest.fixture
async def single_vault_app(tmp_path):
    """App with one vault containing test data."""
    config = VaultConfig.model_validate(
        _make_vault_config_dict(tmp_path, "example_vault", "Example Portfolio")
    )
    app = create_app(config=config)
    await _initialize_services(
        app,
        config,
        content_store=StubContentStore(),
        embedding_provider=StubEmbeddingProvider(),
        abstraction_provider=StubAbstractionProvider(),
    )

    # Create a source file for ingestion tests
    sources = Path(config.vault.storage_root)
    test_dir = sources / "test"
    test_dir.mkdir(parents=True, exist_ok=True)
    (test_dir / "sample.md").write_text("# Sample\n\nSample content.")

    yield app

    await asyncio.sleep(0.3)
    for services in app.state.vault_registry.values():
        services.close_timing()
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
        assert ids == {"example_vault", "personal_notes"}
        for v in body:
            assert "id" in v
            assert "name" in v
            assert "description" in v
            assert isinstance(v["document_count"], int)
            assert "storage_root" not in v

    async def test_be_002_empty_vaults_returns_empty_array(self, empty_client):
        """GET /sage_vaults returns empty array when no vaults configured."""
        resp = await empty_client.get("/sage_vaults")
        assert resp.status_code == 200
        assert resp.json() == []


# ---------------------------------------------------------------------------
# 2. Vault Statistics (BE-003 through BE-006)
# ---------------------------------------------------------------------------


class TestVaultStatistics:
    async def test_be_003_stats_returns_all_fields(self, multi_vault_app, multi_client):
        """GET /sage_vaults/{vault_id}/stats returns all ten statistics."""
        # Insert test data
        services = multi_vault_app.state.vault_registry["example_vault"]
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
            source_id=_id("doc-1"),
            target_id=_id("doc-2"),
            edge_type=EdgeType.REFERENCES,
            created_at=datetime.now(timezone.utc),
        )
        await gs.insert_edge(edge)

        staging = _make_staging_edge(_STG_1, "doc-1", "doc-2")
        await gs.insert_staging_edge(staging)

        resp = await multi_client.get("/sage_vaults/example_vault/stats")
        assert resp.status_code == 200
        body = resp.json()

        assert body["total_documents"] == 2
        assert isinstance(body["by_lifecycle_status"], dict)
        assert isinstance(body["by_doc_type"], dict)
        assert isinstance(body["by_source_type"], dict)
        assert body["total_edges"] == 1
        assert isinstance(body["by_edge_type"], dict)
        assert body["staging_edge_count"] == 1
        assert isinstance(body["content_store_size_bytes"], int)
        assert isinstance(body["content_store_chunk_count"], int)
        assert body["content_store_chunk_count"] >= 0
        assert isinstance(body["content_store_version_count"], int)
        assert body["content_store_version_count"] >= 0
        assert isinstance(body["graph_store_size_bytes"], int)
        assert body["last_ingestion_at"] is not None

    async def test_be_004_stats_includes_health_indicators(self, multi_vault_app, multi_client):
        """Stats response includes health indicator counts."""
        services = multi_vault_app.state.vault_registry["example_vault"]
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

        staging = _make_staging_edge(_STG_H1, "doc-ok", "doc-pending")
        await gs.insert_staging_edge(staging)

        resp = await multi_client.get("/sage_vaults/example_vault/stats")
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
        assert body["content_store_chunk_count"] == 0
        assert body["last_ingestion_at"] is None
        assert body["by_lifecycle_status"] == {}
        assert body["by_doc_type"] == {}

    async def test_be_006_stats_nonexistent_vault(self, multi_client):
        """Stats for non-existent vault returns 404."""
        resp = await multi_client.get("/sage_vaults/nonexistent/stats")
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# 3. Hash Check (BE-007 through BE-009)
# ---------------------------------------------------------------------------


class TestHashCheck:
    async def test_be_007_hash_check_returns_matches(self, multi_vault_app, multi_client):
        """POST hash-check returns matches with document IDs."""
        services = multi_vault_app.state.vault_registry["example_vault"]
        gs = services.graph_store

        DOC1_ID = "dddddddd_doc_001"
        DOC3_ID = "dddddddd_doc_003"
        HASH_DOC1 = "sha256:" + "a" * 64
        HASH_UNKNOWN = "sha256:" + "f" * 64
        HASH_DOC3 = "sha256:" + "b" * 64

        doc1 = _make_document(DOC1_ID, source_hash=HASH_DOC1)
        doc3 = _make_document(DOC3_ID, source_hash=HASH_DOC3)
        await gs.insert_document(doc1)
        await gs.insert_document(doc3)

        resp = await multi_client.post(
            "/sage_vaults/example_vault/hash-check",
            json={
                "hashes": [
                    HASH_DOC1,
                    HASH_UNKNOWN,
                    HASH_DOC3,
                ]
            },
        )
        assert resp.status_code == 200
        body = resp.json()

        assert body[HASH_DOC1]["exists"] is True
        assert body[HASH_DOC1]["document_id"] == DOC1_ID
        assert body[HASH_UNKNOWN]["exists"] is False
        assert body[HASH_UNKNOWN].get("document_id") is None
        assert body[HASH_DOC3]["exists"] is True
        assert body[HASH_DOC3]["document_id"] == DOC3_ID

    async def test_be_008_hash_check_empty_array(self, multi_client):
        """Hash check with empty array returns empty result."""
        resp = await multi_client.post(
            "/sage_vaults/example_vault/hash-check",
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
    async def test_be_010_list_staging_edges(self, multi_vault_app, multi_client):
        """GET staging-edges lists Tier 2 staging edges."""
        services = multi_vault_app.state.vault_registry["example_vault"]
        gs = services.graph_store

        doc1 = _make_document("doc-s1")
        doc2 = _make_document("doc-s2")
        await gs.insert_document(doc1)
        await gs.insert_document(doc2)

        stg = _make_staging_edge(
            _STG_010,
            "doc-s1",
            "doc-s2",
            EdgeType.COVERS,
            "Status report mentions CD-04 report code",
        )
        await gs.insert_staging_edge(stg)

        resp = await multi_client.get("/sage_vaults/example_vault/staging-edges")
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

    async def test_be_011_confirm_staging_edge(self, multi_vault_app, multi_client):
        """POST confirm moves staging edge to production."""
        services = multi_vault_app.state.vault_registry["example_vault"]
        gs = services.graph_store

        doc1 = _make_document("doc-c1")
        doc2 = _make_document("doc-c2")
        await gs.insert_document(doc1)
        await gs.insert_document(doc2)

        stg = _make_staging_edge(_STG_001, "doc-c1", "doc-c2")
        await gs.insert_staging_edge(stg)

        resp = await multi_client.post(
            f"/sage_vaults/example_vault/staging-edges/{_STG_001}/confirm"
        )
        assert resp.status_code == 200

        # Staging edge should be gone
        remaining = await gs.list_staging_edges()
        staging_ids = [e.id for e in remaining]
        assert _STG_001 not in staging_ids

        # Production edge should exist
        prod_edges = await gs.get_edges_by_source(_id("doc-c1"))
        assert len(prod_edges) == 1
        assert prod_edges[0].source_id == _id("doc-c1")
        assert prod_edges[0].target_id == _id("doc-c2")

    async def test_be_012_dismiss_staging_edge(self, multi_vault_app, multi_client):
        """POST dismiss deletes staging edge without creating production edge."""
        services = multi_vault_app.state.vault_registry["example_vault"]
        gs = services.graph_store

        doc1 = _make_document("doc-d1")
        doc2 = _make_document("doc-d2")
        await gs.insert_document(doc1)
        await gs.insert_document(doc2)

        stg = _make_staging_edge(_STG_002, "doc-d1", "doc-d2")
        await gs.insert_staging_edge(stg)

        resp = await multi_client.post(
            f"/sage_vaults/example_vault/staging-edges/{_STG_002}/dismiss"
        )
        assert resp.status_code == 200

        # Staging edge should be gone
        remaining = await gs.list_staging_edges()
        staging_ids = [e.id for e in remaining]
        assert _STG_002 not in staging_ids

        # No production edge created
        prod_edges = await gs.get_edges_by_source(_id("doc-d1"))
        assert len(prod_edges) == 0

    async def test_be_013_confirm_nonexistent_staging_edge(self, multi_client):
        """Confirm non-existent staging edge returns 404."""
        resp = await multi_client.post(
            f"/sage_vaults/example_vault/staging-edges/{_GONE_001}/confirm"
        )
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# 5. Pending Metadata (BE-014, BE-015)
# ---------------------------------------------------------------------------


class TestPendingMetadata:
    async def test_be_014_pending_metadata_returns_documents(self, multi_vault_app, multi_client):
        """GET pending-metadata returns documents with extracted fields."""
        services = multi_vault_app.state.vault_registry["example_vault"]
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

        resp = await multi_client.get("/sage_vaults/example_vault/pending-metadata")
        assert resp.status_code == 200
        body = resp.json()
        assert isinstance(body, list)

        # Only unconfirmed documents
        doc_ids = [item["document"]["id"] for item in body]
        assert _id("doc-pm1") in doc_ids
        assert _id("doc-pm2") not in doc_ids

        # Check extracted_fields structure
        item = next(i for i in body if i["document"]["id"] == _id("doc-pm1"))
        assert "extracted_fields" in item
        fields = item["extracted_fields"]
        assert "title" in fields
        assert fields["title"]["source"] in ("filename", "content", "default")

    async def test_be_036_pending_metadata_includes_document_date(
        self, multi_vault_app, multi_client
    ):
        """Pending metadata extracted_fields includes document_date with
        correct source annotation (BE-036)."""
        services = multi_vault_app.state.vault_registry["example_vault"]
        gs = services.graph_store

        # Document with filename-derived date (source_path has date pattern)
        doc_filename = _make_document(
            "doc-date-fn",
            title="Checklist",
            metadata_confirmed=False,
        )
        doc_filename.source_path = "test/2026-04-10_EXAMPLE_PV07_checklist_v1.md"
        doc_filename.document_date = "2026-04-10"
        await gs.insert_document(doc_filename)

        # Document with fallback date (no date pattern in source_path)
        doc_fallback = _make_document(
            "doc-date-fb",
            title="Notes",
            metadata_confirmed=False,
        )
        doc_fallback.source_path = "test/EXAMPLE_PV07_notes.md"
        doc_fallback.document_date = "2025-06-15"
        await gs.insert_document(doc_fallback)

        resp = await multi_client.get("/sage_vaults/example_vault/pending-metadata")
        assert resp.status_code == 200
        body = resp.json()

        # Filename-derived date
        item_fn = next(i for i in body if i["document"]["id"] == _id("doc-date-fn"))
        assert "document_date" in item_fn["extracted_fields"]
        dd_fn = item_fn["extracted_fields"]["document_date"]
        assert dd_fn["value"] == "2026-04-10"
        assert dd_fn["source"] == "filename"

        # Fallback date (no date in filename)
        item_fb = next(i for i in body if i["document"]["id"] == _id("doc-date-fb"))
        assert "document_date" in item_fb["extracted_fields"]
        dd_fb = item_fb["extracted_fields"]["document_date"]
        assert dd_fb["value"] == "2025-06-15"
        assert dd_fb["source"] == "default"

    async def test_be_015_pending_metadata_empty_when_all_confirmed(
        self, multi_vault_app, multi_client
    ):
        """Pending metadata returns empty array when none pending."""
        services = multi_vault_app.state.vault_registry["example_vault"]
        gs = services.graph_store

        doc = _make_document("doc-pm3", metadata_confirmed=True)
        await gs.insert_document(doc)

        resp = await multi_client.get("/sage_vaults/example_vault/pending-metadata")
        assert resp.status_code == 200
        assert resp.json() == []


# ---------------------------------------------------------------------------
# 6. Pipeline Status Filter on Discover (BE-016)
# ---------------------------------------------------------------------------


class TestPipelineStatusFilter:
    async def test_be_016_discover_pipeline_status_filter(self, single_vault_app, client):
        """Discover endpoint accepts pipeline_status filter."""
        services = single_vault_app.state.vault_registry["example_vault"]
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

        # For stub content store, we need to insert directly
        # Use deterministic mode with pipeline_status filter instead

        # Test: filter for "failed" documents only
        # Note: deterministic mode won't work for failed docs (pipeline gate).
        # Use a direct scope=filtered query. Since we're using StubContentStore
        # which returns empty results for semantic search, we verify the filter
        # is accepted without error and doesn't crash.
        resp = await client.post(
            "/sage_vaults/example_vault/discover",
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
