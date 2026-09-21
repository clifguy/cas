"""REST counterparts of the pipeline-repair, vault-reload and stack-config tools.

CAS-ADR-052 realizes each caller capability beneath both request surfaces.
These tests hold three REST operations to that rule:

- ``POST /sage_vaults/{vault_id}/documents/{document_id}/recompute-pipeline``
- ``POST /sage_vaults/{vault_id}/maintenance/reload``
- ``GET /sage_vaults/maintenance/stack-config``

Each operation has a success case and its principal refusal, and a paired-arm
case drives the MCP tool and the REST route against one app, whose mounts share
the process vault registry, and requires equivalent results for the same input.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Callable
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest
import yaml
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

import sage.mcp_init as sage_mcp_init
from sage import mcp_server
from sage.adapters.stubs import StubContentStore
from sage.api.errors import SAGEError
from sage.config import SageCoreConfig, VaultConfig
from sage.mcp_init import SAGEServices
from sage.models.enums import SourceType
from sage.models.schemas import IngestRequest
from tests.helpers.pipeline_wait import await_pipeline_idle


@pytest.fixture
async def yaml_app(
    minimal_vault_config_dict: dict, tmp_vault_dir: Path
) -> AsyncIterator[tuple[FastAPI, str, Path]]:
    """An app whose vault was loaded from a ``vault_config.yaml`` on disk.

    The config path is threaded into initialization, as the production
    lifespan does, so a reload re-reads the declaration rather than reusing
    the in-memory config. Teardown closes whatever bundle occupies each slot
    this fixture added, including a post-reload replacement.
    """
    config_path = tmp_vault_dir / "vault_config.yaml"
    config_path.write_text(yaml.safe_dump(minimal_vault_config_dict, sort_keys=False))
    config = VaultConfig.model_validate(minimal_vault_config_dict)

    from sage.app import _initialize_services, create_app

    app = create_app(config=config)
    registry = mcp_server._vaults
    before = set(registry)
    await _initialize_services(
        app,
        config,
        config_path=config_path,
        from_declaration=True,
        content_store_factory=lambda _brain: StubContentStore(),
    )
    try:
        yield app, config.vault.id, config_path
    finally:
        await asyncio.sleep(0.1)
        for vault_id in set(registry) - before:
            services = registry.pop(vault_id)
            services.close_timing()
            await services.close_storage()


def _client(app: FastAPI) -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


def _rename_on_disk(config_path: Path, name: str) -> None:
    """Edit the vault's display name in its YAML, as a change SAGE did not make."""
    raw = yaml.safe_load(config_path.read_text())
    raw["vault"]["name"] = name
    config_path.write_text(yaml.safe_dump(raw, sort_keys=False))


async def _ingest(services: SAGEServices, tmp_vault_dir: Path, name: str) -> str:
    """Ingest one real markdown source and wait for its pipeline to settle."""
    source = tmp_vault_dir / "sources" / "samples" / f"{name}.md"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text(f"# {name}\n\nBody content for {name}.")
    result = await services.ingestion_service.ingest(
        IngestRequest(source=f"samples/{name}.md", source_type=SourceType.MARKDOWN)
    )
    doc_id = result.document.id
    await await_pipeline_idle(services.graph_store, doc_id, service=services.ingestion_service)
    return doc_id


async def _await_idle(services: SAGEServices, doc_id: str) -> Any:
    return await await_pipeline_idle(
        services.graph_store, doc_id, service=services.ingestion_service
    )


# ---------------------------------------------------------------------------
# recompute_pipeline
# ---------------------------------------------------------------------------


async def test_recompute_pipeline_route_dispatches_the_repair(yaml_app, tmp_vault_dir):
    """RP-1: the route runs the pipeline repair rather than acknowledging it.

    The document's chunks are removed before the call, so a route that returns
    the envelope without re-running projection and indexing leaves the store
    empty; one bound to the abstract-only path answers ``reabstract_started``.
    """
    app, vault_id, _ = yaml_app
    services: SAGEServices = app.state.vault_registry[vault_id]
    doc_id = await _ingest(services, tmp_vault_dir, "rp_success")
    await services.content_store.remove_document(doc_id)
    assert await services.content_store.get_all_chunks(doc_id) == []

    async with _client(app) as client:
        resp = await client.post(f"/sage_vaults/{vault_id}/documents/{doc_id}/recompute-pipeline")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert set(body) == {"status", "document_id", "dispatched_at"}
    assert body["status"] == "recompute_pipeline_started"
    assert body["document_id"] == doc_id
    datetime.fromisoformat(body["dispatched_at"])

    await _await_idle(services, doc_id)
    assert await services.content_store.get_all_chunks(doc_id), (
        "the repair must re-index the document's chunks"
    )


