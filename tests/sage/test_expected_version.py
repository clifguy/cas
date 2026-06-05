"""Optimistic-concurrency tests for `expected_version` on metadata writes.

Implements CAS-ADR-038 Primitive B. Covers the success path, stale
rejection, back-compat omission, parallel writers, retry workflow,
dry-run interaction, bulk surface, error-envelope shape, and transport
round-trips on both FastMCP and FastAPI.
"""

from __future__ import annotations

import asyncio
import json

import pytest
from httpx import ASGITransport, AsyncClient

from sage import mcp_server as _mcp
from sage.adapters.stubs import (
    StubAbstractionProvider,
    StubContentStore,
    StubEmbeddingProvider,
)
from sage.api.errors import StaleReadError
from sage.app import _initialize_services, create_app
from sage.config import VaultConfig
from sage.mcp_server import (
    get_document,
    ingest_document,
)
from sage.mcp_server import (
    update_metadata as _update_metadata_bulk,
)
from tests.sage.conftest import initialize_services_for_test


async def update_metadata(vault_id, document_id, **kwargs):
    """Singleton-shaped shim around the consolidated update_metadata tool.

    Post-CAS-ADR-029 the MCP tool takes ``items: list[dict]``; this shim
    accepts the legacy ``(vault_id, document_id, **patch)`` signature
    by wrapping the call as a length-1 ``items`` collection and
    unwrapping the per-item result envelope back to a singleton-shape
    response so the existing test assertions continue to apply.
    """
    # dry_run is an envelope-level parameter on the bulk request, not a
    # per-item field; pop it out so it doesn't slip into the items[] entry.
    dry_run = kwargs.pop("dry_run", False)
    item = {"document_id": document_id, **kwargs}
    result = await _update_metadata_bulk(vault_id=vault_id, items=[item], dry_run=dry_run)
    if isinstance(result, dict) and "error" in result and "results" not in result:
        return result
    if isinstance(result, dict) and result.get("results"):
        per = result["results"][0]
        if per.get("status") == "error":
            err = per.get("error") or {}
            return {
                "error": err.get("error"),
                "message": err.get("message"),
                "detail": err.get("detail"),
            }
        out = {"document": per.get("document"), "dry_run": dry_run}
        if "warnings" in per and per["warnings"]:
            out["warnings"] = per["warnings"]
        if "changes" in per:
            out["changes"] = per["changes"]
        return out
    return result


async def bulk_update_metadata(vault_id, items, **kwargs):
    """Direct passthrough for callers that already use the bulk shape."""
    return await _update_metadata_bulk(vault_id=vault_id, items=items, **kwargs)


@pytest.fixture
async def vault_services(minimal_vault_config_dict, tmp_vault_dir):
    """Initialize SAGE services and register them in the MCP vault registry.

    Mirrors the fixture in `test_mcp_server.py` so the new tests reuse
    the same MCP wire-path harness without cross-module fixture
    importing.
    """
    config = VaultConfig.model_validate(minimal_vault_config_dict)
    async with initialize_services_for_test(
        config,
        content_store=StubContentStore(),
        embedding_provider=StubEmbeddingProvider(),
        abstraction_provider=StubAbstractionProvider(),
    ) as services:
        _mcp._vaults["test_vault"] = services

        sources = tmp_vault_dir / "sources"
        test_dir = sources / "test"
        test_dir.mkdir(parents=True, exist_ok=True)
        (test_dir / "sample.md").write_text("# Sample Document\n\nSample content.")
        (test_dir / "second.md").write_text("# Second Document\n\nDifferent content.")
        (test_dir / "third.md").write_text("# Third Document\n\nThird content.")

        try:
            yield services
        finally:
            await asyncio.sleep(0.5)
            _mcp._vaults.pop("test_vault", None)


def _parse(result):
    """Parse a tool's result (dict or JSON string)."""
    if isinstance(result, dict):
        return result
    return json.loads(result)


