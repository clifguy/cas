"""Backend-for-frontend -> SAGE transport seam (CAS-ADR-042).

The application backend reaches SAGE through one port with two
profile-selected bindings, mirroring SAGE's own storage/abstraction/auth
adapter seams: an in-process binding that dispatches against the co-located
SAGE app, and an HTTP binding that calls SAGE over the wire carrying the
signed-in user's delegated bearer on every request. Selection is the
deployment profile -- the same stack-scope switch that binds SAGE's storage,
abstraction, and auth ports -- so the standalone (hosted) deployment binds the
HTTP transport while the co-located deployment keeps direct, in-process
dispatch.

Per CAS-ADR-042's weakest-binding constraint, the port contract carries no
guarantee a binding cannot honor: it is one authenticated request/response
call, satisfiable in-process and over HTTP alike. The seam name lives in the
application layer (the port is the backend's, not a SAGE-internal one) and is
registered against the generic profile registry in :mod:`sage.profiles`.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, cast

import httpx

from sage import profiles

if TYPE_CHECKING:
    from app.backend.auth.sage_client import ObOSageClient
    from app.backend.auth.session_store import Session
    from sage.config import SageCoreConfig

#: Seam name for the BFF->SAGE transport binding. A backend-layer port name
#: registered against the same profile registry that binds SAGE's storage,
#: abstraction, and auth seams.
BFF_TRANSPORT_SEAM = "bff_sage_transport"

#: Internal base URL the in-process binding hands to httpx.ASGITransport. It
#: never reaches a socket -- ASGITransport dispatches into the app object -- so
#: the host is a placeholder, present only to satisfy URL construction.
_ASGI_BASE_URL = "http://bff-inprocess.invalid"


@dataclass(frozen=True)
class SageResponse:
    """Transport-neutral SAGE response: status, headers, raw body.

    Free of any FastAPI or httpx type so the port contract is identical for
    both bindings; the reverse proxy reconstructs its own framework response
    from these three fields.
    """

    status_code: int
    headers: Mapping[str, str]
    content: bytes


@dataclass(frozen=True)
class SageStreamingResponse:
    """Transport-neutral streamed SAGE response: the body is not yet read.

    ``stream`` yields the body chunk-by-chunk from the live upstream response;
    ``aclose`` releases the response and any binding-held resources (the
    in-process binding's per-stream client) and must be awaited after the last
    chunk -- the proxy runs it as a post-response background task.
    """

    status_code: int
    headers: Mapping[str, str]
    stream: AsyncIterator[bytes]
    aclose: Callable[[], Awaitable[None]]


class SageTransport(ABC):
    """Port: one authenticated BFF->SAGE request/response call."""

    @abstractmethod
    async def request(
        self,
        method: str,
        path: str,
        *,
        session: Session | None = None,
        params: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
        content: bytes | None = None,
    ) -> SageResponse:
        """Issue ``method`` against SAGE ``path`` and return the response.

        ``session`` carries the signed-in user: the HTTP binding derives the
        delegated bearer from it (and refuses the call without one), while the
        in-process binding ignores it because the co-located deployment
        authenticates in-process rather than by minting a delegated token.
        """
        ...

    @abstractmethod
    async def stream(
        self,
        method: str,
        path: str,
        *,
        session: Session | None = None,
        params: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> SageStreamingResponse:
        """Issue ``method`` against SAGE ``path``, leaving the body unread.

        The streaming counterpart of ``request`` for large-body relays: the
        returned response's body flows chunk-by-chunk, so no hop holds it
        whole. Carries no request body -- it is a read path. The same
        ``session`` semantics as ``request`` apply. Satisfiable by both
        bindings, so it is part of the port contract (CAS-ADR-042).
        """
        ...


class HttpSageTransport(SageTransport):
    """Hosted binding: reach SAGE over HTTP via the on-behalf-of client.

    Every call carries the user's delegated, SAGE-audienced bearer (acquired
    by :class:`~app.backend.auth.sage_client.ObOSageClient` from the session),
    so SAGE is never reached as a service principal. A request with no session
    is refused before any HTTP call is made.
    """

    def __init__(self, client: ObOSageClient) -> None:
        self._client = client

    async def request(
        self,
        method: str,
        path: str,
        *,
        session: Session | None = None,
        params: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
        content: bytes | None = None,
    ) -> SageResponse:
        if session is None:
            from sage.api.errors import SAGEError

            raise SAGEError(
                "auth_required",
                "A signed-in session is required to reach SAGE.",
                401,
            )
        response = await self._client.request(
            method,
            path,
            session,
            params=dict(params) if params else None,
            headers=dict(headers) if headers else None,
            content=content,
        )
        return SageResponse(
            status_code=response.status_code,
            headers=dict(response.headers),
            content=response.content,
        )

    async def stream(
        self,
        method: str,
        path: str,
        *,
        session: Session | None = None,
        params: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> SageStreamingResponse:
        if session is None:
            from sage.api.errors import SAGEError

            raise SAGEError(
                "auth_required",
                "A signed-in session is required to reach SAGE.",
                401,
            )
        response = await self._client.stream(
            method,
            path,
            session,
            params=dict(params) if params else None,
            headers=dict(headers) if headers else None,
        )
        return SageStreamingResponse(
            status_code=response.status_code,
            headers=dict(response.headers),
            stream=response.aiter_bytes(),
            aclose=response.aclose,
        )

    async def aclose(self) -> None:
        await self._client.aclose()


class InProcessSageTransport(SageTransport):
    """Co-located binding: dispatch against the in-process SAGE app.

    No socket and no bearer -- the request is handed to the in-process app
    directly via :class:`httpx.ASGITransport`, the in-process equivalent of
    the HTTP call the hosted profile makes. ``session`` is ignored: the
    co-located deployment authenticates in-process, not by minting a
    delegated token.
    """

    def __init__(self, sage_app: Any) -> None:
        self._app = sage_app

    async def request(
        self,
        method: str,
        path: str,
        *,
        session: Session | None = None,
        params: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
        content: bytes | None = None,
    ) -> SageResponse:
        transport = httpx.ASGITransport(app=self._app)
        async with httpx.AsyncClient(transport=transport, base_url=_ASGI_BASE_URL) as client:
            response = await client.request(
                method,
                path,
                params=dict(params) if params else None,
                headers=dict(headers) if headers else None,
                content=content,
            )
        return SageResponse(
            status_code=response.status_code,
            headers=dict(response.headers),
            content=response.content,
        )

    async def stream(
        self,
        method: str,
        path: str,
        *,
        session: Session | None = None,
        params: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> SageStreamingResponse:
        # Unlike ``request``'s per-call ``async with``, the client is released
        # by the ``aclose`` closure rather than before returning: the caller
        # consumes the body after this method returns. Note that ASGITransport
        # runs the dispatched app to completion inside ``send`` and replays the
        # collected chunks, so this binding satisfies the port's streaming
        # shape without bounded-memory laziness; the HTTP binding is the one
        # that relays a genuinely live stream.
        transport = httpx.ASGITransport(app=self._app)
        client = httpx.AsyncClient(transport=transport, base_url=_ASGI_BASE_URL)
        request = client.build_request(
            method,
            path,
            params=dict(params) if params else None,
            headers=dict(headers) if headers else None,
        )
        response = await client.send(request, stream=True)

        async def aclose() -> None:
            await response.aclose()
            await client.aclose()

        return SageStreamingResponse(
            status_code=response.status_code,
            headers=dict(response.headers),
            stream=response.aiter_bytes(),
            aclose=aclose,
        )


# A transport builder takes the runtime wiring its binding needs and returns
# the transport. The profile registry selects the builder; the construction
# deps (the OBO client for the hosted binding, the co-located SAGE app for the
# in-process binding) are not in the stack config, so they are supplied at
# resolution time rather than baked into the factory.
TransportBuilder = Callable[..., SageTransport]


def _http_transport_builder(stack_config: SageCoreConfig) -> TransportBuilder:
    """Hosted-profile builder: wrap the supplied on-behalf-of client."""

    def build(*, oidc_client: ObOSageClient, **_ignored: Any) -> SageTransport:
        return HttpSageTransport(oidc_client)

    return build


def _inprocess_transport_builder(stack_config: SageCoreConfig) -> TransportBuilder:
    """Co-located-profile builder: dispatch against the supplied SAGE app."""

    def build(*, sage_app: Any, **_ignored: Any) -> SageTransport:
        return InProcessSageTransport(sage_app)

    return build


# Register the BFF->SAGE transport binding for both deployment profiles
# (CAS-ADR-042). The co-located profile binds the in-process dispatch; the
# hosted profile binds the OBO HTTP client. A future profile attaches its own
# transport by registering a different builder here, not by branching the
# resolver.
profiles.register_binding(
    profiles.LOCAL_PROFILE,
    BFF_TRANSPORT_SEAM,
    _inprocess_transport_builder,
)
profiles.register_binding(
    profiles.CLOUD_PROFILE,
    BFF_TRANSPORT_SEAM,
    _http_transport_builder,
)


def resolve_bff_transport(stack_config: SageCoreConfig, **deps: Any) -> SageTransport:
    """Resolve and build the BFF->SAGE transport for the active profile.

    The profile registry selects the binding's builder (in-process for the
    co-located profile, HTTP for the hosted profile); ``deps`` supplies the
    runtime wiring the chosen builder needs (``sage_app`` for the in-process
    binding, ``oidc_client`` for the HTTP binding). Mirrors the typed
    ``resolve_stack_*`` accessors in :mod:`sage.mcp_init`.
    """
    resolved = profiles.resolve_profile(stack_config.profile, stack_config)
    builder = cast(TransportBuilder, resolved.binding(BFF_TRANSPORT_SEAM))
    return builder(**deps)
