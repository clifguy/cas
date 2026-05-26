"""FastAPI application factory for the SAGE Core API.

Supports multi-vault operation: each vault gets its own SAGEServices
instance, stored in a registry keyed by vault_id. The vault listing
endpoint operates across all vaults; all other endpoints resolve the
correct services via the vault_id path parameter.
"""

import logging
from collections.abc import Callable
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import parse_qs

from anyio import ClosedResourceError
from fastapi import FastAPI
from starlette.types import ASGIApp, Receive, Scope, Send

from app.backend.router import router as app_backend_router
from sage.adapters.interfaces import ContentStore
from sage.api.errors import register_exception_handlers
from sage.api.routers import (
    documents,
    filename_parser,
    graph_ops,
    ingestion,
    lifecycle,
    maintenance,
    metadata,
    pending_metadata,
    retrieval,
    staging_edges,
    users,
    utilities,
    vaults,
)
from sage.config import VaultConfig, load_vault_config
from sage.mcp_init import initialize_services
from sage.services.vault_registry import VaultRegistryService

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# ASGI middleware: suppress double-response errors during shutdown
# ---------------------------------------------------------------------------


def _extract_session_id(scope: Scope) -> str | None:
    """Best-effort pull of the SSE ``session_id`` query parameter from an ASGI scope."""
    raw = scope.get("query_string", b"")
    if not raw:
        return None
    try:
        params = parse_qs(raw.decode("ascii"))
    except UnicodeDecodeError:
        return None
    values = params.get("session_id")
    if not values:
        return None
    return values[0]


class _GracefulSSEMiddleware:
    """Quiet two SSE transport edge cases that surface as noisy tracebacks.

    1. **Server shutdown.** When uvicorn cancels an active SSE connection,
       the SSE transport has already sent response headers; the Starlette
       exception-handling middleware then tries to send an error response,
       producing a second ``http.response.start`` that uvicorn rejects with
       a ``RuntimeError``. This wrapper silently drops the redundant start
       message so the shutdown traceback is avoided.

    2. **Client cancellation.** When an MCP client cancels a long-running
       ``CallToolRequest`` (default 60s client timeout), the mcp SDK's SSE
       writer raises ``anyio.ClosedResourceError`` from
       ``mcp/server/sse.py`` when it tries to send the deferred response
       to the now-closed memory stream. The cancellation is a normal
       outcome, so we log one INFO line and swallow the exception rather
       than emitting an ERROR-level ASGI traceback.
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
        except ClosedResourceError:
            session_id = _extract_session_id(scope)
            if session_id:
                logger.info(
                    "SSE writer closed by client cancellation (session_id=%s)",
                    session_id,
                )
            else:
                logger.info("SSE writer closed by client cancellation")
        except RuntimeError as exc:
            if "http.response.start" not in str(exc):
                raise
            logger.debug("Suppressed SSE shutdown RuntimeError: %s", exc)


# ---------------------------------------------------------------------------
# Root-logger filter: suppress cosmetic notification-validation WARNING
# emitted by mcp.shared.session when a client cancels a long tool call.
# ---------------------------------------------------------------------------


class _CancelledNotificationValidationFilter(logging.Filter):
    """Suppress the cosmetic ``Failed to validate notification`` WARNING
    emitted by ``mcp.shared.session`` when an MCP client cancels a long
    ``CallToolRequest``.

    The emission site (``mcp/shared/session.py:430``) uses
    ``logging.warning(...)`` against the root logger, so the filter attaches
    to the root logger rather than to a named ``mcp.shared.session`` logger.
    Match is narrowed to records that begin with the validation prefix and
    carry both a ``notifications/cancelled`` body and an ``McpError:``
    reason. Unrelated notification-validation warnings pass through.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        if record.levelno != logging.WARNING:
            return True
        msg = record.getMessage()
        if not msg.startswith("Failed to validate notification:"):
            return True
        return not ("notifications/cancelled" in msg and "McpError:" in msg)


def _install_cancelled_notification_filter() -> None:
    """Attach the filter to the root logger if not already present."""
    root = logging.getLogger()
    if any(isinstance(f, _CancelledNotificationValidationFilter) for f in root.filters):
        return
    root.addFilter(_CancelledNotificationValidationFilter())


_install_cancelled_notification_filter()


