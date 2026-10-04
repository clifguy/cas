"""Hardening of the backend-for-frontend sign-in and session lifecycle.

Specified in ``tests/app/bff_auth_hardening_tests.md`` (AH-001 through AH-015).
The identity provider is a stub except in AH-001, which drives the real MSAL
library against a counting HTTP client; nothing here reaches a network. The
Postgres cases run against the disposable test database and skip without
``SAGE_TEST_PG_DSN``.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import threading
import time
import types
import uuid
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.backend.auth import session_store as session_store_module
from app.backend.auth.config import (
    LEGACY_SESSION_COOKIE_NAME,
    LOGIN_BINDING_COOKIE_NAME,
    BffAuthContext,
    BffAuthSettings,
)
from app.backend.auth.oidc import (
    AuthError,
    LoginChallenge,
    LoginResult,
    MsalOidcService,
    TokenGrant,
)
from app.backend.auth.rate_limit import LoginRateLimiter
from app.backend.auth.sage_client import ObOSageClient
from app.backend.auth.session_store import (
    InMemorySessionStore,
    PendingLogin,
    PostgresSessionStore,
    Session,
    TokenCacheCipher,
)
from sage.app import create_app

PG_DSN = os.environ.get("SAGE_TEST_PG_DSN")

_SECRET = "client-secret-one"  # noqa: S105 -- test fixture, not a real secret
_CACHE = "PLAINTEXT_TOKEN_CACHE_SENTINEL"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


class RecordingOidc:
    """A network-free identity-provider client recording the thread of each call."""

    def __init__(self, *, new_cache: str | None = None) -> None:
        self.state = "state-abc"
        self.threads: dict[str, int] = {}
        self.calls: list[str] = []
        self._new_cache = new_cache

    def begin_login(self, redirect_uri: str) -> LoginChallenge:
        self.threads["begin_login"] = threading.get_ident()
        self.calls.append("begin_login")
        return LoginChallenge(
            authorization_url="https://login.example.com/authorize",
            state=self.state,
            flow={"state": self.state, "redirect_uri": redirect_uri},
        )

    def complete_login(self, flow: dict[str, Any], auth_response: dict[str, Any]) -> LoginResult:
        self.threads["complete_login"] = threading.get_ident()
        self.calls.append("complete_login")
        if auth_response.get("state") != flow.get("state"):
            raise AuthError("state mismatch")
        return LoginResult(subject="user-1", claims={"oid": "user-1"}, token_cache=_CACHE)

    def acquire_sage_token(self, token_cache: str) -> TokenGrant:
        self.threads["acquire_sage_token"] = threading.get_ident()
        self.calls.append("acquire_sage_token")
        return TokenGrant(access_token="sage-token", token_cache=self._new_cache)


def _settings(**overrides: Any) -> BffAuthSettings:
    values: dict[str, Any] = {
        "tenant_id": "tenant-1",
        "client_id": "client-1",
        "client_secret": _SECRET,
        "sage_app_id_uri": "api://sage",
        "post_login_redirect": "/app/",
    }
    values.update(overrides)
    return BffAuthSettings(**values)


async def _auth_app(
    oidc: RecordingOidc | None = None,
    *,
    limiter: LoginRateLimiter | None = None,
) -> tuple[FastAPI, InMemorySessionStore, RecordingOidc]:
    oidc = oidc or RecordingOidc()
    store = InMemorySessionStore()
    await store.open()
    app = create_app()
    context_kwargs: dict[str, Any] = {"settings": _settings(), "oidc": oidc, "store": store}
    if limiter is not None:
        context_kwargs["login_limiter"] = limiter
    app.state.bff_auth = BffAuthContext(**context_kwargs)
    return app, store, oidc


def _browser(app: FastAPI) -> AsyncClient:
    # https so Secure cookies round-trip in the client jar.
    return AsyncClient(transport=ASGITransport(app=app), base_url="https://test")


def _set_cookies(response: httpx.Response) -> list[str]:
    return response.headers.get_list("set-cookie")


def _cookie_header(response: httpx.Response, name: str) -> str:
    matches = [value for value in _set_cookies(response) if value.startswith(f"{name}=")]
    assert len(matches) == 1, _set_cookies(response)
    return matches[0]


def _attributes(set_cookie: str) -> dict[str, str]:
    parts = [part.strip() for part in set_cookie.split(";")]
    attributes: dict[str, str] = {}
    for part in parts[1:]:
        key, _, value = part.partition("=")
        attributes[key.lower()] = value
    return attributes


def _state_digest(state: str) -> str:
    return hashlib.sha256(state.encode()).hexdigest()


# ---------------------------------------------------------------------------
# AH-001 -- discovery is fetched once per service, against real MSAL
# ---------------------------------------------------------------------------


class _Response:
    def __init__(self, status_code: int, body: dict[str, Any]) -> None:
        self.status_code = status_code
        self.text = json.dumps(body)
        self.headers: dict[str, str] = {}

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class _CountingHttpClient:
    """An MSAL-compatible HTTP client serving the tenant's OpenID configuration."""

    def __init__(self) -> None:
        self.urls: list[str] = []

    def get(self, url: str, params: Any = None, headers: Any = None, **kwargs: Any) -> _Response:
        self.urls.append(url)
        if url.endswith("/.well-known/openid-configuration"):
            base = "https://login.microsoftonline.com/tenant-1"
            return _Response(
                200,
                {
                    "authorization_endpoint": f"{base}/oauth2/v2.0/authorize",
                    "token_endpoint": f"{base}/oauth2/v2.0/token",
                    "issuer": f"{base}/v2.0",
                },
            )
        if url.endswith("/common/discovery/instance"):
            hosts = ["login.microsoftonline.com", "login.windows.net"]
            return _Response(
                200,
                {
                    "tenant_discovery_endpoint": (
                        "https://login.microsoftonline.com/tenant-1/v2.0/.well-known/"
                        "openid-configuration"
                    ),
                    "metadata": [
                        {
                            "preferred_network": hosts[0],
                            "preferred_cache": hosts[1],
                            "aliases": hosts,
                        }
                    ],
                },
            )
        return _Response(404, {})

    def post(
        self, url: str, params: Any = None, data: Any = None, headers: Any = None, **kwargs: Any
    ) -> _Response:
        self.urls.append(url)
        return _Response(400, {"error": "invalid_request"})

    def close(self) -> None:
        return None


