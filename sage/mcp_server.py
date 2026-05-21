"""SAGE MCP server -- thin adapter translating MCP tool calls to Core API.

Supports multiple vaults loaded at startup. Each tool takes vault_id as
its first parameter to select the target vault.

Tools are defined in:
  - sage.sage_api_tools: SAGE protocol and API query tools
  - sage.app_tools: Application backend tools (scan, batch ingest)

Usage:
    python -m sage.mcp_server <config1.yaml> [config2.yaml ...]
"""

import json as _json
import os
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

# Quiet huggingface library noise before any sage import pulls in
# transformers/tokenizers (T-0060). Env vars are read at library import
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
from mcp.types import ContentBlock
from pydantic import TypeAdapter

import sage.app  # noqa: F401 -- import side-effect: installs T-0022 root-logger filter
from sage.api.errors import SAGEError
from sage.app_tools import register_app_tools
from sage.config import load_vault_config
from sage.mcp_init import (
    SAGEServices,
    build_stack_abstraction_provider,
    get_stack_config,
    initialize_services,
    load_stack_config_or_default,
    set_stack_config,
)
from sage.models.schemas import VaultIdStr
from sage.sage_api_tools import register_sage_tools
from sage.services.vault_registry import VaultRegistryService

# Module-scope TypeAdapter for Pattern 2 boundary validation on the
# server-level tool below. See the parallel adapter declarations in
# ``sage/sage_api_tools.py`` and the Typed-Alias Boundary Conventions
# steering document.
_VAULT_ID_ADAPTER: TypeAdapter[str] = TypeAdapter(VaultIdStr)

# ---------------------------------------------------------------------------
# Vault registry
# ---------------------------------------------------------------------------

_vaults: dict[str, SAGEServices] = {}

# Cross-vault registry service singleton.  Constructed once at module import
# against the `_vaults` dict so both the MCP transport (here) and the FastAPI
# transport (sage/app.py reaches into us via get_vault_registry_service())
# share the same instance, which wraps the same registry dict.  See CAS-ADR-013
# for the registry-dict aliasing rationale.
_vault_registry_service: VaultRegistryService = VaultRegistryService(
    registry=_vaults,
    initialize_services=initialize_services,
)


def get_vault_registry_service() -> VaultRegistryService:
    """Return the module-level VaultRegistryService singleton."""
    return _vault_registry_service


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
    # pre-populated by the parent app's lifespan.  Skip init/teardown
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


class _LoggingFastMCP(FastMCP):
    """FastMCP subclass that distinguishes tool outcomes in the console log.

    T-0061: the underlying uvicorn access log shows every tool call as
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
      FastMCP wraps SAGE dict returns into. [T-0064]
    - raised exception → INFO plus one ERROR line
      (`mcp tool failed: <name>`) with traceback, and re-raise.
    """

    async def call_tool(
        self, name: str, arguments: dict[str, Any]
    ) -> Sequence[ContentBlock] | dict[str, Any]:
        logger = _logging.getLogger(__name__)
        logger.info("mcp tool: %s", name)
        try:
            result = await super().call_tool(name, arguments)
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

# ---------------------------------------------------------------------------
# Server-level tools (not SAGE protocol or app tools)
# ---------------------------------------------------------------------------


