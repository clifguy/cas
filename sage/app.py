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


async def _initialize_vault(
    app: FastAPI, config: VaultConfig, *, migrate: bool = False, **overrides
) -> None:
    """Initialize services for one vault and add to the registry."""
    services = await initialize_services(config, migrate=migrate, **overrides)
    app.state.vault_registry[config.vault.id] = services


async def _initialize_services(app: FastAPI, config: VaultConfig, **overrides) -> None:
    """Initialize a single vault (backward compat for tests).

    Sets up the vault registry and populates it with one vault,
    then sets legacy single-vault attributes on app.state for
    existing test compatibility.

    Keyword arguments are forwarded to initialize_services() to allow
    provider overrides (content_store, embedding_provider, abstraction_provider).
    """
    if not hasattr(app.state, "vault_registry"):
        app.state.vault_registry = {}

    services = await initialize_services(config, **overrides)
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
    config_path: Path | None = None,
    config: VaultConfig | None = None,
    config_paths: list[Path] | None = None,
    configs: list[VaultConfig] | None = None,
    migrate: bool = False,
) -> FastAPI:
    """Create and configure the SAGE Core API application.

    Args:
        config_path: Path to a single vault YAML config file.
        config: Pre-loaded single VaultConfig (used in testing).
        config_paths: Paths to multiple vault YAML config files.
        configs: Pre-loaded VaultConfig list (used in testing).
        migrate: If True, apply any pending schema migrations on startup.
            Default False; legacy schemas cause startup to fail with
            ``SchemaMigrationRequired`` so the operator can opt in.

        Exactly one of the config arguments must be provided.
    """

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # Use the MCP server's _vaults dict as the canonical registry so
        # both the REST API and MCP SSE transport share the same services.
        from sage.mcp_server import _vaults

        app.state.vault_registry = _vaults

        if config_paths is not None:
            for cp in config_paths:
                vc = load_vault_config(cp)
                await _initialize_vault(app, vc, migrate=migrate)
        elif configs is not None:
            for vc in configs:
                await _initialize_vault(app, vc, migrate=migrate)
        elif config_path is not None:
            vc = load_vault_config(config_path)
            await _initialize_vault(app, vc, migrate=migrate)
        elif config is not None:
            await _initialize_vault(app, config, migrate=migrate)
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
