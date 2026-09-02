"""SAGE MCP server -- thin adapter translating MCP tool calls to Core API.

Supports multiple vaults loaded at startup. Each tool takes vault_id as
its first parameter to select the target vault.

Tools are defined in:
  - sage.sage_api_tools: SAGE protocol and API query tools
  - sage.app_tools: Application backend tools (scan, batch ingest)

The surface is served over the MCP Streamable HTTP transport by the SAGE
FastAPI app (``sage/app.py``), which builds one partitioned server per
mount via ``build_partitioned_server``.
"""

import json as _json
import os
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from typing import Any

# Quiet huggingface library noise before any sage import pulls in
# transformers/tokenizers. Env vars are read at library import
# time; setdefault preserves a debugger's explicit override.
os.environ.setdefault("TRANSFORMERS_VERBOSITY", "warning")
os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")

# httpx and sentence_transformers do not read env vars; raise their
# loggers to WARNING explicitly so model-load HTTP fetches don't flood
# the console. SAGE's own "Loading embedding model" line in
# embedding_nomic.py covers the user-relevant signal.
import logging as _logging  # noqa: E402

for _hf_logger in ("httpx", "sentence_transformers"):
    _logging.getLogger(_hf_logger).setLevel(_logging.WARNING)

# ruff: noqa: E402 -- imports below follow the deliberate pre-import side effects above
from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ContentBlock, TextContent
from pydantic import ValidationError

# Side-effect import: monkey-patches ArgModelBase.model_config to add
# extra="forbid", so every FastMCP per-tool argument model rejects
# unknown JSON-RPC kwargs at the framework boundary. Must precede any
# tool registration (register_sage_tools, register_app_tools, the
# @mcp.tool()-decorated entries below) because Tool.from_function()
# snapshots the Pydantic config at subclass construction. Substrate
# decision: CAS-ADR-037.
import sage._fastmcp_strict_args  # noqa: F401 -- substrate side-effect import
import sage.app  # noqa: F401 -- import side-effect: installs root-logger filter
from sage._tool_naming import MAINT_ALIAS_MAPPING, SERVER_ASSIGNMENT
from sage.api.errors import SAGEError, validation_error_envelope
from sage.app_tools import register_app_tools
from sage.build_info import SERVER_INSTRUCTIONS, VERSION_WITH_BUILD
from sage.mcp_init import (
    SAGEServices,
    initialize_services,
)
from sage.sage_api_tools import register_sage_tools
from sage.services.vault_registry import VaultRegistryService

# ---------------------------------------------------------------------------
# Vault registry
# ---------------------------------------------------------------------------

_vaults: dict[str, SAGEServices] = {}

# Cross-vault registry service singleton. Constructed once at module import
# against the `_vaults` dict so both the MCP transport (here) and the FastAPI
# transport (sage/app.py reaches into us via get_vault_registry_service())
# share the same instance, which wraps the same registry dict. See CAS-ADR-013
# for the registry-dict aliasing rationale.
_vault_registry_service: VaultRegistryService = VaultRegistryService(
    registry=_vaults,
    initialize_services=initialize_services,
)


def get_vault_registry_service() -> VaultRegistryService:
    """Return the module-level VaultRegistryService singleton."""
    return _vault_registry_service


def _get_vaults() -> dict[str, SAGEServices]:
    """Return the module-level vault registry dict.

    Used as a call-time getter passed to ``register_sage_tools`` so that
    tools resolving the registry dict pick up the *current* binding rather
    than freezing on whatever was bound at registration time. This matters
    because ``tests/sage/test_cleanup_refactor.py::test_cln_003_import_succeeds``
    invokes ``importlib.reload(sage.mcp_server)`` and then rebinds
    ``_vaults`` and ``_vault_registry_service`` to their pre-reload
    instances to keep other modules consistent — a getter resolves the
    rebound original, whereas a captured instance would freeze on the
    reload-time orphan.
    """
    return _vaults


class VaultNotFoundError(ValueError):
    """Raised when a tool is called with a vault_id that is not registered.

    ValueError-derived so existing tool callsites that catch
    `(SAGEError, ValueError)` continue to handle vault routing without
    change; `_error_response` distinguishes this case from other
    ValueErrors so the latter no longer surface as `unknown_vault`.
    """