def _ensure_registry_service(app: FastAPI) -> VaultRegistryService:
    """Attach the canonical VaultRegistryService singleton to app.state.

    The singleton is owned by sage.mcp_server (it wraps the same `_vaults`
    dict aliased onto app.state.vault_registry per CAS-ADR-013), so the
    MCP and REST transports operate against the same instance. This
    function exists to populate app.state for legacy test fixtures that
    bypass the lifespan and call ``_initialize_services`` directly.
    """
    from sage.mcp_server import _vaults, get_vault_registry_service

    if not hasattr(app.state, "vault_registry"):
        app.state.vault_registry = _vaults
    if not hasattr(app.state, "vault_registry_service"):
        app.state.vault_registry_service = get_vault_registry_service()
    return app.state.vault_registry_service


async def _initialize_vault(
    app: FastAPI,
    config: VaultConfig,
    config_path: Path | None = None,
    **overrides,
) -> None:
    """Initialize services for one vault and add to the registry.

    Schema migrations are no longer applied here. Run ``python -m sage.migrate``
    out of band before starting the server when a vault's schema has advanced.
    """
    registry_service = _ensure_registry_service(app)
    services = await initialize_services(
        config,
        config_path=config_path,
        registry_service=registry_service,
        **overrides,
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
    services = await initialize_services(config, registry_service=registry_service, **overrides)
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
    *,
    content_store_factory: Callable[[Path], ContentStore] | None = None,
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
        content_store_factory: Test-only hook. When provided, the lifespan
            invokes the callable with each vault's ``brain_root`` to build
            that vault's ``ContentStore`` instead of constructing a
            ``LanceDBContentStore``. Forwarded to ``initialize_services``
            via ``_initialize_vault`` and persisted on ``SAGEServices`` so
            ``sage_reload_vault`` reuses the same stub on disk-driven
            reload. Sibling embedding and abstraction stubs are gated by
            ``SAGE_TEST_STUB_PROVIDERS=1``; no factory parameters are
            exposed for those.

    Exactly one of ``vault_root``, ``config``, or ``configs`` should be
    provided. None is also valid: the registry stays empty (BE-002).

    Schema migrations are not applied at startup. Use
    ``python -m sage.migrate`` to advance schemas before starting the server.
    """

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # Use the MCP server's _vaults dict as the canonical registry so
        # both the REST API and MCP SSE transport share the same services.
        from sage.mcp_init import (
            build_stack_abstraction_provider,
            load_stack_config_or_default,
            set_stack_config,
        )
        from sage.mcp_server import _vaults
        from sage.vault_discovery import discover_vault_configs

        app.state.vault_registry = _vaults
        # Construct the registry service against the aliased dict so per-vault
        # VaultConfigService instances pick up the same singleton.
        _ensure_registry_service(app)

        # CAS-ADR-030: load the stack-wide config once and build the
        # abstraction provider once; thread it through every per-vault
        # initialize_services call.
        stack_cfg = load_stack_config_or_default()
        set_stack_config(stack_cfg)
        stack_provider = build_stack_abstraction_provider(stack_cfg)

        init_overrides: dict = {"abstraction_provider": stack_provider}
        if content_store_factory is not None:
            init_overrides["content_store_factory"] = content_store_factory

        if vault_root is not None:
            for cp in discover_vault_configs(vault_root):
                try:
                    vc = load_vault_config(cp)
                    await _initialize_vault(app, vc, config_path=cp, **init_overrides)
                except Exception as exc:
                    logger.error("Skipping vault at %s: failed to load (%s)", cp, exc)
        elif configs is not None:
            for vc in configs:
                await _initialize_vault(app, vc, **init_overrides)
        elif config is not None:
            await _initialize_vault(app, config, **init_overrides)
        else:
            # No configs = empty vault registry (valid per BE-002)
            pass

        yield

        for services in app.state.vault_registry.values():
            await services.graph_store.close()
        app.state.vault_registry.clear()
        set_stack_config(None)

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
    app.include_router(maintenance.router, prefix="/sage_vaults/{vault_id}")

    # Application backend endpoints (BE-017 through BE-035)
    app.include_router(app_backend_router)

    # Mount MCP server (SSE transport) for external clients (e.g. Cowork).
    # The SSE app is mounted as a native Starlette sub-application so
    # FastAPI correctly propagates lifespan and request scope.
    # _GracefulSSEMiddleware is added to the Starlette app's own middleware
    # stack rather than wrapping it externally, which would obscure the
    # app type and interfere with MCP session initialization.
    from sage.mcp_server import mcp

    sse_app = mcp.sse_app()
    sse_app.add_middleware(_GracefulSSEMiddleware)
    app.mount("/mcp", sse_app)

    return app
