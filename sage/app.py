"""FastAPI application factory for the SAGE Core API.

Supports multi-vault operation: each vault gets its own SAGEServices
instance, stored in a registry keyed by vault_id. The vault listing
endpoint operates across all vaults; all other endpoints resolve the
correct services via the vault_id path parameter.
"""

import logging
import os
import sys
from collections.abc import Callable
from contextlib import AsyncExitStack, asynccontextmanager
from pathlib import Path

from fastapi import FastAPI

from app.backend.auth.router import router as auth_router
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
    transfer,
    users,
    utilities,
    vaults,
)
from sage.auth import AuthMiddleware
from sage.build_info import API_VERSION, BUILD_IDENTITY, RELEASE_VERSION
from sage.capabilities import ocr_capability
from sage.config import SageCoreConfig, VaultConfig
from sage.mcp_init import (
    initialize_services,
    load_stack_config_or_default,
    resolve_stack_auth_validator,
)
from sage.services.vault_registry import VaultRegistryService
from sage.startup_banner import render_startup_banner

logger = logging.getLogger(__name__)


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

    The durable store provisions its schema externally, so there is no
    startup-time migration concern here.
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


#: Canonical HTTP MCP mount points as ``(mount_path, surface)`` pairs.
#: Single source of truth for both the mounter below and the
#: ``uvicorn.access`` suppression filter in ``sage.__main__``:
#: a mount added here is covered by both without a second edit.
MCP_HTTP_MOUNTS: tuple[tuple[str, str], ...] = (("/mcp", "sage"), ("/mcp_admin", "sage_admin"))


def _mount_partitioned_mcp(app: FastAPI) -> None:
    """Serve the ordinary and maintenance MCP surfaces over Streamable HTTP.

    Realizes the CAS-ADR-034 ordinary/maintenance partition over the
    Streamable HTTP transport: ``/mcp`` carries the ``sage`` (ordinary)
    roster and ``/mcp_admin`` the ``sage_admin`` (``admin_*``) roster. Both
    are built by ``build_partitioned_server`` and run in this one uvicorn
    process, sharing the app-populated ``_vaults`` registry and the single
    stack abstraction provider (CAS-ADR-030) — partitioning the transport
    adds no per-mount vault re-initialization and no second
    abstraction-model load. The full surface is reached by connecting to
    both mounts.

    A standards MCP client POSTs its JSON-RPC directly to the mount URL —
    the byte-exact, slash-less path the edge's protected-resource metadata
    advertises as the OAuth resource. A Starlette ``Mount`` can never match
    that exact path (its regex requires the trailing slash), so mounting a
    sub-application would answer ``307`` via the parent router's
    ``redirect_slashes`` — a redirect MCP clients do not follow on POST.
    Each transport therefore hangs off an exact-path ``Route``: the server's
    ``streamable_http_path`` is set to the full mount path and the routes of
    the SDK-built transport app are appended to the parent router. The
    session manager each route requires is started by the app lifespan (a
    sub-application's own lifespan never runs under FastAPI). The
    partitioned server for each path is recorded on ``app.state.mcp_mounts``
    (mirroring ``app.state.vault_registry``) so the wiring is inspectable.
    """
    from sage.mcp_server import build_partitioned_server

    mounts: dict[str, object] = {}
    for path, surface in MCP_HTTP_MOUNTS:
        server = build_partitioned_server(surface)
        server.settings.streamable_http_path = path
        app.router.routes.extend(server.streamable_http_app().routes)
        mounts[path] = server
    app.state.mcp_mounts = mounts


async def _initialize_bff_auth(app: FastAPI, stack_cfg: object) -> None:
    """Assemble the backend-for-frontend auth context onto ``app.state``.

    Sets ``app.state.bff_auth`` to a ``BffAuthContext`` when the identity-
    provider coordinates are present in the environment, else ``None``. When
    configured, the externalized session store reuses the stack's Postgres
    endpoint (its own schema), reached over the same libpq connection
    composition the storage engine uses. In the cloud profile the pool
    authenticates via a managed-identity Entra token; in the local profile
    the credential is read from the environment.
    """
    from app.backend.auth.config import BffAuthContext, load_bff_auth_settings

    settings = load_bff_auth_settings(dict(os.environ))
    if settings is None:
        app.state.bff_auth = None
        return

    from psycopg.conninfo import make_conninfo

    from app.backend.auth.oidc import MsalOidcService
    from app.backend.auth.session_store import PostgresSessionStore
    from sage import profiles
    from sage.storage.postgres.pool import PostgresConnectionParams, build_conn_kwargs

    pg = stack_cfg.postgres
    params = PostgresConnectionParams(
        host=pg.host,
        port=pg.port,
        database=pg.database,
        user=pg.user,
        sslmode=pg.sslmode,
    )

    connection_class = None
    conn_environ: dict[str, str] | None = None
    if stack_cfg.profile == profiles.CLOUD_PROFILE:
        from sage.storage.postgres.managed_identity import (
            get_postgres_credential,
            make_token_auth_connection_class,
        )

        connection_class = make_token_auth_connection_class(get_postgres_credential())
        conn_environ = {}

    store = PostgresSessionStore(
        make_conninfo(**build_conn_kwargs(params, conn_environ)),
        connection_class=connection_class,
    )
    await store.open()
    app.state.bff_auth = BffAuthContext(
        settings=settings,
        oidc=MsalOidcService(settings),
        store=store,
    )


