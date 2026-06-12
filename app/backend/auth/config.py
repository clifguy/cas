"""Configuration and runtime context for the backend-for-frontend auth flow.

Auth is gated on the *presence* of this configuration. The interactive OIDC
sign-in, the delegated downstream-token acquisition, and the externalized
session store all activate only when the required identity-provider coordinates
are supplied through the environment -- in a hosted deployment, loaded from the
secret store via a managed identity. When the configuration is absent the
application backend runs exactly as before: no auth, no session store, so the
on-box single-process target is unaffected.

The client secret is never read from a configuration file; like the Postgres
password and the hosted-abstraction key, it is sourced only from the
environment.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - import only for type checkers
    from app.backend.auth.oidc import OidcService
    from app.backend.auth.session_store import SessionStore

# Environment variables carrying the identity-provider coordinates.
_TENANT_ENV = "CAS_BFF_TENANT_ID"
_CLIENT_ID_ENV = "CAS_BFF_CLIENT_ID"
_CLIENT_SECRET_ENV = "CAS_BFF_CLIENT_SECRET"  # noqa: S105 -- env-var name, not a secret
_SAGE_APP_ID_URI_ENV = "CAS_BFF_SAGE_APP_ID_URI"
# Optional overrides.
_AUTHORITY_HOST_ENV = "CAS_BFF_AUTHORITY_HOST"
_POST_LOGIN_REDIRECT_ENV = "CAS_BFF_POST_LOGIN_REDIRECT"
_SAGE_BASE_URL_ENV = "CAS_BFF_SAGE_BASE_URL"

# The four coordinates that must all be present for auth to activate.
_REQUIRED = (_TENANT_ENV, _CLIENT_ID_ENV, _CLIENT_SECRET_ENV, _SAGE_APP_ID_URI_ENV)

DEFAULT_AUTHORITY_HOST = "https://login.microsoftonline.com"
CALLBACK_PATH = "/app/auth/callback"
SESSION_COOKIE_NAME = "cas_session"
DEFAULT_POST_LOGIN_REDIRECT = "/"
DEFAULT_SESSION_TTL_SECONDS = 8 * 60 * 60


@dataclass(frozen=True)
class BffAuthSettings:
    """Resolved identity-provider coordinates for the confidential client."""

    tenant_id: str
    client_id: str
    client_secret: str
    sage_app_id_uri: str
    authority_host: str = DEFAULT_AUTHORITY_HOST
    post_login_redirect: str = DEFAULT_POST_LOGIN_REDIRECT
    session_cookie_name: str = SESSION_COOKIE_NAME
    callback_path: str = CALLBACK_PATH
    session_ttl_seconds: int = DEFAULT_SESSION_TTL_SECONDS
    sage_base_url: str | None = None

    @property
    def authority(self) -> str:
        """The issuer authority URL the confidential client validates against."""
        return f"{self.authority_host.rstrip('/')}/{self.tenant_id}"

    @property
    def sage_scope(self) -> str:
        """The delegated access scope exposed by the SAGE resource server."""
        return f"{self.sage_app_id_uri.rstrip('/')}/Sage.Access"


def load_bff_auth_settings(environ: dict[str, str]) -> BffAuthSettings | None:
    """Build settings from the environment, or ``None`` when auth is unconfigured.

    Returns ``None`` unless every required coordinate is present and non-empty.
    A ``None`` result is the signal the rest of the backend reads as "no auth":
    the auth routes stay registered but inert, and no session store is opened.
    """
    if not all(environ.get(key) for key in _REQUIRED):
        return None
    return BffAuthSettings(
        tenant_id=environ[_TENANT_ENV],
        client_id=environ[_CLIENT_ID_ENV],
        client_secret=environ[_CLIENT_SECRET_ENV],
        sage_app_id_uri=environ[_SAGE_APP_ID_URI_ENV],
        authority_host=environ.get(_AUTHORITY_HOST_ENV) or DEFAULT_AUTHORITY_HOST,
        post_login_redirect=environ.get(_POST_LOGIN_REDIRECT_ENV) or DEFAULT_POST_LOGIN_REDIRECT,
        sage_base_url=environ.get(_SAGE_BASE_URL_ENV),
    )


@dataclass(frozen=True)
class BffAuthContext:
    """The assembled auth runtime: resolved settings plus the wired services.

    Stored on ``app.state.bff_auth`` when auth is configured. Its absence
    (treated as ``None`` by the dependency layer) is what makes the auth routes
    answer ``auth_not_configured`` and leaves the rest of the backend untouched.
    """

    settings: BffAuthSettings
    oidc: OidcService
    store: SessionStore
