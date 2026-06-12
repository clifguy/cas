"""Tests for the backend-for-frontend auth flow.

Covers configuration gating (the on-box profile is unaffected), the OIDC login
challenge, forwarded-host redirect-URI construction, the callback that opens a
session and sets the cookie, the failure modes, session-info/logout, and the
on-behalf-of SAGE client that forwards the delegated bearer.

The identity provider is mocked end to end (a ``StubOidcService``); there is no
live network. The session store is the in-memory binding; the durable Postgres
binding is exercised in ``test_bff_session_store.py``.
"""

import time
from typing import Any

import httpx
import pytest
from httpx import ASGITransport, AsyncClient
from starlette.requests import Request

from app.backend.auth.config import BffAuthContext, BffAuthSettings, load_bff_auth_settings
from app.backend.auth.oidc import AuthError, LoginChallenge, LoginResult
from app.backend.auth.sage_client import ObOSageClient
from app.backend.auth.session_store import InMemorySessionStore, Session
from app.backend.auth.urls import external_base_url
from sage.app import create_app

# A recognizable sentinel for the serialized token cache: D-series tests assert
# it never appears in any response delivered to the browser.
_TOKEN_CACHE_SENTINEL = "SENTINEL_TOKEN_CACHE_BLOB"


class StubOidcService:
    """A network-free OidcService that echoes its inputs for assertions."""

    def __init__(self) -> None:
        self.state = "state-123"

    def begin_login(self, redirect_uri: str) -> LoginChallenge:
        from urllib.parse import urlencode

        query = urlencode(
            {
                "client_id": "test-client",
                "redirect_uri": redirect_uri,
                "state": self.state,
                "code_challenge": "pkce-challenge",
                "scope": "openid api://sage/Sage.Access",
            }
        )
        return LoginChallenge(
            authorization_url=f"https://login.example.com/authorize?{query}",
            state=self.state,
            flow={"state": self.state, "redirect_uri": redirect_uri},
        )

    def complete_login(self, flow: dict[str, Any], auth_response: dict[str, Any]) -> LoginResult:
        if auth_response.get("state") != flow.get("state"):
            raise AuthError("state mismatch")
        return LoginResult(
            subject="user-oid-1",
            claims={
                "name": "Test User",
                "preferred_username": "test@example.com",
                "oid": "user-oid-1",
            },
            token_cache=_TOKEN_CACHE_SENTINEL,
        )

    def acquire_sage_token(self, token_cache: str) -> str:
        if not token_cache:
            raise AuthError("no delegated token in session")
        return "sage-token"


@pytest.fixture
def auth_settings() -> BffAuthSettings:
    return BffAuthSettings(
        tenant_id="tenant-1",
        client_id="test-client",
        client_secret="secret",  # noqa: S106 -- test fixture, not a real secret
        sage_app_id_uri="api://sage",
        post_login_redirect="/app/",
    )


@pytest.fixture
async def auth_app(auth_settings):
    app = create_app()
    store = InMemorySessionStore()
    await store.open()
    app.state.bff_auth = BffAuthContext(settings=auth_settings, oidc=StubOidcService(), store=store)
    yield app
    await store.close()


@pytest.fixture
async def auth_client(auth_app):
    transport = ASGITransport(app=auth_app)
    # https base_url so the Secure session cookie round-trips back to /me, /logout.
    async with AsyncClient(transport=transport, base_url="https://test") as client:
        yield client


async def _sign_in(client: AsyncClient) -> str:
    """Drive login + callback; the session cookie lands in the client jar.

    Returns the issued ``state`` for assertions.
    """
    login = await client.get("/app/auth/login")
    state = login.json()["state"]
    callback = await client.get(f"/app/auth/callback?code=auth-code&state={state}")
    assert callback.status_code == 302
    return state


# ---------------------------------------------------------------------------
# A. Gating -- the on-box profile is unaffected
# ---------------------------------------------------------------------------


def test_a1_settings_absent_without_env():
    assert load_bff_auth_settings({}) is None
    partial = {"CAS_BFF_TENANT_ID": "t", "CAS_BFF_CLIENT_ID": "c"}
    assert load_bff_auth_settings(partial) is None


