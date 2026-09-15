"""A standalone backend-for-frontend with sign-in configured, for tests.

The standalone app's scan, ingest and proxy routes require a signed-in session,
so a test exercising one of them needs an app whose auth context holds a session
and a client presenting its cookie. The
store is in memory and the identity provider is a stub: nothing here reaches a
network or a database.
"""

from __future__ import annotations

import time

import httpx
from fastapi import FastAPI

from app.backend.asgi import create_bff_app
from app.backend.auth.config import BffAuthContext, BffAuthSettings
from app.backend.auth.session_store import InMemorySessionStore, Session
from sage.config import SageCoreConfig

#: The id of the live session ``auth_app`` opens.
SESSION_ID = "sid-1"


def bff_settings() -> BffAuthSettings:
    """Sign-in settings naming a stub tenant and SAGE base URL."""
    return BffAuthSettings(
        tenant_id="t",
        client_id="c",
        client_secret="s",  # noqa: S106 -- test fixture, not a real secret
        sage_app_id_uri="api://sage",
        sage_base_url="http://sage.test",
    )


def live_session() -> Session:
    """A session that expires an hour from now."""
    return Session(
        session_id=SESSION_ID,
        subject="user-1",
        claims={"name": "Test User"},
        token_cache="cache-blob",
        expires_at=time.time() + 3600,
    )


class StubOidc:
    """An identity-provider client that hands out a fixed delegated token."""

    def __init__(self, token: str = "delegated-token") -> None:  # noqa: S107 -- test fixture token, not a real secret
        self._token = token

    def acquire_sage_token(self, token_cache: str) -> str:
        return self._token


async def auth_app(*, with_session: bool) -> FastAPI:
    """A cloud-profile standalone app with sign-in configured over an in-memory
    session store, holding the live session ``SESSION_ID`` when ``with_session``."""
    app = create_bff_app(stack_config=SageCoreConfig(profile="cloud"))
    store = InMemorySessionStore()
    if with_session:
        await store.create_session(live_session())
    app.state.bff_auth = BffAuthContext(settings=bff_settings(), oidc=StubOidc(), store=store)
    return app


def sessioned_client(app: FastAPI, session_id: str = SESSION_ID) -> httpx.AsyncClient:
    """A client presenting ``session_id`` as the session cookie."""
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://bff.test",
        cookies={bff_settings().session_cookie_name: session_id},
    )
