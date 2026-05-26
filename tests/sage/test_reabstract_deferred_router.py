"""HTTP integration tests for the reabstract-deferred router.

POST /sage_vaults/{vault_id}/admin/reabstract-deferred.

replaces the synchronous JSON 200 response with an SSE stream of
per-document progress events followed by a summary event. The error
paths (404 vault_not_found and 409 reabstract_already_in_flight) still
resolve synchronously BEFORE the stream opens, returning the
application/json ErrorResponse envelope with zero SSE events emitted.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone

import pytest
from httpx import ASGITransport, AsyncClient

from sage import mcp_server
from sage.adapters.interfaces import AbstractionProvider, Chunk
from sage.adapters.stubs import StubContentStore
from sage.app import _initialize_services, create_app
from sage.config import VaultConfig
from sage.mcp_init import SAGEServices
from sage.models.enums import PipelineStatus
from sage.models.schemas import ReabstractReport
from tests.sage.test_lifecycle import _id
from tests.sage.test_reabstract_deferred_service import (
    _GatedAbstractionProvider,
    _make_skipped_doc,
    _SelectivelyFailingProvider,
)


def _parse_sse_events(text: str) -> list[dict]:
    """Parse an SSE response body into a list of JSON event payloads.

    Each event in the body is a ``data: <json>\\n\\n`` block. We split on
    newlines and decode every ``data:``-prefixed line. Mirrors the pattern
    at tests/app/test_app_backend.py:1448-1452 (the ingest SSE precedent).
    """
    return [
        json.loads(line.replace("data: ", "", 1))
        for line in text.strip().split("\n")
        if line.startswith("data: ")
    ]


async def _seed_one_skipped(
    services: SAGEServices,
    *,
    doc_id_label: str = "router_skipped",
) -> str:
    """Insert one abstraction_skipped markdown doc and a body chunk for it."""
    doc = _make_skipped_doc(_id(doc_id_label))
    await services.graph_store.insert_document(doc)
    chunk = Chunk(
        document_id=doc.id,
        heading_path="Body",
        content="Body content for projection.",
        chunk_index=0,
    )
    await services.content_store.index_chunks(doc.id, [chunk])
    return doc.id


@pytest.fixture
async def maintenance_app(minimal_vault_config_dict, monkeypatch):
    """Build a FastAPI app with one vault wired through the normal
    initialization path so MaintenanceService.ingestion_service is
    populated and reabstract-deferred is reachable end-to-end."""
    monkeypatch.setenv("SAGE_TEST_STUB_PROVIDERS", "1")
    config = VaultConfig.model_validate(minimal_vault_config_dict)
    app = create_app(config=config)
    await _initialize_services(
        app,
        config,
        content_store_factory=lambda _brain: StubContentStore(),
    )
    vault_id = config.vault.id
    yield app, vault_id, config

    await asyncio.sleep(0.1)
    registry: dict[str, SAGEServices] = app.state.vault_registry
    if vault_id in registry:
        await registry[vault_id].graph_store.close()
    mcp_server._vaults.clear()


async def test_post_reabstract_deferred_streams_sse_events(maintenance_app):
    """200 with content-type text/event-stream; body is a sequence of
    ``data: <json>`` lines: one ``progress(started)`` event and one
    ``progress(completed)`` event for the seeded doc, then a ``summary``
    event whose payload deserializes as ReabstractReport.

    Anti-coincidental-pass: a route that still returns the old synchronous
    JSON ReabstractReport fails the content-type assertion immediately --
    the test does not need to inspect the body to know the route is
    pre-streaming.
    """
    app, vault_id, _config = maintenance_app
    services: SAGEServices = app.state.vault_registry[vault_id]
    await _seed_one_skipped(services, doc_id_label="router_happy")

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            f"/sage_vaults/{vault_id}/admin/reabstract-deferred",
            json={"include_pdf": False},
        )

    assert resp.status_code == 200, resp.text
    assert "text/event-stream" in resp.headers.get("content-type", ""), resp.headers

    events = _parse_sse_events(resp.text)

    progress = [e for e in events if e["event_type"] == "progress"]
    summaries = [e for e in events if e["event_type"] == "summary"]
    assert len(progress) == 2, f"expected started+completed pair, got {progress!r}"
    assert {p["status"] for p in progress} == {"started", "completed"}
    assert len(summaries) == 1
    assert events[-1]["event_type"] == "summary", "summary event must be last"

    summary_payload = {k: v for k, v in summaries[0].items() if k != "event_type"}
    report = ReabstractReport.model_validate(summary_payload)
    assert report.vault_id == vault_id
    assert report.reabstracted_count == 1
    assert report.failed_count == 0
    assert len(report.entries) == 1


async def test_post_reabstract_deferred_streams_failure_without_aborting(
    minimal_vault_config_dict,
    monkeypatch,
):
    """One doc's LLM failure does not abort the stream: the failing doc
    surfaces as a ``progress(failed)`` event with outcome=llm_failure,
    the second doc still gets its started+completed pair, and the
    summary lands with reabstracted_count=1, failed_count=1.

    Anti-coincidental-pass: if the failure mid-stream causes FastAPI to
    truncate the response or emit a 500, ``len(events) >= 5`` and the
    presence of a summary event both fail.
    """
    monkeypatch.setenv("SAGE_TEST_STUB_PROVIDERS", "1")
    config = VaultConfig.model_validate(minimal_vault_config_dict)
    app = create_app(config=config)

    failing: AbstractionProvider = _SelectivelyFailingProvider()
    await _initialize_services(
        app,
        config,
        abstraction_provider=failing,
        content_store_factory=lambda _brain: StubContentStore(),
    )
    vault_id = config.vault.id
    services: SAGEServices = app.state.vault_registry[vault_id]

    # Two skipped docs in deterministic insertion order.
    fail_doc = _make_skipped_doc(_id("router_fail_a"))
    ok_doc = _make_skipped_doc(_id("router_ok_b"))
    for doc in (fail_doc, ok_doc):
        await services.graph_store.insert_document(doc)
        await services.content_store.index_chunks(
            doc.id,
            [Chunk(document_id=doc.id, heading_path="Body", content="Body.", chunk_index=0)],
        )

    transport = ASGITransport(app=app)
    try:
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post(
                f"/sage_vaults/{vault_id}/admin/reabstract-deferred",
                json={"include_pdf": False},
            )

        assert resp.status_code == 200, resp.text
        assert "text/event-stream" in resp.headers.get("content-type", "")

        events = _parse_sse_events(resp.text)
        progress = [e for e in events if e["event_type"] == "progress"]
        summaries = [e for e in events if e["event_type"] == "summary"]

        # 2 docs × (started + terminal) = 4 progress events, then summary.
        assert len(progress) == 4, f"expected 4 progress events, got {progress!r}"
        assert len(summaries) == 1

        # First doc -> started + failed.
        fail_events = [p for p in progress if p["current_document_id"] == fail_doc.id]
        assert {p["status"] for p in fail_events} == {"started", "failed"}
        failed_terminal = next(p for p in fail_events if p["status"] == "failed")
        assert failed_terminal["outcome"] == "llm_failure"
        assert failed_terminal.get("error"), "failed event must carry error message"

        # Second doc -> started + completed (loop continued past the failure).
        ok_events = [p for p in progress if p["current_document_id"] == ok_doc.id]
        assert {p["status"] for p in ok_events} == {"started", "completed"}

        summary_payload = {k: v for k, v in summaries[0].items() if k != "event_type"}
        report = ReabstractReport.model_validate(summary_payload)
        assert report.reabstracted_count == 1
        assert report.failed_count == 1
    finally:
        await asyncio.sleep(0.1)
        await services.graph_store.close()
        mcp_server._vaults.clear()


async def test_post_reabstract_deferred_404_for_unknown_vault(maintenance_app):
    """An unregistered vault id returns 404 via get_vault_id, with
    application/json content-type and zero SSE events emitted.

    Anti-coincidental-pass: if the route opens its StreamingResponse
    before resolving the vault id (against the precedent at
    IngestStreamingService.stream which validates synchronously
    BEFORE returning the StreamingResponse), the 404 path emits a 200
    text/event-stream response with an in-stream error event -- the
    worst possible bug. The content-type assertion catches that even
    if the body happens to contain JSON-looking text.
    """
    app, _vault_id, _config = maintenance_app
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post("/sage_vaults/ghost/admin/reabstract-deferred")

    assert resp.status_code == 404, resp.text
    assert resp.headers.get("content-type", "").startswith("application/json"), resp.headers
    assert "data: " not in resp.text, "404 path must not emit SSE events"
    body = resp.json()
    assert body["code"] == "vault_not_found"


async def test_post_reabstract_deferred_409_when_already_in_flight(
    minimal_vault_config_dict,
    monkeypatch,
):
    """Two concurrent POSTs against the same vault: one 200, one 409 with
    a structured reabstract_already_in_flight payload that includes
    start_time. Built with a gated abstraction provider so the first
    call blocks until the test releases it."""
    monkeypatch.setenv("SAGE_TEST_STUB_PROVIDERS", "1")
    config = VaultConfig.model_validate(minimal_vault_config_dict)
    app = create_app(config=config)

    # Construct services with a gated provider so we can hold call A
    # mid-flight while call B attempts to enter.
    gated: AbstractionProvider = _GatedAbstractionProvider()
    await _initialize_services(
        app,
        config,
        abstraction_provider=gated,
        content_store_factory=lambda _brain: StubContentStore(),
    )
    vault_id = config.vault.id
    services: SAGEServices = app.state.vault_registry[vault_id]
    await _seed_one_skipped(services, doc_id_label="router_gated")

    transport = ASGITransport(app=app)
    try:
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            before = datetime.now(timezone.utc)
            task_a = asyncio.create_task(
                client.post(f"/sage_vaults/{vault_id}/admin/reabstract-deferred")
            )
            # Wait for the background reabstract to hit the gate, by
            # which time the lock is held inside MaintenanceService.
            await asyncio.wait_for(gated.entered.wait(), timeout=5.0)
            after = datetime.now(timezone.utc)

            resp_b = await client.post(f"/sage_vaults/{vault_id}/admin/reabstract-deferred")
            assert resp_b.status_code == 409, resp_b.text
            # no-leak guard: the 409 path must resolve BEFORE the
            # StreamingResponse is constructed -- content-type is
            # application/json and the body carries the ErrorResponse
            # envelope, not an SSE stream with an in-stream error event.
            assert resp_b.headers.get("content-type", "").startswith("application/json"), (
                resp_b.headers
            )
            assert "data: " not in resp_b.text, "409 path must not emit SSE events"
            body_b = resp_b.json()
            assert body_b["code"] == "reabstract_already_in_flight"
            assert body_b["detail"]["vault_id"] == vault_id
            start_time = datetime.fromisoformat(body_b["detail"]["start_time"])
            assert before <= start_time <= after

            # Release the gate; call A should complete with 200 and a
            # summary event whose payload deserializes as ReabstractReport.
            gated.gate.set()
            resp_a = await asyncio.wait_for(task_a, timeout=5.0)
            assert resp_a.status_code == 200, resp_a.text
            assert "text/event-stream" in resp_a.headers.get("content-type", "")
            events_a = _parse_sse_events(resp_a.text)
            summaries_a = [e for e in events_a if e["event_type"] == "summary"]
            assert len(summaries_a) == 1
            summary_payload = {k: v for k, v in summaries_a[0].items() if k != "event_type"}
            report_a = ReabstractReport.model_validate(summary_payload)
            assert report_a.reabstracted_count == 1

            # Confirm the on-disk pipeline_status transitioned (defense
            # against the report-without-effect coincidental pass).
            doc = await services.graph_store.get_document(
                _id("router_gated"),
            )
            assert doc.pipeline_status == PipelineStatus.ABSTRACTION_COMPLETE.value
    finally:
        await asyncio.sleep(0.1)
        await app.state.vault_registry[vault_id].graph_store.close()
        mcp_server._vaults.clear()
