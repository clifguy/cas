"""Agentic round-trip tests: TEST-SAGE-BH-116 through TEST-SAGE-BH-124.

Covers the read-modify-reingest pattern: agents fetch original file bytes
via `get_document(include_content=true)`, edit the file locally, then
re-ingest as a new version via `ingest(supersedes_document_id=...)`.
"""

import asyncio
import base64
import os
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from sage.adapters.stubs import (
    StubAbstractionProvider,
    StubContentStore,
    StubEmbeddingProvider,
)
from sage.api.errors import (
    ContentFileMissingError,
    ContentTooLargeError,
    DocumentNotFoundError,
    IdenticalContentSupersedeError,
    SupersedeTargetNotActiveError,
)
import hashlib
from sage.app import _initialize_services, create_app
from sage.config import VaultConfig
from sage.models.enums import SourceType
from sage.models.schemas import IngestRequest, SetLifecycleRequest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _seed_file(tmp_vault_dir: Path, relative: str, content: str) -> Path:
    full = tmp_vault_dir / "sources" / relative
    full.parent.mkdir(parents=True, exist_ok=True)
    full.write_text(content)
    return full


# ---------------------------------------------------------------------------
# API fixtures (HTTP layer tests for include_content)
# ---------------------------------------------------------------------------


@pytest.fixture
async def app(minimal_vault_config_dict, tmp_vault_dir):
    config = VaultConfig.model_validate(minimal_vault_config_dict)
    app = create_app(config=config)
    await _initialize_services(
        app, config,
        content_store=StubContentStore(),
        embedding_provider=StubEmbeddingProvider(),
        abstraction_provider=StubAbstractionProvider(),
    )
    yield app
    await asyncio.sleep(0.5)
    await app.state.graph_store.close()


