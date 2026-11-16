"""Responses carrying light rows warn that the rows' full coverage is deprecated.

A light document row's full ``processing_coverage`` is deprecated (CAS-ADR-008
clause 8) and is still carried unchanged until the adaptation ships. Every
response that carries a light row warns the caller, on REST and MCP alike, and
no response without one does. The warning follows the rows the response
actually carries, not what the request asked for: an unset ``response_mode``
that the inline budget degrades to light warns, and one that stays full does
not.

Anti-coincidental-pass discipline:

* Each warning arm has a paired control on the same seeded vault that carries
  no light row and must carry no deprecation warning, so a service that
  always warns fails.
* The degraded arm forces the budget between the measured full and light
  sizes, and asserts the degrade happened, so a warning judged from the
  request's ``response_mode`` fails it.
* The pending queue's automatic light mode (more than five rows) is exercised
  apart from the explicit request, so a warning tied to the explicit value
  fails it.
* The expected warning is a literal string compared on both surfaces.
"""

from __future__ import annotations

import json

from httpx import ASGITransport, AsyncClient

from sage.app import create_app
from sage.mcp_server import build_partitioned_server
from tests.sage.test_processing_coverage import _chunks, _doc, _evidence
from tests.sage.test_validation_envelope_parity import vault_services as _shared_services

vault_services = _shared_services

VAULT = "test_vault"
WARNING = (
    "Deprecated: the DocumentSummaryLight.processing_coverage response field is deprecated; "
    'use response_mode="full" or get_document for the full object instead. '
    "The change may ship from 2027-01-04."
)


def _decode(result) -> dict:
    return json.loads(result[0].text)


def _deprecations(body: dict, *, field: str = "hints") -> list[str]:
    carrier = body.get(field) or {}
    warnings = carrier.get("warnings") if isinstance(carrier, dict) else carrier
    return [w for w in warnings or [] if w.startswith("Deprecated:")]


async def _seed(services, count: int) -> None:
    for index in range(1, count + 1):
        # The evidence's processing identity names the first document's source.
        doc = _doc(index).model_copy(update={"source_content_hash": _doc(1).source_content_hash})
        await services.graph_store.insert_document(doc)
        attempt = await services.content_store.begin_processing_attempt(doc.id)
        chunks = _chunks("old")
        for chunk in chunks:
            chunk.document_id = doc.id
        assert await services.content_store.publish_processing(
            doc.id, attempt, _evidence(), chunks=chunks
        )


async def _both(services, tool: str, rest_call, args: dict) -> tuple[dict, dict]:
    """The same call on REST and MCP."""
    server = build_partitioned_server("sage")
    mcp = _decode(await server.call_tool(tool, {"vault_id": VAULT, **args}))
    app = create_app(config=services.config)
    app.state.vault_registry = {VAULT: services}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await rest_call(client, args)
    assert response.status_code == 200, response.text
    return response.json(), mcp


async def _search(services, args: dict) -> tuple[dict, dict]:
    return await _both(
        services,
        "search",
        lambda client, body: client.post(f"/sage_vaults/{VAULT}/discover", json=body),
        args,
    )


async def _pending(services, args: dict) -> tuple[dict, dict]:
    return await _both(
        services,
        "list_pending_metadata",
        lambda client, params: client.get(f"/sage_vaults/{VAULT}/pending-metadata", params=params),
        args,
    )


async def test_an_explicit_light_catalog_page_warns_and_still_carries_the_coverage(vault_services):
    await _seed(vault_services, 2)

    for body in await _search(vault_services, {"mode": "catalog", "response_mode": "light"}):
        assert _deprecations(body) == [WARNING]
        for hit in body["results"]:
            assert hit["document"]["processing_coverage"]["projection"]["scope"] == "limited"


async def test_a_full_or_undegraded_catalog_page_does_not_warn(vault_services):
    await _seed(vault_services, 2)

    for args in ({"mode": "catalog", "response_mode": "full"}, {"mode": "catalog"}):
        for body in await _search(vault_services, args):
            assert _deprecations(body) == [], args
            assert "source_path" in body["results"][0]["document"], args