def test_a1_settings_present_with_full_env():
    settings = load_bff_auth_settings(
        {
            "CAS_BFF_TENANT_ID": "t",
            "CAS_BFF_CLIENT_ID": "c",
            "CAS_BFF_CLIENT_SECRET": "s",
            "CAS_BFF_SAGE_APP_ID_URI": "api://sage",
        }
    )
    assert settings is not None
    assert settings.sage_scope == "api://sage/Sage.Access"


async def test_a2_auth_routes_inert_when_unconfigured():
    """With no auth context, every auth route answers auth_not_configured (503)."""
    app = create_app()  # no app.state.bff_auth set
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="https://test") as client:
        for method, path in (
            ("GET", "/app/auth/login"),
            ("GET", "/app/auth/me"),
            ("POST", "/app/auth/logout"),
        ):
            resp = await client.request(method, path)
            assert resp.status_code == 503, (method, path, resp.text)
            assert resp.json()["code"] == "auth_not_configured"


async def test_a3_scan_path_unaffected_without_auth():
    """The existing /app/scan path is reached and unaffected by the auth layer."""
    app = create_app()
    app.state.vault_registry = {}
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="https://test") as client:
        resp = await client.post(
            "/app/scan", json={"vault_id": "no-such-vault", "directory": "/tmp"}
        )
    # The scan dependency resolves the (unknown) vault -> 404, not an auth error.
    assert resp.status_code == 404
    assert resp.json()["code"] == "vault_not_found"


# ---------------------------------------------------------------------------
# B. Login challenge
# ---------------------------------------------------------------------------


async def test_b1_login_returns_authorization_url_and_state(auth_client):
    resp = await auth_client.get("/app/auth/login")
    assert resp.status_code == 200
    body = resp.json()
    assert body["authorization_url"].startswith("https://login.example.com/authorize")
    assert body["state"]


async def test_b2_login_persists_pending_record(auth_client, auth_app):
    resp = await auth_client.get("/app/auth/login")
    state = resp.json()["state"]
    # The pre-login flow is held server-side, keyed by state; the response body
    # carries no flow/verifier.
    store = auth_app.state.bff_auth.store
    pending = await store.take_pending(state)
    assert pending is not None
    assert "redirect_uri" in pending.flow
    assert "redirect_uri" not in resp.json()


# ---------------------------------------------------------------------------
# C. Forwarded-host redirect-URI construction (AC#3)
# ---------------------------------------------------------------------------


async def test_c1_redirect_uri_honors_forwarded_headers(auth_client):
    resp = await auth_client.get(
        "/app/auth/login",
        headers={"x-forwarded-proto": "https", "x-forwarded-host": "cas.example.com"},
    )
    url = resp.json()["authorization_url"]
    assert "redirect_uri=https%3A%2F%2Fcas.example.com%2Fapp%2Fauth%2Fcallback" in url


async def test_c2_redirect_uri_falls_back_to_request_host(auth_client):
    resp = await auth_client.get("/app/auth/login")
    url = resp.json()["authorization_url"]
    assert "redirect_uri=https%3A%2F%2Ftest%2Fapp%2Fauth%2Fcallback" in url


def _make_request(headers: dict[str, str]) -> Request:
    raw = [(k.lower().encode(), v.encode()) for k, v in headers.items()]
    scope = {
        "type": "http",
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": "/",
        "raw_path": b"/",
        "query_string": b"",
        "headers": raw,
        "server": ("testserver", 80),
        "client": ("test", 1234),
    }
    return Request(scope)


def test_c3_external_base_url_branches():
    forwarded = _make_request(
        {"host": "testserver", "x-forwarded-proto": "https", "x-forwarded-host": "cas.example.com"}
    )
    assert external_base_url(forwarded) == "https://cas.example.com"

    direct = _make_request({"host": "plain.example.com"})
    assert external_base_url(direct) == "http://plain.example.com"


# ---------------------------------------------------------------------------
# D. Callback: cookie + session, no tokens in the SPA (AC#1)
# ---------------------------------------------------------------------------


async def test_d1_callback_sets_cookie_and_redirects(auth_client):
    login = await auth_client.get("/app/auth/login")
    state = login.json()["state"]
    resp = await auth_client.get(f"/app/auth/callback?code=auth-code&state={state}")

    assert resp.status_code == 302
    assert resp.headers["location"] == "/app/"

    set_cookie = resp.headers.get("set-cookie", "")
    assert "cas_session=" in set_cookie
    lowered = set_cookie.lower()
    assert "httponly" in lowered
    assert "secure" in lowered
    assert "samesite=lax" in lowered

    # No tokens reach the browser: neither the serialized cache nor a bearer
    # appears anywhere in the response delivered to the client.
    delivered = (resp.text + str(resp.headers)).lower()
    assert _TOKEN_CACHE_SENTINEL.lower() not in delivered
    assert "access_token" not in delivered
    assert "bearer" not in delivered