_TERMINAL_PIPELINE_STATES = {"abstraction_complete", "abstraction_skipped", "failed"}


async def _seed_doc(source_path: str = "test/sample.md") -> tuple[str, str]:
    """Ingest a fresh document and return (document_id, current_updated_at).

    Waits for the background abstraction pipeline to reach a terminal
    `pipeline_status` before reading `updated_at`. Without this gate,
    the background pipeline can advance `updated_at` between the test's
    read and its compare-and-swap call, producing a `stale_read` from
    the pipeline race rather than the assertion under test.
    """
    ingest_result = _parse(await ingest_document("test_vault", source_path, "markdown"))
    doc_id = ingest_result["id"]
    for _ in range(100):
        doc = _parse(await get_document("test_vault", doc_id))
        if doc.get("pipeline_status") in _TERMINAL_PIPELINE_STATES:
            return doc_id, doc["updated_at"]
        await asyncio.sleep(0.02)
    raise AssertionError(f"document {doc_id!r} pipeline did not reach terminal state in 2s")


# ---------------------------------------------------------------------------
# T8: Error-envelope structural test
# ---------------------------------------------------------------------------


def test_t8_stale_read_error_envelope_shape():
    """The public SAGEError surfaces with code `stale_read`, status 409,
    and a three-field detail payload (document_id, expected_version,
    current_version). Mirrors `test_tier3_uniqueness.py::test_t11`.
    """
    exc = StaleReadError(
        document_id="d1",
        expected_version="v_old",
        current_version="v_new",
    )
    assert exc.code == "stale_read"
    assert exc.status_code == 409
    assert exc.detail == {
        "document_id": "d1",
        "expected_version": "v_old",
        "current_version": "v_new",
    }


# ---------------------------------------------------------------------------
# T1, T3: Back-compat and matching-version happy paths
# ---------------------------------------------------------------------------


async def test_t1_matching_expected_version_succeeds(vault_services):
    """Caller passes the current `updated_at`; write succeeds and
    `updated_at` advances past the supplied version.
    """
    doc_id, v0 = await _seed_doc()
    result = _parse(
        await update_metadata("test_vault", doc_id, title="renamed", expected_version=v0)
    )
    assert "error" not in result, f"unexpected error: {result!r}"
    assert result["document"]["title"] == "renamed"
    v1 = result["document"]["updated_at"]
    assert v1 != v0, "updated_at must advance past the supplied expected_version"


async def test_t3_omitted_expected_version_preserves_back_compat(vault_services):
    """Omitting `expected_version` keeps the pre-Primitive-B contract:
    no check, last-writer-wins. The call must succeed regardless of any
    intervening update.
    """
    doc_id, v0 = await _seed_doc()
    # First write advances version without the caller observing it.
    intermediate = _parse(await update_metadata("test_vault", doc_id, title="first"))
    assert "error" not in intermediate
    v_advanced = intermediate["document"]["updated_at"]
    assert v_advanced != v0

    # Second write omits expected_version: must succeed even though we
    # never observed v_advanced; the check is gated on parameter
    # presence, not always active.
    result = _parse(await update_metadata("test_vault", doc_id, title="second"))
    assert "error" not in result, f"omitted expected_version must succeed: {result!r}"
    assert result["document"]["title"] == "second"


# ---------------------------------------------------------------------------
# T2: Stale rejection (also serves as the MCP-transport round-trip)
# ---------------------------------------------------------------------------


async def test_t2_stale_expected_version_returns_structured_stale_read(vault_services):
    """A mismatched `expected_version` rejects with the structured
    `stale_read` 409 envelope. Document state is unchanged.
    """
    doc_id, v0 = await _seed_doc()

    # Advance the document so v0 is now stale.
    first = _parse(await update_metadata("test_vault", doc_id, title="advance"))
    v_current = first["document"]["updated_at"]
    assert v_current != v0

    result = _parse(
        await update_metadata("test_vault", doc_id, title="should_not_land", expected_version=v0)
    )
    assert result["error"] == "stale_read"
    assert result["detail"]["document_id"] == doc_id
    assert result["detail"]["expected_version"] == v0
    assert result["detail"]["current_version"] == v_current

    # No-side-effect: title is still the prior winner.
    after = _parse(await get_document("test_vault", doc_id))
    assert after["title"] == "advance", (
        f"stale rejection must not mutate the document; got title={after['title']!r}"
    )
    assert after["updated_at"] == v_current, (
        f"stale rejection must not advance updated_at; got {after['updated_at']!r}"
    )


