"""Reverse proxy: forward the SPA's SAGE read/query traffic to SAGE.

When the backend runs standalone (hosted profile) the SPA is served same-origin
by the backend and holds no token, so it cannot reach SAGE directly. This
router forwards the bare ``/sage_vaults`` collection and every ``/sage_vaults/*``
subpath request to SAGE through the transport seam, which attaches the signed-in
user's delegated bearer server-side. Mounted in the SAGE app (co-located
profile), SAGE answers these paths from its own routers and this proxy is unused.
"""

from __future__ import annotations

import httpx
from fastapi import APIRouter, Depends, Request
from fastapi.responses import Response

from app.backend.auth.config import BffAuthSettings
from app.backend.auth.dependencies import get_auth_settings, get_session_service
from app.backend.auth.session_store import SessionService
from app.backend.transport import SageTransport
from sage.api.errors import SAGEError

router = APIRouter(tags=["proxy"])

# Request headers never forwarded upstream: the transport supplies its own
# Authorization; Host, Cookie, and the body-framing headers are connection- or
# session-scoped and httpx recomputes the framing from the forwarded body.
_DROP_REQUEST_HEADERS = frozenset(
    {"host", "authorization", "cookie", "content-length", "connection"}
)

# Response headers dropped before relaying: httpx has already decoded the body,
# so the upstream content-encoding/length/transfer-encoding no longer describe
# the bytes being returned.
_DROP_RESPONSE_HEADERS = frozenset(
    {"content-length", "content-encoding", "transfer-encoding", "connection"}
)


def _get_transport(request: Request) -> SageTransport:
    """Resolve the request-scoped SAGE transport, or raise the structured 503.

    The transport is assembled onto ``app.state`` at startup. When it is unset
    -- a hosted deployment whose identity-provider/SAGE coordinates are absent
    -- the proxy answers ``auth_not_configured`` rather than failing opaquely,
    mirroring the auth router's configuration gate.
    """
    transport = getattr(request.app.state, "sage_transport", None)
    if transport is None:
        raise SAGEError(
            "auth_not_configured",
            "The SAGE transport is not configured for this deployment.",
            503,
        )
    return transport


async def _forward_to_sage(
    upstream_path: str,
    request: Request,
    settings: BffAuthSettings,
    sessions: SessionService,
) -> Response:
    """Forward one request to SAGE ``upstream_path`` under the user's identity.

    Refuses with the structured ``auth_required`` 401 when there is no signed-in
    session, attaches the delegated bearer through the transport, and relays the
    upstream status, body, and content-shape-independent headers back.
    """
    session_id = request.cookies.get(settings.session_cookie_name)
    session = await sessions.read(session_id)
    if session is None:
        raise SAGEError("auth_required", "A signed-in session is required.", 401)

    transport = _get_transport(request)
    forwarded_headers = {
        key: value
        for key, value in request.headers.items()
        if key.lower() not in _DROP_REQUEST_HEADERS
    }
    body = await request.body()
    try:
        sage_response = await transport.request(
            request.method,
            upstream_path,
            session=session,
            params=dict(request.query_params),
            headers=forwarded_headers,
            content=body or None,
        )
    except httpx.TimeoutException as exc:
        # A slow-or-unresponsive upstream is a gateway timeout, not a fault in
        # this proxy. Surfacing the raw httpx error would render as an opaque
        # 500 with a traceback; the structured envelope tells the SPA the hop
        # upstream stalled. TimeoutException subclasses TransportError, so this
        # arm must precede the broader one below.
        raise SAGEError(
            "sage_upstream_timeout",
            "The upstream SAGE service did not respond in time.",
            504,
        ) from exc
    except httpx.TransportError as exc:
        # Any other transport-level failure reaching SAGE (connect refused, DNS,
        # a broken read) is a bad-gateway condition, distinct from the timeout
        # above and from an error SAGE itself returned in a well-formed response.
        raise SAGEError(
            "sage_upstream_unavailable",
            "The upstream SAGE service is unavailable.",
            502,
        ) from exc
    relayed_headers = {
        key: value
        for key, value in sage_response.headers.items()
        if key.lower() not in _DROP_RESPONSE_HEADERS
    }
    return Response(
        content=sage_response.content,
        status_code=sage_response.status_code,
        headers=relayed_headers,
    )


@router.api_route(
    "/sage_vaults",
    methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
    include_in_schema=False,
)
async def proxy_sage_collection(
    request: Request,
    settings: BffAuthSettings = Depends(get_auth_settings),
    sessions: SessionService = Depends(get_session_service),
) -> Response:
    """Forward the bare ``/sage_vaults`` collection call (list/create) to SAGE.

    The subpath route below requires a trailing segment, so the bare collection
    URL would otherwise fall through to the SPA catch-all and return HTML. This
    forwards to the canonical upstream collection path with no trailing slash.
    """
    return await _forward_to_sage("/sage_vaults", request, settings, sessions)


@router.api_route(
    "/sage_vaults/{path:path}",
    methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
    include_in_schema=False,
)
async def proxy_sage(
    path: str,
    request: Request,
    settings: BffAuthSettings = Depends(get_auth_settings),
    sessions: SessionService = Depends(get_session_service),
) -> Response:
    """Forward one ``/sage_vaults/*`` subpath call to SAGE under the user's identity."""
    return await _forward_to_sage(f"/sage_vaults/{path}", request, settings, sessions)
