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
surface.

The independent oracle is ``EXPECTED_SURFACE`` in
``tests/sage/mcp_surface_pin.py``: a hand-written literal with no production
reader. The drift gates compare the table, the built servers, and the live
HTTP mounts to that pin per surface, so a tool silently added to a surface,
dropped from one, or moved between surfaces goes red until the pin is
edited deliberately -- in CI, which has no vault to consult the steering
document in. The pure ``_partition_drift`` classifier is pinned with
synthetic tables and exercised against a live mutation of the table, so the
gate cannot pass by comparing the table to itself.

The maintenance-only outlier verbs (CAS-ADR-033) are scoped by the table
rather than by the verb taxonomy: ``MAINT_ONLY_VERBS`` is pinned to its
population here, and the import-time invariants refuse a table that places
a tool carrying one of those verbs off the maintenance surface.

Nothing in a tool's name signals its surface: the maintenance prefix
CAS-ADR-029 once carried is retired, so the pin and the table are the only
two statements of the partition and these gates hold them together.
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
from sage._tool_naming import (
    MAINT_ONLY_VERBS,
    SERVER_ASSIGNMENT,
    TOOL_ALIASES,
    _check_table_invariants,
    extract_verb,
)
from sage.app import create_app
from tests.sage.mcp_surface_pin import EXPECTED_SURFACE


def _pinned(surface: str) -> set[str]:
    """The tools the hand-maintained pin places on ``surface``."""
    return {name for name, srv in EXPECTED_SURFACE.items() if srv == surface}


EXPECTED_SAGE = _pinned("sage")
EXPECTED_MAINT = _pinned("sage_maint")

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


def _partition_drift(
    assignment: dict[str, str], expected: dict[str, str]
) -> tuple[tuple[str, ...], tuple[str, ...], tuple[tuple[str, str, str], ...]]:
    """Classify drift between a surface table and the pinned partition.

    Returns ``(unpinned, missing, moved)``: names in ``assignment`` but not
    in ``expected``; names in ``expected`` but not in ``assignment``; and
    ``(name, actual, pinned)`` triples for names the two place on different
    surfaces. Pure over its inputs so the classifier itself can be pinned
    with synthetic tables. The comparison is per name and per surface -- a
    name-set comparison would let a moved tool pass.
    """
    unpinned = tuple(sorted(set(assignment) - set(expected)))
    missing = tuple(sorted(set(expected) - set(assignment)))
    moved = tuple(
        (name, assignment[name], expected[name])
        for name in sorted(set(assignment) & set(expected))
        if assignment[name] != expected[name]
    )
    return unpinned, missing, moved


_SYNTHETIC_PIN = {"a": "sage", "b": "sage_maint", "x": "sage"}


@pytest.mark.parametrize(
    ("assignment", "drift"),
    [
        ({"a": "sage", "b": "sage_maint", "x": "sage"}, ((), (), ())),
        ({"a": "sage", "b": "sage_maint", "x": "sage", "probe": "sage"}, (("probe",), (), ())),
        ({"a": "sage", "b": "sage_maint"}, ((), ("x",), ())),
        (
            {"a": "sage", "b": "sage_maint", "x": "sage_maint"},
            ((), (), (("x", "sage_maint", "sage"),)),
        ),
    ],
    ids=["identical", "silently-added", "dropped", "moved"],
)
def test_partition_drift_classifier(assignment, drift):
    """The drift classifier separates added, dropped, and moved tools.

    The ``moved`` row is the one a name-set comparison would miss: the same
    three names, one on the other surface. Pinning it here is what makes
    the live gates below a per-surface check rather than a roster count.
    """
    assert _partition_drift(assignment, _SYNTHETIC_PIN) == drift


def test_assignment_table_matches_pinned_partition():
    """``SERVER_ASSIGNMENT`` equals the hand-maintained pin, per surface.

    The pin is a literal with no production reader, so this is a genuine
    cross-check rather than an echo of the table. A tool added, dropped, or
    moved without a deliberate edit to ``tests/sage/mcp_surface_pin.py``
    fails here, in CI, with the offenders listed by channel.
    """
    unpinned, missing, moved = _partition_drift(SERVER_ASSIGNMENT, EXPECTED_SURFACE)
    assert (unpinned, missing, moved) == ((), (), ()), (
        "SERVER_ASSIGNMENT drifted from the pinned partition: "
        f"unpinned {list(unpinned)}, missing {list(missing)}, moved {list(moved)}. "
        "If the change is deliberate, edit tests/sage/mcp_surface_pin.py."
    )