# ---------------------------------------------------------------------------
# T4: Parallel stale-vs-fresh — exactly one writer lands
# ---------------------------------------------------------------------------


async def test_t4_parallel_same_version_one_wins_one_stale(vault_services):
    """Two parallel updates both holding `expected_version=V0`: exactly
    one succeeds (claims V0 → V1) and the other rejects with
    `stale_read` whose `current_version` matches V1. Per-document lock
    serializes the pair; the second writer through the lock observes
    the advanced version.

    Runs 25 iterations to surface interleaving races, mirroring the
    Primitive A commutative-add sweep (CAS-ADR-038).
    """
    doc_id, _ = await _seed_doc()

    for i in range(25):
        before = _parse(await get_document("test_vault", doc_id))
        v_baseline = before["updated_at"]

        a_title = f"a_{i}"
        b_title = f"b_{i}"
        results = await asyncio.gather(
            update_metadata("test_vault", doc_id, title=a_title, expected_version=v_baseline),
            update_metadata("test_vault", doc_id, title=b_title, expected_version=v_baseline),
        )
        parsed = [_parse(r) for r in results]
        successes = [p for p in parsed if "error" not in p]
        stale = [p for p in parsed if p.get("error") == "stale_read"]
        assert len(successes) == 1, f"iteration {i}: expected exactly one success; got {parsed!r}"
        assert len(stale) == 1, f"iteration {i}: expected exactly one stale_read; got {parsed!r}"

        winner_version = successes[0]["document"]["updated_at"]
        assert stale[0]["detail"]["expected_version"] == v_baseline
        assert stale[0]["detail"]["current_version"] == winner_version, (
            f"iteration {i}: stale envelope's current_version must point to "
            f"the winner's resulting updated_at; got "
            f"{stale[0]['detail']['current_version']!r} vs winner "
            f"{winner_version!r}"
        )

        after = _parse(await get_document("test_vault", doc_id))
        assert after["updated_at"] == winner_version
        assert after["title"] in {a_title, b_title}


# ---------------------------------------------------------------------------
# T5: Retry-after-stale end-to-end
# ---------------------------------------------------------------------------


async def test_t5_retry_after_stale_succeeds_with_current_version(vault_services):
    """End-to-end recovery: stale write returns the current version in
    its detail; caller uses that value as the retry token and the
    follow-up write succeeds. This pins the retry contract — the
    envelope is sufficient without an out-of-band refetch round-trip.
    """
    doc_id, v0 = await _seed_doc()

    # A racer advances the document.
    racer = _parse(await update_metadata("test_vault", doc_id, title="racer", expected_version=v0))
    v_after_racer = racer["document"]["updated_at"]
    assert v_after_racer != v0

    # The original caller holds v0; their write is now stale.
    stale = _parse(
        await update_metadata("test_vault", doc_id, title="caller_attempt", expected_version=v0)
    )
    assert stale["error"] == "stale_read"
    retry_token = stale["detail"]["current_version"]
    assert retry_token == v_after_racer

    # Use the envelope's retry token directly — no separate refetch.
    retry_result = _parse(
        await update_metadata(
            "test_vault",
            doc_id,
            title="caller_attempt",
            expected_version=retry_token,
        )
    )
    assert "error" not in retry_result, f"retry must succeed: {retry_result!r}"
    assert retry_result["document"]["title"] == "caller_attempt"
    assert retry_result["document"]["updated_at"] != retry_token


