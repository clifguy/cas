"""HTTP client that calls SAGE on behalf of the signed-in user.

Every request carries the delegated, SAGE-audienced bearer token acquired from
the user's session, so the user's identity reaches SAGE on every call and SAGE
is never called as a service principal. This is the transport a standalone
backend-for-frontend uses to reach SAGE over HTTP; the in-process deployment
keeps its direct service calls.
"""

from __future__ import annotations

from typing import Any

import httpx

from app.backend.auth.oidc import OidcService
from app.backend.auth.session_store import Session

#: Explicit, generous request budget for the SAGE HTTP hop, replacing httpx's
#: 5 s default read timeout. SAGE can answer a read slower than 5 s when a
#: replica is warming from scale-to-zero, an abstract model is loading cold, or
#: a vector query runs long; the 5 s default cut those slow-but-successful
#: responses off and surfaced them as transport failures. The read/write budget
#: sits above that tail; connect and pool stay tight because a stalled
#: connection or an exhausted pool is a fast-fail condition, not a slow answer.
_DEFAULT_TIMEOUT = httpx.Timeout(connect=5.0, read=30.0, write=30.0, pool=5.0)


class ObOSageClient:
    """Reach SAGE over HTTP, attaching the session's delegated bearer token."""

    def __init__(
        self,
        base_url: str,
        oidc: OidcService,
        *,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._oidc = oidc
        self._client = client or httpx.AsyncClient(
            base_url=self._base_url, timeout=_DEFAULT_TIMEOUT
        )

    async def request(
        self, method: str, path: str, session: Session, **kwargs: Any
    ) -> httpx.Response:
        """Issue a request, deriving the bearer from the session's delegated token.

        Acquiring the token first means a session that cannot mint a delegated
        token raises before any request is sent -- the client never falls back
        to an anonymous or service-principal call.
        """
        token = self._oidc.acquire_sage_token(session.token_cache)
        headers = dict(kwargs.pop("headers", None) or {})
        headers["Authorization"] = f"Bearer {token}"
        return await self._client.request(method, path, headers=headers, **kwargs)

    async def get(self, path: str, session: Session, **kwargs: Any) -> httpx.Response:
        return await self.request("GET", path, session, **kwargs)

    async def post(self, path: str, session: Session, **kwargs: Any) -> httpx.Response:
        return await self.request("POST", path, session, **kwargs)

    async def aclose(self) -> None:
        await self._client.aclose()
