"""Alias continuity for the maintenance-surface tool rename (CAS-ADR-034).

The maintenance MCP surface's tools are canonically named ``maint_*``;
the pre-rename ``admin_*`` names remain callable as dispatch-level
aliases with no scheduled removal. ``_LoggingFastMCP.call_tool``
consults ``MAINT_ALIAS_MAPPING`` before dispatch, rewrites an old name
onto its canonical target, and logs the canonical name per call. The
aliases are deliberately **not** registered as tools: the catalog
presents only the canonical names, so the maintenance surface keeps its
size and the cognitive-load purpose of the ordinary/maintenance split.

These tests pin the rewrite (every mapped name reaches dispatch under
its canonical name), its bounded domain (no name outside the mapping is
rewritten -- including the pre-verb-rename names whose alias layer was
removed and must stay removed), the per-call log line, the mapping's
shape against ``SERVER_ASSIGNMENT`` (every alias targets a registered
tool; the one target on the ordinary surface is there by recorded
decision), and the catalog's alias-freedom. An alias grants nothing the
canonical name does not: it resolves on whichever mount registers its
target and fails on the other, exactly as the canonical name would.
"""

from __future__ import annotations

import json
import logging

import pytest
from mcp.server.fastmcp.exceptions import ToolError

import sage.mcp_server as mcp_server
from sage._tool_naming import (
    MAINT_ALIAS_MAPPING,
    PREFIX_SURFACE_DIVERGENCES,
    SERVER_ASSIGNMENT,
    _check_table_invariants,
)
from sage.adapters.stubs import (
    StubAbstractionProvider,
    StubContentStore,
    StubEmbeddingProvider,
)
from sage.app import _initialize_services, create_app
from sage.mcp_server import _LoggingFastMCP


def _capture_dispatch(monkeypatch) -> list[str]:
    """Stub the SDK dispatch layer, capturing the name each call reaches it with."""
    captured: list[str] = []

    async def fake_super_call(self, name, arguments):
        captured.append(name)
        return {"echoed": name}

    monkeypatch.setattr("mcp.server.fastmcp.FastMCP.call_tool", fake_super_call)
    return captured


@pytest.mark.parametrize(("old_name", "canonical"), sorted(MAINT_ALIAS_MAPPING.items()))
async def test_admin_name_rewrites_to_maint_target(old_name, canonical, monkeypatch) -> None:
    """Every aliased ``admin_*`` name reaches dispatch as its ``maint_*`` target."""
    captured = _capture_dispatch(monkeypatch)
    server = _LoggingFastMCP("test")

    await server.call_tool(old_name, {})

    assert captured == [canonical], (
        f"{old_name!r} should rewrite to {canonical!r}; dispatch saw {captured!r}"
    )


async def test_maint_name_dispatches_verbatim(monkeypatch) -> None:
    """A canonical ``maint_*`` name passes through the alias layer unchanged."""
    captured = _capture_dispatch(monkeypatch)
    server = _LoggingFastMCP("test")

    await server.call_tool("maint_list_vaults", {})

    assert captured == ["maint_list_vaults"]


@pytest.mark.parametrize("name", ["search", "sage_discover", "reload_vault", "create_edge"])
async def test_non_mapped_names_unaffected(name, monkeypatch) -> None:
    """The alias layer's domain is exactly ``MAINT_ALIAS_MAPPING``.

    ``search`` is a live ordinary-surface name; the others are
    pre-verb-rename names whose alias layer was removed -- the rewrite
    reintroduced for the maintenance rename must not resurrect them.
    """
    captured = _capture_dispatch(monkeypatch)
    server = _LoggingFastMCP("test")

    await server.call_tool(name, {})

    assert captured == [name], f"{name!r} was rewritten; dispatch saw {captured!r}"


async def test_alias_call_logs_canonical_name(caplog, monkeypatch) -> None:
    """An aliased call logs one line naming old and canonical names, no removal date.

    The alias has no scheduled removal, so the log line must steer the
    caller to the canonical name without promising or dating a removal.
    """
    _capture_dispatch(monkeypatch)
    server = _LoggingFastMCP("test")

    with caplog.at_level(logging.WARNING, logger="sage.mcp_server"):
        await server.call_tool("admin_list_vaults", {})

    alias_records = [
        rec.getMessage()
        for rec in caplog.records
        if rec.name == "sage.mcp_server"
        and rec.levelno == logging.WARNING
        and "admin_list_vaults" in rec.getMessage()
    ]
    assert len(alias_records) == 1, f"expected one alias WARNING, got: {alias_records}"
    assert "maint_list_vaults" in alias_records[0]
    assert "remov" not in alias_records[0].lower(), (
        f"alias log must not promise a removal: {alias_records[0]!r}"
    )


def test_mapping_is_exact_prefix_swap() -> None:
    """The alias table is the fixed pre-rename cohort, key = prefix-swapped value.

    Cross-checks the hand-written alias table against ``SERVER_ASSIGNMENT``:
    every alias targets a registered tool, every key is the mechanical
    prefix swap of its target, and the cohort size is pinned at the 13 tools
    that existed under the old prefix -- a maintenance tool added after the
    rename must NOT gain a fabricated alias, and a dropped entry must not go
    unnoticed. The alias cohort is fixed by history, not by surface: the one
    target that has since moved to the ordinary surface keeps its alias, and
    the set of such targets must equal the recorded prefix/surface
    divergences rather than be tolerated silently.
    """
    registered = set(SERVER_ASSIGNMENT)
    assert set(MAINT_ALIAS_MAPPING.values()) <= registered, (
        f"orphan alias target(s): {sorted(set(MAINT_ALIAS_MAPPING.values()) - registered)}"
    )
    ordinary_targets = {n for n in MAINT_ALIAS_MAPPING.values() if SERVER_ASSIGNMENT[n] == "sage"}
    assert ordinary_targets == PREFIX_SURFACE_DIVERGENCES, (
        f"alias target(s) on the ordinary surface must be exactly the recorded "
        f"divergences: got {sorted(ordinary_targets)}"
    )
    assert len(MAINT_ALIAS_MAPPING) == 13
    for old, new in MAINT_ALIAS_MAPPING.items():
        assert new.startswith("maint_"), f"non-canonical mapping target: {new!r}"
        assert old == "admin_" + new.removeprefix("maint_"), (
            f"alias key {old!r} is not the prefix-swap of {new!r}"
        )