def create_app(
    vault_root: Path | None = None,
    config: VaultConfig | None = None,
    configs: list[VaultConfig] | None = None,
    *,
    content_store_factory: Callable[[Path], ContentStore] | None = None,
    stack_config: SageCoreConfig | None = None,
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
            that vault's ``ContentStore`` instead of constructing the default
            Postgres content store. Forwarded to ``initialize_services``
            via ``_initialize_vault`` and persisted on ``SAGEServices`` so
            ``reload_vault`` reuses the same stub on disk-driven
            reload. Sibling embedding and abstraction stubs are gated by
            ``SAGE_TEST_STUB_PROVIDERS=1``; no factory parameters are
            exposed for those.

    Exactly one of ``vault_root``, ``config``, or ``configs`` should be
    provided. None is also valid: the registry stays empty (BE-002).

    ``stack_config`` overrides the stack-wide configuration the process
    would otherwise load from ``sage/config.yaml`` (or ``$SAGE_CONFIG_PATH``).
    It selects the deployment profile's bindings, including the auth
    validator the request middleware enforces; tests pass it to exercise the
    enabled-auth path without a config file on disk.

    The durable store provisions its schema externally, so there is no
    startup-time migration concern.
    """

    # Resolve the stack config once. The auth middleware (added at
    # construction, below) and the lifespan's profile-binding resolution both
    # read it, so load it here and let the lifespan reuse it rather than read
    # it a second time.
    stack_cfg = stack_config if stack_config is not None else load_stack_config_or_default()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # Use the MCP server's _vaults dict as the canonical registry so
        # both the REST API and MCP HTTP transport share the same services.
        from sage.mcp_init import (
            resolve_stack_abstraction_provider,
            resolve_stack_vault_source_store,
            set_stack_config,
            set_vault_root,
        )
        from sage.mcp_server import _vaults

        app.state.vault_registry = _vaults
        # Construct the registry service against the aliased dict so per-vault
        # VaultConfigService instances pick up the same singleton.
        _ensure_registry_service(app)

        # Start each MCP mount's Streamable HTTP session manager. The
        # transport routes were appended at construction time, but their
        # task groups only exist inside session_manager.run() — and FastAPI
        # never runs a sub-application's lifespan, so the parent lifespan
        # owns them. Teardown order matters: the stack is closed in the
        # finally block BEFORE vault storage closes, so an in-flight MCP
        # task is cancelled while the services it holds are still alive.
        mcp_session_stack = AsyncExitStack()
        for server in getattr(app.state, "mcp_mounts", {}).values():
            await mcp_session_stack.enter_async_context(server.session_manager.run())
        try:
            # CAS-ADR-042: publish the stack-wide config resolved at construction
            # and resolve the active deployment profile's abstraction binding once;
            # thread it through every per-vault initialize_services call. For the
            # local profile the binding is the stack-wide provider built per
            # CAS-ADR-030.
            set_stack_config(stack_cfg)
            # CAS-ADR-043: publish the resolved vault root so the config write
            # paths (create_vault / update_config) resolve the same filesystem
            # binding discovery uses, rather than the default root. None in the
            # injected-config branches (config=/configs=) is a no-op: the write
            # paths then fall through to the profile seam, unchanged.
            set_vault_root(vault_root)
            stack_provider = resolve_stack_abstraction_provider(stack_cfg)

            init_overrides: dict = {"abstraction_provider": stack_provider}
            if content_store_factory is not None:
                init_overrides["content_store_factory"] = content_store_factory

            # Backend-for-frontend auth, profile-gated on configuration presence: the
            # interactive sign-in, the delegated downstream-token acquisition, and the
            # externalized session store activate only when the identity-provider
            # coordinates are supplied through the environment. When absent, the
            # backend runs without auth and the on-box profile is unaffected.
            await _initialize_bff_auth(app, stack_cfg)

            # Vaults that fail to load are logged-and-dropped below; collect them
            # here too so the end-of-startup banner can report the skipped set.
            skipped_vaults: list[tuple[str, str]] = []

            if vault_root is not None:
                # CAS-ADR-043: discovery and config load go through the active
                # profile's vault-source store. The filesystem binding yields each
                # config's filesystem path, threaded into initialize_services
                # unchanged; a malformed vault is logged-and-dropped per-vault.
                vault_source_store = resolve_stack_vault_source_store(
                    stack_cfg, vault_root=vault_root
                )
                for discovered in vault_source_store.discover():
                    try:
                        vc = vault_source_store.load_config(discovered)
                        await _initialize_vault(
                            app, vc, config_path=discovered.config_path, **init_overrides
                        )
                    except Exception as exc:
                        logger.error(
                            "Skipping vault at %s: failed to load (%s)", discovered.config_path, exc
                        )
                        skipped_vaults.append(
                            (str(discovered.config_path), f"{type(exc).__name__}: {exc}")
                        )
            elif configs is not None:
                for vc in configs:
                    await _initialize_vault(app, vc, **init_overrides)
            elif config is not None:
                await _initialize_vault(app, config, **init_overrides)
            else:
                # No configs = empty vault registry (valid per BE-002)
                pass

            # Re-derive abstraction work left pending by a prior crash or a stopped
            # worker, across every registered vault. The in-memory queue is not
            # itself durable; pipeline_status in the graph store is the durable
            # record the worker reconstructs from. Best-effort per vault.
            for vault_id, services in list(app.state.vault_registry.items()):
                try:
                    recovered = await services.ingestion_service.recover_incomplete_documents()
                    if recovered:
                        logger.info(
                            "Recovered %d incomplete document(s) for vault %s", recovered, vault_id
                        )
                except Exception:
                    logger.exception("Abstraction recovery failed for vault %s", vault_id)

            logger.info(
                "%s",
                render_startup_banner(
                    build_identity=BUILD_IDENTITY,
                    version=RELEASE_VERSION,
                    python_version=sys.version.split()[0],
                    pid=os.getpid(),
                    vault_root=vault_root,
                    loaded_vault_ids=sorted(app.state.vault_registry),
                    skipped_vaults=skipped_vaults,
                    mcp_mounts=sorted(getattr(app.state, "mcp_mounts", {})),
                ),
            )

            yield

        finally:
            # Unwind the MCP session managers FIRST: an in-flight MCP task is
            # cancelled while the vault services it holds are still open.
            await mcp_session_stack.aclose()
            for services in app.state.vault_registry.values():
                await services.ingestion_service.stop_worker()
                services.close_timing()
                await services.close_storage()
            app.state.vault_registry.clear()
            bff_auth = getattr(app.state, "bff_auth", None)
            if bff_auth is not None:
                await bff_auth.store.close()
            app.state.bff_auth = None
            set_stack_config(None)
            set_vault_root(None)

    app = FastAPI(
        title="SAGE Core API",
        version=API_VERSION,
        description="Salience-Aware Graph Engine - Core API",
        lifespan=lifespan,
    )

    register_exception_handlers(app)

    # Cross-vault endpoints (no vault_id prefix). The transfer routes are
    # process-scoped byte legs: the vault binding travels inside the token.
    app.include_router(vaults.router)
    app.include_router(transfer.router)

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

    # Backend-for-frontend interactive sign-in. The routes are always
    # registered; their behavior is gated at request time on whether auth is
    # configured (app.state.bff_auth), so the on-box profile exposes the routes
    # but answers auth_not_configured.
    app.include_router(auth_router)

    # Operational liveness probe for container health checks. A direct app
    # route (not a service-backed router) returning a constant, store-free
    # envelope, so it answers 'process up' independent of vault/store
    # readiness and answers before vaults finish loading. Kept out of the
    # documented OpenAPI surface via include_in_schema=False; the conformance
    # gate's _INFRA_PATHS allowlist covers the route.
    #
    # The envelope also advertises the OCR toolchain's runtime availability so
    # an out-of-container probe (the cloud preflight, over public HTTPS) can
    # catch an image built without it. The capability is probed once here, at
    # app construction, and captured in the closure, so the probe stays store-
    # free and O(1) per request (no re-probe, no import cost on the hot path).
    ocr_caps = ocr_capability()

    async def _health() -> dict[str, object]:
        return {"status": "ok", "version": RELEASE_VERSION, "ocr": ocr_caps}

    app.add_api_route("/health", _health, methods=["GET"], include_in_schema=False)

    # Serve the partitioned MCP surfaces (Streamable HTTP transport) for
    # external clients. Per CAS-ADR-034: /mcp = ordinary, /mcp_admin =
    # maintenance. Full surface = connect to both.
    _mount_partitioned_mcp(app)

    # CAS-ADR-042: enforce the deployment profile's bearer-token validator
    # across every HTTP surface. A pass-through validator under the on-box
    # default (no auth); an Entra JWT validator under a profile that
    # authenticates callers. One pure-ASGI middleware on the parent app guards
    # the REST routes and the mounted MCP sub-apps identically, so
    # authorization is uniform regardless of surface; the liveness probe is
    # exempt so an orchestrator can poll it without a token.
    app.add_middleware(
        AuthMiddleware,
        validator=resolve_stack_auth_validator(stack_cfg),
        exempt_paths=frozenset({"/health", "/upload"}),
        exempt_prefixes=frozenset({"/download/"}),
    )

    return app