@pytest.fixture
async def client(app):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def _ingest_via_api(client, source: str, body_extra: dict | None = None) -> dict:
    payload = {"source": source, "adapter": "markdown"}
    if body_extra:
        payload.update(body_extra)
    resp = await client.post(
        "/sage_vaults/test_vault/documents", json=payload
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["document"]


# ---------------------------------------------------------------------------
# TEST-SAGE-BH-116: get_document without include_content omits file content
# ---------------------------------------------------------------------------


async def test_bh_116_get_document_without_include_content_omits_content(
    client, tmp_vault_dir
):
    _seed_file(tmp_vault_dir, "bh116.md", "# Hello\n\nBody.")
    doc = await _ingest_via_api(client, "bh116.md")

    resp = await client.get(
        f"/sage_vaults/test_vault/documents/{doc['id']}"
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["id"] == doc["id"]
    assert body.get("content") is None
    assert body.get("content_size") is None

    # And explicit false matches the default.
    resp2 = await client.get(
        f"/sage_vaults/test_vault/documents/{doc['id']}",
        params={"include_content": "false"},
    )
    assert resp2.status_code == 200
    body2 = resp2.json()
    assert body2.get("content") is None
    assert body2.get("content_size") is None


# ---------------------------------------------------------------------------
# TEST-SAGE-BH-117: include_content returns base64 bytes and size
# ---------------------------------------------------------------------------


async def test_bh_117_get_document_with_include_content(
    client, tmp_vault_dir
):
    original = "# BH-117\n\nExact byte sequence for round-trip verification.\n"
    _seed_file(tmp_vault_dir, "bh117.md", original)
    doc = await _ingest_via_api(client, "bh117.md")

    resp = await client.get(
        f"/sage_vaults/test_vault/documents/{doc['id']}",
        params={"include_content": "true"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["id"] == doc["id"]
    assert isinstance(body["content"], str)
    decoded = base64.b64decode(body["content"])
    assert decoded == original.encode("utf-8")
    assert body["content_size"] == len(original.encode("utf-8"))


# ---------------------------------------------------------------------------
# TEST-SAGE-BH-118: include_content rejects files above size ceiling
# ---------------------------------------------------------------------------


async def test_bh_118_include_content_rejects_oversize(
    client, tmp_vault_dir, monkeypatch
):
    # Seed a file of ~2 KB and set the ceiling to 512 bytes.
    large_body = "# BH-118\n\n" + ("X" * 2000)
    _seed_file(tmp_vault_dir, "bh118.md", large_body)
    doc = await _ingest_via_api(client, "bh118.md")

    monkeypatch.setenv("SAGE_MAX_INLINE_CONTENT_BYTES", "512")

    resp = await client.get(
        f"/sage_vaults/test_vault/documents/{doc['id']}",
        params={"include_content": "true"},
    )
    assert resp.status_code == 413
    body = resp.json()
    assert body["code"] == "content_too_large"
    detail = body["detail"]
    assert detail["document_id"] == doc["id"]
    assert detail["max_bytes"] == 512
    assert detail["size_bytes"] > 512

    # Metadata-only response still works.
    resp2 = await client.get(
        f"/sage_vaults/test_vault/documents/{doc['id']}"
    )
    assert resp2.status_code == 200


# ---------------------------------------------------------------------------
# TEST-SAGE-BH-119: include_content returns 404 when vault file missing
# ---------------------------------------------------------------------------


async def test_bh_119_include_content_missing_file_404(
    client, tmp_vault_dir
):
    _seed_file(tmp_vault_dir, "bh119.md", "# BH-119\n\nBody.")
    doc = await _ingest_via_api(client, "bh119.md")

    # Remove the vault-local file out-of-band.
    vault_file = tmp_vault_dir / "sources" / doc["source_path"]
    assert vault_file.exists()
    vault_file.unlink()

    resp = await client.get(
        f"/sage_vaults/test_vault/documents/{doc['id']}",
        params={"include_content": "true"},
    )
    assert resp.status_code == 404
    body = resp.json()
    assert body["code"] == "content_file_missing"
    assert body["detail"]["document_id"] == doc["id"]
    assert body["detail"]["source_path"] == doc["source_path"]

    # Metadata-only response still succeeds (the record is intact).
    resp2 = await client.get(
        f"/sage_vaults/test_vault/documents/{doc['id']}"
    )
    assert resp2.status_code == 200


# ---------------------------------------------------------------------------
# TEST-SAGE-BH-125: write_to_path writes file and returns metadata only
# ---------------------------------------------------------------------------


async def test_bh_125_write_to_path_happy_path(
    client, tmp_vault_dir, tmp_path
):
    body = "# BH-125\n\nExact byte sequence for on-disk delivery.\n"
    _seed_file(tmp_vault_dir, "bh125.md", body)
    doc = await _ingest_via_api(client, "bh125.md")

    workspace = tmp_path / "agent_workspace"
    workspace.mkdir()
    target = workspace / "bh125_copy.md"
    assert not target.exists()

    resp = await client.get(
        f"/sage_vaults/test_vault/documents/{doc['id']}",
        params={"write_to_path": str(target)},
    )
    assert resp.status_code == 200, resp.text
    payload = resp.json()
    assert payload["id"] == doc["id"]
    assert payload["written_to"] == str(target)
    assert payload["content_size"] == len(body.encode("utf-8"))
    assert payload["content_hash"] == hashlib.sha256(
        body.encode("utf-8")
    ).hexdigest()
    # No inline content in write_to_path mode.
    assert payload.get("content") is None

    # File landed correctly.
    assert target.exists()
    assert target.read_bytes() == body.encode("utf-8")


# ---------------------------------------------------------------------------
# TEST-SAGE-BH-126: write_to_path refuses existing target
# ---------------------------------------------------------------------------


async def test_bh_126_write_to_path_refuses_existing_target(
    client, tmp_vault_dir, tmp_path
):
    _seed_file(tmp_vault_dir, "bh126.md", "# BH-126\n\nBody.")
    doc = await _ingest_via_api(client, "bh126.md")

    target = tmp_path / "existing.md"
    target.write_text("pre-existing content; must not be overwritten")
    pre_bytes = target.read_bytes()

    resp = await client.get(
        f"/sage_vaults/test_vault/documents/{doc['id']}",
        params={"write_to_path": str(target)},
    )
    assert resp.status_code == 409
    body = resp.json()
    assert body["code"] == "write_path_exists"
    assert body["detail"]["write_to_path"] == str(target)

    # Existing file is unchanged.
    assert target.read_bytes() == pre_bytes


# ---------------------------------------------------------------------------
# TEST-SAGE-BH-127: write_to_path requires existing writable parent
# ---------------------------------------------------------------------------


async def test_bh_127_write_to_path_missing_parent(
    client, tmp_vault_dir, tmp_path
):
    _seed_file(tmp_vault_dir, "bh127a.md", "# BH-127\n\nBody.")
    doc = await _ingest_via_api(client, "bh127a.md")

    # Case 1: missing parent directory.
    missing_target = tmp_path / "does_not_exist" / "out.md"
    resp = await client.get(
        f"/sage_vaults/test_vault/documents/{doc['id']}",
        params={"write_to_path": str(missing_target)},
    )
    assert resp.status_code == 400
    body = resp.json()
    assert body["code"] == "write_path_invalid"
    assert "parent directory does not exist" in body["detail"]["reason"]
    assert not missing_target.exists()


async def test_bh_127_write_to_path_unwritable_parent(
    client, tmp_vault_dir, tmp_path
):
    _seed_file(tmp_vault_dir, "bh127b.md", "# BH-127\n\nBody.")
    doc = await _ingest_via_api(client, "bh127b.md")

    readonly = tmp_path / "readonly"
    readonly.mkdir()
    try:
        readonly.chmod(0o555)  # r-x, no write
        target = readonly / "out.md"

        resp = await client.get(
            f"/sage_vaults/test_vault/documents/{doc['id']}",
            params={"write_to_path": str(target)},
        )
        assert resp.status_code == 400
        body = resp.json()
        assert body["code"] == "write_path_invalid"
        assert "not writable" in body["detail"]["reason"]
        assert not target.exists()
    finally:
        # Restore write so pytest can clean up the tmp dir.
        readonly.chmod(0o755)


# ---------------------------------------------------------------------------
# TEST-SAGE-BH-128: rejects both include_content and write_to_path
# ---------------------------------------------------------------------------


async def test_bh_128_mutual_exclusion(
    client, tmp_vault_dir, tmp_path
):
    _seed_file(tmp_vault_dir, "bh128.md", "# BH-128\n\nBody.")
    doc = await _ingest_via_api(client, "bh128.md")

    target = tmp_path / "bh128_out.md"
    resp = await client.get(
        f"/sage_vaults/test_vault/documents/{doc['id']}",
        params={"include_content": "true", "write_to_path": str(target)},
    )
    assert resp.status_code == 400
    body = resp.json()
    assert body["code"] == "content_delivery_conflict"
    # No file written on the conflict.
    assert not target.exists()


# ---------------------------------------------------------------------------
# TEST-SAGE-BH-120: ingest with supersedes_document_id links new version
# ---------------------------------------------------------------------------


async def test_bh_120_supersedes_document_id_happy_path(
    tmp_vault_dir, graph_store, ingestion_service, lifecycle_service
):
    _seed_file(tmp_vault_dir, "bh120_v1.md", "# Version 1\n\nOriginal content.")
    _seed_file(tmp_vault_dir, "bh120_v2.md", "# Version 2\n\nRevised content.")

    v1 = await ingestion_service.ingest(
        IngestRequest(source="bh120_v1.md", adapter=SourceType.MARKDOWN)
    )
    v2 = await ingestion_service.ingest(
        IngestRequest(
            source="bh120_v2.md",
            adapter=SourceType.MARKDOWN,
            supersedes_document_id=v1.document.id,
        )
    )

    assert v2.is_new is True
    assert v2.document.lifecycle_status == "active"

    predecessor = await graph_store.get_document(v1.document.id)
    assert predecessor.lifecycle_status == "archived"

    # Verify the supersedes edge exists: new -> old.
    edges = await graph_store.get_edges_by_source(v2.document.id)
    supersedes_edges = [
        e for e in edges
        if e.edge_type == "supersedes" and e.target_id == v1.document.id
    ]
    assert len(supersedes_edges) == 1


# ---------------------------------------------------------------------------
# TEST-SAGE-BH-121: ingest with nonexistent predecessor returns 404
# ---------------------------------------------------------------------------


async def test_bh_121_supersedes_nonexistent_predecessor(
    tmp_vault_dir, graph_store, ingestion_service
):
    _seed_file(tmp_vault_dir, "bh121.md", "# BH-121\n\nBody.")

    with pytest.raises(DocumentNotFoundError) as exc_info:
        await ingestion_service.ingest(
            IngestRequest(
                source="bh121.md",
                adapter=SourceType.MARKDOWN,
                supersedes_document_id="nonexistent_id",
            )
        )
    assert exc_info.value.status_code == 404

    # No document record created.
    all_docs = await graph_store.list_all_documents()
    assert all_docs == []


# ---------------------------------------------------------------------------
# TEST-SAGE-BH-122: ingest with non-active predecessor returns 409
# ---------------------------------------------------------------------------


async def test_bh_122_supersedes_non_active_predecessor(
    tmp_vault_dir, graph_store, ingestion_service, lifecycle_service
):
    _seed_file(tmp_vault_dir, "bh122_v1.md", "# V1\n\nOriginal.")
    _seed_file(tmp_vault_dir, "bh122_v2.md", "# V2\n\nRevised.")

    v1 = await ingestion_service.ingest(
        IngestRequest(source="bh122_v1.md", adapter=SourceType.MARKDOWN)
    )
    # Archive the predecessor directly (simulating a non-active target).
    await lifecycle_service.set_lifecycle(
        v1.document.id, SetLifecycleRequest(action="archive")
    )

    with pytest.raises(SupersedeTargetNotActiveError) as exc_info:
        await ingestion_service.ingest(
            IngestRequest(
                source="bh122_v2.md",
                adapter=SourceType.MARKDOWN,
                supersedes_document_id=v1.document.id,
            )
        )
    err = exc_info.value
    assert err.status_code == 409
    assert err.detail["predecessor_id"] == v1.document.id
    assert err.detail["current_state"] == "archived"
    assert err.detail["required_state"] == "active"

    # Only the predecessor exists; no new document record.
    all_docs = await graph_store.list_all_documents()
    assert len(all_docs) == 1
    assert all_docs[0].id == v1.document.id

    # No supersedes edge was created.
    edges = await graph_store.get_edges_by_source(v1.document.id)
    assert [e for e in edges if e.edge_type == "supersedes"] == []


# ---------------------------------------------------------------------------
# TEST-SAGE-BH-123: ingest with identical content returns 409
# ---------------------------------------------------------------------------


async def test_bh_123_supersedes_identical_content(
    tmp_vault_dir, graph_store, ingestion_service
):
    body = "# Identical\n\nSame bytes in both versions.\n"
    _seed_file(tmp_vault_dir, "bh123_a.md", body)
    _seed_file(tmp_vault_dir, "bh123_b.md", body)

    v1 = await ingestion_service.ingest(
        IngestRequest(source="bh123_a.md", adapter=SourceType.MARKDOWN)
    )

    with pytest.raises(IdenticalContentSupersedeError) as exc_info:
        await ingestion_service.ingest(
            IngestRequest(
                source="bh123_b.md",
                adapter=SourceType.MARKDOWN,
                supersedes_document_id=v1.document.id,
            )
        )
    err = exc_info.value
    assert err.status_code == 409
    assert err.code == "identical_content_supersede"
    assert err.detail["predecessor_id"] == v1.document.id

    # Predecessor is unchanged.
    predecessor = await graph_store.get_document(v1.document.id)
    assert predecessor.lifecycle_status == "active"

    # No new document or edge.
    all_docs = await graph_store.list_all_documents()
    assert len(all_docs) == 1
    edges = await graph_store.get_edges_by_source(v1.document.id)
    assert [e for e in edges if e.edge_type == "supersedes"] == []


# ---------------------------------------------------------------------------
# TEST-SAGE-BH-124: predecessor validation runs before projection
# ---------------------------------------------------------------------------


async def test_bh_124_validation_before_projection(
    tmp_vault_dir, graph_store, ingestion_service, lifecycle_service
):
    # Case 1: bogus predecessor ID -> 404, no document created.
    _seed_file(tmp_vault_dir, "bh124_case1.md", "# BH-124 case 1\n\nBody.")
    with pytest.raises(DocumentNotFoundError):
        await ingestion_service.ingest(
            IngestRequest(
                source="bh124_case1.md",
                adapter=SourceType.MARKDOWN,
                supersedes_document_id="bogus_predecessor",
            )
        )
    assert await graph_store.list_all_documents() == []

    # Case 2: archived predecessor -> 409, no new document created.
    _seed_file(tmp_vault_dir, "bh124_pred.md", "# Predecessor\n\nOriginal.")
    _seed_file(tmp_vault_dir, "bh124_case2.md", "# BH-124 case 2\n\nBody.")
    pred = await ingestion_service.ingest(
        IngestRequest(source="bh124_pred.md", adapter=SourceType.MARKDOWN)
    )
    await lifecycle_service.set_lifecycle(
        pred.document.id, SetLifecycleRequest(action="archive")
    )

    with pytest.raises(SupersedeTargetNotActiveError):
        await ingestion_service.ingest(
            IngestRequest(
                source="bh124_case2.md",
                adapter=SourceType.MARKDOWN,
                supersedes_document_id=pred.document.id,
            )
        )
    all_docs = await graph_store.list_all_documents()
    assert [d.id for d in all_docs] == [pred.document.id]