@pytest.mark.filterwarnings("ignore:response_mode='form_post' is recommended")
def test_ah_001_discovery_is_fetched_once_per_service():
    http = _CountingHttpClient()
    service = MsalOidcService(_settings(), http_client=http)

    service.begin_login("https://cas.test/app/auth/callback")
    service.begin_login("https://cas.test/app/auth/callback")
    for _ in range(2):
        with pytest.raises(AuthError):
            service.acquire_sage_token("")

    assert (
        http.urls.count(
            "https://login.microsoftonline.com/tenant-1/v2.0/.well-known/openid-configuration"
        )
        == 1
    )
    assert http.urls.count("https://login.microsoftonline.com/common/discovery/instance") == 1


# ---------------------------------------------------------------------------
# AH-002 -- identity-provider calls run off the event loop
# ---------------------------------------------------------------------------


async def test_ah_002_identity_provider_calls_run_off_the_event_loop():
    loop_thread = threading.get_ident()
    app, store, oidc = await _auth_app()
    async with _browser(app) as browser:
        login = await browser.get("/app/auth/login")
        state = login.json()["state"]
        callback = await browser.get(f"/app/auth/callback?code=c&state={state}")
        assert callback.status_code == 302

    session = next(iter(store._sessions.values()))  # noqa: SLF001 -- test introspection
    client = ObOSageClient(
        "http://sage.test",
        oidc,
        store=store,
        client=httpx.AsyncClient(
            transport=httpx.MockTransport(lambda request: httpx.Response(200, json={})),
            base_url="http://sage.test",
        ),
    )
    await client.get("/health", session)

    assert set(oidc.threads) == {"begin_login", "complete_login", "acquire_sage_token"}
    for name, thread in oidc.threads.items():
        assert thread != loop_thread, name