async def test_aliases_absent_from_catalog() -> None:
    """No ``admin_*`` name appears in any advertised catalog.

    The aliases are dispatch-level only; registering them would double
    the maintenance surface's documented size.
    """
    full = {tool.name for tool in await mcp_server.mcp.list_tools()}
    maint_server = mcp_server.build_partitioned_server("sage_maint")
    partitioned = {t.name for t in maint_server._tool_manager.list_tools()}  # noqa: SLF001

    for catalog, label in ((full, "unpartitioned"), (partitioned, "sage_maint")):
        aliased = {n for n in catalog if n.startswith("admin_") or n in MAINT_ALIAS_MAPPING}
        assert not aliased, f"alias name(s) registered on the {label} catalog: {sorted(aliased)}"


async def _app_with_one_vault(minimal_config):
    app = create_app(config=minimal_config)
    await _initialize_services(
        app,
        minimal_config,
        content_store=StubContentStore(),
        embedding_provider=StubEmbeddingProvider(),
        abstraction_provider=StubAbstractionProvider(),
    )
    return app


async def _close_app_vaults(app, minimal_config) -> None:
    for services in app.state.vault_registry.values():
        services.close_timing()
        await services.graph_store.close()
    mcp_server._vaults.pop(minimal_config.vault.id, None)


def _payload(result: object) -> dict:
    """Decode the JSON body FastMCP wraps a tool's dict return into."""
    return json.loads(result[0].text)  # type: ignore[index]


async def test_alias_end_to_end_identical_behavior(minimal_config) -> None:
    """Old and canonical names answer identically through the ``/mcp_admin`` mount.

    Exercises the full path a legacy client takes: the aliased mount, the
    rewrite, and the shared vault registry. The two calls must return the
    same payload, and that payload must be the config itself -- a
    ``vault_not_found`` envelope also names the vault id, and two identical
    envelopes would satisfy a payload-equality check alone.
    """
    app = await _app_with_one_vault(minimal_config)
    try:
        maint_server = app.state.mcp_mounts["/mcp_admin"]
        args = {"vault_id": minimal_config.vault.id}
        via_alias = await maint_server.call_tool("admin_get_vault_config", args)
        via_canonical = await maint_server.call_tool("maint_get_vault_config", args)
        assert str(via_alias) == str(via_canonical)
        payload = _payload(via_alias)
        assert "error" not in payload
        assert payload["vault"]["id"] == minimal_config.vault.id
    finally:
        await _close_app_vaults(app, minimal_config)


async def test_pre_rename_enumeration_alias_resolves_on_ordinary_mount(minimal_config) -> None:
    """``admin_list_vaults`` follows its target to the ``/mcp`` mount.

    The alias is a name rewrite, not a surface grant: with vault enumeration
    registered on the ordinary surface, a legacy caller holding the old name
    reaches it through the ordinary mount and gets the canonical payload.
    """
    app = await _app_with_one_vault(minimal_config)
    try:
        ordinary = app.state.mcp_mounts["/mcp"]
        via_alias = await ordinary.call_tool("admin_list_vaults", {})
        via_canonical = await ordinary.call_tool("maint_list_vaults", {})
        assert str(via_alias) == str(via_canonical)
        payload = _payload(via_alias)
        assert "error" not in payload
        assert minimal_config.vault.id in {v["id"] for v in payload["vaults"]}
    finally:
        await _close_app_vaults(app, minimal_config)


@pytest.mark.parametrize("mount", ["/mcp_maint", "/mcp_admin"])
async def test_pre_rename_enumeration_alias_fails_on_maintenance_mounts(
    minimal_config, mount
) -> None:
    """``admin_list_vaults`` fails on the maintenance mounts exactly as the
    canonical name does.

    CAS-ADR-034's second alias constraint, made observable: the partition
    is evaluated after resolution, so an aliased call to a mount that does
    not register the target is an unknown tool there. The canonical name is
    the control -- if it resolved, the failure below would be about the
    alias layer, not the partition.
    """
    app = await _app_with_one_vault(minimal_config)
    try:
        server = app.state.mcp_mounts[mount]
        with pytest.raises(ToolError, match="maint_list_vaults"):
            await server.call_tool("maint_list_vaults", {})
        with pytest.raises(ToolError, match="maint_list_vaults"):
            await server.call_tool("admin_list_vaults", {})
    finally:
        await _close_app_vaults(app, minimal_config)


def test_alias_invariant_rejects_unregistered_target() -> None:
    """The import-time table check still refuses an alias with no live target.

    Loosening the check from "target on the maintenance surface" to "target
    registered anywhere" must not loosen it to nothing: an alias pointing at
    a name absent from the assignment table is an orphan and fails. The
    unmodified tables are the positive control.
    """
    _check_table_invariants(SERVER_ASSIGNMENT, MAINT_ALIAS_MAPPING)

    orphaned = {**MAINT_ALIAS_MAPPING, "admin_not_a_tool": "maint_not_a_tool"}
    with pytest.raises(AssertionError, match="admin_not_a_tool"):
        _check_table_invariants(SERVER_ASSIGNMENT, orphaned)
