"""Reverse proxy: forward the SPA's SAGE read/query traffic to SAGE.

When the backend runs standalone (hosted profile) the SPA is served same-origin
by the backend and holds no token, so it cannot reach SAGE directly. This
router forwards the bare ``/sage_vaults`` collection and every ``/sage_vaults/*``
subpath request to SAGE through the transport seam, which attaches the signed-in
user's delegated bearer server-side. Mounted in the SAGE app (co-located
profile), SAGE answers these paths from its own routers and this proxy is unused.
"""

from __future__ import annotations

import re
from urllib.parse import quote, unquote

import httpx
from fastapi import APIRouter, Depends, Request
from fastapi.responses import Response, StreamingResponse
from starlette.background import BackgroundTask
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.backend.auth.dependencies import require_session
from app.backend.auth.session_store import Session
from app.backend.transport import SageTransport
from sage.api.errors import SAGEError

router = APIRouter(tags=["proxy"])

# The proxied routes whose response is relayed as a stream, each a method and a
# full-path match: SAGE's raw source-byte delivery, whose payload can exceed any
# sensible in-memory buffer, and the two operations that answer with an event
# stream, whose progress events must reach the SPA as they are emitted. Any
# other route, or another method on one of these paths, keeps the buffered
# forward, so a route that merely resembles one of these must opt in
# deliberately.
_STREAMED_ROUTES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("GET", re.compile(r"^/sage_vaults/[^/]+/documents/[^/]+/content$")),
    ("POST", re.compile(r"^/sage_vaults/[^/]+/documents:batch$")),
    ("POST", re.compile(r"^/sage_vaults/[^/]+/maintenance/reabstract-deferred$")),
)

# Request headers forwarded upstream; every other header stays here. The
# transport supplies its own Authorization and httpx recomputes the body
# framing. The SAGE routes reached here read only these from the SPA: the
# body's media type (including a multipart boundary), the representation and
# language the SPA accepts, and the user agent writes are attributed to.
_FORWARDED_REQUEST_HEADERS = frozenset({"accept", "accept-language", "content-type", "user-agent"})

# Response headers relayed back; every other header is dropped. httpx has
# already decoded the body, so the upstream content-encoding, length and
# transfer-encoding no longer describe the bytes being returned, and upstream
# cookies or server identity have no meaning on this origin.
_RELAYED_RESPONSE_HEADERS = frozenset(
    {"content-type", "content-disposition", "cache-control", "www-authenticate", "allow"}
)

_COLLECTION_PREFIX = "/sage_vaults/"

# Characters left unescaped when a decoded path segment is re-encoded for the
# upstream request: RFC 3986 ``pchar`` less the percent sign, so a segment such
# as ``documents:batch`` is forwarded as written while a decoded ``/``, ``?`` or
# ``#`` stays escaped inside its segment.
_SEGMENT_SAFE = "!$&'()*+,;=:@-._~"


def _is_dot_or_empty(segment: str) -> bool:
    """Whether a decoded segment is empty or carries a ``.``/``..`` component.

    A decoded segment may hold a ``/`` that arrived encoded; a dot component on
    either side of it is refused too, so no decoding of the path reads as a
    climb out of the collection.
    """
    return segment == "" or any(part in (".", "..") for part in segment.split("/"))


