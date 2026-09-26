"""The identity a request writes as.

The auth middleware validates a request's bearer token and binds the resulting
principal here for the lifetime of that request. The services read it back
when they stamp ``created_by`` and ``last_modified_by``, so a write is
attributed to the credential that made it on every surface -- the REST routes,
the mounted MCP transports, and anything they call -- without any tool or
route reading the request itself.

The binding is a context variable: it is visible to the request's own task and
to every task that request starts, and to nothing else. A long-lived worker
that a request happens to start is created without it. Outside a request, and
under a profile that does not authenticate callers, there is no actor, and the
services fall back to the caller-supplied value or the vault owner.

A human principal is recorded by a key that survives a rename: its tenant and
object id. Its display name is recorded beside the key as a snapshot of what
the token carried at write time, and is never used to identify the principal.

Two further identities are bound alongside the principal and recorded with it
(CAS-ADR-056). The *client* is the registered application the token was issued
to, named through the deployment's client map; it is derived from the validated
token and no parameter can set it. The *agent* is the product or process making
the call, as the caller asserts it: a write tool's explicit ``agent`` argument,
otherwise the request's ``User-Agent`` product name. The agent is always served
marked asserted, never as verified.
"""

from __future__ import annotations

import re
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from typing import TYPE_CHECKING

from sage.models.schemas import AGENT_NAME_PATTERN, AssertedAgent

if TYPE_CHECKING:
    from sage.auth import AuthenticatedPrincipal

request_principal: ContextVar[AuthenticatedPrincipal | None] = ContextVar(
    "sage_request_principal", default=None
)
request_client: ContextVar[str | None] = ContextVar("sage_request_client", default=None)
request_header_agent: ContextVar[str | None] = ContextVar("sage_request_header_agent", default=None)
_parameter_agent: ContextVar[str | None] = ContextVar("sage_parameter_agent", default=None)

# Every contextvar that carries a request's identity. A task that outlives its
# request clears all of them, so it never writes as the request that started it.
IDENTITY_VARS: tuple[ContextVar, ...] = (
    request_principal,
    request_client,
    request_header_agent,
    _parameter_agent,
)

_AGENT_NAME = re.compile(AGENT_NAME_PATTERN)

# User-Agent product names that identify a browser or a general-purpose HTTP
# library rather than the agent using it. A request carrying one records no
# header agent.
_GENERIC_USER_AGENT_PRODUCTS = frozenset(
    {
        "mozilla",
        "python-httpx",
        "python-requests",
        "python-urllib",
        "aiohttp",
        "node",
        "node-fetch",
        "undici",
        "axios",
        "curl",
        "wget",
        "go-http-client",
        "okhttp",
    }
)


def _is_delegated(principal: AuthenticatedPrincipal) -> bool:
    """Whether a token acts for a user (it carries ``scp``) rather than an application."""
    return "scp" in principal.claims


def principal_actor(principal: AuthenticatedPrincipal) -> str | None:
    """The stable attribution key for a validated principal, or None if anonymous.

    A delegated token (one that carries ``scp``) names a user by identifiers
    that a rename or a domain move leaves unchanged: ``<tid>:<oid>``, with
    ``sub`` standing in where the token carries no ``oid``. The tenant, rather
    than the issuer, qualifies the id because one tenant issues under more
    than one issuer string; a token without ``tid`` is qualified by its issuer
    as ``<iss>#<id>`` instead. An app-only token names a client application:
    ``app:<client id>``, from ``azp``, or ``appid`` on a v1 token. ``sub`` is
    the last resort for either.
    """
    if principal.anonymous:
        return None
    claims = principal.claims
    if _is_delegated(principal):
        user = claims.get("oid") or claims.get("sub")
        if user:
            if claims.get("tid"):
                return f"{claims['tid']}:{user}"
            if claims.get("iss"):
                return f"{claims['iss']}#{user}"
            return str(user)
    else:
        client = claims.get("azp") or claims.get("appid")
        if client:
            return f"app:{client}"
    return principal.subject


def principal_display_name(principal: AuthenticatedPrincipal) -> str | None:
    """The name a delegated token shows for its user, or None.

    ``preferred_username``, else ``name``. It is a snapshot of the token at
    write time and never a key: the same principal can carry a different name
    on its next write. An app-only or anonymous principal has none.
    """
    if principal.anonymous or not _is_delegated(principal):
        return None
    name = principal.claims.get("preferred_username") or principal.claims.get("name")
    return str(name) if name else None


