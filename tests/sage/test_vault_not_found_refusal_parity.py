"""An unregistered vault is refused on the same terms on every surface.

CAS-ADR-052 makes refusal part of the contract: both request surfaces refuse
the same request with the same code, status and detail. A well-formed
``vault_id`` naming no registered vault is refused as ``vault_not_found``
(404) with ``detail`` carrying the requested ``vault_id`` and the sorted
``available_vaults``, so a caller can correct the id without a second call.

- VN-1 drives an MCP tool and its REST operation against one app, whose mounts
  share the process vault registry, and requires the same refusal from each.
- VN-2 sweeps every vault-addressed MCP tool on either mount.
- VN-3 holds each vault-addressed tool's ``Error modes:`` block to the code.
- VN-4 pins the typed error's own shape.
- VN-5 holds every vault-scoped published operation to declaring the refusal.
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any, Final

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from sage import mcp_server
from sage.adapters.stubs import StubContentStore
from sage.api.errors import VaultNotFoundError
from sage.config import VaultConfig
from tests.helpers.docstring_blocks import error_modes_block
from tests.helpers.pipeline_wait import drain_vaults
from tests.helpers.spec_loading import load_yaml
from tests.helpers.vault_addressed import NOT_REGISTRY_RESOLVED

_REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[2]
_SPECS: Final[dict[str, Path]] = {
    "sage_core": _REPO_ROOT / "docs" / "fs" / "sage" / "sage_core_api.openapi.yaml",
    "cas_app": _REPO_ROOT / "docs" / "fs" / "cas_app_api.openapi.yaml",
}
GHOST: Final[str] = "ghost_vault"
_DOCUMENT_ID: Final[str] = "deadbeef_absent_document"
_EDGE_ID: Final[str] = "00000000-0000-4000-8000-000000000000"
_ALIAS_VAULT: Final[str] = "aaa_alias_vault"


@pytest.fixture
async def shared_app(
    minimal_vault_config_dict: dict, tmp_vault_dir: Path
) -> AsyncIterator[tuple[FastAPI, str]]:
    """One app whose MCP mounts and routes share a registry holding two vault ids.

    The second id is an alias of the first vault's services, registered after it
    and sorting before it. A refusal that reports only one registered vault, or
    reports them in registration order, then differs from the expected roster --
    which a registry of one cannot show.
    """
    config = VaultConfig.model_validate(minimal_vault_config_dict)

    from sage.app import _initialize_services, create_app

    app = create_app(config=config)
    registry = mcp_server._vaults
    before = set(registry)
    await _initialize_services(
        app,
        config,
        content_store_factory=lambda _brain: StubContentStore(),
    )
    registry[_ALIAS_VAULT] = registry[config.vault.id]
    try:
        yield app, config.vault.id
    finally:
        try:
            await drain_vaults(registry, set(registry) - before)
        finally:
            registry.pop(_ALIAS_VAULT, None)
            for vault_id in set(registry) - before:
                services = registry.pop(vault_id)
                services.close_timing()
                await services.close_storage()


def _client(app: FastAPI) -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


# ---------------------------------------------------------------------------
# VN-1: paired arms
# ---------------------------------------------------------------------------

# (tool, mount, tool arguments, HTTP method, path, JSON body). One pair per
# route to the refusal: the Core API dependency on a read, a bulk tool that
# validates its items before resolving the vault, the maintenance surface, and
# the application API's own dependency.
_PAIRS: Final[list[tuple[str, str, dict[str, Any], str, str, dict[str, Any] | None]]] = [
    (
        "get_document",
        "/mcp",
        {"document_id": _DOCUMENT_ID},
        "GET",
        f"/sage_vaults/{GHOST}/documents/{_DOCUMENT_ID}",
        None,
    ),
    (
        "create_edges",
        "/mcp",
        {"items": []},
        "POST",
        f"/sage_vaults/{GHOST}/edges",
        {"items": []},
    ),
    (
        "reload_vault",
        "/mcp_maint",
        {},
        "POST",
        f"/sage_vaults/{GHOST}/maintenance/reload",
        None,
    ),
    (
        "bulk_ingest_document",
        "/mcp",
        {"files": []},
        "POST",
        "/app/ingest",
        {"vault_id": GHOST, "files": []},
    ),
]


@pytest.mark.parametrize(
    "tool, mount, arguments, method, path, body",
    _PAIRS,
    ids=[pair[0] for pair in _PAIRS],
)
async def test_unregistered_vault_is_refused_alike_on_both_surfaces(
    shared_app,
    tool_payload: Callable[[object], dict],
    tool: str,
    mount: str,
    arguments: dict[str, Any],
    method: str,
    path: str,
    body: dict[str, Any] | None,
) -> None:
    """VN-1: same code, detail and message from the tool and the operation."""
    app, vault_id = shared_app
    registered = sorted(app.state.vault_registry)
    assert registered == [_ALIAS_VAULT, vault_id]

    via_tool = tool_payload(
        await app.state.mcp_mounts[mount].call_tool(tool, {"vault_id": GHOST, **arguments})
    )
    async with _client(app) as client:
        resp = await client.request(method, path, json=body)

    assert resp.status_code == 404, resp.text
    via_route = resp.json()
    assert via_tool["error"] == via_route["code"] == "vault_not_found"
    expected_detail = {"vault_id": GHOST, "available_vaults": registered}
    assert via_tool["detail"] == expected_detail
    assert via_route["detail"] == expected_detail
    assert via_tool["message"] == via_route["message"]
    assert GHOST in via_route["message"]
    assert vault_id in via_route["message"]


# ---------------------------------------------------------------------------
# VN-2: every vault-addressed MCP tool
# ---------------------------------------------------------------------------


def _vault_addressed_tools() -> dict[str, Callable]:
    """Every registered tool that resolves the ``vault_id`` its input schema carries."""
    tools = mcp_server.mcp._tool_manager._tools  # noqa: SLF001 -- FastMCP exposes no public API
    return {
        name: tool.fn
        for name, tool in sorted(tools.items())
        if "vault_id" in tool.parameters.get("properties", {}) and name not in NOT_REGISTRY_RESOLVED
    }


_VAULT_ADDRESSED: Final[dict[str, Callable]] = _vault_addressed_tools()

# The smallest well-formed arguments that carry each tool past the checks it
# makes before resolving the vault. Keyed by every vault-addressed tool: a tool
# added without a row fails VN-2a rather than escaping the sweep.
_MINIMAL_ARGS: Final[dict[str, dict[str, Any]]] = {
    "bulk_ingest_document": {"files": []},
    "chain": {"document_id": _DOCUMENT_ID, "edge_type": "supersedes"},
    "create_edges": {"items": []},
    "delete_edge": {"edge_id": _EDGE_ID},
    "export_projection": {"document_id": _DOCUMENT_ID, "output_path": "exports/out.md"},
    "get_document": {"document_id": _DOCUMENT_ID},
    "get_filename_metadata": {"filename": "note.md", "source_type": "markdown"},
    "get_vault_config": {},
    "get_vault_stats": {},
    "ingest_document": {"source": "note.md"},
    "list_directory": {"directory": "/nonexistent"},
    "list_headings": {"document_id": _DOCUMENT_ID},
    "list_pending_metadata": {},
    "list_staging_edges": {},
    "migrate_vault": {},
    "optimize_vault_content_store": {},
    "read_projection": {"document_id": _DOCUMENT_ID},
    "read_section": {"document_id": _DOCUMENT_ID, "heading_path": "Intro"},
    "recompute_abstract": {"document_id": _DOCUMENT_ID},
    "recompute_deferred_vault_abstracts": {},
    "recompute_pipeline": {"document_id": _DOCUMENT_ID},
    "recompute_views": {},
    "reload_vault": {},
    "restore_vault_source_file": {"source": "/nonexistent/absent.md"},
    "search": {"query": "anything"},
    "traverse": {"start_id": _DOCUMENT_ID},
    "update_lifecycles": {"items": []},
    "update_metadata": {"items": []},
    "update_staging_edge": {"edge_id": _EDGE_ID, "action": "confirm"},
    "update_vault_config": {},
    "verify_hashes": {"hashes": []},
    "verify_preconditions": {"document_id": _DOCUMENT_ID},
    "verify_vault_drift": {},
    "verify_vault_retrieval": {},
    "verify_vault_source_files": {},
}

# Anti-vacuity floor: 33 vault-addressed tools today.
MIN_VAULT_ADDRESSED_TOOLS: Final[int] = 30


async def _mount_for(app: FastAPI, tool: str) -> Any:
    for path in ("/mcp", "/mcp_maint"):
        mount = app.state.mcp_mounts[path]
        if tool in {t.name for t in await mount.list_tools()}:
            return mount
    raise AssertionError(f"{tool} is advertised on neither mount")


@pytest.mark.parametrize("tool", sorted(_MINIMAL_ARGS))
async def test_every_vault_addressed_tool_refuses_an_unregistered_vault(
    shared_app, tool_payload: Callable[[object], dict], tool: str
) -> None:
    """VN-2: the typed refusal, reached through the mount that advertises the tool."""
    app, vault_id = shared_app
    mount = await _mount_for(app, tool)

    payload = tool_payload(await mount.call_tool(tool, {"vault_id": GHOST, **_MINIMAL_ARGS[tool]}))

    assert payload.get("error") == "vault_not_found", payload
    assert payload["detail"]["vault_id"] == GHOST
    assert vault_id in payload["detail"]["available_vaults"]


async def test_the_sweep_covers_every_vault_addressed_tool_on_the_mounts(shared_app) -> None:
    """VN-2a: the argument table, the registry and the live mounts name the same tools."""
    app, _ = shared_app
    advertised: set[str] = set()
    for path in ("/mcp", "/mcp_maint"):
        for tool in await app.state.mcp_mounts[path].list_tools():
            if "vault_id" in tool.inputSchema.get("properties", {}):
                advertised.add(tool.name)

    # Every exemption still names an advertised vault_id-carrying tool, so a
    # removed or renamed tool cannot leave a stale entry behind.
    assert set(NOT_REGISTRY_RESOLVED) <= advertised
    resolving = advertised - set(NOT_REGISTRY_RESOLVED)
    assert len(resolving) >= MIN_VAULT_ADDRESSED_TOOLS, resolving
    assert set(_MINIMAL_ARGS) == resolving
    assert set(_VAULT_ADDRESSED) == resolving


# ---------------------------------------------------------------------------
# VN-3: the docstring contract
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("tool", sorted(_VAULT_ADDRESSED))
def test_vault_addressed_tool_docstrings_declare_vault_not_found(tool: str) -> None:
    """VN-3: the Error modes block a caller reads names the refusal it will receive."""
    block = error_modes_block(_VAULT_ADDRESSED[tool])

    assert re.search(r"``vault_not_found`` \(404\b", block), (
        f"{tool} resolves a vault_id but its Error modes: block does not declare vault_not_found"
    )
    assert "``unknown_vault``" not in block, f"{tool} still declares the retired unknown_vault"


def test_the_docstring_roster_is_not_vacuous() -> None:
    """VN-3's parametrization enumerates the tools rather than quietly returning nothing."""
    assert len(_VAULT_ADDRESSED) >= MIN_VAULT_ADDRESSED_TOOLS
    assert {"get_document", "reload_vault", "bulk_ingest_document"} <= set(_VAULT_ADDRESSED)


