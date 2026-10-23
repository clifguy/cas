"""Established inspection survives actual MCP response variants."""

import json

import pytest

from tests.sage.test_rest_operation_mcp_counterparts import (
    _EXPORT_BODY,
    _ingest,
    _publish_export_inspection,
)
from tests.sage.test_rest_operation_mcp_counterparts import (
    yaml_app as _shared_yaml_app,
)

yaml_app = _shared_yaml_app


@pytest.fixture
async def established_selection(yaml_app, tmp_vault_dir):
    app, vault_id = yaml_app
    services = app.state.vault_registry[vault_id]
    name = "emitter_evidence"
    doc_id = await _ingest(services, tmp_vault_dir, name, body=_EXPORT_BODY)
    coverage = await _publish_export_inspection(services, tmp_vault_dir, doc_id, name)
    await services.graph_store.update_document(doc_id, {"metadata_confirmed": False})
    inspection = coverage["inspection"]
    assert inspection["generation"] > 1
    assert inspection["selection_summary"]["outcome"] == "limited"
    assert inspection["selection_summary"]["established_occurrences"] == 2
    assert inspection["selection_summary"]["associations_complete"] is False
    assert inspection["identity"]["tokenization"] == "utf8-byte-tokens:1"
    return app, vault_id, doc_id, inspection


def _assert_inspection(document, expected):
    assert document["processing_coverage"]["inspection"] == expected
    assert expected["selection_summary"]["boundary"]
    assert expected["selection_summary"]["selected_inputs"] > 0
    assert expected["selection_summary"]["selected_tokens"] > 0


@pytest.mark.parametrize("tool_name", ["search", "list_pending_metadata"])
@pytest.mark.parametrize("response_mode", ["full", "light", None])
async def test_catalog_and_pending_variants_preserve_selection(
    established_selection, tool_payload, tool_name, response_mode
):
    app, vault_id, doc_id, expected = established_selection
    arguments = {"vault_id": vault_id}
    if tool_name == "search":
        arguments["mode"] = "catalog"
    if response_mode is not None:
        arguments["response_mode"] = response_mode
    body = tool_payload(await app.state.mcp_mounts["/mcp"].call_tool(tool_name, arguments))
    rows = body["results"] if tool_name == "search" else body["items"]
    assert len(rows) == 1
    document = rows[0].get("document", rows[0])
    assert document["id"] == doc_id
    _assert_inspection(document, expected)


async def test_traverse_preserves_reached_document_selection(
    established_selection, tmp_vault_dir, tool_payload
):
    app, vault_id, doc_id, expected = established_selection
    services = app.state.vault_registry[vault_id]
    start_id = await _ingest(services, tmp_vault_dir, "emitter_start")
    ordinary = app.state.mcp_mounts["/mcp"]
    linked = tool_payload(
        await ordinary.call_tool(
            "create_edges",
            {
                "vault_id": vault_id,
                "items": [{"source_id": start_id, "target_id": doc_id, "edge_type": "supersedes"}],
            },
        )
    )
    assert linked["success_count"] == 1, linked
    body = tool_payload(
        await ordinary.call_tool("traverse", {"vault_id": vault_id, "start_id": start_id})
    )
    assert len(body["nodes"]) == 1
    document = body["nodes"][0]["document"]
    assert document["id"] == doc_id
    _assert_inspection(document, expected)


@pytest.mark.parametrize("tool_name", ["list_headings", "read_section"])
async def test_ordinary_projection_readers_preserve_selection(
    established_selection, tool_payload, tool_name
):
    app, vault_id, doc_id, expected = established_selection
    arguments = {"vault_id": vault_id, "document_id": doc_id}
    if tool_name == "read_section":
        arguments["heading_path"] = "emitter_evidence"
    body = tool_payload(await app.state.mcp_mounts["/mcp"].call_tool(tool_name, arguments))
    assert "error" not in body
    _assert_inspection(body, expected)


@pytest.mark.parametrize("tool_name", ["update_metadata", "update_lifecycles"])
@pytest.mark.parametrize("response_mode", ["full", "light", None])
async def test_mutation_response_variants_preserve_or_deliberately_omit_selection(
    established_selection, tool_payload, tool_name, response_mode
):
    app, vault_id, doc_id, expected = established_selection
    item = {"document_id": doc_id}
    item.update(
        {"title": "Changed title"} if tool_name == "update_metadata" else {"action": "archive"}
    )
    arguments = {"vault_id": vault_id, "items": [item]}
    if response_mode is not None:
        arguments["response_mode"] = response_mode
    body = tool_payload(await app.state.mcp_mounts["/mcp"].call_tool(tool_name, arguments))
    assert body["success_count"] == 1
    row = body["results"][0]
    assert row["status"] == "success"
    assert row["document_id"] == doc_id
    persisted = await app.state.vault_registry[vault_id].graph_store.get_document(doc_id)
    assert persisted.processing_coverage.inspection.model_dump(mode="json") == expected
    if response_mode == "light":
        assert row.get("document") is None
        assert "processing_coverage" not in row
        assert "selection_summary" not in json.dumps(body)
    else:
        _assert_inspection(row["document"], expected)


async def test_actual_mcp_ingest_reports_its_established_selection(
    yaml_app, tmp_vault_dir, tool_payload
):
    app, vault_id = yaml_app
    source = tmp_vault_dir / "sources" / "samples" / "mcp_selection.md"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text(
        "# MCP selection\n\nFirst authored paragraph.\n\n"
        "Second authored paragraph.\n\nThird authored paragraph."
    )
    body = tool_payload(
        await app.state.mcp_mounts["/mcp"].call_tool(
            "ingest_document",
            {
                "vault_id": vault_id,
                "source": "samples/mcp_selection.md",
                "source_type": "markdown",
                "config": {"discovery_selection": {"max_values": 2, "max_tokens": 64}},
            },
        )
    )
    assert "error" not in body
    # MCP returns the admitted projection snapshot before queued stages settle.
    assert body["pipeline_status"] == "projection_complete"
    assert body["processing_coverage"]["inspection"]["scope"] == "unknown"
    assert "selection_summary" not in body["processing_coverage"]["inspection"]
    from tests.helpers.pipeline_wait import await_pipeline_idle

    services = app.state.vault_registry[vault_id]
    await await_pipeline_idle(services.graph_store, body["id"], service=services.ingestion_service)
    settled = tool_payload(
        await app.state.mcp_mounts["/mcp"].call_tool(
            "get_document",
            {
                "vault_id": vault_id,
                "document_id": body["id"],
            },
        )
    )
    inspection = settled["processing_coverage"]["inspection"]
    summary = inspection["selection_summary"]
    assert inspection["scope"] == "limited"
    assert inspection["generation"] > 0
    assert inspection["identity"]["tokenization"] == "utf8-byte-tokens:1"
    assert inspection["identity"]["configuration"]["selection"]["max_values"] == 2
    assert summary["outcome"] == "limited"
    assert 0 < summary["selected_inputs"] <= 2
    assert 0 < summary["selected_tokens"] <= 64
    assert summary["boundary"]
    persisted = await app.state.vault_registry[vault_id].graph_store.get_document(body["id"])
    assert persisted.processing_coverage.inspection.model_dump(mode="json") == inspection