def current_actor() -> str | None:
    """The attribution string for the request in progress, if it authenticated."""
    principal = request_principal.get()
    if principal is None:
        return None
    return principal_actor(principal)


def current_actor_name() -> str | None:
    """The display-name snapshot for the request in progress, if it names its user."""
    principal = request_principal.get()
    if principal is None:
        return None
    return principal_display_name(principal)


def writer_name(writer: str | None) -> str | None:
    """The display name to record beside ``writer``.

    Only the authenticated principal has one: a caller-supplied value or the
    vault owner written without authentication is recorded without a name.
    """
    if writer is None or writer != current_actor():
        return None
    return current_actor_name()


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


def client_name(principal: AuthenticatedPrincipal, client_names: Mapping[str, str]) -> str | None:
    """The client a validated principal's token was issued to, or None if anonymous.

    The client id is the token's ``azp`` claim, or ``appid`` on a v1 token.
    The deployment's ``client_names`` map names it; an id the map does not
    name is recorded as the id itself, which is still derived from the token.
    """
    if principal.anonymous:
        return None
    client_id = principal.claims.get("azp") or principal.claims.get("appid")
    if not client_id:
        return None
    return client_names.get(str(client_id), str(client_id))


def agent_from_user_agent(user_agent: str | None) -> str | None:
    """The agent a ``User-Agent`` header names, or None if it names none.

    The agent is the first product token's name, lowercased. A browser or a
    general-purpose HTTP library names no agent, nor does a value that is not
    a well-formed product name.
    """
    if not user_agent:
        return None
    product = user_agent.strip().split(" ", 1)[0].split("/", 1)[0].lower()
    if product in _GENERIC_USER_AGENT_PRODUCTS or not _AGENT_NAME.match(product):
        return None
    return product


def header_user_agent(headers: list[tuple[bytes, bytes]]) -> str | None:
    """The ``User-Agent`` header from raw ASGI headers, if present."""
    for name, value in headers:
        if name == b"user-agent":
            return value.decode("latin-1")
    return None


@contextmanager
def asserted_agent(agent: str | None) -> Iterator[None]:
    """Attribute writes within the block to a caller-named agent.

    A None ``agent`` changes nothing, so a write tool can wrap its call
    unconditionally. The name is validated by the request models that carry
    it.
    """
    if agent is None:
        yield
        return
    binding = _parameter_agent.set(agent)
    try:
        yield
    finally:
        _parameter_agent.reset(binding)


def in_request() -> bool:
    """Whether the code is running inside a request the middleware admitted."""
    return request_principal.get() is not None


def current_client() -> str | None:
    """The client the request in progress came through, if it authenticated."""
    return request_client.get()


def current_agent() -> AssertedAgent | None:
    """The asserted agent of the request in progress, if it names one.

    An explicit ``agent`` argument wins over the ``User-Agent`` header.
    """
    named = _parameter_agent.get()
    if named is not None:
        return AssertedAgent(name=named, source="parameter")
    header = request_header_agent.get()
    if header is not None:
        return AssertedAgent(name=header, source="header")
    return None


def provenance_fields(prefix: str) -> dict[str, object]:
    """The client and agent fields for a write, keyed ``<prefix>_client`` and ``<prefix>_agent``."""
    return {f"{prefix}_client": current_client(), f"{prefix}_agent": current_agent()}


def modifier_fields(writer: str) -> dict[str, object]:
    """The last-modification attribution for a write by ``writer``: principal, client, agent."""
    return {
        "last_modified_by": writer,
        "last_modified_by_name": writer_name(writer),
        **provenance_fields("last_modified"),
    }


def edge_attribution(owner: str) -> dict[str, object]:
    """The creation attribution for an edge written now.

    Inside a request, the principal (or ``owner`` when the request did not
    authenticate), the client, and the agent. Outside any request no one is
    attributed, and all three are None; a task started by a request inherits
    that request's identity unless it clears it.
    """
    if not in_request():
        return {
            "created_by": None,
            "created_by_name": None,
            "created_client": None,
            "created_agent": None,
        }
    writer = current_actor() or owner
    return {
        "created_by": writer,
        "created_by_name": writer_name(writer),
        **provenance_fields("created"),
    }
