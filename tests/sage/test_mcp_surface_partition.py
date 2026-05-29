"""Partition conformance for the two-server SAGE MCP surface.

Gates the CAS-ADR-034 / CAS-ADR-029 split of the SAGE MCP tool surface
across two stdio servers:

- ``sage`` — ordinary surface (read spine + everyday mutation spine +
  multi-record operations); always enabled.
- ``sage_admin`` — maintenance surface (every ``admin_*`` tool); opt-in,
  additive, and does **not** duplicate the read spine.

Server assignment is derived purely from each tool name's first segment
(``admin_`` -> ``sage_admin``; everything else -> ``sage``). These tests
cross-check the built partitions against ``SERVER_ASSIGNMENT`` in
``sage/_tool_rename_mapping.py`` — the in-code transcription of the
*SAGE MCP Tool Surface* steering-document registration map. That oracle is
hand-maintained from the steering doc rather than re-derived from the
prefix rule at runtime, so the comparison is a genuine cross-check, not a
tautology against the production logic.
"""

from __future__ import annotations

import sage.mcp_server as mcp_server
import sage.mcp_server_admin as mcp_server_admin
from sage._tool_rename_mapping import SERVER_ASSIGNMENT

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


def test_cas_app_mcp_mount_advertises_full_surface():
    """The ``/mcp`` mount source (module-level ``mcp``) is full/unpartitioned.

    ``sage/app.py`` mounts ``mcp.sse_app()`` at ``/mcp``; that instance must
    advertise both server rosters combined, with no partition.
    """
    full = {tool.name for tool in mcp_server.mcp._tool_manager.list_tools()}  # noqa: SLF001
    sage = _registered_names("sage")
    admin = _registered_names("sage_admin")
    assert sage and admin, "both partitions must be non-empty"
    assert full == EXPECTED_SAGE | EXPECTED_ADMIN
    assert full == sage | admin


def test_sage_admin_entry_module_builds_admin_surface():
    """The ``sage.mcp_server_admin`` entry module is wired to the maintenance partition."""
    assert mcp_server_admin.SURFACE == "sage_admin"
    assert _registered_names(mcp_server_admin.SURFACE) == EXPECTED_ADMIN
