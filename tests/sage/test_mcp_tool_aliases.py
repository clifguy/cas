"""Alias continuity for the maintenance tools' retired names (CAS-ADR-034).

The maintenance tools have carried two prefixes since their names were
last stable -- ``admin_`` and then ``maint_`` -- and both spellings remain
callable as dispatch-level aliases with no scheduled removal.
``_LoggingFastMCP.call_tool`` consults ``TOOL_ALIASES`` before dispatch,
rewrites a retired name onto its canonical target in one lookup, and logs
the canonical name per call. The aliases are deliberately **not**
registered as tools: the catalog presents only the canonical names, so
the maintenance surface keeps its size and the cognitive-load purpose of
the ordinary/maintenance split.

These tests pin the rewrite (every retired name reaches dispatch under
its canonical name, whichever generation the caller holds), its bounded
domain (no name outside the table is rewritten -- including the
pre-verb-rename names whose alias layer was removed and must stay
removed), the per-call log line, the table's shape against
``SERVER_ASSIGNMENT`` (every alias targets a registered tool, and the two
generations are exactly the cohorts history produced), the catalog's
alias-freedom, and the import-time invariants that keep one-hop
resolution complete. An alias grants nothing the canonical name does
not: it resolves on whichever mount registers its target and is refused
on the other exactly as the canonical name is -- and the refusal names
the mount that does serve it.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from typing import Any

import pytest
from fastapi import FastAPI
from mcp.server.fastmcp.exceptions import ToolError

import sage.mcp_server as mcp_server
from sage._tool_naming import (
    SERVER_ASSIGNMENT,
    TOOL_ALIASES,
    _check_table_invariants,
)
from sage.app import create_app
from sage.config import VaultConfig
from sage.mcp_server import _LoggingFastMCP

#: The maintenance-era cohort: every tool that carried the ``maint_``
#: prefix when it was retired. Written out by hand so the alias table is
#: held to history rather than to whatever the maintenance roster holds
#: today -- a tool added later carries no retired spelling and gets no
#: alias.
_MAINT_ERA_STEMS: frozenset[str] = frozenset(
    {
        "list_vaults",
        "get_vault_config",
        "get_vault_stats",
        "get_stack_config",
        "create_vault",
        "reload_vault",
        "update_vault_config",
        "verify_vault_drift",
        "verify_vault_source_files",
        "restore_vault_source_file",
        "migrate_vault",
        "recompute_views",
        "recompute_deferred_vault_abstracts",
        "optimize_vault_content_store",
    }
)

#: The one maintenance-era tool that post-dates the ``admin_`` era.
_ADDED_AFTER_ADMIN_ERA: frozenset[str] = frozenset({"restore_vault_source_file"})


def _capture_dispatch(monkeypatch) -> list[str]:
    """Stub the SDK dispatch layer, capturing the name each call reaches it with."""
    captured: list[str] = []

    async def fake_super_call(self, name, arguments):
        captured.append(name)
        return {"echoed": name}

    monkeypatch.setattr("mcp.server.fastmcp.FastMCP.call_tool", fake_super_call)
    return captured


@pytest.mark.parametrize(("old_name", "canonical"), sorted(TOOL_ALIASES.items()))
async def test_retired_name_rewrites_to_canonical(old_name, canonical, monkeypatch) -> None:
    """Every retired spelling, from either generation, reaches dispatch as its canonical name."""
    captured = _capture_dispatch(monkeypatch)
    server = _LoggingFastMCP("test")

    await server.call_tool(old_name, {})

    assert captured == [canonical], (
        f"{old_name!r} should rewrite to {canonical!r}; dispatch saw {captured!r}"
    )


@pytest.mark.parametrize("name", ["list_vaults", "get_vault_config"])
async def test_canonical_name_dispatches_verbatim(name, monkeypatch) -> None:
    """A canonical name passes through the alias layer unchanged."""
    captured = _capture_dispatch(monkeypatch)
    server = _LoggingFastMCP("test")

    await server.call_tool(name, {})

    assert captured == [name]


@pytest.mark.parametrize("name", ["search", "sage_discover", "update_lifecycle", "create_edge"])
async def test_non_mapped_names_unaffected(name, monkeypatch) -> None:
    """The alias layer's domain is exactly ``TOOL_ALIASES``.

    ``search`` is a live ordinary-surface name; the others are
    pre-verb-rename names whose alias layer was removed -- the rewrite
    kept for the retired maintenance spellings must not resurrect them.
    """
    captured = _capture_dispatch(monkeypatch)
    server = _LoggingFastMCP("test")

    await server.call_tool(name, {})

    assert captured == [name], f"{name!r} was rewritten; dispatch saw {captured!r}"


@pytest.mark.parametrize("old_name", ["admin_migrate_vault", "maint_migrate_vault"])
async def test_alias_call_logs_canonical_name(old_name, caplog, monkeypatch) -> None:
    """An aliased call logs one line naming old and canonical names, no removal date.

    Both generations log identically: the alias has no scheduled removal,
    so the line must steer the caller to the canonical name without
    promising or dating a removal.
    """
    captured = _capture_dispatch(monkeypatch)
    server = _LoggingFastMCP("test")

    with caplog.at_level(logging.WARNING, logger="sage.mcp_server"):
        await server.call_tool(old_name, {})

    assert captured == ["migrate_vault"]
    alias_records = [
        rec.getMessage()
        for rec in caplog.records
        if rec.name == "sage.mcp_server"
        and rec.levelno == logging.WARNING
        and old_name in rec.getMessage()
    ]
    assert len(alias_records) == 1, f"expected one alias WARNING, got: {alias_records}"
    assert "migrate_vault" in alias_records[0]
    assert "remov" not in alias_records[0].lower(), (
        f"alias log must not promise a removal: {alias_records[0]!r}"
    )


def test_alias_table_is_the_two_retired_cohorts() -> None:
    """The alias table is exactly the two cohorts history produced, one hop each.

    Cross-checks the hand-written alias table against ``SERVER_ASSIGNMENT``
    and against the maintenance-era cohort pinned above: every alias
    targets a registered tool; every key is its target under one retired
    prefix; the ``maint_`` generation covers exactly the maintenance-era
    stems; the ``admin_`` generation covers those stems minus the tool
    added after the ``admin_`` era, which must NOT gain a fabricated alias;
    and the cohort size is pinned so a dropped entry does not go unnoticed.
    """
    registered = set(SERVER_ASSIGNMENT)
    assert set(TOOL_ALIASES.values()) <= registered, (
        f"orphan alias target(s): {sorted(set(TOOL_ALIASES.values()) - registered)}"
    )
    admin = {old: new for old, new in TOOL_ALIASES.items() if old.startswith("admin_")}
    maint = {old: new for old, new in TOOL_ALIASES.items() if old.startswith("maint_")}
    assert set(admin) | set(maint) == set(TOOL_ALIASES), "alias key outside both generations"
    for old, new in TOOL_ALIASES.items():
        assert old.partition("_")[2] == new, f"alias key {old!r} is not a prefixed {new!r}"
    assert set(maint.values()) == _MAINT_ERA_STEMS
    assert set(admin.values()) == _MAINT_ERA_STEMS - _ADDED_AFTER_ADMIN_ERA
    assert len(TOOL_ALIASES) == 27


async def test_aliases_absent_from_catalog() -> None:
    """No retired spelling appears in any advertised catalog.

    The aliases are dispatch-level only; registering them would more than
    double the maintenance surface's documented size.
    """
    full = {tool.name for tool in await mcp_server.mcp.list_tools()}
    maint_server = mcp_server.build_partitioned_server("sage_maint")
    partitioned = {t.name for t in maint_server._tool_manager.list_tools()}  # noqa: SLF001

    for catalog, label in ((full, "unpartitioned"), (partitioned, "sage_maint")):
        aliased = {n for n in catalog if n.startswith(("admin_", "maint_")) or n in TOOL_ALIASES}
        assert not aliased, f"alias name(s) registered on the {label} catalog: {sorted(aliased)}"


@pytest.mark.parametrize("old_name", ["admin_get_vault_config", "maint_get_vault_config"])
async def test_alias_resolution_is_generation_agnostic(
    app_with_one_vault: FastAPI,
    minimal_config: Any,
    tool_payload: Callable[[object], dict],
    old_name: str,
) -> None:
    """Either retired generation answers identically to the canonical name
    through the ``/mcp_admin`` mount.

    Exercises the full path a legacy client takes: the aliased mount, the
    rewrite, and the shared vault registry, without the caller knowing
    which generation it holds. The two calls must return the same payload,
    and that payload must be the config itself -- a ``vault_not_found``
    envelope also names the vault id, and two identical envelopes would
    satisfy a payload-equality check alone.
    """
    maint_server = app_with_one_vault.state.mcp_mounts["/mcp_admin"]
    args = {"vault_id": minimal_config.vault.id}
    via_alias = await maint_server.call_tool(old_name, args)
    via_canonical = await maint_server.call_tool("get_vault_config", args)
    assert str(via_alias) == str(via_canonical)
    payload = tool_payload(via_alias)
    assert "error" not in payload
    assert payload["vault"]["id"] == minimal_config.vault.id


@pytest.mark.parametrize("old_name", ["admin_list_vaults", "maint_list_vaults"])
async def test_enumeration_aliases_resolve_on_ordinary_mount(
    app_with_one_vault: FastAPI,
    minimal_config: Any,
    tool_payload: Callable[[object], dict],
    old_name: str,
) -> None:
    """Both retired spellings of vault enumeration follow their target to ``/mcp``.

    The alias is a name rewrite, not a surface grant: with vault enumeration
    registered on the ordinary surface, a legacy caller holding either old
    name reaches it through the ordinary mount and gets the canonical
    payload.
    """
    ordinary = app_with_one_vault.state.mcp_mounts["/mcp"]
    via_alias = await ordinary.call_tool(old_name, {})
    via_canonical = await ordinary.call_tool("list_vaults", {})
    assert str(via_alias) == str(via_canonical)
    payload = tool_payload(via_alias)
    assert "error" not in payload
    assert minimal_config.vault.id in {v["id"] for v in payload["vaults"]}


@pytest.mark.parametrize("mount", ["/mcp_maint", "/mcp_admin"])
@pytest.mark.parametrize("name", ["admin_list_vaults", "maint_list_vaults", "list_vaults"])
async def test_enumeration_refused_on_maintenance_mounts_naming_the_ordinary_mount(
    minimal_config: VaultConfig, mount: str, name: str, caplog
) -> None:
    """Vault enumeration is refused on the maintenance mounts under every
    spelling, and the refusal says where the tool lives.

    CAS-ADR-034's second alias constraint, made observable: the partition
    is evaluated after resolution, so an aliased call to a mount that does
    not register the target is refused there. The canonical name is the
    control -- if it resolved, the failure below would be about the alias
    layer, not the partition. The refusal names the spelling the caller
    sent, the canonical name, and the ordinary mount, and is logged as a
    WARNING rather than as an ERROR with a traceback: a caller on the wrong
    mount is a client-side condition, not a server fault. The mounts exist
    before any vault is initialized, and the refusal is raised before any
    vault is read, so no vault is initialized here.
    """
    server = create_app(config=minimal_config).state.mcp_mounts[mount]
    pattern = re.escape(repr(name)) + r".*'list_vaults' is registered on the 'sage' surface.*/mcp;"
    with caplog.at_level(logging.WARNING, logger="sage.mcp_server"):
        with pytest.raises(ToolError, match=pattern):
            await server.call_tool(name, {})

    records = [rec for rec in caplog.records if rec.name == "sage.mcp_server"]
    assert not [rec for rec in records if rec.levelno >= logging.ERROR], (
        f"cross-surface refusal logged at ERROR: {[r.getMessage() for r in records]}"
    )
    refusals = [
        rec.getMessage()
        for rec in records
        if rec.levelno == logging.WARNING and "registered on" in rec.getMessage()
    ]
    assert len(refusals) == 1, f"expected one refusal WARNING, got: {refusals}"


@pytest.mark.parametrize(
    "name", ["admin_get_vault_config", "maint_get_vault_config", "get_vault_config"]
)
async def test_maintenance_tool_refused_on_ordinary_mount_naming_the_maintenance_mount(
    minimal_config: VaultConfig, name: str
) -> None:
    """A maintenance tool is refused on ``/mcp`` under every spelling, and
    the refusal names the maintenance mount.

    The other direction of the partition, and the check that the refusal
    reads the mount path from the surface table: every enumeration case
    above resolves to the ordinary surface, so a refusal that named
    ``/mcp`` unconditionally would pass there. This one must name
    ``/mcp_maint``.

    The maintenance surface is served at two paths, so the refusal must
    name both: a caller holding the alias path is sent somewhere that works.
    """
    server = create_app(config=minimal_config).state.mcp_mounts["/mcp"]
    pattern = (
        re.escape(repr(name))
        + r".*'get_vault_config' is registered on the 'sage_maint' surface, served at "
        + r"/mcp_maint or /mcp_admin;"
    )
    with pytest.raises(ToolError, match=pattern):
        await server.call_tool(name, {"vault_id": minimal_config.vault.id})


@pytest.mark.parametrize("which", ["bare", "unpartitioned"])
async def test_cross_surface_refusal_only_on_partitioned_servers(which, monkeypatch) -> None:
    """A server not named for a surface never refuses; the name reaches dispatch.

    The refusal is gated on the server being one of the partitioned
    surfaces. The full unpartitioned server (``mcp_server.mcp``, named
    ``SAGE``, which registers everything) and a bare server built by a
    test register whatever they register and leave unknown-tool handling
    to the SDK -- so an aliased maintenance name on either reaches the
    (stubbed) dispatch layer as its canonical name.
    """
    captured = _capture_dispatch(monkeypatch)
    server = _LoggingFastMCP("test") if which == "bare" else mcp_server.mcp

    await server.call_tool("maint_list_vaults", {})

    assert captured == ["list_vaults"]


async def test_refused_alias_call_logs_each_line_once_and_no_dispatch(
    minimal_config: VaultConfig, caplog
) -> None:
    """On a partitioned mount, a refused aliased call logs the rewrite once,
    the refusal once, no dispatch line, and nothing at ERROR.

    The alias-log test above runs on a bare server, which never refuses, so
    it cannot see the second WARNING the refusal adds; this pins the full
    partitioned-mount trail. The dispatch INFO line is written only after
    the refusal, so a call that was refused leaves no record of a dispatch
    that never happened.

    Anti-coincidental: a second call that *does* dispatch (a maintenance
    tool on its own mount, against a vault that does not exist, so it
    answers with an envelope and needs no vault) must produce exactly one
    dispatch line. Without it, "no dispatch line" would also hold if INFO
    were simply not being captured.
    """
    server = create_app(config=minimal_config).state.mcp_mounts["/mcp_maint"]
    with caplog.at_level(logging.INFO, logger="sage.mcp_server"):
        with pytest.raises(ToolError):
            await server.call_tool("admin_list_vaults", {})
        await server.call_tool("get_vault_config", {"vault_id": "no_such_vault"})

    records = [rec for rec in caplog.records if rec.name == "sage.mcp_server"]
    rewrites = [r for r in records if "dispatched as its canonical name" in r.getMessage()]
    refusals = [r for r in records if "registered on" in r.getMessage()]
    dispatches = [r.getMessage() for r in records if r.getMessage().startswith("mcp tool: ")]
    assert [r.levelno for r in rewrites] == [logging.WARNING], rewrites
    assert [r.levelno for r in refusals] == [logging.WARNING], refusals
    assert dispatches == ["mcp tool: get_vault_config"], dispatches
    assert not [r for r in records if r.levelno >= logging.ERROR]


async def test_unknown_tool_on_partitioned_server_still_fails_as_unknown(
    minimal_config: VaultConfig,
) -> None:
    """A name no surface registers fails as an unknown tool, not as a cross-surface refusal.

    Proves the refusal wording is specific: it fires only when the other
    surface registers the resolved name, so a plain typo is not redirected
    to a mount that would not serve it either.
    """
    server = create_app(config=minimal_config).state.mcp_mounts["/mcp_maint"]
    with pytest.raises(ToolError) as excinfo:
        await server.call_tool("no_such_tool", {})
    assert "no_such_tool" in str(excinfo.value)
    assert "registered on" not in str(excinfo.value)


def test_table_invariants_reject_orphan_colliding_and_chained_alias() -> None:
    """The import-time table check refuses an alias whose target is not a
    registered tool, one that collides with a registered name, and one
    whose target is itself an alias.

    The unmodified tables are the positive control. The chained case is the
    one a flat table needs: dispatch resolves exactly one hop, so an alias
    pointing at another alias would resolve to a name no surface registers.
    Its intermediate is deliberately absent from the table so the check
    must name the chained key for being chained, not for being orphaned.
    """
    _check_table_invariants(SERVER_ASSIGNMENT, TOOL_ALIASES)

    orphaned = {**TOOL_ALIASES, "admin_not_a_tool": "not_a_tool"}
    with pytest.raises(AssertionError, match=r"not a registered tool.*admin_not_a_tool"):
        _check_table_invariants(SERVER_ASSIGNMENT, orphaned)

    colliding = {**TOOL_ALIASES, "search": "get_document"}
    with pytest.raises(AssertionError, match=r"collide.*search"):
        _check_table_invariants(SERVER_ASSIGNMENT, colliding)

    chained = {**TOOL_ALIASES, "admin_x": "maint_x", "maint_x": "get_document"}
    with pytest.raises(AssertionError, match=r"itself an alias.*admin_x"):
        _check_table_invariants(SERVER_ASSIGNMENT, chained)