async def test_recompute_pipeline_route_refuses_an_unknown_document(yaml_app):
    """RP-2: a well-formed id naming no document is refused as not found."""
    app, vault_id, _ = yaml_app
    async with _client(app) as client:
        resp = await client.post(
            f"/sage_vaults/{vault_id}/documents/deadbeef_ghost/recompute-pipeline"
        )

    assert resp.status_code == 404, resp.text
    assert resp.json()["code"] == "document_not_found"


async def test_recompute_pipeline_route_refuses_work_already_in_flight(yaml_app, tmp_vault_dir):
    """RP-3: a held claim on the document is refused rather than duplicated.

    A route that bypassed the service's single-flight claim would dispatch a
    second task and answer 200.
    """
    app, vault_id, _ = yaml_app
    services: SAGEServices = app.state.vault_registry[vault_id]
    doc_id = await _ingest(services, tmp_vault_dir, "rp_inflight")

    assert services.ingestion_service._try_claim(doc_id, "recompute") is None  # noqa: SLF001
    try:
        async with _client(app) as client:
            resp = await client.post(
                f"/sage_vaults/{vault_id}/documents/{doc_id}/recompute-pipeline"
            )
    finally:
        services.ingestion_service._release_claim(doc_id)  # noqa: SLF001

    assert resp.status_code == 409, resp.text
    body = resp.json()
    assert body["code"] == "recompute_pipeline_already_in_flight"
    assert body["detail"]["document_id"] == doc_id


async def test_recompute_pipeline_tool_and_route_agree(
    yaml_app, tmp_vault_dir, tool_payload: Callable[[object], dict]
):
    """RP-4: the tool and the route answer and repair alike for one document."""
    app, vault_id, _ = yaml_app
    services: SAGEServices = app.state.vault_registry[vault_id]
    doc_id = await _ingest(services, tmp_vault_dir, "rp_paired")
    mcp = app.state.mcp_mounts["/mcp"]

    await services.content_store.remove_document(doc_id)
    via_tool = tool_payload(
        await mcp.call_tool("recompute_pipeline", {"vault_id": vault_id, "document_id": doc_id})
    )
    tool_doc = await _await_idle(services, doc_id)
    tool_chunks = len(await services.content_store.get_all_chunks(doc_id))

    await services.content_store.remove_document(doc_id)
    async with _client(app) as client:
        resp = await client.post(f"/sage_vaults/{vault_id}/documents/{doc_id}/recompute-pipeline")
    assert resp.status_code == 200, resp.text
    via_route = resp.json()
    route_doc = await _await_idle(services, doc_id)
    route_chunks = len(await services.content_store.get_all_chunks(doc_id))

    assert set(via_tool) == set(via_route)
    assert via_tool["status"] == via_route["status"] == "recompute_pipeline_started"
    assert via_tool["document_id"] == via_route["document_id"] == doc_id
    datetime.fromisoformat(via_tool["dispatched_at"])
    datetime.fromisoformat(via_route["dispatched_at"])
    # Both spellings parse, so parsing alone would pass an arm that serialized
    # the service's raw ISO string instead of the response model.
    assert via_tool["dispatched_at"].endswith("Z")
    assert via_route["dispatched_at"].endswith("Z")
    assert tool_doc.pipeline_status == route_doc.pipeline_status
    assert tool_chunks == route_chunks > 0


# ---------------------------------------------------------------------------
# reload_vault
# ---------------------------------------------------------------------------


async def test_reload_vault_route_rereads_the_declaration(yaml_app, tmp_vault_dir):
    """RV-1: a reload picks up an on-disk config edit and reports the count.

    A route reloading from the in-memory config reports ``reloaded`` but keeps
    the old name; the seeded document makes a hard-coded zero count visible.
    """
    app, vault_id, config_path = yaml_app
    services: SAGEServices = app.state.vault_registry[vault_id]
    await _ingest(services, tmp_vault_dir, "rv_success")
    _rename_on_disk(config_path, "Renamed Outside SAGE")

    async with _client(app) as client:
        resp = await client.post(f"/sage_vaults/{vault_id}/maintenance/reload")
        config_resp = await client.get(f"/sage_vaults/{vault_id}/config")

    assert resp.status_code == 200, resp.text
    assert resp.json() == {"vault_id": vault_id, "reloaded": True, "document_count": 1}
    assert config_resp.json()["vault"]["name"] == "Renamed Outside SAGE"
    assert app.state.vault_registry[vault_id] is not services