# ---------------------------------------------------------------------------
# AH-003 / AH-004 / AH-011 -- sweeps and idle timeout, in memory
# ---------------------------------------------------------------------------


def _session(session_id: str, *, expires_in: float = 3600.0, last_seen_ago: float = 0.0) -> Session:
    now = time.time()
    return Session(
        session_id=session_id,
        subject=f"subject-{session_id}",
        claims={"n": 1},
        token_cache=_CACHE,
        expires_at=now + expires_in,
        last_seen_at=now - last_seen_ago,
    )


async def test_ah_003_put_pending_sweeps_expired_records_in_memory():
    store = InMemorySessionStore()
    await store.put_pending(PendingLogin(state="old", flow={}, expires_at=time.time() - 1))
    await store.put_pending(PendingLogin(state="live", flow={}, expires_at=time.time() + 600))
    await store.put_pending(PendingLogin(state="new", flow={}, expires_at=time.time() + 600))

    assert set(store._pending) == {"live", "new"}  # noqa: SLF001 -- test introspection


async def test_ah_004_create_session_sweeps_expired_and_idle_sessions_in_memory():
    store = InMemorySessionStore(idle_seconds=600)
    store._sessions["expired"] = _session("expired", expires_in=-1)  # noqa: SLF001
    store._sessions["idle"] = _session("idle", last_seen_ago=601)  # noqa: SLF001
    store._sessions["live"] = _session("live", last_seen_ago=10)  # noqa: SLF001

    await store.create_session(_session("new"))

    assert set(store._sessions) == {"live", "new"}  # noqa: SLF001


async def test_ah_011_idle_session_is_absent_and_reads_touch_when_stale_in_memory(monkeypatch):
    store = InMemorySessionStore(idle_seconds=600)
    store._sessions["idle"] = _session("idle", last_seen_ago=601)  # noqa: SLF001
    store._sessions["fresh"] = _session("fresh", last_seen_ago=30)  # noqa: SLF001
    store._sessions["stale"] = _session("stale", last_seen_ago=90)  # noqa: SLF001
    fresh_seen = store._sessions["fresh"].last_seen_at  # noqa: SLF001

    assert await store.get_session("idle") is None
    assert (await store.get_session("fresh")) is not None
    assert (await store.get_session("stale")) is not None

    assert store._sessions["fresh"].last_seen_at == fresh_seen  # noqa: SLF001 -- not touched
    assert store._sessions["stale"].last_seen_at >= time.time() - 5  # noqa: SLF001 -- touched


# ---------------------------------------------------------------------------
# AH-005 / AH-006 -- sign-in rate limit
# ---------------------------------------------------------------------------


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


async def test_ah_005_sign_in_is_rate_limited_per_client():
    clock = _Clock()
    limiter = LoginRateLimiter(limit=10, window_seconds=60, clock=clock)
    app, _store, oidc = await _auth_app(limiter=limiter)
    first = {"x-forwarded-for": "203.0.113.7"}
    other = {"x-forwarded-for": "198.51.100.9"}
    async with _browser(app) as browser:
        for _ in range(10):
            assert (await browser.get("/app/auth/login", headers=first)).status_code == 200
        calls_before = len(oidc.calls)

        refused = await browser.get("/app/auth/login", headers=first)
        assert refused.status_code == 429
        body = refused.json()
        assert body["code"] == "rate_limited"
        retry_after = body["detail"]["retry_after_seconds"]
        assert 0 < retry_after <= 60
        assert refused.headers["retry-after"] == str(retry_after)
        assert len(oidc.calls) == calls_before  # refused before the provider is called

        assert (await browser.get("/app/auth/login", headers=other)).status_code == 200

        clock.now += 61
        assert (await browser.get("/app/auth/login", headers=first)).status_code == 200


async def test_ah_006_rate_limit_keys_on_the_ingress_appended_address():
    limiter = LoginRateLimiter(limit=3, window_seconds=60, clock=_Clock())
    app, _store, _oidc = await _auth_app(limiter=limiter)
    async with _browser(app) as browser:
        statuses = [
            (
                await browser.get(
                    "/app/auth/login", headers={"x-forwarded-for": f"10.0.0.{n}, 203.0.113.7"}
                )
            ).status_code
            for n in range(5)
        ]
        assert statuses == [200, 200, 200, 429, 429]

        # Without the header the peer address keys the limit.
        no_header = [(await browser.get("/app/auth/login")).status_code for _ in range(4)]
        assert no_header == [200, 200, 200, 429]


