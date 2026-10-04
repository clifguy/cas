"""OIDC sign-in and delegated downstream-token acquisition via MSAL.

Wraps a Microsoft Authentication Library confidential-client application. The
backend-for-frontend runs the interactive authorization-code flow itself,
holding the client credential server-side, then -- for each call it makes to
SAGE on the user's behalf -- acquires a SAGE-audienced token that carries the
*delegated* user identity. SAGE is never called as a service principal. The
token cache is serializable, so it round-trips through the externalized session
store and any replica can refresh the downstream token.

The flow correctness (PKCE, state, nonce) is the library's responsibility; this
module owns only the wiring and the serialized-cache round-trip.

The library's calls block on network I/O, so callers on an event loop run them
in a worker thread. Each call builds a fresh library application, because a
user's token cache binds to the application at construction and one shared
application would mix users' tokens; the applications of one service share a
single HTTP cache, so the tenant's discovery document is fetched once per
process rather than once per call.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from app.backend.auth.config import BffAuthSettings


@dataclass
class LoginChallenge:
    """The authorization-code challenge to hand the browser, plus the flow to persist."""

    authorization_url: str
    state: str
    flow: dict[str, Any]


@dataclass
class LoginResult:
    """The outcome of redeeming an authorization code."""

    subject: str
    claims: dict[str, Any]
    token_cache: str


@dataclass
class TokenGrant:
    """A delegated access token, plus the session's token cache when it changed.

    ``token_cache`` is the re-serialized cache when acquiring the token altered
    it (a refresh rotated the tokens it holds), and ``None`` when it did not.
    """

    access_token: str
    token_cache: str | None


class AuthError(Exception):
    """An OIDC or token operation failed (no code, token-endpoint error, ...)."""


class OidcService(Protocol):
    """The auth operations the router depends on, independent of the library."""

    def begin_login(self, redirect_uri: str) -> LoginChallenge: ...

    def complete_login(
        self, flow: dict[str, Any], auth_response: dict[str, Any]
    ) -> LoginResult: ...

    def acquire_sage_token(self, token_cache: str) -> TokenGrant: ...


class MsalOidcService:
    """MSAL-backed :class:`OidcService` for a Microsoft Entra confidential client."""

    def __init__(self, settings: BffAuthSettings, *, http_client: Any = None) -> None:
        self._settings = settings
        self._http_client = http_client
        # Shared by every application this service builds; the library keeps
        # discovery responses here, each entry expiring on its own schedule.
        self._http_cache: dict[str, Any] = {}

    def _application(self, cache: Any = None) -> Any:
        import msal

        return msal.ConfidentialClientApplication(
            self._settings.client_id,
            authority=self._settings.authority,
            client_credential=self._settings.client_secret,
            token_cache=cache,
            http_cache=self._http_cache,
            http_client=self._http_client,
        )

    def begin_login(self, redirect_uri: str) -> LoginChallenge:
        flow = self._application().initiate_auth_code_flow(
            scopes=[self._settings.sage_scope],
            redirect_uri=redirect_uri,
        )
        if "auth_uri" not in flow or "state" not in flow:
            raise AuthError("identity provider did not return an authorization URL")
        return LoginChallenge(authorization_url=flow["auth_uri"], state=flow["state"], flow=flow)

    def complete_login(self, flow: dict[str, Any], auth_response: dict[str, Any]) -> LoginResult:
        import msal

        cache = msal.SerializableTokenCache()
        result = self._application(cache=cache).acquire_token_by_auth_code_flow(flow, auth_response)
        if "error" in result or "id_token_claims" not in result:
            detail = (
                result.get("error_description") or result.get("error") or "token exchange failed"
            )
            raise AuthError(str(detail))
        claims = result["id_token_claims"]
        subject = claims.get("oid") or claims.get("sub") or ""
        return LoginResult(subject=subject, claims=claims, token_cache=cache.serialize())

    def acquire_sage_token(self, token_cache: str) -> TokenGrant:
        import msal

        cache = msal.SerializableTokenCache()
        if token_cache:
            cache.deserialize(token_cache)
        application = self._application(cache=cache)
        accounts = application.get_accounts()
        result = None
        if accounts:
            result = application.acquire_token_silent(
                [self._settings.sage_scope], account=accounts[0]
            )
        if not result or "access_token" not in result:
            raise AuthError("could not acquire a delegated SAGE token from the session")
        changed = cache.serialize() if cache.has_state_changed else None
        return TokenGrant(access_token=result["access_token"], token_cache=changed)