async def test_reload_vault_route_refuses_an_unknown_vault(yaml_app):
    """RV-2: a vault id naming no registered vault is refused as not found."""
    app, _, _ = yaml_app
    async with _client(app) as client:
        resp = await client.post("/sage_vaults/ghost_vault/maintenance/reload")

    assert resp.status_code == 404, resp.text
    assert resp.json()["code"] == "vault_not_found"


async def test_reload_vault_route_failure_keeps_the_serving_services(yaml_app, monkeypatch):
    """RV-3: a failed rebuild is reported and leaves the old services serving.

    Identity alone would pass a restore of closed services; the read through
    the old graph store is what shows they were never torn down.
    """
    app, vault_id, _ = yaml_app
    old = app.state.vault_registry[vault_id]

    async def failing_initialize_services(*args: Any, **kwargs: Any) -> SAGEServices:
        raise SAGEError(
            code="simulated_rebuild_failure",
            message="simulated rebuild failure",
            status_code=409,
        )

    monkeypatch.setattr(sage_mcp_init, "initialize_services", failing_initialize_services)

    async with _client(app) as client:
        resp = await client.post(f"/sage_vaults/{vault_id}/maintenance/reload")

    assert resp.status_code == 409, resp.text
    assert resp.json()["code"] == "simulated_rebuild_failure"
    assert app.state.vault_registry[vault_id] is old
    assert isinstance(await old.graph_store.list_all_documents(), list)


async def test_reload_vault_tool_and_route_agree(
    yaml_app, tmp_vault_dir, tool_payload: Callable[[object], dict]
):
    """RV-4: each surface reloads the edit made before it and reports alike."""
    app, vault_id, config_path = yaml_app
    services: SAGEServices = app.state.vault_registry[vault_id]
    await _ingest(services, tmp_vault_dir, "rv_paired")
    maint = app.state.mcp_mounts["/mcp_maint"]

    _rename_on_disk(config_path, "Edit Before Tool")
    via_tool = tool_payload(await maint.call_tool("reload_vault", {"vault_id": vault_id}))
    name_after_tool = app.state.vault_registry[vault_id].config.vault.name

    _rename_on_disk(config_path, "Edit Before Route")
    async with _client(app) as client:
        resp = await client.post(f"/sage_vaults/{vault_id}/maintenance/reload")
    assert resp.status_code == 200, resp.text
    via_route = resp.json()
    name_after_route = app.state.vault_registry[vault_id].config.vault.name

    assert via_tool == via_route == {"vault_id": vault_id, "reloaded": True, "document_count": 1}
    assert name_after_tool == "Edit Before Tool"
    assert name_after_route == "Edit Before Route"


async def test_reload_vault_refuses_a_malformed_declaration_on_both_surfaces(
    yaml_app, tool_payload: Callable[[object], dict]
):
    """RV-6: an unusable declaration is the same typed refusal on both arms.

    The edit that breaks the declaration is the edit this operation exists to
    pick up, so an untranslated failure surfaces where callers meet it: an
    opaque 500 on REST and an envelope naming the wrong subject on MCP. Both
    arms also leave the vault serving, which is what the refusal claims.
    """
    app, vault_id, config_path = yaml_app
    services: SAGEServices = app.state.vault_registry[vault_id]

    raw = yaml.safe_load(config_path.read_text())
    raw["vault"]["id"] = "Not A Valid Id!!"
    config_path.write_text(yaml.safe_dump(raw, sort_keys=False))

    maint = app.state.mcp_mounts["/mcp_maint"]
    via_tool = tool_payload(await maint.call_tool("reload_vault", {"vault_id": vault_id}))

    async with _client(app) as client:
        resp = await client.post(f"/sage_vaults/{vault_id}/maintenance/reload")

    assert resp.status_code == 400, resp.text
    via_route = resp.json()
    assert via_tool["error"] == via_route["code"] == "vault_config_validation_error"
    assert via_tool["detail"]["errors"] == via_route["detail"]["errors"]
    assert any("vault.id" in message for message in via_route["detail"]["errors"])
    # The vault kept serving from the services it already had, on both arms.
    assert app.state.vault_registry[vault_id] is services
    assert isinstance(await services.graph_store.list_all_documents(), list)


async def test_reload_vault_route_reports_success_when_the_count_fails(yaml_app, monkeypatch):
    """RV-5: an unreadable count after a completed reload degrades to null.

    Reporting an error would send the caller to retry a reload that already
    happened; null says the count is unknown, where zero would say empty.
    """
    app, vault_id, _ = yaml_app

    class _FailingCountGraphStore:
        async def get_total_document_count(self) -> int:
            raise RuntimeError("simulated driver failure on the count read")

    class _FakeServices:
        graph_store = _FailingCountGraphStore()

    async def fake_reload(*args: Any, **kwargs: Any) -> _FakeServices:
        return _FakeServices()

    monkeypatch.setattr(sage_mcp_init, "reload_vault_in_registry", fake_reload)

    async with _client(app) as client:
        resp = await client.post(f"/sage_vaults/{vault_id}/maintenance/reload")

    assert resp.status_code == 200, resp.text
    assert resp.json() == {"vault_id": vault_id, "reloaded": True, "document_count": None}