async def test_ah_005b_hosted_app_refuses_with_retry_after():
    from app.backend.asgi import create_bff_app
    from sage.config import SageCoreConfig

    app = create_bff_app(stack_config=SageCoreConfig(profile="cloud"))
    store = InMemorySessionStore()
    app.state.bff_auth = BffAuthContext(
        settings=_settings(),
        oidc=RecordingOidc(),
        store=store,
        login_limiter=LoginRateLimiter(limit=1, window_seconds=60, clock=_Clock()),
    )
    async with _browser(app) as browser:
        assert (await browser.get("/app/auth/login")).status_code == 200
        refused = await browser.get("/app/auth/login")
    assert refused.status_code == 429
    assert refused.json()["code"] == "rate_limited"
    assert refused.headers["retry-after"] == str(refused.json()["detail"]["retry_after_seconds"])


def test_ah_016_limiter_forgets_the_least_recently_seen_client_beyond_its_bound():
    limiter = LoginRateLimiter(limit=1, window_seconds=60, max_clients=2, clock=_Clock())
    assert limiter.check("a") is None
    assert limiter.check("b") is None
    assert limiter.check("a") is not None  # a is at its limit, and now the most recent
    assert limiter.check("c") is None  # a third client evicts b, the least recent
    assert limiter.check("a") is not None  # a was kept
    assert limiter.check("b") is None  # b was forgotten, so it starts afresh


# ---------------------------------------------------------------------------
# AH-007 / AH-008 / AH-009 -- browser binding of the pending sign-in
# ---------------------------------------------------------------------------


async def test_ah_007_sign_in_sets_the_binding_cookie():
    app, _store, oidc = await _auth_app()
    async with _browser(app) as browser:
        login = await browser.get("/app/auth/login")

    cookie = _cookie_header(login, LOGIN_BINDING_COOKIE_NAME)
    assert LOGIN_BINDING_COOKIE_NAME == "__Host-cas_login"
    value = cookie.split(";", 1)[0].split("=", 1)[1]
    assert value == _state_digest(oidc.state)
    attributes = _attributes(cookie)
    assert "secure" in attributes
    assert "httponly" in attributes
    assert attributes["samesite"].lower() == "lax"
    assert attributes["path"] == "/"
    assert attributes["max-age"] == "600"
    assert "domain" not in attributes


async def test_ah_008_callback_from_another_browser_is_refused():
    app, store, _oidc = await _auth_app()
    async with _browser(app) as victim, _browser(app) as attacker:
        login = await victim.get("/app/auth/login")
        state = login.json()["state"]

        # No binding cookie at all.
        bare = await attacker.get(f"/app/auth/callback?code=c&state={state}")
        assert bare.status_code == 400
        assert bare.json()["code"] == "invalid_state"

        # Another browser's binding cookie.
        attacker.cookies.set(LOGIN_BINDING_COOKIE_NAME, _state_digest("other-state"))
        foreign = await attacker.get(f"/app/auth/callback?code=c&state={state}")
        assert foreign.status_code == 400
        assert foreign.json()["code"] == "invalid_state"

        assert store._sessions == {}  # noqa: SLF001 -- no session opened
        assert state in store._pending  # noqa: SLF001 -- pending record left in place

        # Positive control: the originating browser completes the sign-in.
        own = await victim.get(f"/app/auth/callback?code=c&state={state}")
        assert own.status_code == 302
        assert len(store._sessions) == 1  # noqa: SLF001


async def test_ah_009_successful_callback_expires_the_binding_cookie():
    app, _store, _oidc = await _auth_app()
    async with _browser(app) as browser:
        state = (await browser.get("/app/auth/login")).json()["state"]
        callback = await browser.get(f"/app/auth/callback?code=c&state={state}")

    assert callback.status_code == 302
    binding = _attributes(_cookie_header(callback, LOGIN_BINDING_COOKIE_NAME))
    assert binding["max-age"] == "0"
    _cookie_header(callback, "__Host-cas_session")