# ---------------------------------------------------------------------------
# VN-4: the typed error
# ---------------------------------------------------------------------------


def test_vault_not_found_error_names_the_registered_vaults() -> None:
    """VN-4: sorted available vaults in detail and message."""
    err = VaultNotFoundError("x", available_vaults=["b", "a"])

    assert err.code == "vault_not_found"
    assert err.status_code == 404
    assert err.detail == {"vault_id": "x", "available_vaults": ["a", "b"]}
    assert err.message == "Vault 'x' not found. Available vaults: a, b"


def test_vault_not_found_error_with_no_registered_vaults() -> None:
    """VN-4: the empty registry is stated, not rendered as a dangling colon."""
    err = VaultNotFoundError("x", available_vaults=[])

    assert err.detail == {"vault_id": "x", "available_vaults": []}
    assert err.message == "Vault 'x' not found. Available vaults: (none)"


# ---------------------------------------------------------------------------
# VN-5: the published operations
# ---------------------------------------------------------------------------

_HTTP_METHODS: Final[frozenset[str]] = frozenset({"get", "post", "put", "patch", "delete"})


def _vault_scoped_operations() -> list[tuple[str, str, dict[str, Any]]]:
    """Every operation that resolves a vault: by path, or by a request-body ``vault_id``."""
    found: list[tuple[str, str, dict[str, Any]]] = []
    for surface, path in _SPECS.items():
        spec = load_yaml(path)
        schemas = (spec.get("components") or {}).get("schemas") or {}
        for route, item in spec["paths"].items():
            for method, op in item.items():
                if method not in _HTTP_METHODS:
                    continue
                body = (
                    ((op.get("requestBody") or {}).get("content") or {})
                    .get("application/json", {})
                    .get("schema", {})
                )
                ref = body.get("$ref", "").rsplit("/", 1)[-1]
                in_body = "vault_id" in (schemas.get(ref, {}).get("properties") or {})
                if "{vault_id}" in route or in_body:
                    found.append((surface, op["operationId"], op))
    return found


