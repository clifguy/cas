"""Partition conformance for the two-surface SAGE MCP tool roster.

Gates the CAS-ADR-034 / CAS-ADR-029 split of the SAGE MCP tool surface
across the Streamable HTTP mounts on the SAGE app — ``/mcp`` (ordinary)
and ``/mcp_maint`` (maintenance, with ``/mcp_admin`` as its pre-rename
alias path serving the identical roster), all built by the same
partition factory in the one uvicorn process:

- ``sage`` — ordinary surface (read spine + everyday mutation spine +
  multi-record operations).
- ``sage_maint`` — maintenance surface (every ``maint_*`` tool); opt-in,
  additive, and does **not** duplicate the read spine.

Surface assignment is derived purely from each tool name's first segment
(``maint_`` -> ``sage_maint``; everything else -> ``sage``). These tests
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
EXPECTED_MAINT = {name for name, srv in SERVER_ASSIGNMENT.items() if srv == "sage_maint"}

# The shared read spine (CAS-ADR-034): these live on the ``sage`` server
# only and must never be duplicated on the ``sage_maint`` server.
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


def test_sage_maint_server_registers_exactly_maintenance_tools():
    """The ``sage_maint`` server registers exactly the maintenance roster."""
    assert _registered_names("sage_maint") == EXPECTED_MAINT


def test_sage_maint_contains_only_maint_prefixed_tools():
    """Partition invariant: every ``sage_maint`` tool name is ``maint_*``."""
    names = _registered_names("sage_maint")
    offenders = {n for n in names if not n.startswith("maint_")}
    assert not offenders, f"non-maint_ tool(s) on sage_maint: {sorted(offenders)}"


def test_no_maint_prefixed_tool_on_sage_server():
    """Partition invariant: no ``maint_*`` tool is registered on ``sage``."""
    names = _registered_names("sage")
    leaked = {n for n in names if n.startswith("maint_")}
    assert not leaked, f"maint_ tool(s) leaked onto sage: {sorted(leaked)}"


def test_read_spine_not_duplicated_on_sage_maint():
    """The shared read spine is not duplicated on the maintenance server."""
    names = _registered_names("sage_maint")
    dup = names & READ_SPINE
    assert not dup, f"read-spine tool(s) duplicated on sage_maint: {sorted(dup)}"


def test_partition_is_disjoint_and_exhaustive():
    """The two partitions are disjoint and together cover the full roster."""
    sage_names = _registered_names("sage")
    maint = _registered_names("sage_maint")
    assert sage_names.isdisjoint(maint), f"tool(s) on both servers: {sorted(sage_names & maint)}"
    assert sage_names | maint == set(SERVER_ASSIGNMENT), (
        "partition union does not equal the full roster: "
        f"missing {sorted(set(SERVER_ASSIGNMENT) - (sage_names | maint))}, "
        f"extra {sorted((sage_names | maint) - set(SERVER_ASSIGNMENT))}"
    )


def test_mcp_mount_advertises_ordinary_surface_only(minimal_config):
    """The ``/mcp`` HTTP mount advertises exactly the ordinary roster.

    Revises the prior full-surface assertion: per CAS-ADR-034 the HTTP
    transport is partitioned, so ``/mcp`` carries the ``sage`` surface only
    and no ``maint_*`` tool appears there.
    """
    app = create_app(config=minimal_config)
    names = _mounted_names(app, "/mcp")
    assert names == EXPECTED_SAGE
    assert names, "ordinary mount roster must be non-empty"
    leaked = {n for n in names if n.startswith("maint_")}
    assert not leaked, f"maint_ tool(s) advertised on /mcp: {sorted(leaked)}"


@pytest.mark.parametrize("mount", ["/mcp_maint", "/mcp_admin"])
def test_maintenance_mounts_advertise_maintenance_surface_only(minimal_config, mount):
    """Both maintenance mount paths advertise exactly the maintenance roster.

    ``/mcp_maint`` is canonical; ``/mcp_admin`` is its pre-rename alias
    path and must stay roster-identical for as long as it is served.
    """
    app = create_app(config=minimal_config)
    names = _mounted_names(app, mount)
    assert names == EXPECTED_MAINT
    offenders = {n for n in names if not n.startswith("maint_")}
    assert not offenders, f"non-maint_ tool(s) on {mount}: {sorted(offenders)}"
    dup = names & READ_SPINE
    assert not dup, f"read-spine tool(s) duplicated on {mount}: {sorted(dup)}"


def test_all_mcp_mounts_are_exact_path_routes(minimal_config):
    """One uvicorn process/app serves every partitioned mount as an exact-path
    raw Starlette route (CAS-ADR-034 v7).

    A ``Mount`` at these paths is the structural form of the trailing-slash
    307 regression: its path regex requires ``/mcp/...``, so an exact
    ``POST /mcp`` — the byte-exact resource URI the edge advertises — falls
    through to the parent router's redirect. The transport must hang off an
    exact-path ``Route`` (raw ASGI, not an ``APIRoute``) instead.
    """
    app = create_app(config=minimal_config)
    for mount in ("/mcp", "/mcp_maint", "/mcp_admin"):
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
    assert set(app.state.mcp_mounts) == {"/mcp", "/mcp_maint", "/mcp_admin"}


@pytest.mark.parametrize(
    ("mount", "surface"),
    [("/mcp", "sage"), ("/mcp_maint", "sage_maint"), ("/mcp_admin", "sage_maint")],
)
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


async def test_maintenance_mount_reads_shared_vault_registry(minimal_config):
    """The maintenance mount's tools read the app-shared ``_vaults`` registry.

    A vault initialized through the app populates ``mcp_server._vaults``;
    calling ``maint_list_vaults`` through the ``/mcp_maint`` mount must then
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
        maint_server = app.state.mcp_mounts["/mcp_maint"]
        result = await maint_server.call_tool("maint_list_vaults", {})
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


@pytest.mark.parametrize("surface", ["sage", "sage_maint"])
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
