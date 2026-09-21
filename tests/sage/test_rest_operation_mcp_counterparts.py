"""MCP counterparts of the default-config, retrieval-health, export and view operations.

CAS-ADR-052 realizes each caller capability beneath both request surfaces.
These tests hold four maintenance tools to that rule against the REST
operations they mirror:

- ``get_default_vault_config`` -- ``GET /sage_vaults/default-config``
- ``verify_vault_retrieval`` -- ``POST /sage_vaults/{vault_id}/eval-retrieval``
- ``export_projection`` -- ``POST /sage_vaults/{vault_id}/documents/{document_id}/export``
- ``recompute_views`` -- ``POST /sage_vaults/{vault_id}/refresh-views``

Each tool has a success case and its principal refusal, and a paired-arm case
drives the tool and the route against one app, whose mounts share the process
vault registry, and requires equivalent results for the same input. Where the
two arms could agree by reimplementing the same logic, a spy on the service
entry point requires that both arms reach it.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any

import pytest
import yaml
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from mcp.server.fastmcp.exceptions import ToolError

from sage import mcp_server
from sage.adapters.stubs import StubContentStore
from sage.config import VaultConfig
from sage.mcp_init import SAGEServices
from sage.models.enums import SourceType
from sage.models.schemas import IngestRequest
from sage.services.utilities import UtilitiesService
from sage.services.vault_registry import VaultRegistryService
from tests.helpers.pipeline_wait import await_pipeline_idle

_MISSING_DOCUMENT_ID = "00000000_no_such_document"
_HIT_ID = "aaaaaaaa_retrieval_hit"
_MISS_ID = "bbbbbbbb_retrieval_miss"


async def _serve(config_dict: dict, tmp_vault_dir: Path) -> AsyncIterator[tuple[FastAPI, str]]:
    config_path = tmp_vault_dir / "vault_config.yaml"
    config_path.write_text(yaml.safe_dump(config_dict, sort_keys=False))
    config = VaultConfig.model_validate(config_dict)

    from sage.app import _initialize_services, create_app

    app = create_app(config=config)
    registry = mcp_server._vaults
    before = set(registry)
    await _initialize_services(
        app,
        config,
        config_path=config_path,
        content_store_factory=lambda _brain: StubContentStore(),
    )
    try:
        yield app, config.vault.id
    finally:
        await asyncio.sleep(0.1)
        for vault_id in set(registry) - before:
            services = registry.pop(vault_id)
            services.close_timing()
            await services.close_storage()


@pytest.fixture
async def yaml_app(
    minimal_vault_config_dict: dict, tmp_vault_dir: Path
) -> AsyncIterator[tuple[FastAPI, str]]:
    """An app serving one vault with no retrieval-health configuration."""
    async for served in _serve(minimal_vault_config_dict, tmp_vault_dir):
        yield served


@pytest.fixture
async def retrieval_app(
    minimal_vault_config_dict: dict, tmp_vault_dir: Path
) -> AsyncIterator[tuple[FastAPI, str]]:
    """An app whose vault declares a two-assertion retrieval-health file."""
    (tmp_vault_dir / "sources" / "retrieval_assertions.yaml").write_text(
        yaml.safe_dump(
            {
                "assertions": [
                    {"query": "found query", "expected_document_id": _HIT_ID, "top_k": 5},
                    {"query": "missed query", "expected_document_id": _MISS_ID, "top_k": 5},
                ]
            }
        )
    )
    config_dict = {
        **minimal_vault_config_dict,
        "retrieval_health": {"assertions_file": "retrieval_assertions.yaml"},
    }
    async for served in _serve(config_dict, tmp_vault_dir):
        yield served


@pytest.fixture
def fixed_ranking(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every retrieval-health query ranks exactly the hit document, so one assertion misses."""

    async def ranked(self: UtilitiesService, query: str, limit: int) -> list[str]:
        return [_HIT_ID]

    monkeypatch.setattr(UtilitiesService, "_eval_ranked_document_ids", ranked)


