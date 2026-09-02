"""Partition conformance for the two-surface SAGE MCP tool roster.

Gates the CAS-ADR-034 / CAS-ADR-029 split of the SAGE MCP tool surface
across the Streamable HTTP mounts on the SAGE app — ``/mcp`` (ordinary)
and ``/mcp_maint`` (maintenance, with ``/mcp_admin`` as its pre-rename
alias path serving the identical roster), all built by the same
partition factory in the one uvicorn process:

- ``sage`` — ordinary surface (read spine + everyday mutation spine +
  multi-record operations).
- ``sage_maint`` — maintenance surface (substrate-altering and vault- or
  stack-scoped maintenance tools); opt-in, additive, and does **not**
  duplicate the read spine.

Surface assignment is read from ``SERVER_ASSIGNMENT`` in
``sage/_tool_naming.py`` — the in-code transcription of the *SAGE MCP Tool
Surface* steering-document registration map — and from nothing else: a tool
absent from the table fails registration rather than landing on a default
surface. The ``maint_`` prefix remains the naming convention for maintenance
tools (CAS-ADR-029) but no longer decides registration, so the prefix and
the table are two independent statements of the same partition. These
tests cross-check them: the set of tools on which they disagree must equal
``PREFIX_SURFACE_DIVERGENCES`` exactly, and that set is pinned to its one
recorded member, so a divergence can neither appear unrecorded nor
accumulate as an allowlist.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.routing import APIRoute
from starlette.routing import Mount, Route

import sage
import sage.mcp_server as mcp_server
from sage._tool_naming import PREFIX_SURFACE_DIVERGENCES, SERVER_ASSIGNMENT
from sage.app import create_app

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


def _prefix_surface(name: str) -> str:
    """The surface the naming convention alone would imply for ``name``.

    Kept here, not in production code: the prefix is a naming convention
    (CAS-ADR-029), and registration no longer derives anything from it.
    The tests re-derive it so the prefix-vs-table cross-check compares two
    independent sources.
    """
    return "sage_maint" if name.startswith("maint_") else "sage"


def test_sage_server_registers_exactly_ordinary_tools():
    """The ``sage`` server registers exactly the tools the table assigns to it.

    ``EXPECTED_SAGE`` is derived from the same table registration reads, so
    this verifies that the registration path honours the table -- not an
    independent transcription. The independent cross-check is the
    prefix-vs-table equality and the population pin below.
    """
    assert _registered_names("sage") == EXPECTED_SAGE


def test_sage_maint_server_registers_exactly_maintenance_tools():
    """The ``sage_maint`` server registers exactly the tools the table assigns to it.

    Same oracle as the ordinary-surface test: registration honours the
    table, with independence carried by the prefix-vs-table cross-check.
    """
    assert _registered_names("sage_maint") == EXPECTED_MAINT


def test_sage_maint_contains_only_maint_prefixed_tools():
    """Partition invariant: every ``sage_maint`` tool name is ``maint_*``."""
    names = _registered_names("sage_maint")
    offenders = {n for n in names if not n.startswith("maint_")}
    assert not offenders, f"non-maint_ tool(s) on sage_maint: {sorted(offenders)}"


def test_maint_prefixed_tools_on_sage_are_only_declared_divergences():
    """Partition invariant: a ``maint_*`` tool on ``sage`` is a recorded divergence.

    Read from the built server, not the table, so it fails on a registration
    path that stops honouring the table as much as on a table edit.
    """
    names = _registered_names("sage")
    on_sage = {n for n in names if n.startswith("maint_")}
    assert on_sage == PREFIX_SURFACE_DIVERGENCES, (
        f"maint_ tool(s) on sage other than the declared divergences: "
        f"unexpected {sorted(on_sage - PREFIX_SURFACE_DIVERGENCES)}, "
        f"missing {sorted(PREFIX_SURFACE_DIVERGENCES - on_sage)}"
    )


def test_prefix_and_table_disagree_exactly_on_declared_divergences():
    """The prefix convention and the assignment table disagree on exactly
    ``PREFIX_SURFACE_DIVERGENCES`` — set equality in both directions.

    An entry moved off its prefix's surface without being declared fails
    here; so does a declared divergence whose table row still agrees with
    its prefix (a stale declaration).
    """
    divergent = {
        name for name, surface in SERVER_ASSIGNMENT.items() if _prefix_surface(name) != surface
    }
    assert divergent == PREFIX_SURFACE_DIVERGENCES, (
        f"undeclared prefix/surface divergence(s): "
        f"{sorted(divergent - PREFIX_SURFACE_DIVERGENCES)}; "
        f"declared but not divergent: {sorted(PREFIX_SURFACE_DIVERGENCES - divergent)}"
    )


def test_declared_divergences_are_exactly_vault_enumeration():
    """The divergence set is a recorded decision, not an open allowlist.

    Pinning the population means a second divergence needs its own recorded
    decision and a deliberate edit here, not a one-line append.
    """
    assert PREFIX_SURFACE_DIVERGENCES == frozenset({"maint_list_vaults"})


def test_table_entry_for_vault_enumeration_is_ordinary():
    """Vault enumeration is assigned to the ordinary surface in the table.

    Cheap table pin so a reverted row reads as its own failure rather than
    only through the built-server assertions below.
    """
    assert SERVER_ASSIGNMENT["maint_list_vaults"] == "sage"


def test_vault_enumeration_registers_on_ordinary_surface_only():
    """Vault enumeration is registered on ``sage`` and absent from ``sage_maint``.

    Read from the built servers: the ordinary surface is vault-addressed, so
    it must carry the one tool that enumerates the vaults its ``vault_id``
    arguments range over, and the maintenance surface must not carry a
    second copy. Goes red if the assignment reverts to ``sage_maint``.
    """
    assert "maint_list_vaults" in _registered_names("sage")
    assert "maint_list_vaults" not in _registered_names("sage_maint")


def test_surface_of_is_gone():
    """Name removal-guard: the retired prefix-derived helper does not return.

    Guards the symbol only; a helper reintroduced under another name would
    pass. The property -- registration reads the table and nothing else --
    is carried by ``test_unassigned_tool_fails_registration_loudly``.
    """
    assert not hasattr(mcp_server, "_surface_of")


@pytest.mark.parametrize("surface", ["sage", "sage_maint"])
def test_unassigned_tool_fails_registration_loudly(surface: str, monkeypatch: Any) -> None:
    """A registered tool with no ``SERVER_ASSIGNMENT`` row fails the build.

    The registration path must look the name up, not default it: a
    fallback (``.get(name, "sage")``) would quietly widen the ordinary
    catalog by one tool per omission. The positive control — the same probe
    tool with a table row — must build cleanly, so the failure is the missing
    row and not the probe tool itself.
    """
    real_register_app_tools = mcp_server.register_app_tools

    def register_with_probe(server: Any, *args: Any, **kwargs: Any) -> dict[str, Callable]:
        tools = real_register_app_tools(server, *args, **kwargs)

        @server.tool(name="unassigned_probe_tool")
        async def unassigned_probe_tool() -> dict:
            return {}

        return {**tools, "unassigned_probe_tool": unassigned_probe_tool}

    monkeypatch.setattr(mcp_server, "register_app_tools", register_with_probe)

    with pytest.raises(LookupError, match="unassigned_probe_tool") as excinfo:
        mcp_server.build_partitioned_server(surface)
    assert "SERVER_ASSIGNMENT" in str(excinfo.value)

    # Positive control: with a table row the probe registers on its surface.
    monkeypatch.setitem(SERVER_ASSIGNMENT, "unassigned_probe_tool", surface)
    server = mcp_server.build_partitioned_server(surface)
    names = {t.name for t in server._tool_manager.list_tools()}  # noqa: SLF001
    assert "unassigned_probe_tool" in names


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
    on_mount = {n for n in names if n.startswith("maint_")}
    assert on_mount == PREFIX_SURFACE_DIVERGENCES, (
        f"maint_ tool(s) advertised on /mcp beyond the declared divergences: "
        f"{sorted(on_mount ^ PREFIX_SURFACE_DIVERGENCES)}"
    )


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


async def test_mounts_read_shared_vault_registry(
    app_with_one_vault: FastAPI, minimal_config: Any, tool_payload: Callable[[object], dict]
) -> None:
    """Both mounts' tools read the app-shared ``_vaults`` registry.

    A vault initialized through the app populates ``mcp_server._vaults``;
    enumerating vaults through the ``/mcp`` mount and reading that vault's
    config through the ``/mcp_maint`` mount must both see it, proving each
    mount shares the one registry rather than building its own (no
    duplicate vault initialization).

    Anti-coincidental: the payloads are decoded and checked for an error
    envelope rather than substring-matched, because a ``vault_not_found``
    envelope -- exactly what a mount with its own empty registry returns --
    carries the vault id in its detail and would satisfy a substring check.
    """
    mounts = app_with_one_vault.state.mcp_mounts
    listed = tool_payload(await mounts["/mcp"].call_tool("maint_list_vaults", {}))
    assert "error" not in listed
    assert minimal_config.vault.id in {v["id"] for v in listed["vaults"]}
    config = tool_payload(
        await mounts["/mcp_maint"].call_tool(
            "maint_get_vault_config", {"vault_id": minimal_config.vault.id}
        )
    )
    assert "error" not in config
    assert config["vault"]["id"] == minimal_config.vault.id


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
