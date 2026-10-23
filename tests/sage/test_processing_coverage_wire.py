"""The retained coverage answer survives real REST and MCP serialization."""

import json

import pytest
from httpx import ASGITransport, AsyncClient

from sage.app import create_app
from sage.mcp_server import build_partitioned_server
from tests.sage.test_processing_coverage import _chunks, _doc, _evidence
from tests.sage.test_validation_envelope_parity import vault_services as _shared_services

vault_services = _shared_services


def _decode(result):
    return json.loads(result[0].text)


@pytest.mark.parametrize("response_mode", ["full", "light", None])
async def test_document_search_and_maintenance_wire_keep_coverage(vault_services, response_mode):
    services = vault_services
    await services.graph_store.insert_document(_doc(1))
    store = services.content_store
    attempt = await store.begin_processing_attempt(_doc(1).id)
    assert await store.publish_processing(_doc(1).id, attempt, _evidence(), chunks=_chunks("old"))
    ordinary = build_partitioned_server("sage")
    app = create_app(config=services.config)
    app.state.vault_registry = {"test_vault": services}
    args = {"vault_id": "test_vault", "mode": "catalog"}
    if response_mode is not None:
        args["response_mode"] = response_mode
    searched = _decode(await ordinary.call_tool("search", args))
    expected = searched["results"][0]["document"]["processing_coverage"]
    assert expected["projection"]["scope"] == "limited"
    assert expected["projection"]["generation"] == attempt
    pending = _decode(
        await ordinary.call_tool(
            "list_pending_metadata",
            {
                "vault_id": "test_vault",
                **({"response_mode": response_mode} if response_mode else {}),
            },
        )
    )
    item = pending["items"][0]
    assert item.get("document", item)["processing_coverage"] == expected
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        read = await client.get(f"/sage_vaults/test_vault/documents/{_doc(1).id}")
        assert read.status_code == 200
        assert read.json()["processing_coverage"] == expected
        for tool in ["read_projection", "read_section"]:
            arguments = {"vault_id": "test_vault", "document_id": _doc(1).id}
            if tool == "read_section":
                arguments["heading_path"] = "Section"
            body = _decode(await ordinary.call_tool(tool, arguments))
            assert body["processing_coverage"] == expected
        document = _decode(
            await ordinary.call_tool(
                "get_document", {"vault_id": "test_vault", "document_id": _doc(1).id}
            )
        )
        assert document["processing_coverage"] == expected


@pytest.mark.parametrize("kind", ["ingest", "metadata", "bulk"])
async def test_forged_coverage_cannot_cross_rest_or_mcp_writes(vault_services, kind):
    services = vault_services
    await services.graph_store.insert_document(_doc(1))
    original = (await services.graph_store.get_document(_doc(1).id)).processing_coverage
    app = create_app(config=services.config)
    app.state.vault_registry = {"test_vault": services}
    ordinary = build_partitioned_server("sage")
    forged = {"source_retention": {"scope": "complete"}}
    if kind == "ingest":
        route, body, tool = (
            "documents",
            {"source": "missing.md", "processing_coverage": forged},
            "ingest_document",
        )
    elif kind == "metadata":
        route, body, tool = (
            "metadata",
            {"items": [{"document_id": _doc(1).id, "processing_coverage": forged}]},
            "update_metadata",
        )
    else:
        route, body, tool = (
            "documents:batch",
            {"files": [{"file_path": "missing.md", "processing_coverage": forged}]},
            "bulk_ingest_document",
        )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        if kind == "bulk":
            response = await client.post(
                f"/sage_vaults/test_vault/{route}",
                files={"files": ("source.md", b"hello", "text/markdown")},
                data={"metadata": json.dumps({"files": [{"processing_coverage": forged}]})},
            )
        else:
            response = await client.post(f"/sage_vaults/test_vault/{route}", json=body)
        assert response.status_code in (400, 422), response.text
        assert "processing_coverage" in response.text
    result = _decode(await ordinary.call_tool(tool, {"vault_id": "test_vault", **body}))
    assert result.get("error") or any(
        item.get("status") == "error" for item in result.get("results", [])
    ), result
    assert "processing_coverage" in json.dumps(result)
    assert (await services.graph_store.get_document(_doc(1).id)).processing_coverage == original


