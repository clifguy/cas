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


class AuthError(Exception):
    """An OIDC or token operation failed (no code, token-endpoint error, ...)."""


class OidcService(Protocol):
    """The auth operations the router depends on, independent of the library."""

    def begin_login(self, redirect_uri: str) -> LoginChallenge: ...

    def complete_login(
        self, flow: dict[str, Any], auth_response: dict[str, Any]
    ) -> LoginResult: ...

    def acquire_sage_token(self, token_cache: str) -> str: ...


class MsalOidcService:
    """MSAL-backed :class:`OidcService` for a Microsoft Entra confidential client."""

    def __init__(self, settings: BffAuthSettings) -> None:
        self._settings = settings

    def _application(self, cache: Any = None) -> Any:
        import msal

        return msal.ConfidentialClientApplication(
            self._settings.client_id,
            authority=self._settings.authority,
            client_credential=self._settings.client_secret,
            token_cache=cache,
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

    def acquire_sage_token(self, token_cache: str) -> str:
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
        return result["access_token"]
