"""HTTP integration tests for the per-document reabstract route.

POST /sage_vaults/{vault_id}/documents/{document_id}/reabstract.

Exposes the shared per-document re-abstraction service path (the same path the
MCP recompute_abstract tool wraps) over REST: a fire-and-forget dispatch that
returns a ReabstractStartedResponse envelope and leaves the caller to poll
GET /documents/{document_id} for the terminal pipeline_status. The route
regenerates a document's abstract regardless of its current terminal
pipeline_status, so long as its projection chunks are still stored.
"""

from __future__ import annotations

import asyncio
from datetime import datetime

import pytest
from httpx import ASGITransport, AsyncClient

from sage import mcp_server
from sage.adapters.interfaces import AbstractionProvider, Chunk
from sage.adapters.stubs import StubContentStore
from sage.app import _initialize_services, create_app
from sage.config import VaultConfig
from sage.mcp_init import SAGEServices
from sage.models.enums import PipelineStatus
from sage.models.schemas import ReabstractStartedResponse
from tests.helpers.pipeline_wait import await_pipeline_idle
from tests.sage.test_graph_ops import _make_doc
from tests.sage.test_lifecycle import _id
from tests.sage.test_reabstract_deferred_service import _GatedAbstractionProvider

# A distinctive pre-existing abstract so a test can prove the re-abstraction
# overwrote it rather than passing coincidentally on a doc that already had one.
_STALE_SENTINEL = "STALE pre-swap abstract -- must be overwritten."


async def _seed_doc(
    services: SAGEServices,
    *,
    label: str,
    pipeline_status: PipelineStatus = PipelineStatus.ABSTRACTION_COMPLETE,
    with_chunks: bool = True,
    semantic_abstract: str | None = _STALE_SENTINEL,
) -> str:
    """Insert one markdown document at ``pipeline_status`` and (optionally) a
    body chunk so the re-abstraction path has a projection to work from."""
    doc = _make_doc(_id(label), pipeline_status=pipeline_status)
    doc.semantic_abstract = semantic_abstract
    await services.graph_store.insert_document(doc)
    if with_chunks:
        await services.content_store.index_chunks(
            doc.id,
            [
                Chunk(
                    document_id=doc.id,
                    heading_path="Body",
                    content="Body content for projection.",
                    chunk_index=0,
                )
            ],
        )
    return doc.id


async def _poll_pipeline_status(
    services: SAGEServices,
    doc_id: str,
    *,
    timeout_polls: int = 200,
) -> str:
    """Wait until ``doc_id`` is settled and unclaimed; return its status.

    Thin adapter over the shared wait. The accept-set used to be a caller
    argument, which put this module's polls out of reach of the poll-discipline
    gate at the very surface -- the ``/reabstract`` route -- that rejects a
    document whose claim is still held.
    """
    doc = await await_pipeline_idle(
        services.graph_store,
        doc_id,
        service=services.ingestion_service,
        attempts=timeout_polls,
        delay=0.05,
    )
    return doc.pipeline_status


@pytest.fixture
async def document_app(minimal_vault_config_dict, monkeypatch):
    """FastAPI app with one vault wired through the normal initialization path
    so the documents router's reabstract route reaches IngestionService and its
    background worker end-to-end."""
    monkeypatch.setenv("SAGE_TEST_STUB_PROVIDERS", "1")
    config = VaultConfig.model_validate(minimal_vault_config_dict)
    app = create_app(config=config)
    await _initialize_services(
        app,
        config,
        content_store_factory=lambda _brain: StubContentStore(),
    )
    vault_id = config.vault.id
    yield app, vault_id

    await asyncio.sleep(0.1)
    registry: dict[str, SAGEServices] = app.state.vault_registry
    if vault_id in registry:
        registry[vault_id].close_timing()
        await registry[vault_id].graph_store.close()
    mcp_server._vaults.clear()


# ---------------------------------------------------------------------------
# Happy path + observed completion
# ---------------------------------------------------------------------------


async def test_post_reabstract_document_returns_200_started(document_app):
    """200 with a JSON body that round-trips as ReabstractStartedResponse:
    status='reabstract_started' and the dispatched document_id."""
    app, vault_id = document_app
    services: SAGEServices = app.state.vault_registry[vault_id]
    doc_id = await _seed_doc(services, label="reabs_happy")

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(f"/sage_vaults/{vault_id}/documents/{doc_id}/reabstract")

    assert resp.status_code == 200, resp.text
    envelope = ReabstractStartedResponse.model_validate(resp.json())
    assert envelope.status == "reabstract_started"
    assert envelope.document_id == doc_id


async def test_reabstract_document_completes_and_refreshes_abstract(document_app):
    """The fire-and-forget job actually runs to terminal via the shared worker:
    after the POST, polling get_document shows pipeline_status back at
    abstraction_complete and a regenerated semantic_abstract (not the stale
    pre-swap value).

    Anti-coincidental-pass: seeding a distinctive stale abstract and asserting
    it changed rules out a route that returns 'started' but dispatches no real
    work (the doc would keep the sentinel forever)."""
    app, vault_id = document_app
    services: SAGEServices = app.state.vault_registry[vault_id]
    doc_id = await _seed_doc(services, label="reabs_refresh")

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(f"/sage_vaults/{vault_id}/documents/{doc_id}/reabstract")
    assert resp.status_code == 200, resp.text

    await _poll_pipeline_status(services, doc_id)
    doc = await services.graph_store.get_document(doc_id)
    assert doc.semantic_abstract, "abstract must be populated after completion"
    assert doc.semantic_abstract != _STALE_SENTINEL, (
        "the abstract must be regenerated, not left at the pre-swap value"
    )


