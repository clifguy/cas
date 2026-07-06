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
from sage.api.errors import _TYPED_ALIAS_CODES, SAGEError, translate_validation_error
from sage.app_tools import register_app_tools
from sage.build_info import SERVER_INSTRUCTIONS, VERSION_WITH_BUILD
from sage.mcp_init import (
    SAGEServices,
    initialize_services,
    load_stack_config_or_default,
    resolve_stack_abstraction_provider,
    resolve_stack_vault_source_store,
    set_stack_config,
    set_vault_root,
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
    elif isinstance(exc, ValidationError) and (
        (sage_err := translate_validation_error(exc)) is not None
        and sage_err.code in _TYPED_ALIAS_CODES
    ):
        # A malformed typed-alias boundary value (vault_id, edge_id, sha256,
        # function_id, document_date, user_id, or document_id) surfaces as its
        # structured invalid_<alias> (400) rather than the generic
        # internal_error. Scoped to the family via _TYPED_ALIAS_CODES so every
        # other ValidationError category -- e.g. the discover/filters
        # mode/unknown-key cases -- keeps its current MCP shape.
        payload = {"error": sage_err.code, "message": sage_err.message}
        if sage_err.detail:
            payload["detail"] = sage_err.detail
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
        # CAS-ADR-042: resolve the active deployment profile once at process
        # startup and share its abstraction binding across every vault that
        # doesn't opt out via vault.abstraction.enabled = False. For the local
        # profile the binding is the stack-wide provider built per CAS-ADR-030.
        stack_cfg = load_stack_config_or_default()
        set_stack_config(stack_cfg)
        # CAS-ADR-043: publish the resolved vault root so the config write paths
        # (create_vault / update_config) resolve the same filesystem binding this
        # discovery uses, rather than falling through to the default root.
        set_vault_root(_vault_root)
        stack_abstraction_provider = resolve_stack_abstraction_provider(stack_cfg)

        # CAS-ADR-043: vault discovery and config load go through the active
        # profile's vault-source store. The filesystem binding (the on-box
        # default) discovers vaults under the resolved root and yields each
        # config's filesystem path, threaded into initialize_services unchanged;
        # a malformed vault is skipped per-vault, the rest still load.
        vault_source_store = resolve_stack_vault_source_store(stack_cfg, vault_root=_vault_root)

        for discovered in vault_source_store.discover():
            try:
                config = vault_source_store.load_config(discovered)
                services = await initialize_services(
                    config,
                    config_path=discovered.config_path,
                    abstraction_provider=stack_abstraction_provider,
                    registry_service=_vault_registry_service,
                )
                _vaults[config.vault.id] = services
            except Exception as exc:
                import logging

                logging.getLogger(__name__).error(
                    "Skipping vault %s: failed to load (%s)",
                    # Pathless bindings discover by id only; name whichever
                    # locator this one carries.
                    discovered.vault_id or discovered.config_path,
                    exc,
                )

        # Re-derive abstraction work left pending by a prior crash or a stopped
        # worker across every registered vault. The in-memory queue is not
        # itself durable; pipeline_status in the graph store is the durable
        # record the worker reconstructs from. Best-effort per vault.
        for vault_id, services in list(_vaults.items()):
            try:
                recovered = await services.ingestion_service.recover_incomplete_documents()
                if recovered:
                    _logging.getLogger(__name__).info(
                        "Recovered %d incomplete document(s) for vault %s",
                        recovered,
                        vault_id,
                    )
            except Exception:
                _logging.getLogger(__name__).exception(
                    "Abstraction recovery failed for vault %s", vault_id
                )

    yield

    if standalone:
        for services in _vaults.values():
            await services.ingestion_service.stop_worker()
            services.close_timing()
            await services.close_storage()
        _vaults.clear()
        set_stack_config(None)
        set_vault_root(None)


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
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        # Disable the SDK's loopback-host DNS-rebinding allow-list on every SAGE
        # MCP server (see _MCP_TRANSPORT_SECURITY) unless a caller pins its own
        # transport security. Covers both HTTP-mounted partitions and the
        # standalone full-surface mount; stdio carries no Host header.
        kwargs.setdefault("transport_security", _MCP_TRANSPORT_SECURITY)
        super().__init__(*args, **kwargs)

    async def call_tool(
        self, name: str, arguments: dict[str, Any]
    ) -> Sequence[ContentBlock] | dict[str, Any]:
        logger = _logging.getLogger(__name__)
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
recompute_views = _sage_tools["admin_recompute_views"]
list_vaults = _sage_tools["admin_list_vaults"]
create_vault = _sage_tools["admin_create_vault"]
get_vault_config = _sage_tools["admin_get_vault_config"]
update_vault_config = _sage_tools["admin_update_vault_config"]
get_vault_stats = _sage_tools["admin_get_vault_stats"]
verify_hash = _sage_tools["verify_hashes"]
list_staging_edges = _sage_tools["list_staging_edges"]
update_staging_edge = _sage_tools["update_staging_edge"]
list_pending_metadata = _sage_tools["list_pending_metadata"]
migrate_vault = _sage_tools["admin_migrate_vault"]
verify_vault_drift = _sage_tools["admin_verify_vault_drift"]
verify_vault_source_files = _sage_tools["admin_verify_vault_source_files"]
recompute_deferred_vault_abstracts = _sage_tools["admin_recompute_deferred_vault_abstracts"]
optimize_vault_content_store = _sage_tools["admin_optimize_vault_content_store"]
reload_vault = _sage_tools["admin_reload_vault"]
get_stack_config = _sage_tools["admin_get_stack_config"]

list_directory = _app_tools["list_directory"]
bulk_ingest_document = _app_tools["bulk_ingest_document"]

# ---------------------------------------------------------------------------
# Surface partition (CAS-ADR-034 / CAS-ADR-029)
# ---------------------------------------------------------------------------
#
# The SAGE MCP surface is split into two: ``sage`` (the ordinary surface,
# always enabled) and ``sage_admin`` (the maintenance surface, opt-in
# additive). Per CAS-ADR-034 v7 the partition is realized over both
# transports — two stdio servers, and two Streamable HTTP mounts on the
# SAGE app (``/mcp`` = ordinary, ``/mcp_admin`` = maintenance; see
# ``sage/app.py``). Server/mount selection *is* the role declaration.
# Surface assignment is derived purely from each tool name's first segment
# per CAS-ADR-029's prefix-encodes-surface rule — there is no per-tool
# override table. The module-level ``mcp`` above remains the full,
# unpartitioned surface whose tool functions are re-exported for direct
# import; the partitioned servers are built by ``build_partitioned_server``
# below.


def _surface_of(tool_name: str) -> str:
    """Return the MCP server a tool registers on, derived from its name.

    Per CAS-ADR-029's prefix-encodes-surface rule: a tool whose name's first
    segment is ``admin_`` belongs to the maintenance server (``sage_admin``);
    every other tool belongs to the ordinary server (``sage``).
    """
    return "sage_admin" if tool_name.startswith("admin_") else "sage"


def build_partitioned_server(surface: str) -> _LoggingFastMCP:
    """Build an MCP server registering only the tools for ``surface``.

    Both servers share the underlying tool-implementation layer
    (``register_sage_tools`` / ``register_app_tools``). The partition is
    applied after registration by dropping every tool whose ``_surface_of``
    does not match ``surface``, so registration is purely prefix-derived
    (CAS-ADR-029) with no per-tool override table. The maintenance server
    therefore does not duplicate the shared read spine, which carries no
    ``admin_`` prefix and so resolves to ``sage``.

    The server carries the Streamable HTTP transport settings its HTTP
    mounting requires; stdio use never reads them. ``stateless_http=True``
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
        if _surface_of(name) != surface:
            server.remove_tool(name)
    return server


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def run_stdio(surface: str, prog: str = "python -m sage.mcp_server") -> None:
    """Run a partitioned SAGE MCP server over stdio.

    ``surface`` selects which tools register: ``"sage"`` for the ordinary
    surface, ``"sage_admin"`` for the maintenance surface. Assignment is
    derived from each tool name's first segment per CAS-ADR-029's
    prefix-encodes-surface rule (see ``build_partitioned_server``). Vaults
    are auto-discovered from the vault root, resolved from ``--vault-root``,
    then ``$SAGE_VAULT_ROOT``, then ``~/sage_vaults``.

    ``prog`` sets the argparse program name so each entry module reports its
    own ``python -m ...`` invocation in ``--help``.
    """
    global _vault_root
    import argparse

    parser = argparse.ArgumentParser(
        prog=prog,
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

    build_partitioned_server(surface).run(transport="stdio")


if __name__ == "__main__":
    run_stdio("sage", prog="python -m sage.mcp_server")
