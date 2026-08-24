"""Regression: the verb-rename MCP tool-name alias layer stays gone.

The verb-convention rename (CAS-ADR-033) and the two-server prefix
simplification (CAS-ADR-034) were rolled out behind a transitional
``_LoggingFastMCP.call_tool`` alias middleware that rewrote pre-rename
tool names onto their current targets and short-circuited dropped names
with a ``tool_removed`` envelope. That deprecation window closed and
the middleware was removed; those names dispatch verbatim.

A *separate, standing* alias layer now exists for the maintenance
surface's prefix rename (``MAINT_ALIAS_MAPPING``; covered by
``test_mcp_maint_alias.py``). Its domain is exactly the mapped
maintenance names, so these tests double as the boundary proof: the
verb-rename-era names below must never re-enter any alias table.

These tests pin the *absence* of the verb-rename rewrite, the *absence*
of the removed-name short-circuit, and the *absence* of a per-call
warning for those names. This file is the one sanctioned place a
verb-rename-era tool name appears in the tree: the literals exist only
to prove such a name is no longer special-cased.
"""

from __future__ import annotations

import logging

import pytest

from sage.mcp_server import _LoggingFastMCP


@pytest.mark.parametrize("old_name", ["sage_discover", "reload_vault", "create_edge"])
async def test_old_name_dispatches_without_rewrite(old_name: str, monkeypatch) -> None:
    """A pre-rename name reaches dispatch verbatim -- no alias rewrite.

    Against the pre-removal middleware this failed: the name was rewritten
    to its current target (``sage_discover`` -> ``search``,
    ``reload_vault`` -> ``admin_reload_vault``, ``create_edge`` ->
    ``create_edges``) before dispatch, so the captured name differed from
    the input. With the middleware gone the name passes through unchanged.
    """
    captured: list[str] = []

    async def fake_super_call(self, name, arguments):
        captured.append(name)
        return {"echoed": name}

    monkeypatch.setattr("mcp.server.fastmcp.FastMCP.call_tool", fake_super_call)
    mcp = _LoggingFastMCP("test")

    await mcp.call_tool(old_name, {"probe": True})

    assert captured == [old_name], (
        f"{old_name!r} was rewritten to {captured!r}; the alias middleware "
        "should be gone and the name should dispatch verbatim."
    )


async def test_removed_name_dispatches_without_short_circuit(monkeypatch) -> None:
    """A formerly-removed name reaches dispatch -- no ``tool_removed`` envelope.

    Against the pre-removal middleware this failed: names in the old
    ``REMOVED_TOOLS`` set short-circuited to an envelope error and never
    reached the parent ``call_tool``. With the middleware gone the name
    dispatches like any other and the parent decides tool-not-found.
    """
    captured: list[str] = []

    async def fake_super_call(self, name, arguments):
        captured.append(name)
        return {"echoed": name}

    monkeypatch.setattr("mcp.server.fastmcp.FastMCP.call_tool", fake_super_call)
    mcp = _LoggingFastMCP("test")

    result = await mcp.call_tool("sage_register_user", {})

    assert captured == ["sage_register_user"], (
        "a formerly-removed name short-circuited instead of dispatching; "
        f"captured={captured!r}, result={result!r}"
    )


async def test_no_deprecation_warning_emitted(caplog, monkeypatch) -> None:
    """No call emits a deprecation / alias / removal WARNING.

    Against the pre-removal middleware an old-name call logged a WARNING
    naming the old name, its target, and the removal date. With the
    middleware gone no such record is emitted.
    """

    async def fake_super_call(self, name, arguments):
        return {"ok": True}

    monkeypatch.setattr("mcp.server.fastmcp.FastMCP.call_tool", fake_super_call)
    mcp = _LoggingFastMCP("test")

    with caplog.at_level(logging.WARNING, logger="sage.mcp_server"):
        await mcp.call_tool("sage_discover", {})

    flagged = [
        rec.getMessage()
        for rec in caplog.records
        if rec.name == "sage.mcp_server"
        and rec.levelno == logging.WARNING
        and any(word in rec.getMessage().lower() for word in ("deprecat", "alias", "removed"))
    ]
    assert not flagged, f"unexpected deprecation/alias WARNING(s): {flagged}"