# ---------------------------------------------------------------------------
# T6: Dry-run with stale expected_version rejects
# ---------------------------------------------------------------------------


async def test_t6_dry_run_with_stale_expected_version_rejects(vault_services):
    """Design decision D4 in the plan: the compare-and-swap is part of
    the write contract, not the persistence step. A dry-run with a
    stale version must reject in the same shape a real run would.
    """
    doc_id, v0 = await _seed_doc()
    advanced = _parse(
        await update_metadata("test_vault", doc_id, title="advance", expected_version=v0)
    )
    v_current = advanced["document"]["updated_at"]

    result = _parse(
        await update_metadata(
            "test_vault",
            doc_id,
            title="x",
            expected_version=v0,
            dry_run=True,
        )
    )
    assert result["error"] == "stale_read"
    assert result["detail"]["current_version"] == v_current

    # State unchanged: title is still the prior winner.
    after = _parse(await get_document("test_vault", doc_id))
    assert after["title"] == "advance"


# ---------------------------------------------------------------------------
# T7: Bulk path with one stale item among fresh items
# ---------------------------------------------------------------------------


async def test_t7_bulk_one_stale_item_rejects_per_item_rest_succeed(vault_services):
    """Per-item compare-and-swap: a stale item rejects with `stale_read`
    inside the response envelope while fresh items commit. Per-item
    isolation per CAS-ADR-029.
    """
    doc1_id, v1 = await _seed_doc("test/sample.md")
    doc2_id, v2 = await _seed_doc("test/second.md")
    doc3_id, v3 = await _seed_doc("test/third.md")

    items = [
        {"document_id": doc1_id, "title": "d1_new", "expected_version": v1},
        {"document_id": doc2_id, "title": "d2_new", "expected_version": "STALE_VERSION"},
        {"document_id": doc3_id, "title": "d3_new", "expected_version": v3},
    ]
    result = _parse(await bulk_update_metadata("test_vault", items, response_mode="full"))
    assert "error" not in result, f"bulk envelope must not error: {result!r}"
    assert result["success_count"] == 2
    assert result["error_count"] == 1
    results_by_id = {r["document_id"]: r for r in result["results"]}
    assert results_by_id[doc1_id]["status"] == "success"
    assert results_by_id[doc1_id]["document"]["title"] == "d1_new"
    assert results_by_id[doc3_id]["status"] == "success"
    assert results_by_id[doc3_id]["document"]["title"] == "d3_new"

    failure = results_by_id[doc2_id]
    assert failure["status"] == "error"
    # Per-item envelope mirrors the MCP envelope (`error` key, not
    # `code`), per `sage.services._bulk_envelope.sage_error_to_envelope`.
    assert failure["error"]["error"] == "stale_read"
    assert failure["error"]["detail"]["document_id"] == doc2_id
    assert failure["error"]["detail"]["expected_version"] == "STALE_VERSION"
    assert failure["error"]["detail"]["current_version"] == v2

    # State of doc2 is unchanged.
    doc2_after = _parse(await get_document("test_vault", doc2_id))
    assert doc2_after["title"] != "d2_new"
    assert doc2_after["updated_at"] == v2


# ---------------------------------------------------------------------------
# T9: FastMCP wire-path round-trip for the stale_read envelope
# ---------------------------------------------------------------------------


