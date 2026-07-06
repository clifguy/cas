"""Partition conformance for the two-surface SAGE MCP tool roster.

Gates the CAS-ADR-034 / CAS-ADR-029 split of the SAGE MCP tool surface
across the pair of Streamable HTTP mounts on the SAGE app — ``/mcp``
(ordinary) and ``/mcp_admin`` (maintenance), both built by the same
partition factory in the one uvicorn process:

- ``sage`` — ordinary surface (read spine + everyday mutation spine +
  multi-record operations).
- ``sage_admin`` — maintenance surface (every ``admin_*`` tool); opt-in,
  additive, and does **not** duplicate the read spine.

Surface assignment is derived purely from each tool name's first segment
(``admin_`` -> ``sage_admin``; everything else -> ``sage``). These tests
cross-check the built partitions against ``SERVER_ASSIGNMENT`` in
``sage/_tool_naming.py`` — the in-code transcription of the
*SAGE MCP Tool Surface* steering-document registration map. That oracle is
hand-maintained from the steering doc rather than re-derived from the
prefix rule at runtime, so the comparison is a genuine cross-check, not a
tautology against the production logic.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.routing import APIRoute
from starlette.routing import Mount, Route

import sage
import sage.mcp_server as mcp_server
from sage._tool_naming import SERVER_ASSIGNMENT
from sage.adapters.stubs import (
    StubAbstractionProvider,
    StubContentStore,
    StubEmbeddingProvider,
)
from sage.app import _initialize_services, create_app

EXPECTED_SAGE = {name for name, srv in SERVER_ASSIGNMENT.items() if srv == "sage"}
EXPECTED_ADMIN = {name for name, srv in SERVER_ASSIGNMENT.items() if srv == "sage_admin"}

# The shared read spine (CAS-ADR-034): these live on the ``sage`` server
# only and must never be duplicated on the ``sage_admin`` server.
READ_SPINE = {
    "search",
    "get_document",
    "read_section",
    "read_projection",
    "list_headings",
    "traverse",
    "chain",
}


def _registered_names(surface: str) -> set[str]:
    """Tool names registered on a freshly built partitioned server."""
    server = mcp_server.build_partitioned_server(surface)
    return {tool.name for tool in server._tool_manager.list_tools()}  # noqa: SLF001


def _mounted_names(app: FastAPI, path: str) -> set[str]:
    """Tool names advertised by the partitioned MCP server mounted at ``path``."""
    server = app.state.mcp_mounts[path]
    return {tool.name for tool in server._tool_manager.list_tools()}  # noqa: SLF001


def test_sage_server_registers_exactly_ordinary_tools():
    """The ``sage`` server registers exactly the ordinary-surface roster."""
    assert _registered_names("sage") == EXPECTED_SAGE


def test_sage_admin_server_registers_exactly_maintenance_tools():
    """The ``sage_admin`` server registers exactly the maintenance roster."""
    assert _registered_names("sage_admin") == EXPECTED_ADMIN


def test_sage_admin_contains_only_admin_prefixed_tools():
    """Partition invariant: every ``sage_admin`` tool name is ``admin_*``."""
    names = _registered_names("sage_admin")
    offenders = {n for n in names if not n.startswith("admin_")}
    assert not offenders, f"non-admin_ tool(s) on sage_admin: {sorted(offenders)}"


def test_no_admin_prefixed_tool_on_sage_server():
    """Partition invariant: no ``admin_*`` tool is registered on ``sage``."""
    names = _registered_names("sage")
    leaked = {n for n in names if n.startswith("admin_")}
    assert not leaked, f"admin_ tool(s) leaked onto sage: {sorted(leaked)}"


def test_read_spine_not_duplicated_on_sage_admin():
    """The shared read spine is not duplicated on the maintenance server."""
    names = _registered_names("sage_admin")
    dup = names & READ_SPINE
    assert not dup, f"read-spine tool(s) duplicated on sage_admin: {sorted(dup)}"


def test_partition_is_disjoint_and_exhaustive():
    """The two partitions are disjoint and together cover the full roster."""
    sage = _registered_names("sage")
    admin = _registered_names("sage_admin")
    assert sage.isdisjoint(admin), f"tool(s) on both servers: {sorted(sage & admin)}"
    assert sage | admin == set(SERVER_ASSIGNMENT), (
        "partition union does not equal the full roster: "
        f"missing {sorted(set(SERVER_ASSIGNMENT) - (sage | admin))}, "
        f"extra {sorted((sage | admin) - set(SERVER_ASSIGNMENT))}"
    )


def test_mcp_mount_advertises_ordinary_surface_only(minimal_config):
    """The ``/mcp`` HTTP mount advertises exactly the ordinary roster.

    Revises the prior full-surface assertion: per CAS-ADR-034 the HTTP
    transport is partitioned, so ``/mcp`` carries the ``sage`` surface only
    and no ``admin_*`` tool appears there.
    """
    app = create_app(config=minimal_config)
    names = _mounted_names(app, "/mcp")
    assert names == EXPECTED_SAGE
    assert names, "ordinary mount roster must be non-empty"
    leaked = {n for n in names if n.startswith("admin_")}
    assert not leaked, f"admin_ tool(s) advertised on /mcp: {sorted(leaked)}"


def test_mcp_admin_mount_advertises_maintenance_surface_only(minimal_config):
    """The ``/mcp_admin`` HTTP mount advertises exactly the maintenance roster."""
    app = create_app(config=minimal_config)
    names = _mounted_names(app, "/mcp_admin")
    assert names == EXPECTED_ADMIN
    offenders = {n for n in names if not n.startswith("admin_")}
    assert not offenders, f"non-admin_ tool(s) on /mcp_admin: {sorted(offenders)}"
    dup = names & READ_SPINE
    assert not dup, f"read-spine tool(s) duplicated on /mcp_admin: {sorted(dup)}"


def test_both_mcp_mounts_are_exact_path_routes(minimal_config):
    """One uvicorn process/app serves both partitioned mounts as exact-path
    raw Starlette routes (CAS-ADR-034 v7).

    A ``Mount`` at these paths is the structural form of the trailing-slash
    307 regression: its path regex requires ``/mcp/...``, so an exact
    ``POST /mcp`` — the byte-exact resource URI the edge advertises — falls
    through to the parent router's redirect. The transport must hang off an
    exact-path ``Route`` (raw ASGI, not an ``APIRoute``) instead.
    """
    app = create_app(config=minimal_config)
    for mount, _surface in (("/mcp", "sage"), ("/mcp_admin", "sage_admin")):
        matches = [
            route
            for route in app.routes
            if isinstance(route, Route) and not isinstance(route, APIRoute) and route.path == mount
        ]
        assert len(matches) == 1, (
            f"expected exactly one raw exact-path Route at {mount}, found {len(matches)}"
        )
        mounted = [
            route for route in app.routes if isinstance(route, Mount) and route.path == mount
        ]
        assert not mounted, f"a Mount at {mount} reintroduces the trailing-slash redirect"
    assert set(app.state.mcp_mounts) == {"/mcp", "/mcp_admin"}


@pytest.mark.parametrize(("mount", "surface"), [("/mcp", "sage"), ("/mcp_admin", "sage_admin")])
def test_mount_transport_settings_pinned(minimal_config, mount, surface):
    """The HTTP-mounted servers run the stateless, JSON-response transport.

    ``stateless_http=True`` because the cloud runtime scales out with no
    session affinity (in-memory per-session transports would break on the
    second replica); ``json_response=True`` so tool responses are plain JSON
    bodies rather than SSE frames an intermediary may buffer. The path
    setting is the per-mount coordinate the exact-path route is built from.
    """
    app = create_app(config=minimal_config)
    server = app.state.mcp_mounts[mount]
    assert server.settings.stateless_http is True
    assert server.settings.json_response is True
    assert server.settings.streamable_http_path == mount


async def test_mcp_admin_mount_reads_shared_vault_registry(minimal_config):
    """The maintenance mount's tools read the app-shared ``_vaults`` registry.

    A vault initialized through the app populates ``mcp_server._vaults``;
    calling ``admin_list_vaults`` through the ``/mcp_admin`` mount must then
    see that vault, proving the mount shares the one registry rather than
    building its own (no duplicate vault initialization).
    """
    app = create_app(config=minimal_config)
    await _initialize_services(
        app,
        minimal_config,
        content_store=StubContentStore(),
        embedding_provider=StubEmbeddingProvider(),
        abstraction_provider=StubAbstractionProvider(),
    )
    try:
        admin_server = app.state.mcp_mounts["/mcp_admin"]
        result = await admin_server.call_tool("admin_list_vaults", {})
        assert minimal_config.vault.id in str(result)
    finally:
        for services in app.state.vault_registry.values():
            services.close_timing()
            await services.graph_store.close()
        mcp_server._vaults.pop(minimal_config.vault.id, None)


def test_stdio_entry_points_absent():
    """The stdio transport is retired: no stdio entry point remains.

    Per CAS-ADR-034 the MCP surface is served exclusively over the Streamable
    HTTP mounts. A reappearing ``run_stdio`` or ``sage/mcp_server_admin.py``
    means the retired transport is creeping back in. Absence is probed
    against the imported package's own ``__path__`` (not ``find_spec``,
    which an editable-install finder can satisfy from a different checkout);
    the ``mcp_server.py`` existence check is a spelling/location control
    proving the probe looks at the real package directory.
    """
    assert not hasattr(mcp_server, "run_stdio")
    sage_pkg_dir = Path(next(iter(sage.__path__)))
    assert (sage_pkg_dir / "mcp_server.py").exists()
    assert not (sage_pkg_dir / "mcp_server_admin.py").exists()


@pytest.mark.parametrize("surface", ["sage", "sage_admin"])
def test_partitioned_server_disables_dns_rebinding_host_validation(surface):
    """Both HTTP-mounted MCP surfaces ship with the SDK's DNS-rebinding Host
    allow-list disabled.

    The MCP SDK auto-enables DNS-rebinding protection whenever the server's bind
    host is a loopback value (the default); its allow-list then rejects every
    non-loopback Host with HTTP 421 on the handshake -- i.e. every request
    that arrives through a proxy. The public-edge boundary is the JWT/identity
    layer (CAS-ADR-034), not a browser-localhost threat model, so SAGE disables
    the SDK check rather than letting it 421 legitimate proxied traffic. This
    pins the disabled setting on the servers SAGE actually builds; the faithful
    end-to-end guard lives in ``tests/deploy/test_mcp_preflight_probe.py``.
    """
    ts = mcp_server.build_partitioned_server(surface).settings.transport_security
    assert ts is not None, "transport_security must be set explicitly, not left to auto-enable"
    assert ts.enable_dns_rebinding_protection is False