# ---------------------------------------------------------------------------
# get_stack_config
# ---------------------------------------------------------------------------

_STACK_CONFIG_PATH = "/sage_vaults/maintenance/stack-config"


@pytest.fixture
def probe_stack_config(monkeypatch) -> SageCoreConfig:
    """A stack config no default could reproduce, installed for the test.

    Derived from the loaded config so the storage binding the harness set up
    still resolves. Several sections keep null fields, so a response that
    dropped null keys fails the whole-body comparison.
    """
    base = sage_mcp_init.get_stack_config()
    cfg = base.model_copy(
        update={
            "abstraction": base.abstraction.model_copy(
                update={"provider": "stub", "model": "probe-model-7", "context_window": 4096}
            ),
            "transfer": base.transfer.model_copy(
                update={
                    "public_base_url": "https://transfer.probe.invalid",
                    "token_ttl_seconds": 321,
                }
            ),
        }
    )
    assert cfg.model_dump(mode="json") != SageCoreConfig().model_dump(mode="json")
    monkeypatch.setattr(sage_mcp_init, "_stack_config", cfg)
    return cfg


async def test_stack_config_route_returns_the_loaded_config(yaml_app, probe_stack_config):
    """SC-1: the route answers with the process's loaded stack config, whole."""
    app, _, _ = yaml_app
    async with _client(app) as client:
        resp = await client.get(_STACK_CONFIG_PATH)

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body == probe_stack_config.model_dump(mode="json")
    assert body["document_store"]["site_id"] is None, "null fields keep their keys"


async def test_stack_config_route_refuses_an_undeclared_parameter(yaml_app, probe_stack_config):
    """SC-2: the operation takes no parameters, so a query name is refused."""
    app, _, _ = yaml_app
    async with _client(app) as client:
        resp = await client.get(_STACK_CONFIG_PATH, params={"bogus_query_x": "1"})

    assert resp.status_code == 400, resp.text
    body = resp.json()
    assert body["code"] == "unknown_parameter"
    assert body["detail"]["tool"] == "get_stack_config"


async def test_stack_config_tool_and_route_agree(
    yaml_app, probe_stack_config, tool_payload: Callable[[object], dict]
):
    """SC-3: both surfaces return the same JSON for the same loaded config."""
    app, _, _ = yaml_app
    maint = app.state.mcp_mounts["/mcp_maint"]
    via_tool = tool_payload(await maint.call_tool("get_stack_config", {}))

    async with _client(app) as client:
        resp = await client.get(_STACK_CONFIG_PATH)
    assert resp.status_code == 200, resp.text

    assert json.dumps(via_tool, sort_keys=True) == json.dumps(resp.json(), sort_keys=True)
    assert via_tool["abstraction"]["model"] == "probe-model-7"


async def test_stack_config_path_coexists_with_a_vault_named_maintenance(
    minimal_vault_config_dict, tmp_path, probe_stack_config
):
    """SC-4: the literal path and a vault whose id is ``maintenance`` both resolve.

    The stack read sits one segment below the vault collection, where a vault
    id would sit. A vault so named must keep its own operations, and must not
    capture the stack read.
    """
    from sage.app import _initialize_services, create_app

    raw = json.loads(json.dumps(minimal_vault_config_dict))
    raw["vault"]["id"] = "maintenance"
    for root in ("sources", "brain"):
        (tmp_path / "maint_vault" / root).mkdir(parents=True)
    raw["vault"]["storage_root"] = str(tmp_path / "maint_vault" / "sources")
    raw["vault"]["brain_root"] = str(tmp_path / "maint_vault" / "brain")
    config = VaultConfig.model_validate(raw)

    app = create_app(config=config)
    await _initialize_services(app, config, content_store_factory=lambda _brain: StubContentStore())
    try:
        async with _client(app) as client:
            stack = await client.get(_STACK_CONFIG_PATH)
            vault_config = await client.get("/sage_vaults/maintenance/config")
    finally:
        services = mcp_server._vaults.pop("maintenance")
        services.close_timing()
        await services.close_storage()

    assert stack.status_code == 200, stack.text
    assert stack.json() == probe_stack_config.model_dump(mode="json")
    assert vault_config.status_code == 200, vault_config.text
    assert vault_config.json()["vault"]["id"] == "maintenance"