async def test_a_scored_light_page_carries_no_light_rows_and_no_warning(vault_services):
    await _seed(vault_services, 2)

    args = {"mode": "keyword", "query": "old", "response_mode": "light"}
    for body in await _search(vault_services, args):
        assert body["results"], "the keyword query must match the seeded passages"
        assert _deprecations(body) == []


async def test_an_unset_mode_degraded_to_light_warns(vault_services, monkeypatch):
    from sage.models.schemas import DiscoverRequest
    from sage.services.retrieval import _serialized_response_bytes

    await _seed(vault_services, 6)
    service = vault_services.retrieval_service
    full = await service.discover(DiscoverRequest(mode="catalog", response_mode="full"))
    light = await service.discover(DiscoverRequest(mode="catalog", response_mode="light"))
    budget = (_serialized_response_bytes(full) + _serialized_response_bytes(light)) // 2
    monkeypatch.setenv("SAGE_MCP_INLINE_BUDGET_BYTES", str(budget))

    response = _decode(
        await build_partitioned_server("sage").call_tool(
            "search", {"vault_id": VAULT, "mode": "catalog"}
        )
    )

    assert response["hints"]["reason"] == "catalog_response_degraded_to_light"
    assert _deprecations(response) == [WARNING]
    assert all("source_path" not in hit["document"] for hit in response["results"])


async def test_the_degrade_decision_counts_the_warning_it_will_deliver(vault_services, monkeypatch):
    """A degraded response fits the budget with its warning, not only without it.

    The pair: at the delivered size the degrade happens; one byte under it,
    the degrade must not happen, because a candidate measured without the
    warning it will carry would fit there and then overrun once the warning is
    added.
    """
    from sage.models.schemas import DiscoverRequest
    from sage.services.retrieval import _serialized_response_bytes

    await _seed(vault_services, 6)
    service = vault_services.retrieval_service
    full = await service.discover(DiscoverRequest(mode="catalog", response_mode="full"))
    light = await service.discover(DiscoverRequest(mode="catalog", response_mode="light"))
    monkeypatch.setenv(
        "SAGE_MCP_INLINE_BUDGET_BYTES",
        str((_serialized_response_bytes(full) + _serialized_response_bytes(light)) // 2),
    )
    degraded = await service.discover(DiscoverRequest(mode="catalog"))
    assert degraded.hints["reason"] == "catalog_response_degraded_to_light"
    assert WARNING in degraded.hints["warnings"]
    delivered = _serialized_response_bytes(degraded)

    monkeypatch.setenv("SAGE_MCP_INLINE_BUDGET_BYTES", str(delivered))
    at_size = await service.discover(DiscoverRequest(mode="catalog"))
    assert at_size.hints["reason"] == "catalog_response_degraded_to_light"
    assert _serialized_response_bytes(at_size) == delivered

    monkeypatch.setenv("SAGE_MCP_INLINE_BUDGET_BYTES", str(delivered - 1))
    under = await service.discover(DiscoverRequest(mode="catalog"))
    assert under.hints["reason"] != "catalog_response_degraded_to_light"
    assert _deprecations(under.model_dump(mode="json")) == []


async def test_a_light_pending_page_warns_and_a_full_one_does_not(vault_services):
    await _seed(vault_services, 2)

    for body in await _pending(vault_services, {"response_mode": "light"}):
        assert _deprecations(body, field="warnings") == [WARNING]
        assert "source_path" not in body["items"][0]
    for args in ({"response_mode": "full"}, {}):
        for body in await _pending(vault_services, args):
            assert _deprecations(body, field="warnings") == [], args
            assert "warnings" not in body or body["warnings"] is None, args


async def test_an_automatically_light_pending_page_warns(vault_services):
    """More than five rows and no response_mode: the queue answers light by itself."""
    await _seed(vault_services, 6)

    for body in await _pending(vault_services, {}):
        assert body["response_mode"] == "light"
        assert _deprecations(body, field="warnings") == [WARNING]
        assert all("source_path" not in item for item in body["items"])