_VAULT_SCOPED_OPERATIONS: Final[list[tuple[str, str, dict[str, Any]]]] = _vault_scoped_operations()


@pytest.mark.parametrize(
    "surface, operation_id, operation",
    _VAULT_SCOPED_OPERATIONS,
    ids=[f"{surface}-{op_id}" for surface, op_id, _ in _VAULT_SCOPED_OPERATIONS],
)
def test_vault_scoped_operations_declare_vault_not_found(
    surface: str, operation_id: str, operation: dict[str, Any]
) -> None:
    """VN-5: a caller reading the operation learns the refusal and its detail."""
    description = (operation.get("responses") or {}).get("404", {}).get("description", "")

    assert "`vault_not_found`" in description, (
        f"{surface}.{operation_id} resolves a vault but its 404 does not declare vault_not_found"
    )
    assert "`detail.available_vaults`" in description
    assert "ault not found" not in description.replace("`vault_not_found`", "")


def test_the_operation_roster_is_not_vacuous() -> None:
    """VN-5 enumerates both specifications, body-addressed operations included."""
    names = {(surface, op_id) for surface, op_id, _ in _VAULT_SCOPED_OPERATIONS}
    assert len(names) >= MIN_VAULT_ADDRESSED_TOOLS
    assert {("sage_core", "get_document"), ("cas_app", "bulk_ingest_document")} <= names
