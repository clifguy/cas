"""tests: synced-from provenance attributes on edges.

Schema-only ship. Covers:

- TEST-1: round-trip via ``create_edge`` -> ``traverse`` (both fields).
- TEST-2: omitted attributes default to None and persist as SQL NULL.
- TEST-3: one attribute set, the other null (no coupled enforcement).
- TEST-5: Pydantic type rejection at the FastAPI router boundary.

TEST-4 (migration idempotency on a pre--shaped DB) lives in
``tests/sage/test_migrate_flag.py`` alongside the other migration tests.
The OpenAPI conformance regression (TEST-6) is covered by
``tests/sage/test_openapi_conformance.py``; no new test required.

/ CAS-ADR-017.
"""

from __future__ import annotations

import asyncio
import hashlib
import sqlite3
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
from sage.models.enums import (
    EdgeType,
    PipelineStatus,
    SourceType,
    TraversalDirection,
)
from sage.models.schemas import Document, LinkRequest, TraverseRequest

# ── helpers (mirror test_rationale_kind.py) ────────────────────────────


def _id(name: str) -> str:
    return f"{hashlib.sha256(name.encode()).hexdigest()[:8]}_{name}"


def _sha(name: str) -> str:
    return "sha256:" + hashlib.sha256(f"sage-test-hash:{name}".encode()).hexdigest()


def _make_doc(doc_id: str) -> Document:
    now = datetime.now(timezone.utc)
    return Document(
        id=doc_id,
        title=f"Doc {doc_id}",
        source_type=SourceType.MARKDOWN,
        source_path=f"test/{doc_id}.md",
        source_content_hash=_sha(doc_id),
        adapter_version="0.1.0",
        created_by="testuser",
        created_at=now,
        last_modified_by="testuser",
        updated_at=now,
        projected_at=now,
        pipeline_status=PipelineStatus.ABSTRACTION_COMPLETE,
    )


def _edge_sql_row(db_path: Path, edge_id: str) -> tuple[str | None, str | None]:
    """Read ``(synced_from_version, synced_from_content_hash)`` directly from
    SQLite. Used by TEST-2 to confirm the storage layer writes SQL NULL
    rather than an empty string (the Pydantic boundary cannot discriminate
    between ``None`` and a buggy ``''`` substitution).
    """
    conn = sqlite3.connect(str(db_path))
    try:
        row = conn.execute(
            "SELECT synced_from_version, synced_from_content_hash FROM edges WHERE id = ?",
            (edge_id,),
        ).fetchone()
    finally:
        conn.close()
    assert row is not None, f"edge {edge_id} not found"
    return row[0], row[1]


# ── TEST-1: round-trip via create_edge -> traverse ─────────────────


async def test_t1_round_trip_both_attributes_via_link_and_traverse(
    graph_store, graph_ops_service, tmp_vault_dir
):
    """TEST-1. A ``derived_from`` edge created with both
    ``synced_from_version`` and ``synced_from_content_hash`` set must
    store both values and return them verbatim when the edge is
    traversed.

    Equality assertion against the supplied non-None value (NOT a
    presence check). A storage layer that silently drops the fields
    would let an ``is not None`` assertion pass against the Pydantic
    default; only equality detects that defect.
    """
    src = _id("t1_src")
    tgt = _id("t1_tgt")
    # chain-membership guard: synced_from_version must be a member
    # of target_id's supersedes chain. tgt itself is a one-element chain;
    # synced_from = tgt is valid (recorded == head → "current" per the
    # detector's classification).
    synced_from = tgt
    expected_hash = "sha256:" + "a1" * 32

    await graph_store.insert_document(_make_doc(src))
    await graph_store.insert_document(_make_doc(tgt))

    await graph_ops_service.link(
        LinkRequest(
            source_id=src,
            target_id=tgt,
            edge_type=EdgeType.DERIVED_FROM,
            source_valid_from_version=src,
            synced_from_version=synced_from,
            synced_from_content_hash=expected_hash,
        )
    )

    out = await graph_ops_service.traverse(
        TraverseRequest(
            start_id=src,
            edge_type=EdgeType.DERIVED_FROM,
            direction=TraversalDirection.OUTBOUND,
            depth=1,
        )
    )
    assert [n.document.id for n in out.nodes] == [tgt]
    edge = out.nodes[0].edge
    assert edge.synced_from_version == synced_from
    assert edge.synced_from_content_hash == expected_hash


# ── TEST-2: omitted attributes default to None and persist as SQL NULL ─