@mcp.tool()
async def sage_reload_vault(vault_id: str) -> dict:
    """Reload a vault by closing its current services and reinitializing.

    When the vault was originally loaded from a YAML file (the production
    path), the file is re-read from disk so on-disk edits to vault_config.yaml
    take effect. Vaults initialized from an in-memory ``VaultConfig`` (for
    example, in tests that bypass the file system) reuse the existing config.

    Use this after modifying vault_config.yaml on disk, or when external
    database changes (FastAPI server, another MCP client, direct DB writes)
    leave the current MCP session with stale data.

    Args:
        vault_id: Target vault identifier.
    """
    try:
        vault_id = _VAULT_ID_ADAPTER.validate_python(vault_id)
    except (SAGEError, ValueError) as e:
        return _error_response(e)
    if vault_id not in _vaults:
        return _error_response(
            VaultNotFoundError(
                f"Unknown vault_id: {vault_id}. "
                f"Available vaults: {', '.join(sorted(_vaults.keys())) or '(none)'}"
            )
        )

    old_services = _vaults[vault_id]
    config_path = old_services.config_path
    if config_path is not None:
        config = load_vault_config(config_path)
    else:
        config = old_services.config

    # Tear down old services
    await old_services.graph_store.close()

    # Reinitialize, preserving the config_path so a subsequent reload can
    # also re-read from disk, and the content_store_factory so hermetic
    # lifespan tests keep their stub ContentStore across reload. The
    # stack-wide abstraction provider is built once at lifespan startup
    # (CAS-ADR-030) and threaded through here so the reload doesn't
    # construct a second Qwen3 process.
    stack_provider = build_stack_abstraction_provider(get_stack_config())
    new_services = await initialize_services(
        config,
        config_path=config_path,
        content_store_factory=old_services.content_store_factory,
        abstraction_provider=stack_provider,
    )
    _vaults[vault_id] = new_services

    # Return confirmation with basic stats
    total_docs = len(await new_services.graph_store.list_all_documents())
    return {
        "vault_id": vault_id,
        "reloaded": True,
        "document_count": total_docs,
    }


@mcp.tool()
async def sage_get_stack_config() -> dict:
    """Return the SAGE-stack-wide configuration (CAS-ADR-030).

    Stack-wide config governs resources whose enforcement spans the whole
    SAGE process (e.g., the abstraction provider singleton). Per-vault
    knobs live in `sage_get_vault_config`.

    Today the response carries one section, `abstraction`, with:
      - `provider`: dispatch key (`"qwen3-mlx"` or `"stub"`).
      - `model`: the model identifier passed to the provider's factory
        (string, or null when the stack is stub-only).

    The shape is forward-compatible: new top-level sections can be added
    without changing the contract of existing callers.
    """
    cfg = get_stack_config()
    return cfg.model_dump(mode="json")


_sage_tools = register_sage_tools(
    mcp, _get_vault, _serialize, _error_response, _vaults, _vault_registry_service
)
_app_tools = register_app_tools(mcp, _get_vault, _serialize, _error_response)

# ---------------------------------------------------------------------------
# Re-export tool functions for backward-compatible imports
# (e.g. from sage.mcp_server import sage_ingest)
# ---------------------------------------------------------------------------

sage_ingest = _sage_tools["sage_ingest"]
sage_parse_filename = _sage_tools["sage_parse_filename"]
sage_reabstract = _sage_tools["sage_reabstract"]
sage_get_document = _sage_tools["sage_get_document"]
sage_update_metadata = _sage_tools["sage_update_metadata"]
sage_set_lifecycle = _sage_tools["sage_set_lifecycle"]
sage_register_user = _sage_tools["sage_register_user"]
sage_link = _sage_tools["sage_link"]
sage_unlink = _sage_tools["sage_unlink"]
sage_check_preconditions = _sage_tools["sage_check_preconditions"]
sage_traverse = _sage_tools["sage_traverse"]
sage_chain = _sage_tools["sage_chain"]
sage_discover = _sage_tools["sage_discover"]
sage_export_projection = _sage_tools["sage_export_projection"]
sage_read_projection = _sage_tools["sage_read_projection"]
sage_read_section = _sage_tools["sage_read_section"]
sage_list_headings = _sage_tools["sage_list_headings"]
sage_refresh_views = _sage_tools["sage_refresh_views"]
sage_list_vaults = _sage_tools["sage_list_vaults"]
sage_create_vault = _sage_tools["sage_create_vault"]
sage_get_vault_config = _sage_tools["sage_get_vault_config"]
sage_update_vault_config = _sage_tools["sage_update_vault_config"]
sage_vault_stats = _sage_tools["sage_vault_stats"]
sage_hash_check = _sage_tools["sage_hash_check"]
sage_list_staging_edges = _sage_tools["sage_list_staging_edges"]
sage_confirm_staging_edge = _sage_tools["sage_confirm_staging_edge"]
sage_dismiss_staging_edge = _sage_tools["sage_dismiss_staging_edge"]
sage_pending_metadata = _sage_tools["sage_pending_metadata"]
sage_admin_migrate_vault = _sage_tools["sage_admin_migrate_vault"]

app_scan_directory = _app_tools["app_scan_directory"]
app_batch_ingest = _app_tools["app_batch_ingest"]

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
