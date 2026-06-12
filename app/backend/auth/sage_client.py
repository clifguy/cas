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
        self._client = client or httpx.AsyncClient(base_url=self._base_url)

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