def _get_vault(vault_id: str) -> SAGEServices:
    """Look up services for a vault. Raises VaultNotFoundError if unknown."""
    if vault_id not in _vaults:
        available = ", ".join(sorted(_vaults.keys())) or "(none)"
        raise VaultNotFoundError(f"Unknown vault_id: {vault_id}. Available vaults: {available}")
    return _vaults[vault_id]


def _serialize(obj: object) -> dict:
    """Convert a Pydantic model or dict to a plain dict for MCP response.

    Returns a dict so FastMCP serializes it once for the wire, avoiding
    double JSON encoding.
    """
    if hasattr(obj, "model_dump"):
        return obj.model_dump(mode="json", exclude_none=True)
    if isinstance(obj, dict):
        return obj
    return {"value": str(obj)}


def _error_response(exc: SAGEError | ValueError) -> dict:
    """Format a SAGE error, vault-routing error, or other ValueError for MCP."""
    if isinstance(exc, SAGEError):
        payload: dict = {"error": exc.code, "message": exc.message}
        if exc.detail:
            payload["detail"] = exc.detail
    elif isinstance(exc, VaultNotFoundError):
        payload = {"error": "unknown_vault", "message": str(exc)}
    elif isinstance(exc, ValidationError):
        # Every validation failure carries a structured envelope: the most
        # specific code that applies -- a malformed typed-alias boundary
        # value, an unknown filter key, an out-of-vocabulary enum -- or the
        # general invalid_parameter for a bound or coercion failure with no
        # more specific code. What a caller must never receive is the
        # rendered form of the error, which names the model class and links
        # to the validator's documentation site.
        sage_err = validation_error_envelope(exc)
        payload = {"error": sage_err.code, "message": sage_err.message}
        if sage_err.detail:
            payload["detail"] = sage_err.detail
    else:
        payload = {"error": "internal_error", "message": str(exc)}
    return payload


# ---------------------------------------------------------------------------
# Lifespan
# ---------------------------------------------------------------------------


@asynccontextmanager
async def _lifespan(server: FastMCP) -> AsyncIterator[None]:
    # The FastAPI lifespan (sage/app.py) owns the vault lifecycle: it
    # discovers vaults, populates the shared `_vaults` registry, and tears
    # it down. The MCP server's own lifespan must therefore be a no-op —
    # any init or teardown here would fight the parent app's.
    yield


def _envelope_error_kind(result: Any) -> str | None:
    """Return the SAGE error-envelope kind from a FastMCP call_tool result, or None.

    SAGE tools return error envelopes as `{"error": "<code>", "message": "..."}`
    dicts (see `_error_response`). At the `_LoggingFastMCP.call_tool` layer the
    dict has already passed through FastMCP's `_convert_to_content`, which
    JSON-serializes it and wraps the result in
    `[TextContent(type="text", text=<json>)]`. The production return shape is
    therefore a single-element list of `TextContent`, not the raw dict — see
    `mcp.server.fastmcp.utilities.func_metadata._convert_to_content`.

    Three shapes are checked:
    - `Sequence[ContentBlock]` (production): JSON-parse each text block and
      look for the `"error"` key.
    - `dict` (defensive): direct `"error"` key — covers a future FastMCP that
      stops wrapping, and matches the unit test that mocks `super().call_tool`
      to return a raw dict.
    - `CallToolResult.isError` (defensive, per MCP spec): when a tool returns
      a `CallToolResult` directly, FastMCP passes it through unwrapped.
    """
    if getattr(result, "isError", False):
        return "is_error"
    if isinstance(result, dict) and "error" in result:
        return str(result["error"])
    if isinstance(result, Sequence) and not isinstance(result, str | bytes):
        for block in result:
            if getattr(block, "type", None) != "text":
                continue
            text = getattr(block, "text", None)
            if not isinstance(text, str):
                continue
            try:
                payload = _json.loads(text)
            except (_json.JSONDecodeError, ValueError):
                continue
            if isinstance(payload, dict) and "error" in payload:
                return str(payload["error"])
    return None


