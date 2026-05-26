"""Reject unknown caller-supplied keyword arguments at the SAGE MCP boundary.

Imported for its side effect: replaces ``ArgModelBase.model_config`` with
a ``ConfigDict`` that carries ``extra="forbid"`` in addition to the
upstream default ``arbitrary_types_allowed=True``. After this module
runs, every per-tool argument model that FastMCP builds via
``create_model(..., __base__=ArgModelBase)`` rejects unknown JSON-RPC
kwargs with a structured ``ValidationError`` of type
``extra_forbidden`` instead of silently dropping them.

The rejection is translated to the SAGE ``unknown_parameter`` error
envelope at the substrate seam (``_LoggingFastMCP.call_tool`` in
``sage.mcp_server``). The configuration here is just the upstream knob
that surfaces the failure; the envelope shaping lives in the server.

Timing constraint -- load-bearing:
The configuration must be in place BEFORE any ``@mcp.tool()``
decoration runs, because ``Tool.from_function()`` -> ``func_metadata()``
-> ``create_model(__base__=ArgModelBase)`` snapshots the parent's
Pydantic config at subclass construction. Post-decoration mutations of
``ArgModelBase.model_config`` are silent no-ops -- the already-built
per-tool models continue to carry whatever config they were created
with. The discipline this module enforces is therefore: import this
module at SAGE MCP server module-import scope, before the
``register_sage_tools(...)`` and ``register_app_tools(...)`` calls.

Mechanism:
A monkey-patch on a vendored package's class. The upstream library
(``mcp.server.fastmcp.utilities.func_metadata``) does not expose a
configuration knob for the per-tool argument model, so a class-level
attribute mutation is the only available seam in the current upstream
release. The substrate anchor (CAS-ADR-037) treats this as the
strategy of record for the SAGE surface; a parallel upstream change
would eventually obviate the monkey-patch by carrying the same
configuration upstream.

Substrate decision: CAS-ADR-037 (SAGE MCP tools reject unknown
caller-supplied kwargs at the framework boundary).
"""

from __future__ import annotations

from mcp.server.fastmcp.utilities.func_metadata import ArgModelBase
from pydantic import ConfigDict

# Replace -- do not merge -- so the new config carries both fields
# explicitly. Merging via ``{**ArgModelBase.model_config, "extra":
# "forbid"}`` would work but obscures the upstream baseline. An
# explicit replacement makes the post-patch config readable in one
# place when diagnosing a future drift.
ArgModelBase.model_config = ConfigDict(
    arbitrary_types_allowed=True,
    extra="forbid",
)