def _spy(monkeypatch: pytest.MonkeyPatch, name: str) -> list[tuple[Any, ...]]:
    """Count calls reaching ``UtilitiesService.<name>`` while delegating to it."""
    calls: list[tuple[Any, ...]] = []
    original = getattr(UtilitiesService, name)

    async def spy(self: UtilitiesService, *args: Any) -> Any:
        calls.append(args)
        return await original(self, *args)

    monkeypatch.setattr(UtilitiesService, name, spy)
    return calls


def _client(app: FastAPI) -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def _ingest(services: SAGEServices, tmp_vault_dir: Path, name: str) -> str:
    source = tmp_vault_dir / "sources" / "samples" / f"{name}.md"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text(f"# {name}\n\nBody content for {name}.")
    result = await services.ingestion_service.ingest(
        IngestRequest(source=f"samples/{name}.md", source_type=SourceType.MARKDOWN)
    )
    doc_id = result.document.id
    await await_pipeline_idle(services.graph_store, doc_id, service=services.ingestion_service)
    return doc_id


async def _projection_text(app: FastAPI, vault_id: str, doc_id: str) -> str:
    async with _client(app) as client:
        resp = await client.get(f"/sage_vaults/{vault_id}/documents/{doc_id}/projection")
    assert resp.status_code == 200, resp.text
    return resp.json()["projection_text"]


# ---------------------------------------------------------------------------
# get_default_vault_config
# ---------------------------------------------------------------------------


async def test_default_vault_config_tool_serves_the_scaffold(
    yaml_app, tool_payload: Callable[[object], dict]
):
    """DC-1: the scaffold precedes the vault and creates nothing."""
    app, _ = yaml_app
    maint = app.state.mcp_mounts["/mcp_maint"]

    via_tool = tool_payload(
        await maint.call_tool("get_default_vault_config", {"vault_id": "brand_new"})
    )

    assert via_tool == VaultRegistryService.get_default_config("brand_new")
    assert via_tool["vault"]["name"] == ""
    assert via_tool["vault"]["owner"] == ""
    assert via_tool["vault"]["storage_root"].endswith("brand_new/sources")
    assert "brand_new" not in app.state.vault_registry


async def test_default_vault_config_tool_and_route_agree(
    yaml_app, tool_payload: Callable[[object], dict]
):
    """DC-2: both surfaces serve the same scaffold for the same id."""
    app, _ = yaml_app
    maint = app.state.mcp_mounts["/mcp_maint"]

    via_tool = tool_payload(
        await maint.call_tool("get_default_vault_config", {"vault_id": "brand_new"})
    )
    async with _client(app) as client:
        resp = await client.get("/sage_vaults/default-config", params={"vault_id": "brand_new"})

    assert resp.status_code == 200, resp.text
    assert via_tool == resp.json()


async def test_default_vault_config_refuses_a_malformed_id_on_both_surfaces(
    yaml_app, tool_payload: Callable[[object], dict]
):
    """DC-3: a malformed vault id is the same typed refusal on both arms."""
    app, _ = yaml_app
    maint = app.state.mcp_mounts["/mcp_maint"]

    via_tool = tool_payload(
        await maint.call_tool("get_default_vault_config", {"vault_id": "Bad Id!"})
    )
    async with _client(app) as client:
        resp = await client.get("/sage_vaults/default-config", params={"vault_id": "Bad Id!"})

    assert resp.status_code == 400, resp.text
    assert via_tool["error"] == resp.json()["code"] == "invalid_vault_id"


# ---------------------------------------------------------------------------
# verify_vault_retrieval
# ---------------------------------------------------------------------------


