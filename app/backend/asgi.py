"""Standalone CAS application-backend ASGI app (CAS-ADR-042).

The hosted profile runs the backend as its own process: this app serves the SPA
same-origin, exposes a liveness probe, and reaches SAGE over HTTP through the
transport seam carrying the user's delegated identity -- the co-located profile
keeps the backend mounted inside the SAGE app instead. It boots with no SAGE in
process: there is no vault registry, so the directory-scan and bulk-ingest
routes (a local-filesystem capability) report that they belong to the
co-located profile rather than failing on the absent registry.

The SPA bundle is mounted last, as a catch-all serving ``index.html`` for
unmatched client routes, so the earlier API and health routes still match
first.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.backend.auth.router import router as auth_router
from app.backend.proxy import router as proxy_router
from app.backend.router import router as app_backend_router
from app.backend.transport import resolve_bff_transport
from sage.api.errors import register_exception_handlers
from sage.build_info import API_VERSION, RELEASE_VERSION
from sage.config import SageCoreConfig

#: Default location of the built SPA bundle, relative to the repository's
#: ``app/`` directory. Overridable via ``create_bff_app(spa_dir=...)`` for
#: tests and for images that stage the bundle elsewhere.
_DEFAULT_SPA_DIR = Path(__file__).resolve().parents[1] / "dist"


async def _assemble_transport(app: FastAPI, stack_cfg: SageCoreConfig) -> None:
    """Assemble the BFF->SAGE transport for the active profile onto app.state.

    Hosted profile: build the on-behalf-of HTTP client from the auth context
    (set by ``_initialize_bff_auth``) plus the configured SAGE base URL. When
    auth is unconfigured, or no SAGE base URL is set, the transport stays
    ``None`` and the proxy answers ``auth_not_configured`` -- the app still
    boots and serves the SPA and health probe. The co-located profile does not
    proxy (SAGE answers its own routes), so it assembles no transport here.
    """
    if stack_cfg.profile != "cloud":
        app.state.sage_transport = None
        return

    bff_auth = getattr(app.state, "bff_auth", None)
    if bff_auth is None or not bff_auth.settings.sage_base_url:
        app.state.sage_transport = None
        return

    from app.backend.auth.sage_client import ObOSageClient

    client = ObOSageClient(bff_auth.settings.sage_base_url, bff_auth.oidc)
    app.state.sage_transport = resolve_bff_transport(stack_cfg, oidc_client=client)


def create_bff_app(
    *,
    spa_dir: Path | None = None,
    stack_config: SageCoreConfig | None = None,
) -> FastAPI:
    """Build the standalone backend-for-frontend ASGI app.

    Args:
        spa_dir: Directory holding the built SPA bundle. Defaults to
            ``app/dist``; the static mount is skipped when the directory is
            absent so the app boots in environments without a built SPA.
        stack_config: Stack configuration override. Defaults to the
            process configuration; tests pass a ``cloud``-profile config to
            exercise the hosted transport.
    """
    from sage.mcp_init import load_stack_config_or_default

    stack_cfg = stack_config if stack_config is not None else load_stack_config_or_default()
    spa = spa_dir if spa_dir is not None else _DEFAULT_SPA_DIR

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        from sage.app import _initialize_bff_auth, _teardown_bff_auth

        await _initialize_bff_auth(app, stack_cfg)
        await _assemble_transport(app, stack_cfg)
        yield
        transport = getattr(app.state, "sage_transport", None)
        aclose = getattr(transport, "aclose", None)
        if aclose is not None:
            await aclose()
        app.state.sage_transport = None
        # Close the BFF session store and release the process-wide async Entra
        # credential (built by _initialize_bff_auth for the session store's
        # managed-identity pool). Delegated to the profile-aware application core
        # so this server entry does not import the cloud managed-identity module
        # directly; a no-op on the local profile.
        await _teardown_bff_auth(app)

    app = FastAPI(
        title="CAS Application Backend",
        version=API_VERSION,
        description="CAS backend-for-frontend: SPA serving and SAGE access.",
        lifespan=lifespan,
    )

    register_exception_handlers(app)

    # Application backend (scan/ingest) and interactive sign-in routers, plus
    # the SAGE reverse proxy. These match before the SPA catch-all below.
    app.include_router(app_backend_router)
    app.include_router(auth_router)
    app.include_router(proxy_router)

    # Operational liveness probe for container health checks: a constant,
    # store-free envelope independent of SAGE reachability. Kept out of the
    # documented OpenAPI surface, mirroring the SAGE app's probe.
    async def _health() -> dict[str, str]:
        return {"status": "ok", "version": RELEASE_VERSION}

    app.add_api_route("/health", _health, methods=["GET"], include_in_schema=False)

    # SPA bundle. Real bundle files (the hashed ``/assets/*`` JS and CSS) are
    # served from a static mount; every other GET path falls back to
    # ``index.html`` so client-side deep links resolve to the SPA shell.
    # Registered last, so the API, auth, proxy, and health routes match first.
    # Skipped when the bundle is absent (e.g. before the SPA is built).
    if spa.is_dir():
        index_html = spa / "index.html"
        assets_dir = spa / "assets"
        if assets_dir.is_dir():
            app.mount("/assets", StaticFiles(directory=str(assets_dir)), name="spa-assets")

        @app.get("/{spa_path:path}", include_in_schema=False)
        async def _serve_spa(spa_path: str) -> FileResponse:
            """Serve a real bundle file when one matches, else the SPA shell."""
            candidate = spa / spa_path
            if spa_path and candidate.is_file():
                return FileResponse(candidate)
            return FileResponse(index_html)

    return app