# ---------------------------------------------------------------------------
# AH-010 -- the __Host- session cookie
# ---------------------------------------------------------------------------


async def test_ah_010_session_cookie_uses_the_host_prefix():
    app, store, _oidc = await _auth_app()
    async with _browser(app) as browser:
        state = (await browser.get("/app/auth/login")).json()["state"]
        callback = await browser.get(f"/app/auth/callback?code=c&state={state}")
        cookie = _cookie_header(callback, "__Host-cas_session")
        attributes = _attributes(cookie)
        assert "secure" in attributes
        assert attributes["path"] == "/"
        assert "domain" not in attributes

        assert (await browser.get("/app/auth/me")).json()["authenticated"] is True

        logout = await browser.post("/app/auth/logout")
        assert logout.status_code == 204
        cleared = {value.split("=", 1)[0] for value in _set_cookies(logout)}
        assert {"__Host-cas_session", LEGACY_SESSION_COOKIE_NAME} <= cleared

    assert store._sessions == {}  # noqa: SLF001 -- sign-out removed the session

    # Control: a cookie under the legacy name no longer resolves a session.
    await store.create_session(_session("legacy-sid"))
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="https://test",
        cookies={LEGACY_SESSION_COOKIE_NAME: "legacy-sid"},
    ) as legacy:
        assert (await legacy.get("/app/auth/me")).json()["authenticated"] is False


# ---------------------------------------------------------------------------
# AH-012 -- the refreshed token cache is written back to the session
# ---------------------------------------------------------------------------


def _ok_sage() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json={})),
        base_url="http://sage.test",
    )


async def test_ah_012_changed_token_cache_is_written_back():
    store = InMemorySessionStore()
    await store.create_session(_session("sid"))
    stored = await store.get_session("sid")
    assert stored is not None
    # A detached copy, as a durable store returns: only a write through the
    # store can change what the store holds.
    session = dataclasses.replace(stored)

    changed = ObOSageClient(
        "http://sage.test", RecordingOidc(new_cache="REFRESHED"), store=store, client=_ok_sage()
    )
    await changed.get("/x", session)
    assert store._sessions["sid"].token_cache == "REFRESHED"  # noqa: SLF001


async def test_ah_012b_unchanged_token_cache_writes_nothing():
    class _CountingStore(InMemorySessionStore):
        writes = 0

        async def update_token_cache(self, session_id: str, token_cache: str) -> None:
            type(self).writes += 1
            await super().update_token_cache(session_id, token_cache)

    store = _CountingStore()
    await store.create_session(_session("sid"))
    session = await store.get_session("sid")
    assert session is not None

    unchanged = ObOSageClient(
        "http://sage.test", RecordingOidc(new_cache=None), store=store, client=_ok_sage()
    )
    await unchanged.get("/x", session)
    assert _CountingStore.writes == 0
    assert store._sessions["sid"].token_cache == _CACHE  # noqa: SLF001


async def test_ah_012c_standalone_app_wires_the_store_into_the_sage_client():
    from app.backend.asgi import create_bff_app
    from sage.config import SageCoreConfig

    store = InMemorySessionStore()
    app = create_bff_app(stack_config=SageCoreConfig(profile="cloud"))
    app.state.bff_auth = BffAuthContext(
        settings=_settings(sage_base_url="http://sage.test"), oidc=RecordingOidc(), store=store
    )
    from app.backend.asgi import _assemble_transport

    await _assemble_transport(app, SageCoreConfig(profile="cloud"))
    transport = app.state.sage_transport
    assert transport._client._store is store  # noqa: SLF001 -- wiring introspection


# ---------------------------------------------------------------------------
# AH-014 -- key derivation from the client secret
# ---------------------------------------------------------------------------