def _argument_validation_envelope(
    tool_name: str,
    exc: ToolError,
    tool_manager: Any,
) -> list[TextContent] | None:
    """Translate an argument-validation ToolError into the SAGE error envelope.

    FastMCP validates incoming arguments against a model generated from
    the tool signature, before the tool body runs. A failure there wraps
    in ``ToolError`` at ``Tool.run``
    (``mcp/server/fastmcp/tools/base.py``), with the original Pydantic
    ``ValidationError`` surviving as ``ToolError.__cause__``. Because the
    body never executes, its own error handling cannot see the failure;
    this boundary is the only place the substrate can normalize it
    (CAS-ADR-037).

    Two envelopes come out of here. An ``extra_forbidden`` error — the
    signal that ``ArgModelBase``'s ``extra="forbid"`` rejected a
    caller-supplied kwarg — yields ``unknown_parameter``, whose detail
    enumerates the rejected names alongside the full set of valid ones so
    the caller can correct the invocation in one round-trip. Any other
    argument-validation failure yields whatever envelope
    ``validation_error_envelope`` selects, typically
    ``invalid_parameter``.

    ``extra_forbidden`` is checked first and wins outright: when a call
    carries both an unknown kwarg and a malformed known one, naming the
    valid parameter set is the more useful of the two answers, and which
    failure Pydantic happens to report first is not a basis for choosing.

    Returns ``None`` when the cause is not a ``ValidationError`` at all —
    a genuine tool-body failure — so the caller propagates that ToolError
    unchanged. The envelope is wrapped as ``[TextContent(text=<json>)]``
    to match the production wire shape ``_envelope_error_kind`` already
    understands, so the existing WARNING-log path covers these error
    kinds without duplication.
    """
    cause = exc.__cause__
    if not isinstance(cause, ValidationError):
        return None

    rejected = [
        err["loc"][0]
        for err in cause.errors()
        if err.get("type") == "extra_forbidden" and err.get("loc")
    ]

    if rejected:
        tool = tool_manager.get_tool(tool_name)
        if tool is not None:
            valid_params = sorted(tool.parameters.get("properties", {}).keys())
        else:  # pragma: no cover -- defensive; ToolError implies the tool exists
            valid_params = []
        rejected_sorted = sorted(set(rejected))

        sage_err: SAGEError = SAGEError(
            code="unknown_parameter",
            message=(f"Tool {tool_name!r} received unknown parameter(s): {rejected_sorted}."),
            status_code=400,
            detail={
                "tool": tool_name,
                "rejected_params": rejected_sorted,
                "valid_params": valid_params,
            },
        )
    else:
        sage_err = validation_error_envelope(cause)

    envelope = _error_response(sage_err)
    return [TextContent(type="text", text=_json.dumps(envelope))]


# The MCP SDK's FastMCP auto-enables DNS-rebinding Host validation whenever the
# bind host is a loopback value (its default); the resulting allow-list
# (127.0.0.1 / localhost / ::1 only) rejects every non-loopback Host with HTTP
# 421 on the handshake -- i.e. every request that reaches an HTTP-mounted
# surface through a proxy. SAGE's public edge authenticates at the JWT/identity
# layer (CAS-ADR-034); DNS-rebinding is a browser-localhost threat model that
# does not apply to that server-to-server path, so the SDK check is disabled
# rather than left to 421 legitimate proxied traffic.
_MCP_TRANSPORT_SECURITY = TransportSecuritySettings(enable_dns_rebinding_protection=False)


