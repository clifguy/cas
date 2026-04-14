"""SAGE MCP server -- thin adapter translating MCP tool calls to Core API.

Supports multiple vaults loaded at startup. Each tool takes vault_id as
its first parameter to select the target vault.

Tools are defined in:
  - sage.sage_api_tools: SAGE protocol and API query tools
  - sage.app_tools: Application backend tools (scan, batch ingest)

Usage:
    python -m sage.mcp_server <config1.yaml> [config2.yaml ...]
"""

import json
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from mcp.server.fastmcp import FastMCP

from sage.api.errors import SAGEError
from sage.app_tools import register_app_tools
from sage.config import load_vault_config
from sage.mcp_init import SAGEServices, initialize_services
from sage.sage_api_tools import register_sage_tools

# ---------------------------------------------------------------------------
# Vault registry
# ---------------------------------------------------------------------------

_vaults: dict[str, SAGEServices] = {}


def _get_vault(vault_id: str) -> SAGEServices:
    """Look up services for a vault. Raises ValueError if unknown."""
    if vault_id not in _vaults:
        available = ", ".join(sorted(_vaults.keys())) or "(none)"
        raise ValueError(
            f"Unknown vault_id: {vault_id}. Available vaults: {available}"
        )
    return _vaults[vault_id]


def _serialize(obj: object) -> str:
    """Serialize a Pydantic model or dict to JSON string for MCP response."""
    if hasattr(obj, "model_dump"):
        return json.dumps(obj.model_dump(mode="json", exclude_none=True), indent=2)
    return json.dumps(obj, indent=2, default=str)


def _error_response(exc: SAGEError | ValueError) -> str:
    """Format a SAGE or vault-routing error as a JSON string for MCP response."""
    if isinstance(exc, SAGEError):
        payload: dict = {"error": exc.code, "message": exc.message}
        if exc.detail:
            payload["detail"] = exc.detail
    else:
        payload = {"error": "unknown_vault", "message": str(exc)}
    return json.dumps(payload, indent=2)


# ---------------------------------------------------------------------------
# Lifespan: load vault configs and initialize services
# ---------------------------------------------------------------------------

_config_paths: list[Path] = []


@asynccontextmanager
async def _lifespan(server: FastMCP) -> AsyncIterator[None]:
    for config_path in _config_paths:
        config = load_vault_config(config_path)
        services = await initialize_services(config)
        _vaults[config.vault.id] = services

    yield

    for services in _vaults.values():
        await services.graph_store.close()
    _vaults.clear()


mcp = FastMCP("SAGE", lifespan=_lifespan)

# ---------------------------------------------------------------------------
# Register tools from submodules
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Server-level tools (not SAGE protocol or app tools)
# ---------------------------------------------------------------------------


@mcp.tool()
async def sage_reload_vault(vault_id: str) -> str:
    """Reload a vault by closing its current services and reinitializing from
    the same configuration. Use this when vault databases have been modified
    externally (e.g. by the FastAPI server, another MCP client, or direct
    database operations) and the current MCP session is seeing stale data.

    Args:
        vault_id: Target vault identifier.
    """
    if vault_id not in _vaults:
        return _error_response(
            ValueError(
                f"Unknown vault_id: {vault_id}. "
                f"Available vaults: {', '.join(sorted(_vaults.keys())) or '(none)'}"
            )
        )

    old_services = _vaults[vault_id]
    config = old_services.config

    # Tear down old services
    await old_services.graph_store.close()

    # Reinitialize from the same config
    new_services = await initialize_services(config)
    _vaults[vault_id] = new_services

    # Return confirmation with basic stats
    total_docs = len(await new_services.graph_store.list_all_documents())
    return json.dumps(
        {
            "vault_id": vault_id,
            "reloaded": True,
            "document_count": total_docs,
        },
        indent=2,
    )


_sage_tools = register_sage_tools(mcp, _get_vault, _serialize, _error_response, _vaults)
_app_tools = register_app_tools(mcp, _get_vault, _serialize, _error_response)

# ---------------------------------------------------------------------------
# Re-export tool functions for backward-compatible imports
# (e.g. from sage.mcp_server import sage_ingest)
# ---------------------------------------------------------------------------

sage_ingest = _sage_tools["sage_ingest"]
sage_reabstract = _sage_tools["sage_reabstract"]
sage_get_document = _sage_tools["sage_get_document"]
sage_update_metadata = _sage_tools["sage_update_metadata"]
sage_set_lifecycle = _sage_tools["sage_set_lifecycle"]
sage_register_user = _sage_tools["sage_register_user"]
sage_link = _sage_tools["sage_link"]
sage_check_preconditions = _sage_tools["sage_check_preconditions"]
sage_traverse = _sage_tools["sage_traverse"]
sage_discover = _sage_tools["sage_discover"]
sage_export_projection = _sage_tools["sage_export_projection"]
sage_refresh_views = _sage_tools["sage_refresh_views"]
sage_list_vaults = _sage_tools["sage_list_vaults"]
sage_vault_stats = _sage_tools["sage_vault_stats"]
sage_hash_check = _sage_tools["sage_hash_check"]
sage_list_staging_edges = _sage_tools["sage_list_staging_edges"]
sage_confirm_staging_edge = _sage_tools["sage_confirm_staging_edge"]
sage_dismiss_staging_edge = _sage_tools["sage_dismiss_staging_edge"]
sage_pending_metadata = _sage_tools["sage_pending_metadata"]

app_scan_directory = _app_tools["app_scan_directory"]
app_batch_ingest = _app_tools["app_batch_ingest"]

# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python -m sage.mcp_server <config1.yaml> [config2.yaml ...]")
        sys.exit(1)

    for arg in sys.argv[1:]:
        path = Path(arg)
        if not path.exists():
            print(f"Config file not found: {path}")
            sys.exit(1)
        _config_paths.append(path)

    mcp.run(transport="stdio")