def test_ah_014_token_cache_key_derives_from_the_client_secret():
    ciphertext = TokenCacheCipher(_SECRET).encrypt(_CACHE)
    assert _CACHE not in ciphertext
    assert TokenCacheCipher(_SECRET).decrypt(ciphertext) == _CACHE
    with pytest.raises(ValueError):
        TokenCacheCipher("a-different-secret").decrypt(ciphertext)


async def test_ah_013d_hosted_init_hands_the_store_a_cipher_and_idle_limit(monkeypatch):
    from sage.app import _initialize_bff_auth

    captured: dict[str, Any] = {}

    class _RecordingStore:
        def __init__(self, conninfo: str, **kwargs: Any) -> None:
            captured.update(kwargs)

        async def open(self) -> None:
            return None

    settings = _settings(session_idle_seconds=1234)
    monkeypatch.setattr("app.backend.auth.config.load_bff_auth_settings", lambda env: settings)
    monkeypatch.setattr("app.backend.auth.session_store.PostgresSessionStore", _RecordingStore)
    stack_cfg = types.SimpleNamespace(
        profile="local",
        postgres=types.SimpleNamespace(
            host="h", port=5432, database="d", user="u", sslmode="disable"
        ),
    )
    app = FastAPI()
    await _initialize_bff_auth(app, stack_cfg)

    assert captured["idle_seconds"] == 1234
    cipher = captured["cipher"]
    assert TokenCacheCipher(_SECRET).decrypt(cipher.encrypt(_CACHE)) == _CACHE