def test_retrieval_health_operation_is_named_as_its_tool(yaml_app):
    """VR-1: the live and committed contracts both carry the verify_* name."""
    app, _ = yaml_app
    path = "/sage_vaults/{vault_id}/eval-retrieval"

    live = app.openapi()
    assert live["paths"][path]["post"]["operationId"] == "verify_vault_retrieval"

    committed_path = (
        Path(__file__).resolve().parents[2] / "docs" / "fs" / "sage" / "sage_core_api.openapi.yaml"
    )
    committed = yaml.safe_load(committed_path.read_text())
    assert committed["paths"][path]["post"]["operationId"] == "verify_vault_retrieval"

    for spec in (live, committed):
        operation_ids = {
            op.get("operationId")
            for item in spec["paths"].values()
            for op in item.values()
            if isinstance(op, dict)
        }
        assert "eval_retrieval" not in operation_ids


async def test_verify_vault_retrieval_tool_reports_each_verdict(
    retrieval_app, fixed_ranking, tool_payload: Callable[[object], dict]
):
    """VR-2: one assertion found and one missed yields a failing, itemized report."""
    app, vault_id = retrieval_app
    maint = app.state.mcp_mounts["/mcp_maint"]

    report = tool_payload(await maint.call_tool("verify_vault_retrieval", {"vault_id": vault_id}))

    assert report["vault_id"] == vault_id
    assert report["passed"] is False
    assert report["assertion_count"] == 2
    assert report["failure_count"] == 1
    assert [f["query"] for f in report["failures"]] == ["missed query"]
    assert report["failures"][0]["expected_document_id"] == _MISS_ID


async def test_verify_vault_retrieval_tool_and_route_agree(
    retrieval_app, fixed_ranking, monkeypatch, tool_payload: Callable[[object], dict]
):
    """VR-3: both arms reach the one service entry point and report alike.

    The reports are compared as bodies. A report with a missed assertion
    carries an optional field whose value is null, ``actual_rank``, which both
    surfaces omit under the one per-field null rule.
    """
    app, vault_id = retrieval_app
    calls = _spy(monkeypatch, "eval_retrieval")
    maint = app.state.mcp_mounts["/mcp_maint"]

    via_tool = tool_payload(await maint.call_tool("verify_vault_retrieval", {"vault_id": vault_id}))
    assert len(calls) == 1
    async with _client(app) as client:
        resp = await client.post(f"/sage_vaults/{vault_id}/eval-retrieval")

    assert resp.status_code == 200, resp.text
    assert len(calls) == 2
    via_route = resp.json()
    assert via_route["failure_count"] == 1
    assert via_tool == via_route


async def test_verify_vault_retrieval_refuses_an_unconfigured_vault_on_both_surfaces(
    yaml_app, tool_payload: Callable[[object], dict]
):
    """VR-4: a vault with no assertions file is the same typed refusal on both arms."""
    app, vault_id = yaml_app
    maint = app.state.mcp_mounts["/mcp_maint"]

    via_tool = tool_payload(await maint.call_tool("verify_vault_retrieval", {"vault_id": vault_id}))
    async with _client(app) as client:
        resp = await client.post(f"/sage_vaults/{vault_id}/eval-retrieval")

    assert resp.status_code == 400, resp.text
    assert via_tool["error"] == resp.json()["code"] == "assertions_not_configured"