@pytest.mark.parametrize("surface", ["sage", "sage_maint"])
def test_built_server_matches_pinned_partition(surface: str):
    """Each built server registers exactly the tools the pin places on it.

    Compared to the pin, not the table, so a registration path that stops
    honouring the table fails here even when table and pin agree.
    """
    assert _registered_names(surface) == _pinned(surface)


@pytest.mark.parametrize(
    ("mount", "surface"),
    [("/mcp", "sage"), ("/mcp_maint", "sage_maint"), ("/mcp_admin", "sage_maint")],
)
def test_live_mount_matches_pinned_partition(minimal_config, mount: str, surface: str):
    """Each live HTTP mount advertises exactly the tools the pin places on it."""
    app = create_app(config=minimal_config)
    assert _mounted_names(app, mount) == _pinned(surface)


def test_silently_added_tool_trips_gate(monkeypatch: Any):
    """A table row with no pin entry is reported as ``unpinned``.

    Live positive control for the drift gate: the mutation is asserted
    visible before the classifier runs, so a monkeypatch that landed on a
    copy would fail here rather than pass vacuously.
    """
    monkeypatch.setitem(SERVER_ASSIGNMENT, "probe_tool", "sage")
    assert "probe_tool" in SERVER_ASSIGNMENT
    unpinned, missing, moved = _partition_drift(SERVER_ASSIGNMENT, EXPECTED_SURFACE)
    assert unpinned == ("probe_tool",)
    assert (missing, moved) == ((), ())


def test_moved_tool_trips_gate(monkeypatch: Any):
    """A table row moved to the other surface is reported as ``moved`` and
    changes the built roster.

    The second half proves the built server follows the table: with
    ``search`` reassigned, ``sage_maint`` now carries it and no longer
    equals its pin. A registration path that ignored the table would leave
    the roster unchanged and fail here.
    """
    monkeypatch.setitem(SERVER_ASSIGNMENT, "search", "sage_maint")
    assert SERVER_ASSIGNMENT["search"] == "sage_maint"
    _, _, moved = _partition_drift(SERVER_ASSIGNMENT, EXPECTED_SURFACE)
    assert moved == (("search", "sage_maint", "sage"),)
    maint = _registered_names("sage_maint")
    assert "search" in maint
    assert maint != _pinned("sage_maint")


async def test_unpartitioned_server_roster_equals_pin():
    """The full, unpartitioned server carries exactly the pinned roster.

    Roster only: the module-level server has no placement, so this checks
    the name set and nothing about surfaces.
    """
    names = {tool.name for tool in await mcp_server.mcp.list_tools()}
    assert names == set(EXPECTED_SURFACE)


def test_maintenance_only_verbs_population():
    """``MAINT_ONLY_VERBS`` is exactly the recorded closed class.

    Pinning the population means a fifth outlier verb needs its own
    recorded decision and a deliberate edit here, not a one-line append.
    """
    assert MAINT_ONLY_VERBS == frozenset({"reload", "migrate", "optimize", "restore"})


def test_table_invariants_reject_maintenance_verb_on_ordinary_surface():
    """The import-time check refuses a table that places a tool carrying a
    maintenance-only verb on the ordinary surface.

    The outlier verbs are scoped by the table, not by a name prefix: the
    broken copy keeps every name and prefix intact and changes one row's
    surface, so only a check that reads the table can reject it. The
    unmodified tables are the positive control.

    Anti-coincidental: the second copy moves a maintenance tool whose verb
    is *not* maintenance-only and must pass -- a check that flagged any
    maintenance-surface tool moved to ``sage`` (reading the pin or the
    roster rather than the verb) would reject it; a verb-reading one
    accepts it.
    """
    _check_table_invariants(SERVER_ASSIGNMENT, TOOL_ALIASES)

    misplaced = {**SERVER_ASSIGNMENT, "reload_vault": "sage"}
    assert misplaced["reload_vault"] == "sage"
    with pytest.raises(AssertionError, match=r"reload_vault.*reload"):
        _check_table_invariants(misplaced, TOOL_ALIASES)

    ordinary_verb_moved = {**SERVER_ASSIGNMENT, "get_vault_stats": "sage"}
    assert extract_verb("get_vault_stats") not in MAINT_ONLY_VERBS
    _check_table_invariants(ordinary_verb_moved, TOOL_ALIASES)


def test_table_entry_for_vault_enumeration_is_ordinary():
    """Vault enumeration is assigned to the ordinary surface in the table.

    Cheap table pin so a reverted row reads as its own failure rather than
    only through the built-server assertions below.
    """
    assert SERVER_ASSIGNMENT["list_vaults"] == "sage"


