"""FastAPI dependency factories for the backend-for-frontend auth router.

Auth is gated on configuration presence: when ``app.state.bff_auth`` is unset
-- the on-box profile, or any deployment without identity-provider coordinates
-- every auth dependency raises a structured ``auth_not_configured`` 503, so
the auth routes exist but are inert and the rest of the application backend is
unaffected. The ``get_*_service`` factories are the load-bearing entry points
the router-conformance gate recognizes.
"""

from __future__ import annotations

from fastapi import Request

from app.backend.auth.config import BffAuthContext, BffAuthSettings
from app.backend.auth.oidc import OidcService
from app.backend.auth.session_store import SessionService, SessionStore
from sage.api.errors import SAGEError


def _context(request: Request) -> BffAuthContext:
    """Return the configured auth context, or raise the structured 503."""
    context = getattr(request.app.state, "bff_auth", None)
    if context is None:
        raise SAGEError(
            "auth_not_configured",
            "Interactive sign-in is not configured for this deployment.",
            503,
        )
    return context


def get_auth_settings(request: Request) -> BffAuthSettings:
    """Resolve the active auth settings, or raise ``auth_not_configured``."""
    return _context(request).settings


def get_session_store(request: Request) -> SessionStore:
    """Resolve the externalized session store, or raise ``auth_not_configured``."""
    return _context(request).store


def get_oidc_service(request: Request) -> OidcService:
    """Resolve the OIDC service, or raise ``auth_not_configured``."""
    return _context(request).oidc


def get_session_service(request: Request) -> SessionService:
    """Build the cookie-facing session service, or raise ``auth_not_configured``."""
    context = _context(request)
    return SessionService(store=context.store, settings=context.settings)