# ---------------------------------------------------------------------------
# Postgres binding: AH-003, AH-004, AH-011, AH-013, AH-015
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not PG_DSN, reason="SAGE_TEST_PG_DSN not set")
class TestPostgresHardening:
    @pytest.fixture
    def pg_dsn(self, _provision_isolated_test_database) -> str:
        dsn = os.environ.get("SAGE_TEST_PG_DSN")
        if not dsn:
            pytest.skip("SAGE_TEST_PG_DSN not set")
        return dsn

    @pytest.fixture
    async def schema(self, pg_dsn):
        name = "sage_test_bff_" + uuid.uuid4().hex[:12]
        yield name
        import psycopg

        async with await psycopg.AsyncConnection.connect(pg_dsn, autocommit=True) as conn:
            await conn.execute(f'DROP SCHEMA IF EXISTS "{name}" CASCADE')  # noqa: S608

    async def _store(self, pg_dsn: str, schema: str, **kwargs: Any) -> PostgresSessionStore:
        kwargs.setdefault("cipher", TokenCacheCipher(_SECRET))
        store = PostgresSessionStore(pg_dsn, schema=schema, **kwargs)
        await store.open()
        return store

    async def _raw(self, pg_dsn: str, sql: str, params: tuple = ()) -> list[tuple]:
        import psycopg

        async with await psycopg.AsyncConnection.connect(pg_dsn, autocommit=True) as conn:
            cur = await conn.execute(sql, params)
            return await cur.fetchall()

    async def test_ah_003_put_pending_sweeps_expired_records(self, pg_dsn, schema):
        store = await self._store(pg_dsn, schema)
        try:
            await store.put_pending(PendingLogin(state="old", flow={}, expires_at=time.time() - 1))
            await store.put_pending(
                PendingLogin(state="live", flow={}, expires_at=time.time() + 600)
            )
            await store.put_pending(
                PendingLogin(state="new", flow={}, expires_at=time.time() + 600)
            )
            rows = await self._raw(pg_dsn, f'SELECT state FROM "{schema}".pending_logins')  # noqa: S608
            assert {row[0] for row in rows} == {"live", "new"}
        finally:
            await store.close()

    async def test_ah_004_create_session_sweeps_expired_and_idle(self, pg_dsn, schema):
        store = await self._store(pg_dsn, schema, idle_seconds=600)
        try:
            await store.create_session(_session("expired", expires_in=-1))
            await store.create_session(_session("idle", last_seen_ago=601))
            await store.create_session(_session("live", last_seen_ago=10))
            await store.create_session(_session("new"))
            rows = await self._raw(pg_dsn, f'SELECT session_id FROM "{schema}".sessions')  # noqa: S608
            assert {row[0] for row in rows} == {"live", "new"}
        finally:
            await store.close()

    async def test_ah_011_idle_session_is_absent_and_reads_touch_when_stale(self, pg_dsn, schema):
        store = await self._store(pg_dsn, schema, idle_seconds=600)
        try:
            await store.create_session(_session("fresh", last_seen_ago=30))
            await store.create_session(_session("stale", last_seen_ago=90))
            await store.create_session(_session("idle", last_seen_ago=601))
            seen = dict(
                await self._raw(
                    pg_dsn,
                    f'SELECT session_id, last_seen_at FROM "{schema}".sessions',  # noqa: S608
                )
            )

            assert await store.get_session("idle") is None
            assert await store.get_session("fresh") is not None
            assert await store.get_session("stale") is not None

            after = dict(
                await self._raw(
                    pg_dsn,
                    f'SELECT session_id, last_seen_at FROM "{schema}".sessions',  # noqa: S608
                )
            )
            assert after["fresh"] == seen["fresh"]
            assert after["stale"] > seen["stale"]
        finally:
            await store.close()

    async def test_ah_013_token_cache_is_encrypted_at_rest(self, pg_dsn, schema):
        store = await self._store(pg_dsn, schema)
        try:
            await store.create_session(_session("sid"))
            await store.update_token_cache("sid", _CACHE + "-REFRESHED")
            raw = await self._raw(pg_dsn, f'SELECT token_cache FROM "{schema}".sessions')  # noqa: S608
            assert len(raw) == 1
            assert _CACHE not in raw[0][0]
            got = await store.get_session("sid")
            assert got is not None
            assert got.token_cache == _CACHE + "-REFRESHED"
        finally:
            await store.close()

        rotated = await self._store(pg_dsn, schema, cipher=TokenCacheCipher("rotated-secret"))
        try:
            assert await rotated.get_session("sid") is None
            remaining = await self._raw(pg_dsn, f'SELECT session_id FROM "{schema}".sessions')  # noqa: S608
            assert remaining == []
        finally:
            await rotated.close()

    async def test_ah_015_bootstrap_upgrades_a_pre_existing_schema(self, pg_dsn, schema):
        # The schema as provisioned before the idle column and indexes existed.
        import psycopg

        async with await psycopg.AsyncConnection.connect(pg_dsn, autocommit=True) as conn:
            await conn.execute(f'CREATE SCHEMA "{schema}"')
            await conn.execute(
                f'CREATE TABLE "{schema}"."sessions" ('  # noqa: S608
                "session_id text PRIMARY KEY, subject text NOT NULL, claims jsonb NOT NULL, "
                "token_cache text NOT NULL, expires_at double precision NOT NULL)"
            )
            await conn.execute(
                f'CREATE TABLE "{schema}"."pending_logins" ('  # noqa: S608
                "state text PRIMARY KEY, flow jsonb NOT NULL, "
                "expires_at double precision NOT NULL)"
            )
            await conn.execute(
                f'INSERT INTO "{schema}".sessions VALUES '  # noqa: S608
                "('old', 's', '{}', %s, %s)",
                (TokenCacheCipher(_SECRET).encrypt(_CACHE), time.time() + 3600),
            )

        for _ in range(2):  # the second open is a no-op
            store = await self._store(pg_dsn, schema)
            await store.close()

        columns = await self._raw(
            pg_dsn,
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = %s AND table_name = 'sessions'",
            (schema,),
        )
        assert "last_seen_at" in {row[0] for row in columns}
        indexes = await self._raw(
            pg_dsn,
            "SELECT tablename, indexdef FROM pg_indexes WHERE schemaname = %s",
            (schema,),
        )
        expiry_indexed = {table for table, definition in indexes if "(expires_at)" in definition}
        assert expiry_indexed == {"sessions", "pending_logins"}

        store = await self._store(pg_dsn, schema)
        try:
            assert await store.get_session("old") is None  # pre-existing row reads as idle
        finally:
            await store.close()


def test_session_store_module_exposes_the_touch_interval():
    # The idle-touch throttle is a minute, which AH-011's stale/fresh pair straddles.
    assert session_store_module.TOUCH_INTERVAL_SECONDS == 60