@pytest.mark.parametrize(
    "seed_status",
    [
        PipelineStatus.ABSTRACTION_COMPLETE,
        PipelineStatus.ABSTRACTION_SKIPPED,
        PipelineStatus.FAILED,
    ],
)
async def test_post_reabstract_document_regardless_of_terminal_status(document_app, seed_status):
    """The headline acceptance criterion: a document at any terminal
    pipeline_status (abstraction_complete / abstraction_skipped / failed) can be
    re-abstracted, returning 200 'started'.

    Anti-coincidental-pass: a route bound to the deferred-backfill path
    (MaintenanceService.reabstract_deferred, which processes only
    abstraction_skipped) would 404/no-op the complete and failed cases; binding
    to IngestionService.reabstract (status-agnostic) is what makes all three
    pass."""
    app, vault_id = document_app
    services: SAGEServices = app.state.vault_registry[vault_id]
    doc_id = await _seed_doc(
        services, label=f"reabs_{seed_status.value}", pipeline_status=seed_status
    )

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(f"/sage_vaults/{vault_id}/documents/{doc_id}/reabstract")

    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "reabstract_started"

    # Let the background job finish so it does not outlive the fixture teardown.
    await _poll_pipeline_status(services, doc_id)


# ---------------------------------------------------------------------------
# Error envelopes
# ---------------------------------------------------------------------------


async def test_post_reabstract_document_unknown_vault_returns_404(document_app):
    """An unregistered vault id returns 404 vault_not_found via get_vault_id."""
    app, _vault_id = document_app
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post("/sage_vaults/ghost/documents/deadbeef_doc/reabstract")

    assert resp.status_code == 404, resp.text
    assert resp.json()["code"] == "vault_not_found"


async def test_post_reabstract_document_unknown_document_returns_404(document_app):
    """A well-formed but nonexistent document id returns 404 document_not_found."""
    app, vault_id = document_app
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(f"/sage_vaults/{vault_id}/documents/deadbeef_ghost/reabstract")

    assert resp.status_code == 404, resp.text
    assert resp.json()["code"] == "document_not_found"


async def test_post_reabstract_document_no_chunks_returns_404(document_app):
    """A document with no stored projection chunks returns 404 no_projection.

    Anti-coincidental-pass: the route must surface the service's
    NoProjectionError as a structured 404, not swallow it or 500."""
    app, vault_id = document_app
    services: SAGEServices = app.state.vault_registry[vault_id]
    doc_id = await _seed_doc(services, label="reabs_nochunks", with_chunks=False)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(f"/sage_vaults/{vault_id}/documents/{doc_id}/reabstract")

    assert resp.status_code == 404, resp.text
    assert resp.json()["code"] == "no_projection"


async def test_post_reabstract_document_malformed_id_returns_400(document_app):
    """A syntactically malformed document_id is rejected at the request boundary
    with the structured invalid_document_id (400) envelope, exercising the
    DocumentIdStr typed alias on the path parameter."""
    app, vault_id = document_app
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(f"/sage_vaults/{vault_id}/documents/not-a-doc-id/reabstract")

    assert resp.status_code == 400, resp.text
    assert resp.json()["code"] == "invalid_document_id"


async def test_post_reabstract_document_concurrent_returns_409(
    minimal_vault_config_dict, monkeypatch
):
    """Two POSTs against the same document while the first is mid-flight: the
    second returns 409 reabstract_document_already_in_flight with a structured
    detail payload. A gated abstraction provider holds the first job (and thus
    the per-document claim) until the test releases it.

    Anti-coincidental-pass: proves the route reuses IngestionService's
    per-document single-flight claim (the one shared service path) rather than a
    parallel implementation that would let the second call dispatch."""
    monkeypatch.setenv("SAGE_TEST_STUB_PROVIDERS", "1")
    config = VaultConfig.model_validate(minimal_vault_config_dict)
    app = create_app(config=config)

    gated: AbstractionProvider = _GatedAbstractionProvider()
    await _initialize_services(
        app,
        config,
        abstraction_provider=gated,
        content_store_factory=lambda _brain: StubContentStore(),
    )
    vault_id = config.vault.id
    services: SAGEServices = app.state.vault_registry[vault_id]
    doc_id = await _seed_doc(services, label="reabs_gated")

    transport = ASGITransport(app=app)
    try:
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp_a = await client.post(f"/sage_vaults/{vault_id}/documents/{doc_id}/reabstract")
            assert resp_a.status_code == 200, resp_a.text
            assert resp_a.json()["status"] == "reabstract_started"

            # Wait until the background worker holds the claim (blocked at the gate).
            await asyncio.wait_for(gated.entered.wait(), timeout=5.0)

            resp_b = await client.post(f"/sage_vaults/{vault_id}/documents/{doc_id}/reabstract")
            assert resp_b.status_code == 409, resp_b.text
            body_b = resp_b.json()
            assert body_b["code"] == "reabstract_document_already_in_flight"
            assert body_b["detail"]["document_id"] == doc_id
            # detail["start_time"] is an ISO 8601 string; confirm it parses.
            datetime.fromisoformat(body_b["detail"]["start_time"])

            # Release the gate; the first job completes and drops the claim.
            gated.gate.set()
            await _poll_pipeline_status(services, doc_id)
    finally:
        await asyncio.sleep(0.1)
        app.state.vault_registry[vault_id].close_timing()
        await app.state.vault_registry[vault_id].graph_store.close()
        mcp_server._vaults.clear()