async def test_automatic_reduction_keeps_partial_evidence(vault_services, monkeypatch):
    from sage.models.schemas import DiscoverRequest
    from sage.services.retrieval import _serialized_response_bytes

    services = vault_services
    for index in range(1, 7):
        doc = _doc(index).model_copy(update={"source_content_hash": _doc(1).source_content_hash})
        await services.graph_store.insert_document(doc)
        attempt = await services.content_store.begin_processing_attempt(doc.id)
        chunks = [c for c in _chunks("old")]
        for chunk in chunks:
            chunk.document_id = doc.id
        assert await services.content_store.publish_processing(
            doc.id, attempt, _evidence(), chunks=chunks
        )
    full = await services.retrieval_service.discover(
        DiscoverRequest(mode="catalog", response_mode="full")
    )
    light = await services.retrieval_service.discover(
        DiscoverRequest(mode="catalog", response_mode="light")
    )
    budget = (_serialized_response_bytes(full) + _serialized_response_bytes(light)) // 2
    monkeypatch.setenv("SAGE_MCP_INLINE_BUDGET_BYTES", str(budget))
    ordinary = build_partitioned_server("sage")
    response = _decode(
        await ordinary.call_tool("search", {"vault_id": "test_vault", "mode": "catalog"})
    )
    assert response["hints"]["reason"] == "catalog_response_degraded_to_light"
    assert "source_type" not in response["results"][0]["document"]
    assert all(
        hit["document"]["processing_coverage"]["projection"]["scope"] == "limited"
        for hit in response["results"]
    )


@pytest.mark.parametrize("transport", ["rest", "mcp"])
@pytest.mark.parametrize("response_mode", ["full", "light", None])
@pytest.mark.parametrize("established", [False, True])
async def test_metadata_mutation_wire_returns_post_maintenance_coverage(
    vault_services, transport, response_mode, established
):
    from sage.adapters.content_store_postgres import PostgresContentStore
    from sage.adapters.interfaces import DocumentSurface
    from sage.models.coverage import CoverageDimension
    from sage.models.wire import to_wire

    services = vault_services
    await services.graph_store.insert_document(_doc(1))
    store = PostgresContentStore(services.graph_store._pool)
    services.metadata_service._content = store
    attempt = await store.begin_processing_attempt(_doc(1).id)
    evidence = (
        _evidence()
        if established
        else {
            name: CoverageDimension()
            for name in ("projection", "keyword_indexing", "passage_embeddings")
        }
    )
    assert await store.publish_processing(
        _doc(1).id,
        attempt,
        evidence,
        chunks=_chunks("old"),
        surface=DocumentSurface(document_id=_doc(1).id, matchable="old", orienting="abstract old"),
    )
    before = await services.graph_store.get_document(_doc(1).id)
    assert before.processing_coverage.keyword_indexing.scope == (
        "limited" if established else "unknown"
    )
    request = {"items": [{"document_id": _doc(1).id, "title": "Renamed"}]}
    if response_mode is not None:
        request["response_mode"] = response_mode
    if transport == "mcp":
        body = _decode(
            await build_partitioned_server("sage").call_tool(
                "update_metadata", {"vault_id": "test_vault", **request}
            )
        )
    else:
        app = create_app(config=services.config)
        app.state.vault_registry = {"test_vault": services}
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.post("/sage_vaults/test_vault/metadata", json=request)
            assert response.status_code == 200
            body = response.json()
    persisted = await services.graph_store.get_document(_doc(1).id)
    assert body["success_count"] == 1
    assert persisted.title == "Renamed"
    assert persisted.processing_coverage.keyword_indexing.generation > attempt
    if response_mode == "light":
        assert body["results"][0].get("document") is None
    else:
        document = body["results"][0]["document"]
        assert document["title"] == persisted.title
        assert document["processing_coverage"] == to_wire(persisted)["processing_coverage"]
