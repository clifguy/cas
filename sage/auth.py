"""OAuth resource-server token validation for the SAGE HTTP surfaces.

Binds the SAGE process as an OAuth resource server (CAS-ADR-042): a single
bearer-token validator enforces identical authorization on the REST API and
the HTTP/SSE MCP mounts, so a caller is authorized uniformly regardless of
surface. The validator is selected by the deployment profile's auth seam --
a pass-through validator where the deployment authenticates no one (the
on-box default), an Entra JWT validator where it does.

The delegated user identity is carried by the caller's bearer token; the
process never authenticates as a service principal. Token-signing keys are
fetched from the issuer's published JWKS endpoint and cached; the
signing-key resolver is injectable so the validator is exercised without
network access.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

import anyio
import jwt
from jwt import PyJWKClient
from starlette.types import ASGIApp, Receive, Scope, Send

from sage.config import StackAuthConfig

logger = logging.getLogger(__name__)

#: ASGI scope key under which an accepted request's principal is carried.
SCOPE_PRINCIPAL_KEY = "sage_auth_principal"


@dataclass(frozen=True)
class AuthenticatedPrincipal:
    """The identity established for a request after token validation.

    The ``anonymous`` principal is what a pass-through validator returns
    where the deployment authenticates no one; a validated bearer token
    yields a principal carrying its subject and the scopes/roles it
    presented.
    """

    subject: str | None = None
    scopes: frozenset[str] = field(default_factory=frozenset)
    roles: frozenset[str] = field(default_factory=frozenset)
    claims: dict[str, Any] = field(default_factory=dict)
    anonymous: bool = False


_ANONYMOUS = AuthenticatedPrincipal(subject=None, anonymous=True)


class AuthError(Exception):
    """A token was missing, malformed, or insufficiently authorized.

    Carries the HTTP status and the ``WWW-Authenticate`` challenge value the
    surface returns -- 401 for a missing or invalid token, 403 for a valid
    token that lacks a required scope or role.
    """

    def __init__(
        self,
        status_code: int,
        error: str,
        description: str,
        *,
        resource_metadata_url: str | None = None,
    ) -> None:
        super().__init__(description)
        self.status_code = status_code
        self.error = error
        self.description = description
        self.resource_metadata_url = resource_metadata_url

    def www_authenticate(self) -> str:
        """The ``WWW-Authenticate`` header value advertising the challenge.

        Includes the ``resource_metadata`` pointer (RFC 9728) when one is
        configured so a client can discover where to obtain a token; the
        metadata document itself is served by the edge facade, not here.
        """
        params = [f'error="{self.error}"', f'error_description="{self.description}"']
        if self.resource_metadata_url is not None:
            params.append(f'resource_metadata="{self.resource_metadata_url}"')
        return "Bearer " + ", ".join(params)


class TokenValidator(Protocol):
    """Validates a bearer token for one request, or raises ``AuthError``."""

    async def validate(self, token: str | None) -> AuthenticatedPrincipal: ...


class NoAuthValidator:
    """Pass-through validator: authenticates no one.

    Installed where the deployment runs without auth (the on-box default).
    Every request -- with or without a token -- resolves to the anonymous
    principal and is allowed through, preserving the no-auth behavior.
    """

    async def validate(self, token: str | None) -> AuthenticatedPrincipal:
        return _ANONYMOUS


#: Maps a raw token to the public key its signature is verified against.
SigningKeyResolver = Callable[[str], Any]


def _parse_scopes(scp: Any) -> frozenset[str]:
    """Normalize a delegated-scope claim to a set.

    The ``scp`` claim is conventionally a space-delimited string; tolerate a
    list form as well.
    """
    if isinstance(scp, str):
        return frozenset(scp.split())
    if isinstance(scp, (list, tuple)):
        return frozenset(str(s) for s in scp)
    return frozenset()


class EntraTokenValidator:
    """Validate an Entra-issued JWT and check its authorization.

    Verifies the RS256 signature against the issuer's signing key, then the
    audience, issuer, and expiry, then that the token presents at least one
    required delegated scope (``scp``) or application role (``roles``). The
    same instance enforces the policy for every surface, which is what makes
    authorization uniform across REST and MCP.
    """

    def __init__(
        self,
        *,
        audience: str,
        issuer: str,
        required_scopes: frozenset[str],
        required_roles: frozenset[str],
        signing_key_resolver: SigningKeyResolver,
        resource_metadata_url: str | None = None,
        algorithms: tuple[str, ...] = ("RS256",),
        leeway: int = 60,
    ) -> None:
        self._audience = audience
        self._issuer = issuer
        self._required_scopes = required_scopes
        self._required_roles = required_roles
        self._resolve_key = signing_key_resolver
        self._resource_metadata_url = resource_metadata_url
        self._algorithms = list(algorithms)
        self._leeway = leeway

    async def validate(self, token: str | None) -> AuthenticatedPrincipal:
        if not token:
            raise AuthError(
                401,
                "invalid_request",
                "A bearer token is required.",
                resource_metadata_url=self._resource_metadata_url,
            )
        try:
            # The signing-key lookup may fetch the issuer's JWKS on a cache
            # miss; offload the (blocking) resolver so the event loop is free.
            key = await anyio.to_thread.run_sync(self._resolve_key, token)
        except Exception as exc:
            raise AuthError(
                401,
                "invalid_token",
                "Token signing key could not be resolved.",
                resource_metadata_url=self._resource_metadata_url,
            ) from exc
        try:
            claims = jwt.decode(
                token,
                key,
                algorithms=self._algorithms,
                audience=self._audience,
                issuer=self._issuer,
                leeway=self._leeway,
                options={"require": ["exp", "iss", "aud"]},
            )
        except jwt.PyJWTError as exc:
            raise AuthError(
                401,
                "invalid_token",
                f"Token validation failed: {exc}",
                resource_metadata_url=self._resource_metadata_url,
            ) from exc

        scopes = _parse_scopes(claims.get("scp"))
        roles = frozenset(claims.get("roles") or [])
        if not self._is_authorized(scopes, roles):
            raise AuthError(
                403,
                "insufficient_scope",
                "Token lacks a required scope or role.",
                resource_metadata_url=self._resource_metadata_url,
            )
        return AuthenticatedPrincipal(
            subject=claims.get("sub"),
            scopes=scopes,
            roles=roles,
            claims=claims,
        )

    def _is_authorized(self, scopes: frozenset[str], roles: frozenset[str]) -> bool:
        # No scope/role gate configured -> any validated token is authorized.
        if not self._required_scopes and not self._required_roles:
            return True
        return bool(scopes & self._required_scopes) or bool(roles & self._required_roles)


def build_auth_validator(auth_config: StackAuthConfig | None) -> TokenValidator:
    """Select the token validator for the resolved deployment profile.

    Returns a pass-through ``NoAuthValidator`` where auth is absent or
    disabled, and an ``EntraTokenValidator`` bound to the configured issuer
    and audience where it is enabled. Called by the profile's auth-seam
    binding factory; the on-box default leaves auth absent, so the binding
    is a no-op there.
    """
    if auth_config is None or not auth_config.enabled:
        return NoAuthValidator()

    issuer = auth_config.resolved_issuer()
    audience = auth_config.audience
    jwks_uri = auth_config.resolved_jwks_uri()
    if not issuer or not audience or not jwks_uri:
        raise ValueError(
            "sage_core_config.auth is enabled but incomplete: 'audience' and "
            "either 'tenant_id' or both 'issuer' and 'jwks_uri' are required."
        )

    jwks_client = PyJWKClient(jwks_uri)

    def _resolve(token: str) -> Any:
        return jwks_client.get_signing_key_from_jwt(token).key

    return EntraTokenValidator(
        audience=audience,
        issuer=issuer,
        required_scopes=frozenset(auth_config.required_scopes),
        required_roles=frozenset(auth_config.required_roles),
        signing_key_resolver=_resolve,
        resource_metadata_url=auth_config.resource_metadata_url,
    )


def _bearer_token(scope: Scope) -> str | None:
    """Extract the bearer credential from an ASGI request scope, if present."""
    for name, value in scope.get("headers", []):
        if name == b"authorization":
            scheme, _, param = value.decode("latin-1").partition(" ")
            if scheme.lower() == "bearer" and param.strip():
                return param.strip()
            return None
    return None


async def _send_challenge(send: Send, exc: AuthError) -> None:
    """Answer a rejected request with its status and ``WWW-Authenticate``."""
    body = json.dumps({"error": exc.error, "error_description": exc.description}).encode("utf-8")
    headers = [
        (b"content-type", b"application/json"),
        (b"www-authenticate", exc.www_authenticate().encode("latin-1")),
        (b"content-length", str(len(body)).encode("latin-1")),
    ]
    await send({"type": "http.response.start", "status": exc.status_code, "headers": headers})
    await send({"type": "http.response.body", "body": body})


class AuthMiddleware:
    """Enforce the resolved token validator across every HTTP surface.

    A pure-ASGI middleware on the parent application so the one validator
    guards the REST routes and the mounted HTTP/SSE MCP sub-apps identically
    -- authorization is uniform regardless of surface. Exempt paths (the
    liveness probe) are answered without a token. A rejected request is
    answered here with the validator's status and ``WWW-Authenticate``
    challenge; an accepted request carries its principal forward on the ASGI
    scope under :data:`SCOPE_PRINCIPAL_KEY`.
    """

    def __init__(
        self,
        app: ASGIApp,
        *,
        validator: TokenValidator,
        exempt_paths: frozenset[str] = frozenset(),
    ) -> None:
        self.app = app
        self._validator = validator
        self._exempt_paths = exempt_paths

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope.get("path") in self._exempt_paths:
            await self.app(scope, receive, send)
            return
        try:
            principal = await self._validator.validate(_bearer_token(scope))
        except AuthError as exc:
            await _send_challenge(send, exc)
            return
        scope[SCOPE_PRINCIPAL_KEY] = principal
        await self.app(scope, receive, send)
