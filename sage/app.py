"""FastAPI application factory for the SAGE Core API.

Supports multi-vault operation: each vault gets its own SAGEServices
instance, stored in a registry keyed by vault_id. The vault listing
endpoint operates across all vaults; all other endpoints resolve the
correct services via the vault_id path parameter.
"""

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from starlette.types import ASGIApp, Receive, Scope, Send

from sage.api.errors import register_exception_handlers
from sage.api.routers import (
    documents,
    filename_parser,
    graph_ops,
    ingestion,
    lifecycle,
    metadata,
    pending_metadata,
    retrieval,
    staging_edges,
    users,
    utilities,
    vaults,
)
from app.backend.router import router as app_backend_router
from sage.config import VaultConfig, load_vault_config
from sage.mcp_init import SAGEServices, initialize_services
from sage.services.vault_registry import VaultRegistryService

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# ASGI middleware: suppress double-response errors during shutdown
# ---------------------------------------------------------------------------

class _GracefulSSEMiddleware:
    """Catch the double ``http.response.start`` that occurs when uvicorn
    cancels an active SSE connection during shutdown.

    The SSE transport has already sent response headers; the Starlette
    exception-handling middleware then tries to send an error response,
    producing a second ``http.response.start`` that uvicorn rejects
    with a ``RuntimeError``.  This wrapper silently drops the redundant
    start message so the shutdown traceback is avoided.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        response_started = False

        async def _safe_send(message: dict) -> None:
            nonlocal response_started
            if message["type"] == "http.response.start":
                if response_started:
                    logger.debug("Suppressed duplicate http.response.start during shutdown")
                    return
                response_started = True
            await send(message)

        try:
            await self.app(scope, receive, _safe_send)
        except RuntimeError as exc:
            if "http.response.start" not in str(exc):
                raise
            logger.debug("Suppressed SSE shutdown RuntimeError: %s", exc)


def _ensure_registry_service(app: FastAPI) -> VaultRegistryService:
    """Construct the singleton VaultRegistryService if not already present.

    The lifespan path constructs it inline; legacy test fixtures that bypass
    the lifespan and call _initialize_services directly need this defensive
    construction so app.state.vault_registry_service is always populated
    before any request reaches a handler that depends on it.
    """
    if not hasattr(app.state, "vault_registry"):
        app.state.vault_registry = {}
    if not hasattr(app.state, "vault_registry_service"):
        app.state.vault_registry_service = VaultRegistryService(
            registry=app.state.vault_registry,
            initialize_services=initialize_services,
        )
    return app.state.vault_registry_service


async def _initialize_vault(
    app: FastAPI, config: VaultConfig, **overrides
) -> None:
    """Initialize services for one vault and add to the registry.

    Schema migrations are no longer applied here. Run ``python -m sage.migrate``
    out of band before starting the server when a vault's schema has advanced.
    """
    registry_service = _ensure_registry_service(app)
    services = await initialize_services(
        config, registry_service=registry_service, **overrides
    )
    app.state.vault_registry[config.vault.id] = services


async def _initialize_services(app: FastAPI, config: VaultConfig, **overrides) -> None:
    """Initialize a single vault (backward compat for tests).

    Sets up the vault registry and populates it with one vault,
    then sets legacy single-vault attributes on app.state for
    existing test compatibility.

    Keyword arguments are forwarded to initialize_services() to allow
    provider overrides (content_store, embedding_provider, abstraction_provider).
    """
    registry_service = _ensure_registry_service(app)
    services = await initialize_services(
        config, registry_service=registry_service, **overrides
    )
    app.state.vault_registry[config.vault.id] = services

    # Legacy single-vault attributes (used by existing tests)
    app.state.config = services.config
    app.state.graph_store = services.graph_store
    app.state.lock_manager = services.lock_manager
    app.state.user_service = services.user_service
    app.state.lifecycle_service = services.lifecycle_service
    app.state.metadata_service = services.metadata_service
    app.state.ingestion_service = services.ingestion_service
    app.state.graph_ops_service = services.graph_ops_service
    app.state.retrieval_service = services.retrieval_service
    app.state.utilities_service = services.utilities_service


def create_app(
    vault_root: Path | None = None,
    config: VaultConfig | None = None,
    configs: list[VaultConfig] | None = None,
) -> FastAPI:
    """Create and configure the SAGE Core API application.

    Args:
        vault_root: Directory containing one subdirectory per vault. Each
            subdirectory must contain a ``vault_config.yaml``. The lifespan
            discovers and initializes every qualifying vault. A vault whose
            config fails to parse or whose services fail to initialize is
            logged and skipped; healthy vaults still load.
        config: Pre-loaded single VaultConfig (used in testing).
        configs: Pre-loaded VaultConfig list (used in testing).

    Exactly one of ``vault_root``, ``config``, or ``configs`` should be
    provided. None is also valid: the registry stays empty (BE-002).

    Schema migrations are not applied at startup. Use
    ``python -m sage.migrate`` to advance schemas before starting the server.
    """

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # Use the MCP server's _vaults dict as the canonical registry so
        # both the REST API and MCP SSE transport share the same services.
        from sage.mcp_server import _vaults
        from sage.vault_discovery import discover_vault_configs

        app.state.vault_registry = _vaults
        # Construct the registry service against the aliased dict so per-vault
        # VaultConfigService instances pick up the same singleton.
        _ensure_registry_service(app)

        if vault_root is not None:
            for cp in discover_vault_configs(vault_root):
                try:
                    vc = load_vault_config(cp)
                    await _initialize_vault(app, vc)
                except Exception as exc:
                    logger.error(
                        "Skipping vault at %s: failed to load (%s)", cp, exc
                    )
        elif configs is not None:
            for vc in configs:
                await _initialize_vault(app, vc)
        elif config is not None:
            await _initialize_vault(app, config)
        else:
            # No configs = empty vault registry (valid per BE-002)
            pass

        yield

        for services in app.state.vault_registry.values():
            await services.graph_store.close()
        app.state.vault_registry.clear()

    app = FastAPI(
        title="SAGE Core API",
        version="0.1.0",
        description="Salience-Aware Graph Engine - Core API",
        lifespan=lifespan,
    )

    register_exception_handlers(app)

    # Cross-vault endpoint (no vault_id prefix)
    app.include_router(vaults.router)

    # Vault-scoped endpoints
    app.include_router(ingestion.router, prefix="/sage_vaults/{vault_id}")
    app.include_router(documents.router, prefix="/sage_vaults/{vault_id}")
    app.include_router(lifecycle.router, prefix="/sage_vaults/{vault_id}")
    app.include_router(metadata.router, prefix="/sage_vaults/{vault_id}")
    app.include_router(users.router, prefix="/sage_vaults/{vault_id}")
    app.include_router(graph_ops.router, prefix="/sage_vaults/{vault_id}")
    app.include_router(retrieval.router, prefix="/sage_vaults/{vault_id}")
    app.include_router(utilities.router, prefix="/sage_vaults/{vault_id}")
    app.include_router(staging_edges.router, prefix="/sage_vaults/{vault_id}")
    app.include_router(pending_metadata.router, prefix="/sage_vaults/{vault_id}")
    app.include_router(filename_parser.router, prefix="/sage_vaults/{vault_id}")

    # Application backend endpoints (BE-017 through BE-035)
    app.include_router(app_backend_router)

    # Mount MCP server (SSE transport) for external clients (e.g. Cowork).
    # The SSE app is mounted as a native Starlette sub-application so
    # FastAPI correctly propagates lifespan and request scope.
    # _GracefulSSEMiddleware is added to the Starlette app's own middleware
    # stack rather than wrapping it externally, which would obscure the
    # app type and interfere with MCP session initialization.
    from starlette.middleware import Middleware
    from sage.mcp_server import mcp
    sse_app = mcp.sse_app()
    sse_app.add_middleware(_GracefulSSEMiddleware)
    app.mount("/mcp", sse_app)

    return app
