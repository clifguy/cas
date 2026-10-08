"""Security response headers for the standalone backend-for-frontend (CAS-ADR-042).

Every response the backend sends -- its SPA, its health probe, its proxied SAGE
responses and every error, including the one the framework produces for an
unhandled exception -- carries a page content-security policy, a no-sniff
directive, a referrer policy and frame protection, and under the cloud profile
a transport-security policy. The headers are added by an ASGI wrapper around
the whole application, outside its error middleware, so no response path
escapes them.

A header a response already carries is left as it is. The proxy relays SAGE's
own policy for raw document bytes, which is stricter than the page policy and
must not be replaced by it.
"""

from __future__ import annotations

import os
from collections.abc import Sequence

from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

#: The SPA loads only its own scripts and styles and connects only to its own
#: origin; it frames nothing and may be framed by nothing.
PAGE_POLICY = (
    "default-src 'self'; script-src 'self'; object-src 'none'; "
    "base-uri 'none'; frame-ancestors 'none'"
)

#: One year, the conventional minimum for a transport-security policy.
TRANSPORT_SECURITY = "max-age=31536000; includeSubDomains"

#: Setting this to a true value sends the page policy as an enforcing policy;
#: otherwise it is sent report-only, so the browser reports a violation without
#: blocking anything while the policy is proven against the live application.
CSP_ENFORCE_ENV = "CAS_BFF_CSP_ENFORCE"

_TRUE = frozenset({"1", "true", "yes", "on"})


def csp_enforced_from_env(environ: dict[str, str] | None = None) -> bool:
    """Whether the environment asks for the page policy to be enforced."""
    value = (environ if environ is not None else os.environ).get(CSP_ENFORCE_ENV, "")
    return value.strip().lower() in _TRUE


def security_headers(*, enforce_csp: bool, transport_security: bool) -> tuple[tuple[str, str], ...]:
    """The headers every response carries, as ``(name, value)`` pairs.

    A report-only page policy cannot carry ``frame-ancestors`` -- browsers
    ignore that directive there -- so ``X-Frame-Options`` provides frame
    protection in both modes.
    """
    policy_header = (
        "Content-Security-Policy" if enforce_csp else "Content-Security-Policy-Report-Only"
    )
    headers = [
        (policy_header, PAGE_POLICY),
        ("X-Content-Type-Options", "nosniff"),
        ("Referrer-Policy", "no-referrer"),
        ("X-Frame-Options", "DENY"),
    ]
    if transport_security:
        headers.append(("Strict-Transport-Security", TRANSPORT_SECURITY))
    return tuple(headers)


class SecurityHeadersMiddleware:
    """Add ``headers`` to every HTTP response that does not already carry them."""

    def __init__(self, app: ASGIApp, headers: Sequence[tuple[str, str]]) -> None:
        self.app = app
        self.headers = tuple(headers)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                present = MutableHeaders(scope=message)
                for name, value in self.headers:
                    if name not in present:
                        present.append(name, value)
            await send(message)

        await self.app(scope, receive, send_with_headers)