async def test_t2_omitted_attributes_default_to_none_and_persist_as_null(
    graph_store, graph_ops_service, tmp_vault_dir
):
    """TEST-2. A ``derived_from`` edge created without supplying either
    attribute stores SQL ``NULL`` and serializes as ``None`` on the Edge
    model. Both the Pydantic round-trip and a direct ``SELECT`` confirm
    that no empty-string substitution or chain-anchor inference occurs.

    The ticket says: "an edge that has not recorded a synced-from value
    is explicitly ``null``, never inferred from the chain anchors."
    Empty-string default or implicit derivation from
    ``source_valid_from_version`` silently misleads future detection.
    """
    src = _id("t2_src")
    tgt = _id("t2_tgt")

    await graph_store.insert_document(_make_doc(src))
    await graph_store.insert_document(_make_doc(tgt))

    created = (
        await graph_ops_service.link(
            LinkRequest(
                source_id=src,
                target_id=tgt,
                edge_type=EdgeType.DERIVED_FROM,
                source_valid_from_version=src,
            )
        )
    ).edge
    assert created.synced_from_version is None
    assert created.synced_from_content_hash is None

    out = await graph_ops_service.traverse(
        TraverseRequest(
            start_id=src,
            edge_type=EdgeType.DERIVED_FROM,
            direction=TraversalDirection.OUTBOUND,
            depth=1,
        )
    )
    edge = out.nodes[0].edge
    assert edge.synced_from_version is None
    assert edge.synced_from_content_hash is None
    # Defends explicitly against an empty-string substitution that would
    # round-trip through Pydantic but show up at the SQL boundary.
    assert edge.synced_from_version != ""
    assert edge.synced_from_content_hash != ""

    sql_version, sql_hash = _edge_sql_row(tmp_vault_dir / "brain" / "graph.db", created.id)
    assert sql_version is None, (
        f"storage layer wrote {sql_version!r} for synced_from_version, expected SQL NULL"
    )
    assert sql_hash is None, (
        f"storage layer wrote {sql_hash!r} for synced_from_content_hash, expected SQL NULL"
    )


# ── TEST-3: one attribute set, the other null ────────────────────────


async def test_t3_one_attribute_set_other_null_no_coupled_enforcement(
    graph_store, graph_ops_service, tmp_vault_dir
):
    """TEST-3. Supplying only ``synced_from_version`` (with no hash)
    stores the version verbatim and leaves the hash as null. The hash
    is documented as an *optional companion*, so a coupled enforcement
    bug requiring both-or-neither would block the documented usage
    pattern.
    """
    src = _id("t3_src")
    tgt = _id("t3_tgt")
    # chain-membership guard: synced_from_version must be a member
    # of target_id's supersedes chain.
    synced_from = tgt

    await graph_store.insert_document(_make_doc(src))
    await graph_store.insert_document(_make_doc(tgt))

    created = (
        await graph_ops_service.link(
            LinkRequest(
                source_id=src,
                target_id=tgt,
                edge_type=EdgeType.DERIVED_FROM,
                source_valid_from_version=src,
                synced_from_version=synced_from,
            )
        )
    ).edge
    assert created.synced_from_version == synced_from
    assert created.synced_from_content_hash is None

    out = await graph_ops_service.traverse(
        TraverseRequest(
            start_id=src,
            edge_type=EdgeType.DERIVED_FROM,
            direction=TraversalDirection.OUTBOUND,
            depth=1,
        )
    )
    edge = out.nodes[0].edge
    assert edge.synced_from_version == synced_from
    assert edge.synced_from_content_hash is None


# ── TEST-5: Pydantic type rejection at the API boundary ──────────────


@pytest.fixture
async def app(minimal_vault_config_dict, tmp_vault_dir):
    """Create a FastAPI app with test config, manually initializing
    services. Mirrors the fixture in tests/sage/test_api_integration.py.
    """
    config = VaultConfig.model_validate(minimal_vault_config_dict)
    app = create_app(config=config)
    await _initialize_services(
        app,
        config,
        content_store=StubContentStore(),
        embedding_provider=StubEmbeddingProvider(),
        abstraction_provider=StubAbstractionProvider(),
    )
    yield app
    await asyncio.sleep(0.1)
    await app.state.graph_store.close()


@pytest.fixture
async def client(app):
    """Async HTTP client for the SAGE API."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def _ingest_doc_via_router(client: AsyncClient, source_path: str) -> str:
    """Ingest a markdown source file via the router and return the
    resulting document id.
    """
    resp = await client.post(
        "/sage_vaults/test_vault/documents",
        json={"source": source_path, "source_type": "markdown"},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["document"]["id"]


async def test_t5_pydantic_rejects_non_string_at_router_boundary(client, tmp_vault_dir):
    """TEST-5. Supplying a non-string ``synced_from_version`` (here:
    int 42) to ``POST /edges`` raises a 422 validation error rather
    than silently coercing to ``'42'``.

    Drives through the FastAPI router so coercion at any layer below
    Pydantic (e.g., MCP wrapper type widening) is also caught.
    Drift detection downstream compares synced-from values as opaque
    strings; silent int coercion would corrupt the comparison surface.
    """
    # Seed source files so /documents ingest succeeds.
    sources = tmp_vault_dir / "sources"
    test_dir = sources / "test"
    test_dir.mkdir(parents=True, exist_ok=True)
    (test_dir / "t5_src.md").write_text("# T5 src\n\n.")
    (test_dir / "t5_tgt.md").write_text("# T5 tgt\n\n.")

    src_id = await _ingest_doc_via_router(client, "test/t5_src.md")
    tgt_id = await _ingest_doc_via_router(client, "test/t5_tgt.md")

    resp = await client.post(
        "/sage_vaults/test_vault/edges",
        json={
            "source_id": src_id,
            "target_id": tgt_id,
            "edge_type": "derived_from",
            "source_valid_from_version": src_id,
            "synced_from_version": 42,  # int, not str — must be rejected
        },
    )
    assert resp.status_code == 422, (
        f"expected 422 for non-string synced_from_version, got {resp.status_code}: {resp.text}"
    )
