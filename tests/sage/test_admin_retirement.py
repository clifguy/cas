"""Independent negative controls for the end of the SAGE Admin transition."""

from __future__ import annotations

from contextlib import AsyncExitStack

import pytest
from fastapi.routing import APIRoute
from httpx import ASGITransport, AsyncClient
from mcp.server.fastmcp.exceptions import ToolError

from sage.app import create_app
from sage.config import SageCoreConfig, StackAuthConfig
from tests.sage.mcp_surface_pin import EXPECTED_SURFACE

# Explicit historical cohort: enumerating the production alias map would lose
# these cases precisely when the aliases are removed.
_ADMIN_STEMS = (
    "list_vaults",
    "get_vault_config",
    "get_vault_stats",
    "get_stack_config",
    "create_vault",
    "reload_vault",
    "update_vault_config",
    "verify_vault_drift",
    "verify_vault_source_files",
    "migrate_vault",
    "recompute_views",
    "recompute_deferred_vault_abstracts",
    "optimize_vault_content_store",
)
_REST_SUFFIXES = (
    "migrate",
    "detect-drift",
    "reabstract-deferred",
    "optimize-content-store",
    "verify-source-files",
    "restore-source-file",
)


def test_only_canonical_mcp_mounts_exist() -> None:
    app = create_app(stack_config=SageCoreConfig())
    assert set(app.state.mcp_mounts) == {"/mcp", "/mcp_maint"}
    for path, surface in (("/mcp", "sage"), ("/mcp_maint", "sage_maint")):
        server = app.state.mcp_mounts[path]
        assert {t.name for t in server._tool_manager.list_tools()} == {
            name for name, assigned in EXPECTED_SURFACE.items() if assigned == surface
        }


async def test_retired_mcp_mount_returns_404() -> None:
    app = create_app(stack_config=SageCoreConfig())
    request = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "retirement-test", "version": "1"},
        },
    }
    # Start the actual session managers, so a resurrected mount produces a
    # successful response rather than failing only for an uninitialized fixture.
    async with AsyncExitStack() as stack:
        for server in app.state.mcp_mounts.values():
            await stack.enter_async_context(server.session_manager.run())
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
            headers={"Accept": "application/json, text/event-stream"},
        ) as client:
            response = await client.post("/mcp_admin", json=request)
            assert response.status_code == 404
            assert "location" not in response.headers
            for path, surface in (("/mcp", "sage"), ("/mcp_maint", "sage_maint")):
                control = await client.post(path, json=request)
                assert control.status_code == 200, control.text
                assert control.json()["result"]["serverInfo"]["name"] == surface


@pytest.mark.parametrize("stem", _ADMIN_STEMS)
async def test_admin_tool_names_fail_real_dispatch(stem: str, tool_payload) -> None:
    app = create_app(stack_config=SageCoreConfig())
    for server in app.state.mcp_mounts.values():
        with pytest.raises(ToolError, match="Unknown tool") as error:
            await server.call_tool(f"admin_{stem}", {})
        assert f"admin_{stem}" in str(error.value)
        assert "registered on" not in str(error.value)
    # Real SDK dispatch positive control, including the retained alias rewrite:
    # list_vaults needs no initialized vault and must return success.
    ordinary = app.state.mcp_mounts["/mcp"]
    via_alias = await ordinary.call_tool("maint_list_vaults", {})
    assert via_alias == await ordinary.call_tool("list_vaults", {})
    payload = tool_payload(via_alias)
    assert "error" not in payload
    assert isinstance(payload["vaults"], list)


@pytest.mark.parametrize("suffix", _REST_SUFFIXES)
async def test_rest_maintenance_path_replaces_admin_without_alias(suffix: str) -> None:
    app = create_app(stack_config=SageCoreConfig())
    routes = {
        route.path
        for route in app.routes
        if isinstance(route, APIRoute) and "POST" in route.methods
    }
    assert f"/sage_vaults/{{vault_id}}/maintenance/{suffix}" in routes
    assert f"/sage_vaults/{{vault_id}}/admin/{suffix}" not in routes
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(f"/sage_vaults/ghost/admin/{suffix}", json={})
    assert response.status_code == 404
    # Routing absence, not a false pass from a still-live route's vault lookup.
    assert response.json() == {"detail": "Not Found"}
    assert "location" not in response.headers


@pytest.mark.parametrize(
    "auth, expected",
    [
        (None, False),
        (StackAuthConfig(enabled=False), False),
        (StackAuthConfig(enabled=True, tenant_id="tid", audience="api://sage"), True),
    ],
)
def test_app_exposes_resolved_auth_posture(auth: StackAuthConfig | None, expected: bool) -> None:
    app = create_app(stack_config=SageCoreConfig(auth=auth))
    assert app.state.auth_enabled is expected