class _LoggingFastMCP(FastMCP):
    """FastMCP subclass that distinguishes tool outcomes in the console log.

    The underlying uvicorn access log shows every tool call as
    `POST /mcp` — uninformative because the tool name lives in the
    JSON-RPC body, not the URL. Overriding the single
    dispatch point (`FastMCP.call_tool`, wired in `_setup_handlers`)
    surfaces the tool name in the console without touching individual
    `@mcp.tool()` registrations. Three outcomes get three log shapes:

    - success → one INFO line (`mcp tool: <name>`).
    - envelope-error → INFO plus one WARNING line
      (`mcp tool error: <name> (<error_kind>)`); the result is returned to the
      caller unchanged. The envelope is detected via `_envelope_error_kind`,
      which handles the production `[TextContent(text=<json>)]` shape that
      FastMCP wraps SAGE dict returns into. []
    - raised exception → INFO plus one ERROR line
      (`mcp tool failed: <name>`) with traceback, and re-raise.

    The ``ToolError`` branch additionally checks whether the cause is an
    ``extra_forbidden`` Pydantic ``ValidationError`` — the signal that
    ``ArgModelBase``'s ``extra="forbid"`` rejected a caller-supplied
    kwarg. When it is, the translation returns the SAGE
    ``unknown_parameter`` envelope (wrapped to match the production
    ``[TextContent(text=<json>)]`` wire shape) and falls through to the
    existing ``_envelope_error_kind`` warning path; the ToolError is
    NOT re-raised. Substrate decision: CAS-ADR-037.

    Pre-dispatch alias resolution (standing, no scheduled removal):
    before dispatch, the requested ``name`` is checked against
    ``MAINT_ALIAS_MAPPING`` (the maintenance surface's pre-rename tool
    names per CAS-ADR-034). An old name is rewritten to its canonical
    target and a per-call WARNING names both, so callers can migrate at
    their own pace. Canonical names and every name outside the mapping
    dispatch verbatim.
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        # Disable the SDK's loopback-host DNS-rebinding allow-list on every SAGE
        # MCP server (see _MCP_TRANSPORT_SECURITY) unless a caller pins its own
        # transport security. Covers both HTTP-mounted partitions and the
        # standalone full-surface mount.
        kwargs.setdefault("transport_security", _MCP_TRANSPORT_SECURITY)
        super().__init__(*args, **kwargs)

    async def call_tool(
        self, name: str, arguments: dict[str, Any]
    ) -> Sequence[ContentBlock] | dict[str, Any]:
        logger = _logging.getLogger(__name__)
        # Pre-dispatch alias resolution for the maintenance-surface rename
        # (CAS-ADR-034): a pre-rename ``admin_*`` name rewrites to its
        # canonical ``maint_*`` target so existing callers keep working.
        # No removal is scheduled; the log line steers callers to the
        # canonical name without promising one.
        if name in MAINT_ALIAS_MAPPING:
            target = MAINT_ALIAS_MAPPING[name]
            logger.warning(
                "mcp tool alias: %r dispatched as its canonical name %r",
                name,
                target,
            )
            name = target
        logger.info("mcp tool: %s", name)
        try:
            result = await super().call_tool(name, arguments)
        except ToolError as e:
            envelope = _argument_validation_envelope(name, e, self._tool_manager)
            if envelope is None:
                logger.exception("mcp tool failed: %s", name)
                raise
            result = envelope
        except Exception:
            logger.exception("mcp tool failed: %s", name)
            raise
        error_kind = _envelope_error_kind(result)
        if error_kind is not None:
            logger.warning("mcp tool error: %s (%s)", name, error_kind)
        return result


# The release version and build identity captured at import are advertised on
# the MCP initialize handshake (``instructions`` and ``serverInfo.version``, the
# latter as ``<version>+<build>``) so a connecting client can read the running
# version and tell at a glance whether this long-running process is serving code
# older than the working tree. See ``sage.build_info``.
mcp = _LoggingFastMCP("SAGE", lifespan=_lifespan, instructions=SERVER_INSTRUCTIONS)
mcp._mcp_server.version = VERSION_WITH_BUILD

# ---------------------------------------------------------------------------
# Register tools from submodules
# ---------------------------------------------------------------------------

_sage_tools = register_sage_tools(
    mcp, _get_vault, _serialize, _error_response, _get_vaults, get_vault_registry_service
)
_app_tools = register_app_tools(mcp, _get_vault, _serialize, _error_response)

# ---------------------------------------------------------------------------
# Re-export tool functions for backward-compatible imports
# (e.g. from sage.mcp_server import ingest_document)
# ---------------------------------------------------------------------------

ingest_document = _sage_tools["ingest_document"]
get_filename_metadata = _sage_tools["get_filename_metadata"]
recompute_abstract = _sage_tools["recompute_abstract"]
recompute_pipeline = _sage_tools["recompute_pipeline"]
get_document = _sage_tools["get_document"]
update_metadata = _sage_tools["update_metadata"]
update_lifecycles = _sage_tools["update_lifecycles"]
create_edges = _sage_tools["create_edges"]
delete_edge = _sage_tools["delete_edge"]
verify_preconditions = _sage_tools["verify_preconditions"]
traverse = _sage_tools["traverse"]
chain = _sage_tools["chain"]
search = _sage_tools["search"]
read_projection = _sage_tools["read_projection"]
read_section = _sage_tools["read_section"]
list_headings = _sage_tools["list_headings"]
recompute_views = _sage_tools["maint_recompute_views"]
list_vaults = _sage_tools["maint_list_vaults"]
create_vault = _sage_tools["maint_create_vault"]
get_vault_config = _sage_tools["maint_get_vault_config"]
update_vault_config = _sage_tools["maint_update_vault_config"]
get_vault_stats = _sage_tools["maint_get_vault_stats"]
verify_hash = _sage_tools["verify_hashes"]
list_staging_edges = _sage_tools["list_staging_edges"]
update_staging_edge = _sage_tools["update_staging_edge"]
list_pending_metadata = _sage_tools["list_pending_metadata"]
migrate_vault = _sage_tools["maint_migrate_vault"]
verify_vault_drift = _sage_tools["maint_verify_vault_drift"]
verify_vault_source_files = _sage_tools["maint_verify_vault_source_files"]
restore_vault_source_file = _sage_tools["maint_restore_vault_source_file"]
recompute_deferred_vault_abstracts = _sage_tools["maint_recompute_deferred_vault_abstracts"]
optimize_vault_content_store = _sage_tools["maint_optimize_vault_content_store"]
reload_vault = _sage_tools["maint_reload_vault"]
get_stack_config = _sage_tools["maint_get_stack_config"]

list_directory = _app_tools["list_directory"]
bulk_ingest_document = _app_tools["bulk_ingest_document"]

# ---------------------------------------------------------------------------
# Surface partition (CAS-ADR-034 / CAS-ADR-029)
# ---------------------------------------------------------------------------
#
# The SAGE MCP surface is split into two: ``sage`` (the ordinary surface)
# and ``sage_maint`` (the maintenance surface, opt-in additive). Per
# CAS-ADR-034 the partition is realized as Streamable HTTP mounts on
# the SAGE app (``/mcp`` = ordinary, ``/mcp_maint`` = maintenance, with
# ``/mcp_admin`` as the maintenance surface's pre-rename alias path; see
# ``sage/app.py``). Mount selection *is* the role declaration. Surface
# assignment is read from ``SERVER_ASSIGNMENT`` in ``sage._tool_naming``
# and from nothing else: the ``maint_`` prefix is a naming convention
# (CAS-ADR-029), not a registration rule, so a tool can move between
# surfaces without a rename. The module-level ``mcp`` above remains the
# full, unpartitioned surface whose tool functions are re-exported for
# direct import; the partitioned servers are built by
# ``build_partitioned_server`` below.


def build_partitioned_server(surface: str) -> _LoggingFastMCP:
    """Build an MCP server registering only the tools for ``surface``.

    Both servers share the underlying tool-implementation layer
    (``register_sage_tools`` / ``register_app_tools``). The partition is
    applied after registration by dropping every tool whose
    ``SERVER_ASSIGNMENT`` row names a different surface. A registered tool
    with no row fails the build with ``LookupError`` rather than landing on
    a default surface: an omission must be visible at startup, not as a
    tool that quietly appears on the ordinary catalog. The maintenance
    server does not duplicate the shared read spine, which the table
    assigns to ``sage`` only.

    The server carries the Streamable HTTP transport settings its HTTP
    mounting requires. ``stateless_http=True``
    because the cloud runtime scales out with no session affinity — an
    in-memory per-session transport would strand a session on its minting
    replica. ``json_response=True`` so a tool response is a plain JSON body
    rather than an SSE-framed stream an intermediary may buffer. The
    per-mount ``streamable_http_path`` is a mounting coordinate, not a
    surface property, so it is set by the mounter (``sage/app.py``).
    """
    server = _LoggingFastMCP(
        surface,
        lifespan=_lifespan,
        instructions=SERVER_INSTRUCTIONS,
        stateless_http=True,
        json_response=True,
    )
    server._mcp_server.version = VERSION_WITH_BUILD
    sage_tools = register_sage_tools(
        server, _get_vault, _serialize, _error_response, _get_vaults, get_vault_registry_service
    )
    app_tools = register_app_tools(server, _get_vault, _serialize, _error_response)
    for name in {**sage_tools, **app_tools}:
        if name not in SERVER_ASSIGNMENT:
            raise LookupError(
                f"MCP tool {name!r} has no SERVER_ASSIGNMENT row; every registered "
                "tool must be assigned a surface in sage._tool_naming.SERVER_ASSIGNMENT"
            )
        if SERVER_ASSIGNMENT[name] != surface:
            server.remove_tool(name)
    return server