async def test_d2_callback_persists_session_with_cache(auth_client, auth_app):
    await _sign_in(auth_client)
    store = auth_app.state.bff_auth.store
    # Exactly one session was opened; it carries the externalized token cache.
    sessions = list(store._sessions.values())  # noqa: SLF001 -- test introspection
    assert len(sessions) == 1
    session = sessions[0]
    assert session.subject == "user-oid-1"
    assert session.token_cache == _TOKEN_CACHE_SENTINEL


# ---------------------------------------------------------------------------
# E. Callback failure modes
# ---------------------------------------------------------------------------


async def test_e1_unknown_state_is_rejected(auth_client, auth_app):
    resp = await auth_client.get("/app/auth/callback?code=auth-code&state=forged")
    assert resp.status_code == 400
    assert resp.json()["code"] == "invalid_state"
    # No session opened, no cookie set.
    assert auth_app.state.bff_auth.store._sessions == {}  # noqa: SLF001
    assert "set-cookie" not in resp.headers


async def test_e2_provider_error_is_rejected(auth_client, auth_app):
    resp = await auth_client.get(
        "/app/auth/callback?error=access_denied&error_description=user+declined"
    )
    assert resp.status_code == 400
    assert resp.json()["code"] == "auth_failed"
    assert auth_app.state.bff_auth.store._sessions == {}  # noqa: SLF001


# ---------------------------------------------------------------------------
# F. Session info + logout
# ---------------------------------------------------------------------------


async def test_f1_me_reports_authenticated_after_login(auth_client):
    await _sign_in(auth_client)
    resp = await auth_client.get("/app/auth/me")
    assert resp.status_code == 200
    body = resp.json()
    assert body["authenticated"] is True
    assert body["user"]["subject"] == "user-oid-1"
    assert body["user"]["name"] == "Test User"
    assert body["user"]["email"] == "test@example.com"


async def test_f2_me_reports_unauthenticated_without_cookie(auth_client):
    resp = await auth_client.get("/app/auth/me")
    assert resp.status_code == 200
    assert resp.json() == {"authenticated": False, "user": None}


async def test_f3_logout_clears_session_and_cookie(auth_client):
    await _sign_in(auth_client)
    logout = await auth_client.post("/app/auth/logout")
    assert logout.status_code == 204
    # The cookie is cleared (expired Set-Cookie).
    assert "cas_session=" in logout.headers.get("set-cookie", "")
    # The session is gone; clear the jar so a stale cookie cannot mask it.
    auth_client.cookies.clear()
    follow_up = await auth_client.get("/app/auth/me")
    assert follow_up.json()["authenticated"] is False


# ---------------------------------------------------------------------------
# G. On-behalf-of SAGE client (AC#2)
# ---------------------------------------------------------------------------


async def test_g1_obo_client_attaches_delegated_bearer():
    captured: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["authorization"] = request.headers.get("authorization", "")
        return httpx.Response(200, json={"ok": True})

    mock = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://sage")
    client = ObOSageClient("http://sage", StubOidcService(), client=mock)
    session = Session(
        session_id="s", subject="u", claims={}, token_cache="cache", expires_at=time.time() + 100
    )
    resp = await client.get("/sage_vaults/cas/documents/x", session)
    assert resp.status_code == 200
    assert captured["authorization"] == "Bearer sage-token"
    await client.aclose()


async def test_g2_obo_client_never_calls_sage_without_a_token():
    calls = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["count"] += 1
        return httpx.Response(200)

    mock = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://sage")
    client = ObOSageClient("http://sage", StubOidcService(), client=mock)
    # An empty token cache cannot mint a delegated token; the stub raises.
    session = Session(
        session_id="s", subject="u", claims={}, token_cache="", expires_at=time.time() + 100
    )
    with pytest.raises(AuthError):
        await client.get("/x", session)
    assert calls["count"] == 0  # SAGE was never reached anonymously
    await client.aclose()