async def test_t9_fastmcp_wire_path_surfaces_stale_read_envelope(vault_services):
    """Through the JSON-RPC dispatch (call_tool), a stale
    `expected_version` produces `{"error": "stale_read", "message": ...,
    "detail": {...}}` on real MCP transport. Pins MCP/HTTP transport
    symmetry — the envelope shape must match across surfaces, a
    discipline this codebase has historically had to enforce
    explicitly.
    """
    from mcp.types import TextContent

    doc_id, v0 = await _seed_doc()
    advanced = _parse(
        await update_metadata("test_vault", doc_id, title="advance", expected_version=v0)
    )
    v_current = advanced["document"]["updated_at"]

    # Post-CAS-ADR-029: update_metadata MCP tool takes items: list[dict].
    # The stale_read surfaces as a per-item error envelope inside results[].
    response = await _mcp.mcp.call_tool(
        "update_metadata",
        {
            "vault_id": "test_vault",
            "items": [
                {
                    "document_id": doc_id,
                    "title": "x",
                    "expected_version": v0,
                },
            ],
        },
    )
    assert isinstance(response, list)
    assert len(response) == 1
    assert isinstance(response[0], TextContent)
    envelope = json.loads(response[0].text)
    assert envelope["success_count"] == 0 and envelope["error_count"] == 1
    per_item = envelope["results"][0]
    assert per_item["status"] == "error"
    err = per_item["error"]
    assert err["error"] == "stale_read"
    assert err["detail"]["document_id"] == doc_id
    assert err["detail"]["expected_version"] == v0
    assert err["detail"]["current_version"] == v_current


# ---------------------------------------------------------------------------
# T10: FastAPI / HTTP transport round-trip
# ---------------------------------------------------------------------------


@pytest.fixture
async def seeded_http_app(minimal_vault_config_dict, monkeypatch):
    """Boot the ASGI app and seed one document; expose (app, vault_id,
    doc_id, initial_updated_at).
    """
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

    from tests.sage.test_lifecycle import _id, _make_doc

    doc_id = _id("http_t10")
    await graph_store.insert_document(_make_doc(doc_id))
    await graph_store.update_document(doc_id, {"doc_type": "note", "metadata_confirmed": True})
    seeded = await graph_store.get_document(doc_id)
    # Mirror the canonical wire form (Pydantic emits UTC datetimes with
    # a `Z` suffix in JSON) so the caller can round-trip the value.
    wire_v0 = seeded.updated_at.isoformat().replace("+00:00", "Z")

    yield app, vault_id, doc_id, wire_v0

    await asyncio.sleep(0.1)
    if vault_id in app.state.vault_registry:
        app.state.vault_registry[vault_id].close_timing()
        await app.state.vault_registry[vault_id].graph_store.close()
    _mcp._vaults.clear()


async def test_t10_http_post_stale_expected_version_returns_per_item_stale_read(
    seeded_http_app,
):
    """POST /sage_vaults/{vault_id}/metadata with a stale per-item
    `expected_version` returns HTTP 200 carrying a per-item `stale_read`
    error envelope inside `results[]` (post-CAS-ADR-029 plural-noun shape).
    Per CAS-ADR-029 v4 the batch is not atomic; a per-item write
    rejection surfaces as a per-item envelope, not a batch-level HTTP
    409.
    """
    app, vault_id, doc_id, v0 = seeded_http_app

    # Advance the document via a first POST so v0 is stale.
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        first = await client.post(
            f"/sage_vaults/{vault_id}/metadata",
            json={
                "items": [
                    {"document_id": doc_id, "title": "advance", "expected_version": v0},
                ],
            },
        )
        assert first.status_code == 200, first.text
        first_body = first.json()
        assert first_body["success_count"] == 1
        v_current = first_body["results"][0]["document"]["updated_at"]
        assert v_current != v0

        response = await client.post(
            f"/sage_vaults/{vault_id}/metadata",
            json={
                "items": [
                    {
                        "document_id": doc_id,
                        "title": "should_not_land",
                        "expected_version": v0,
                    },
                ],
            },
        )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["success_count"] == 0 and body["error_count"] == 1
    per = body["results"][0]
    assert per["status"] == "error"
    err = per["error"]
    assert err["error"] == "stale_read"
    assert err["detail"]["document_id"] == doc_id
    assert err["detail"]["expected_version"] == v0
    assert err["detail"]["current_version"] == v_current


# Alias the renamed test to preserve the legacy name for any external
# selection by node-id; the function body is identical.
test_t10_http_patch_stale_expected_version_returns_409_envelope = (
    test_t10_http_post_stale_expected_version_returns_per_item_stale_read
)