def _upstream_path(request: Request) -> str:
    """The SAGE path for a ``/sage_vaults/*`` request, built from its raw target.

    Each segment below the collection is percent-decoded and refused when it is
    empty or has a ``.`` or ``..`` component, whatever its encoding, then
    re-encoded on its own, so an encoded separator cannot split it and no
    segment can climb out of the collection when the HTTP client normalizes the
    path. A refusal is the unrouted-path answer: such a path addresses no SAGE
    operation.
    """
    raw = request.scope.get("raw_path") or request.url.path.encode()
    raw_path = raw.decode("latin-1").split("?", 1)[0]
    if not raw_path.startswith(_COLLECTION_PREFIX):
        raise StarletteHTTPException(status_code=404)
    segments = [unquote(segment) for segment in raw_path[len(_COLLECTION_PREFIX) :].split("/")]
    if any(_is_dot_or_empty(segment) for segment in segments):
        raise StarletteHTTPException(status_code=404)
    upstream = _COLLECTION_PREFIX + "/".join(
        quote(segment, safe=_SEGMENT_SAFE) for segment in segments
    )
    # The path as the HTTP client will send it, after its own normalization.
    sent = httpx.URL(f"http://upstream{upstream}").raw_path
    if not sent.startswith(_COLLECTION_PREFIX.encode()):
        raise StarletteHTTPException(status_code=404)
    return upstream


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
    session: Session,
) -> Response:
    """Forward one request to SAGE ``upstream_path`` under the user's identity.

    The caller's signed-in session has already been resolved by the route's
    ``require_session`` dependency. Attaches the delegated bearer through the
    transport, and relays the upstream status, body, and
    content-shape-independent headers back.
    """
    transport = _get_transport(request)
    forwarded_headers = {
        key: value
        for key, value in request.headers.items()
        if key.lower() in _FORWARDED_REQUEST_HEADERS
    }
    body = await request.body()
    if any(
        request.method == method and pattern.fullmatch(upstream_path)
        for method, pattern in _STREAMED_ROUTES
    ):
        return await _stream_from_sage(
            upstream_path, request, transport, session, forwarded_headers, body
        )
    try:
        sage_response = await transport.request(
            request.method,
            upstream_path,
            session=session,
            params=request.query_params.multi_items(),
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
        if key.lower() in _RELAYED_RESPONSE_HEADERS
    }
    return Response(
        content=sage_response.content,
        status_code=sage_response.status_code,
        headers=relayed_headers,
    )


async def _stream_from_sage(
    upstream_path: str,
    request: Request,
    transport: SageTransport,
    session: Session,
    forwarded_headers: dict[str, str],
    body: bytes,
) -> StreamingResponse:
    """Relay one SAGE response chunk-by-chunk as it arrives, never buffering it.

    Opening the stream (connect + response headers) carries the same
    504/502 mapping as the buffered forward. Once the headers have been sent
    downstream, remapping is impossible by HTTP construction: a mid-stream
    upstream failure truncates the response body, and the client sees the body
    end abnormally rather than complete; no Content-Length is relayed, since the
    decoded bytes need not match the upstream one. The upstream response (and
    the binding's per-stream resources) are released by the background task,
    which runs after the last byte or on client disconnect.
    """
    try:
        sage_stream = await transport.stream(
            request.method,
            upstream_path,
            session=session,
            params=request.query_params.multi_items(),
            headers=forwarded_headers,
            content=body or None,
        )
    except httpx.TimeoutException as exc:
        raise SAGEError(
            "sage_upstream_timeout",
            "The upstream SAGE service did not respond in time.",
            504,
        ) from exc
    except httpx.TransportError as exc:
        raise SAGEError(
            "sage_upstream_unavailable",
            "The upstream SAGE service is unavailable.",
            502,
        ) from exc
    relayed_headers = {
        key: value
        for key, value in sage_stream.headers.items()
        if key.lower() in _RELAYED_RESPONSE_HEADERS
    }
    return StreamingResponse(
        sage_stream.stream,
        status_code=sage_stream.status_code,
        headers=relayed_headers,
        background=BackgroundTask(sage_stream.aclose),
    )


@router.api_route(
    "/sage_vaults",
    methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
    include_in_schema=False,
)
async def proxy_sage_collection(
    request: Request,
    session: Session = Depends(require_session),
) -> Response:
    """Forward the bare ``/sage_vaults`` collection call (list/create) to SAGE.

    The subpath route below requires a trailing segment, so the bare collection
    URL would otherwise fall through to the SPA catch-all and return HTML. This
    forwards to the canonical upstream collection path with no trailing slash.
    """
    return await _forward_to_sage("/sage_vaults", request, session)


@router.api_route(
    "/sage_vaults/{path:path}",
    methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
    include_in_schema=False,
)
async def proxy_sage(
    path: str,
    request: Request,
    session: Session = Depends(require_session),
) -> Response:
    """Forward one ``/sage_vaults/*`` subpath call to SAGE under the user's identity.

    The upstream path is rebuilt from the raw request target rather than taken
    from the decoded ``path`` parameter, which has already lost the distinction
    between a separator and an encoded one.
    """
    return await _forward_to_sage(_upstream_path(request), request, session)