def test_vault_enumeration_registers_on_ordinary_surface_only():
    """Vault enumeration is registered on ``sage`` and absent from ``sage_maint``.

    Read from the built servers: the ordinary surface is vault-addressed, so
    it must carry the one tool that enumerates the vaults its ``vault_id``
    arguments range over, and the maintenance surface must not carry a
    second copy. Goes red if the assignment reverts to ``sage_maint``.
    """
    assert "list_vaults" in _registered_names("sage")
    assert "list_vaults" not in _registered_names("sage_maint")


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
    catalog by one tool per omission. Two probe tools are registered and
    both must be named in the one failure, so a build that raised on the
    first omission it met -- reporting one of two -- fails here. The
    positive control — the same probes with table rows — must build cleanly,
    so the failure is the missing rows and not the probe tools themselves.
    """
    probes = ("unassigned_probe_tool", "unassigned_probe_tool_2")
    real_register_app_tools = mcp_server.register_app_tools

    def register_with_probe(server: Any, *args: Any, **kwargs: Any) -> dict[str, Callable]:
        tools = real_register_app_tools(server, *args, **kwargs)
        for probe in probes:

            async def probe_tool() -> dict:
                return {}

            server.tool(name=probe)(probe_tool)
            tools[probe] = probe_tool
        return tools

    monkeypatch.setattr(mcp_server, "register_app_tools", register_with_probe)

    with pytest.raises(LookupError) as excinfo:
        mcp_server.build_partitioned_server(surface)
    message = str(excinfo.value)
    assert "SERVER_ASSIGNMENT" in message
    for probe in probes:
        assert probe in message, f"batched failure omits {probe!r}: {message}"

    # Positive control: with table rows the probes register on their surface.
    for probe in probes:
        monkeypatch.setitem(SERVER_ASSIGNMENT, probe, surface)
    server = mcp_server.build_partitioned_server(surface)
    names = {t.name for t in server._tool_manager.list_tools()}  # noqa: SLF001
    assert set(probes) <= names


def test_read_spine_not_duplicated_on_sage_maint():
    """The shared read spine is not duplicated on the maintenance server."""
    names = _registered_names("sage_maint")
    dup = names & READ_SPINE
    assert not dup, f"read-spine tool(s) duplicated on sage_maint: {sorted(dup)}"


def test_partition_is_disjoint_and_exhaustive():
    """The two built rosters are disjoint and together equal the pinned roster.

    Measured against the pin rather than the table, so the union check is
    a cross-check and not the table compared to itself.
    """
    sage_names = _registered_names("sage")
    maint = _registered_names("sage_maint")
    assert sage_names.isdisjoint(maint), f"tool(s) on both servers: {sorted(sage_names & maint)}"
    assert sage_names | maint == set(EXPECTED_SURFACE), (
        "partition union does not equal the pinned roster: "
        f"missing {sorted(set(EXPECTED_SURFACE) - (sage_names | maint))}, "
        f"extra {sorted((sage_names | maint) - set(EXPECTED_SURFACE))}"
    )


def test_mcp_mount_advertises_ordinary_surface_only(minimal_config):
    """The ``/mcp`` HTTP mount advertises exactly the ordinary roster.

    Revises the prior full-surface assertion: per CAS-ADR-034 the HTTP
    transport is partitioned, so ``/mcp`` carries the ``sage`` surface only.
    """
    app = create_app(config=minimal_config)
    names = _mounted_names(app, "/mcp")
    assert names == EXPECTED_SAGE
    assert names, "ordinary mount roster must be non-empty"


@pytest.mark.parametrize("mount", ["/mcp_maint", "/mcp_admin"])
def test_maintenance_mounts_advertise_maintenance_surface_only(minimal_config, mount):
    """Both maintenance mount paths advertise exactly the maintenance roster.

    ``/mcp_maint`` is canonical; ``/mcp_admin`` is its pre-rename alias
    path and must stay roster-identical for as long as it is served.
    """
    app = create_app(config=minimal_config)
    names = _mounted_names(app, mount)
    assert names == EXPECTED_MAINT
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
    listed = tool_payload(await mounts["/mcp"].call_tool("list_vaults", {}))
    assert "error" not in listed
    assert minimal_config.vault.id in {v["id"] for v in listed["vaults"]}
    config = tool_payload(
        await mounts["/mcp_maint"].call_tool(
            "get_vault_config", {"vault_id": minimal_config.vault.id}
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