@pytest.fixture
async def document_store_retrieval_app(
    minimal_vault_config_dict: dict, tmp_vault_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[tuple[FastAPI, str, Any]]:
    """An app on the document-store binding whose vault names an assertions file.

    Nothing is written to the local tree: the file, when a test seeds one,
    lives only in the faked store, as it does under the cloud profile.
    """
    from tests.helpers.vault_source_selection import select_vault_source_binding

    fake = select_vault_source_binding(monkeypatch, "document_store")
    config_dict = {
        **minimal_vault_config_dict,
        "retrieval_health": {"assertions_file": "retrieval_assertions.yaml"},
    }
    async for app, vault_id in _serve(config_dict, tmp_vault_dir):
        yield app, vault_id, fake


async def test_verify_vault_retrieval_reads_a_document_store_file_on_both_surfaces(
    document_store_retrieval_app, fixed_ranking, tool_payload: Callable[[object], dict]
):
    """VR-5: a file held only in the document store yields one verdict on both arms."""
    app, vault_id, fake = document_store_retrieval_app
    fake.sources["retrieval_assertions.yaml"] = yaml.safe_dump(
        {"assertions": [{"query": "found query", "expected_document_id": _HIT_ID}]}
    ).encode()
    maint = app.state.mcp_mounts["/mcp_maint"]

    via_tool = tool_payload(await maint.call_tool("verify_vault_retrieval", {"vault_id": vault_id}))
    async with _client(app) as client:
        resp = await client.post(f"/sage_vaults/{vault_id}/eval-retrieval")

    assert resp.status_code == 200, resp.text
    assert via_tool["passed"] is True
    assert via_tool["assertion_count"] == 1
    assert via_tool == resp.json()


async def test_verify_vault_retrieval_missing_store_file_is_the_same_refusal_on_both_surfaces(
    document_store_retrieval_app, tool_payload: Callable[[object], dict]
):
    """VR-6: an assertions file absent from the store is one typed 404 on both arms."""
    app, vault_id, _ = document_store_retrieval_app
    maint = app.state.mcp_mounts["/mcp_maint"]

    via_tool = tool_payload(await maint.call_tool("verify_vault_retrieval", {"vault_id": vault_id}))
    async with _client(app) as client:
        resp = await client.post(f"/sage_vaults/{vault_id}/eval-retrieval")

    assert resp.status_code == 404, resp.text
    assert via_tool["error"] == resp.json()["code"] == "assertions_file_not_found"


# ---------------------------------------------------------------------------
# export_projection
# ---------------------------------------------------------------------------


async def test_export_projection_tool_writes_under_the_storage_root(
    yaml_app, tmp_vault_dir, tool_payload: Callable[[object], dict]
):
    """EX-1: the projection lands at the storage-root-relative path, parents created."""
    app, vault_id = yaml_app
    doc_id = await _ingest(app.state.vault_registry[vault_id], tmp_vault_dir, "ex_one")
    maint = app.state.mcp_mounts["/mcp_maint"]
    target = tmp_vault_dir / "sources" / "exports" / "a" / "doc.md"
    assert not target.parent.exists()

    via_tool = tool_payload(
        await maint.call_tool(
            "export_projection",
            {"vault_id": vault_id, "document_id": doc_id, "output_path": "exports/a/doc.md"},
        )
    )

    assert via_tool == {"document_id": doc_id, "output_path": str(target)}
    assert target.read_text(encoding="utf-8") == await _projection_text(app, vault_id, doc_id)


async def test_export_projection_tool_and_route_agree(
    yaml_app, tmp_vault_dir, monkeypatch, tool_payload: Callable[[object], dict]
):
    """EX-2: both arms reach the one service entry point and write the same bytes."""
    app, vault_id = yaml_app
    doc_id = await _ingest(app.state.vault_registry[vault_id], tmp_vault_dir, "ex_pair")
    calls = _spy(monkeypatch, "export_projection")
    maint = app.state.mcp_mounts["/mcp_maint"]
    root = tmp_vault_dir / "sources"

    via_tool = tool_payload(
        await maint.call_tool(
            "export_projection",
            {"vault_id": vault_id, "document_id": doc_id, "output_path": "exports/tool.md"},
        )
    )
    assert calls == [(doc_id, "exports/tool.md")]
    async with _client(app) as client:
        resp = await client.post(
            f"/sage_vaults/{vault_id}/documents/{doc_id}/export",
            json={"output_path": "exports/route.md"},
        )

    assert resp.status_code == 200, resp.text
    assert calls == [(doc_id, "exports/tool.md"), (doc_id, "exports/route.md")]
    via_route = resp.json()
    assert via_tool == {**via_route, "output_path": str(root / "exports" / "tool.md")}
    assert (root / "exports" / "tool.md").read_bytes() == (
        root / "exports" / "route.md"
    ).read_bytes()


async def test_export_projection_tool_overwrites_an_existing_file(
    yaml_app, tmp_vault_dir, tool_payload: Callable[[object], dict]
):
    """EX-3: an occupied target is replaced, as the REST operation replaces it."""
    app, vault_id = yaml_app
    doc_id = await _ingest(app.state.vault_registry[vault_id], tmp_vault_dir, "ex_over")
    target = tmp_vault_dir / "sources" / "exports" / "x.md"
    target.parent.mkdir(parents=True)
    target.write_text("stale", encoding="utf-8")
    maint = app.state.mcp_mounts["/mcp_maint"]
    projection_text = await _projection_text(app, vault_id, doc_id)
    assert target.read_text(encoding="utf-8") == "stale" != projection_text

    via_tool = tool_payload(
        await maint.call_tool(
            "export_projection",
            {"vault_id": vault_id, "document_id": doc_id, "output_path": "exports/x.md"},
        )
    )

    assert via_tool == {"document_id": doc_id, "output_path": str(target)}
    assert target.read_text(encoding="utf-8") == projection_text


@pytest.mark.parametrize("document", ["real", "missing"])
async def test_export_projection_refuses_an_escaping_path_on_both_surfaces(
    yaml_app, tmp_vault_dir, document: str, tool_payload: Callable[[object], dict]
):
    """EX-4: a path resolving outside the storage root is refused alike, nothing written.

    The argument is settled before the read, so a missing document still
    answers ``path_traversal_denied`` rather than ``document_not_found``.
    """
    app, vault_id = yaml_app
    doc_id = (
        await _ingest(app.state.vault_registry[vault_id], tmp_vault_dir, "ex_escape")
        if document == "real"
        else _MISSING_DOCUMENT_ID
    )
    maint = app.state.mcp_mounts["/mcp_maint"]
    escaped = tmp_vault_dir / "escape.md"

    via_tool = tool_payload(
        await maint.call_tool(
            "export_projection",
            {"vault_id": vault_id, "document_id": doc_id, "output_path": "../escape.md"},
        )
    )
    async with _client(app) as client:
        resp = await client.post(
            f"/sage_vaults/{vault_id}/documents/{doc_id}/export",
            json={"output_path": "../escape.md"},
        )

    assert resp.status_code == 400, resp.text
    via_route = resp.json()
    assert via_tool["error"] == via_route["code"] == "path_traversal_denied"
    assert via_tool["detail"] == via_route["detail"]
    assert not escaped.exists()


@pytest.mark.parametrize("document", ["real", "missing"])
async def test_export_projection_refused_under_the_cloud_profile_before_any_read(
    yaml_app, tmp_vault_dir, monkeypatch, document: str, tool_payload: Callable[[object], dict]
):
    """EX-5: without a caller-visible filesystem both arms refuse, before the read.

    The missing-document arm separates a refusal made before the read from
    one made after it, which would answer ``document_not_found`` instead.
    """
    app, vault_id = yaml_app
    doc_id = (
        await _ingest(app.state.vault_registry[vault_id], tmp_vault_dir, "ex_cloud")
        if document == "real"
        else _MISSING_DOCUMENT_ID
    )
    monkeypatch.setattr("sage.mcp_init.caller_local_filesystem_reachable", lambda: False)
    maint = app.state.mcp_mounts["/mcp_maint"]
    exports = tmp_vault_dir / "sources" / "exports"

    via_tool = tool_payload(
        await maint.call_tool(
            "export_projection",
            {"vault_id": vault_id, "document_id": doc_id, "output_path": "exports/cloud.md"},
        )
    )
    async with _client(app) as client:
        resp = await client.post(
            f"/sage_vaults/{vault_id}/documents/{doc_id}/export",
            json={"output_path": "exports/cloud.md"},
        )

    assert resp.status_code == 501, resp.text
    via_route = resp.json()
    assert via_tool["error"] == via_route["code"] == "caller_filesystem_unavailable"
    assert via_tool["detail"] == via_route["detail"]
    assert via_route["detail"]["operation"] == "export_projection"
    assert not exports.exists()


async def test_export_projection_lives_on_the_maintenance_surface_only(yaml_app):
    """EX-6: export is a maintenance tool, and the ordinary read stays read-only."""
    app, _ = yaml_app
    ordinary = app.state.mcp_mounts["/mcp"]

    with pytest.raises(ToolError, match=r"'export_projection' is registered on the 'sage_maint'"):
        await ordinary.call_tool("export_projection", {})

    tools = {tool.name: tool for tool in await ordinary.list_tools()}
    assert "output_path" not in tools["read_projection"].inputSchema["properties"]


# ---------------------------------------------------------------------------
# recompute_views
# ---------------------------------------------------------------------------


async def test_recompute_views_answers_alike_on_both_surfaces_under_the_local_profile(
    yaml_app, tmp_vault_dir, tool_payload: Callable[[object], dict]
):
    """RV-1: with a caller-visible filesystem both arms regenerate the views."""
    app, vault_id = yaml_app
    await _ingest(app.state.vault_registry[vault_id], tmp_vault_dir, "rv_local")
    maint = app.state.mcp_mounts["/mcp_maint"]
    views = tmp_vault_dir / "sources" / "views"

    via_tool = tool_payload(await maint.call_tool("recompute_views", {"vault_id": vault_id}))
    async with _client(app) as client:
        resp = await client.post(f"/sage_vaults/{vault_id}/refresh-views")

    assert resp.status_code == 200, resp.text
    assert via_tool == resp.json() == {"vault_id": vault_id, "views_generated": 2}
    assert (views / "by_lifecycle" / "active" / "rv_local.md").is_symlink()


async def test_recompute_views_refused_under_the_cloud_profile_before_the_wipe(
    yaml_app, tmp_vault_dir, monkeypatch, tool_payload: Callable[[object], dict]
):
    """RV-2: without a caller-visible filesystem both arms refuse, before the wipe.

    A sentinel under an existing ``views/`` separates a refusal made before the
    wipe from one made after it, which would still answer 501 but leave the
    views gone; the absent ``by_lifecycle/`` rules out a refusal after the
    rebuild.
    """
    import jsonschema

    from sage.models.error_contract import tool_error_schema

    app, vault_id = yaml_app
    await _ingest(app.state.vault_registry[vault_id], tmp_vault_dir, "rv_cloud")
    views = tmp_vault_dir / "sources" / "views"
    sentinel = views / "by_doc_type" / "sentinel" / "keep.txt"
    sentinel.parent.mkdir(parents=True)
    sentinel.write_text("kept", encoding="utf-8")
    monkeypatch.setattr("sage.mcp_init.caller_local_filesystem_reachable", lambda: False)
    maint = app.state.mcp_mounts["/mcp_maint"]

    via_tool = tool_payload(await maint.call_tool("recompute_views", {"vault_id": vault_id}))
    async with _client(app) as client:
        resp = await client.post(f"/sage_vaults/{vault_id}/refresh-views")

    assert resp.status_code == 501, resp.text
    via_route = resp.json()
    assert via_tool["error"] == via_route["code"] == "caller_filesystem_unavailable"
    assert via_tool["detail"] == via_route["detail"]
    assert via_route["detail"]["operation"] == "recompute_views"
    jsonschema.validate(via_tool, tool_error_schema("", "recompute_views"))
    assert sentinel.read_text(encoding="utf-8") == "kept"
    assert not (views / "by_lifecycle").exists()
