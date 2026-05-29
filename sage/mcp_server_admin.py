"""SAGE maintenance MCP server (stdio) — the ``sage_admin`` surface.

Registers only the maintenance tools (every tool whose name's first segment
is ``admin_``) per CAS-ADR-034 and CAS-ADR-029. This server is opt-in and
additive: a maintenance-mode agent enables both ``sage`` and ``sage_admin``
in its MCP-client settings and reads documents via the ``sage`` server,
which carries the shared read spine. The ordinary surface lives in
``sage.mcp_server``; both entry modules share the same tool-implementation
layer and vault registry.

Usage:
    python -m sage.mcp_server_admin [--vault-root DIR]
"""

from sage.mcp_server import run_stdio

#: The surface this entry module serves. Server assignment is derived from
#: each tool name's first segment per CAS-ADR-029's prefix-encodes-surface
#: rule; this constant names the maintenance partition.
SURFACE = "sage_admin"

if __name__ == "__main__":
    run_stdio(SURFACE, prog="python -m sage.mcp_server_admin")
