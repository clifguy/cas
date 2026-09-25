"""The identity a request writes as (CAS-ADR-042).

The auth middleware validates a request's bearer token and binds the resulting
principal here for the lifetime of that request. The services read it back
when they stamp ``created_by`` and ``last_modified_by``, so a write is
attributed to the credential that made it on every surface -- the REST routes,
the mounted MCP transports, and anything they call -- without any tool or
route reading the request itself.

The binding is a context variable: it is visible to the request's own task and
to every task that request starts, and to nothing else. Outside a request, and
under a profile that does not authenticate callers, there is no actor, and the
services fall back to the caller-supplied value or the vault owner.
"""

from __future__ import annotations

from contextvars import ContextVar
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from sage.auth import AuthenticatedPrincipal

request_principal: ContextVar[AuthenticatedPrincipal | None] = ContextVar(
    "sage_request_principal", default=None
)


def principal_actor(principal: AuthenticatedPrincipal) -> str | None:
    """The attribution string for a validated principal, or None if anonymous.

    A delegated token (one that carries ``scp``) names a user: its
    ``preferred_username``, else its ``oid``. An app-only token names a client
    application: ``app:<client id>``, from ``azp``, or ``appid`` on a v1
    token. ``sub`` is the last resort for either, since it is issued per
    application rather than per user.
    """
    if principal.anonymous:
        return None
    claims = principal.claims
    if "scp" in claims:
        user = claims.get("preferred_username") or claims.get("oid")
        if user:
            return str(user)
    else:
        client = claims.get("azp") or claims.get("appid")
        if client:
            return f"app:{client}"
    return principal.subject


def current_actor() -> str | None:
    """The attribution string for the request in progress, if it authenticated."""
    principal = request_principal.get()
    if principal is None:
        return None
    return principal_actor(principal)


def attributed_writer(caller_value: str | None, fallback: str) -> tuple[str, list[str]]:
    """The writer to record, and any warning the caller should be shown.

    An authenticated request always writes as its principal; a differing
    caller-supplied value is ignored and named in the warning rather than
    refused, since existing callers pass one. Without authentication the
    caller-supplied value is used, and ``fallback`` when there is none.
    """
    actor = current_actor()
    if actor is None:
        return caller_value or fallback, []
    if caller_value and caller_value != actor:
        return actor, [
            f"created_by {caller_value!r} was ignored: this request authenticated as "
            f"{actor!r}, and a write is attributed to the authenticated principal."
        ]
    return actor, []
