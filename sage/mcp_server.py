"""SAGE MCP server -- thin adapter translating MCP tool calls to Core API.

Supports multiple vaults loaded at startup. Each tool takes vault_id as
its first parameter to select the target vault.

Tools are defined in:
  - sage.sage_api_tools: SAGE protocol and API query tools
  - sage.app_tools: Application backend tools (scan, batch ingest)

Usage:
    python -m sage.mcp_server <config1.yaml> [config2.yaml...]
"""

import json as _json
import os
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from pathlib import Path
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
from sage._tool_rename_mapping import REMOVED_TOOLS, RENAME_MAPPING
from sage.api.errors import SAGEError
from sage.app_tools import register_app_tools
from sage.config import load_vault_config
from sage.mcp_init import (
    SAGEServices,
    build_stack_abstraction_provider,
    initialize_services,
    load_stack_config_or_default,
    set_stack_config,
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
    else:
        payload = {"error": "internal_error", "message": str(exc)}
    return payload


# ---------------------------------------------------------------------------
# Lifespan: discover vault configs and initialize services
# ---------------------------------------------------------------------------

_vault_root: Path | None = None


@asynccontextmanager
async def _lifespan(server: FastMCP) -> AsyncIterator[None]:
    # When mounted on FastAPI, _vault_root is None and _vaults is
    # pre-populated by the parent app's lifespan. Skip init/teardown
    # so the FastAPI lifespan owns the vault lifecycle.
    standalone = _vault_root is not None

    if standalone:
        from sage.vault_discovery import discover_vault_configs

        # CAS-ADR-030: build the SAGE-stack-wide abstraction provider once
        # at process startup; share it across every vault that doesn't opt
        # out via vault.abstraction.enabled = False.
        stack_cfg = load_stack_config_or_default()
        set_stack_config(stack_cfg)
        stack_abstraction_provider = build_stack_abstraction_provider(stack_cfg)

        for config_path in discover_vault_configs(_vault_root):
            try:
                config = load_vault_config(config_path)
                services = await initialize_services(
                    config,
                    config_path=config_path,
                    abstraction_provider=stack_abstraction_provider,
                    registry_service=_vault_registry_service,
                )
                _vaults[config.vault.id] = services
            except Exception as exc:
                import logging

                logging.getLogger(__name__).error(
                    "Skipping vault at %s: failed to load (%s)", config_path, exc
                )

    yield

    if standalone:
        for services in _vaults.values():
            await services.graph_store.close()
        _vaults.clear()
        set_stack_config(None)


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


def _unknown_parameter_envelope(
    tool_name: str,
    exc: ToolError,
    tool_manager: Any,
) -> list[TextContent] | None:
    """Translate an ``extra_forbidden`` ToolError into the SAGE error envelope.

    FastMCP wraps every per-tool exception in ``ToolError`` at
    ``Tool.run`` (``mcp/server/fastmcp/tools/base.py``); the original
    Pydantic ``ValidationError`` survives as ``ToolError.__cause__``. When
    the cause carries one or more errors of type ``extra_forbidden`` —
    the signal that ``ArgModelBase``'s ``extra="forbid"`` rejected a
    caller-supplied kwarg — the SAGE substrate translates the failure
    into the ``unknown_parameter`` envelope at the framework boundary
    (CAS-ADR-037).

    Returns ``None`` when the ToolError is not the result of an
    ``extra_forbidden`` rejection — the cause is a different
    ``ValidationError`` category (type coercion, missing required
    field) or a non-validation tool-body failure. The caller continues
    to propagate the ToolError unchanged in that case.

    The envelope detail enumerates the rejected parameter names and the
    full set of valid parameters declared in the tool's signature, so
    the caller can correct the invocation in one round-trip without
    consulting external documentation. The envelope is wrapped as
    ``[TextContent(text=<json>)]`` to match the production wire shape
    that ``_envelope_error_kind`` already understands, so the existing
    WARNING-log path covers the new error kind without duplication.
    """
    cause = exc.__cause__
    if not isinstance(cause, ValidationError):
        return None
    rejected = [
        err["loc"][0]
        for err in cause.errors()
        if err.get("type") == "extra_forbidden" and err.get("loc")
    ]
    if not rejected:
        return None

    tool = tool_manager.get_tool(tool_name)
    if tool is not None:
        valid_params = sorted(tool.parameters.get("properties", {}).keys())
    else:  # pragma: no cover -- defensive; ToolError implies the tool exists
        valid_params = []
    rejected_sorted = sorted(set(rejected))

    sage_err = SAGEError(
        code="unknown_parameter",
        message=(f"Tool {tool_name!r} received unknown parameter(s): {rejected_sorted}."),
        status_code=400,
        detail={
            "tool": tool_name,
            "rejected_params": rejected_sorted,
            "valid_params": valid_params,
        },
    )
    envelope = _error_response(sage_err)
    return [TextContent(type="text", text=_json.dumps(envelope))]


#: Calendar date by which the deprecation alias layer is scheduled
#: for removal. The follow-up change that removes the alias rewrites
#: this module and the ``RENAME_MAPPING`` / ``REMOVED_TOOLS`` entries
#: in ``sage/_tool_rename_mapping.py``. The date is surfaced in the
#: per-call deprecation log so callers see when their old-name usage
#: stops working.
_ALIAS_REMOVAL_DATE: str = "2026-06-15"


def _removed_tool_envelope(name: str) -> Sequence[ContentBlock]:
    """Build an envelope-shaped error for a removed tool name.

    Returned in place of dispatch when an old-name call targets a tool
    that was dropped from the MCP surface entirely (e.g. folded into
    another tool, or retained as REST-only). The envelope matches the
    production ``[TextContent(text=<json>)]`` shape so existing
    envelope-error logging picks it up.
    """
    envelope = {
        "error": "tool_removed",
        "message": (
            f"Tool {name!r} has been removed from the MCP surface. "
            "See sage._tool_rename_mapping.REMOVED_TOOLS for the disposition "
            "of each removed name."
        ),
    }
    return [TextContent(type="text", text=_json.dumps(envelope))]


class _LoggingFastMCP(FastMCP):
    """FastMCP subclass that distinguishes tool outcomes in the console log.

    The underlying uvicorn access log shows every tool call as
    `POST /mcp/messages/?session_id=...` — uninformative because the tool
    name lives in the JSON-RPC body, not the URL. Overriding the single
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

    Pre-dispatch alias resolution (transitional, removal date
    ``_ALIAS_REMOVAL_DATE``):
    Before dispatch, the requested ``name`` is checked against
    ``RENAME_MAPPING`` (verb convention per CAS-ADR-033, prefix
    simplification per CAS-ADR-034). An old name is rewritten to its
    current target and a per-call deprecation WARNING is logged so
    callers see both the old name, the current target, and the
    scheduled removal date. Old names listed in ``REMOVED_TOOLS``
    return an envelope-shaped ``tool_removed`` error rather than
    dispatching. New names (the value side of ``RENAME_MAPPING``)
    bypass the rewrite and dispatch directly.
    """

    async def call_tool(
        self, name: str, arguments: dict[str, Any]
    ) -> Sequence[ContentBlock] | dict[str, Any]:
        logger = _logging.getLogger(__name__)
        # Pre-dispatch alias resolution. Old names rewrite to their
        # current target; removed names short-circuit to an envelope
        # error. New names fall through unchanged.
        if name in REMOVED_TOOLS:
            logger.warning("mcp tool removed: %s (no replacement on MCP surface)", name)
            return _removed_tool_envelope(name)
        if name in RENAME_MAPPING:
            target = RENAME_MAPPING[name]
            logger.warning(
                "mcp tool deprecated: %r is aliased to %r; "
                "the alias is scheduled for removal on %s",
                name,
                target,
                _ALIAS_REMOVAL_DATE,
            )
            name = target
        logger.info("mcp tool: %s", name)
        try:
            result = await super().call_tool(name, arguments)
        except ToolError as e:
            envelope = _unknown_parameter_envelope(name, e, self._tool_manager)
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


mcp = _LoggingFastMCP("SAGE", lifespan=_lifespan)

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
get_document = _sage_tools["get_document"]
update_metadata = _sage_tools["update_metadata"]
update_lifecycle = _sage_tools["update_lifecycle"]
bulk_update_lifecycle = _sage_tools["bulk_update_lifecycle"]
bulk_create_edge = _sage_tools["bulk_create_edge"]
bulk_update_metadata = _sage_tools["bulk_update_metadata"]
create_edge = _sage_tools["create_edge"]
delete_edge = _sage_tools["delete_edge"]
verify_preconditions = _sage_tools["verify_preconditions"]
traverse = _sage_tools["traverse"]
chain = _sage_tools["chain"]
search = _sage_tools["search"]
read_projection = _sage_tools["read_projection"]
read_section = _sage_tools["read_section"]
list_headings = _sage_tools["list_headings"]
recompute_views = _sage_tools["recompute_views"]
list_vaults = _sage_tools["list_vaults"]
create_vault = _sage_tools["create_vault"]
get_vault_config = _sage_tools["get_vault_config"]
update_vault_config = _sage_tools["update_vault_config"]
get_vault_stats = _sage_tools["get_vault_stats"]
verify_hash = _sage_tools["verify_hash"]
list_staging_edges = _sage_tools["list_staging_edges"]
update_staging_edge = _sage_tools["update_staging_edge"]
list_pending_metadata = _sage_tools["list_pending_metadata"]
migrate_vault = _sage_tools["migrate_vault"]
verify_vault_drift = _sage_tools["verify_vault_drift"]
recompute_deferred_vault_abstracts = _sage_tools["recompute_deferred_vault_abstracts"]
optimize_vault_content_store = _sage_tools["optimize_vault_content_store"]
reload_vault = _sage_tools["reload_vault"]
get_stack_config = _sage_tools["get_stack_config"]

list_directory = _app_tools["list_directory"]
bulk_ingest_document = _app_tools["bulk_ingest_document"]

# ---------------------------------------------------------------------------
# Mounting on FastAPI (shared-process mode)
# ---------------------------------------------------------------------------


def mount_on_app(
    app: "FastAPI",  # noqa: F821 -- imported only at call site
    path: str = "/mcp",
) -> None:
    """Mount the MCP server on an existing FastAPI application.

    The caller's lifespan should populate the module-level ``_vaults``
    dict directly (via ``sage.mcp_server._vaults``) so the MCP tools
    and the FastAPI routes share the same SAGEServices instances.
    """
    app.mount(path, mcp.sse_app())


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse
    import os

    parser = argparse.ArgumentParser(
        prog="python -m sage.mcp_server",
        description=(
            "Run the SAGE MCP server over stdio. Vaults are auto-discovered from the vault root."
        ),
    )
    parser.add_argument(
        "--vault-root",
        type=Path,
        default=None,
        help=(
            "Directory containing one subdirectory per vault. "
            "Defaults to $SAGE_VAULT_ROOT, then ~/sage_vaults."
        ),
    )
    args = parser.parse_args()

    if args.vault_root is not None:
        _vault_root = args.vault_root.expanduser()
    elif os.environ.get("SAGE_VAULT_ROOT"):
        _vault_root = Path(os.environ["SAGE_VAULT_ROOT"]).expanduser()
    else:
        _vault_root = Path.home() / "sage_vaults"

    mcp.run(transport="stdio")
