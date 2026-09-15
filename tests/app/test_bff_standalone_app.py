"""Tests for the standalone backend-for-frontend ASGI app (CAS-ADR-042).

The hosted profile runs the backend as its own process: it serves the SPA,
exposes a health probe, reverse-proxies the SPA's SAGE traffic with the user's
delegated token, and boots with no SAGE in process. These tests pin that boot
shape, the SPA serving (including deep-link client routing), the reverse proxy's
token attachment and auth gating, the profile boundary on the local-filesystem
scan/ingest routes, and the session requirement on every route that is not
exempt from it.

Test IDs follow APP-NNN (standalone APP).
"""

from __future__ import annotations

import json
import time

import httpx
import pytest
from fastapi import Depends, FastAPI

from app.backend.asgi import create_bff_app
from app.backend.auth.config import BffAuthContext
from app.backend.auth.dependencies import require_session
from app.backend.auth.sage_client import ObOSageClient
from app.backend.auth.session_store import InMemorySessionStore, Session
from app.backend.transport import HttpSageTransport, SageTransport
from sage.config import SageCoreConfig
from tests.helpers.bff_session import StubOidc as _StubOidc
from tests.helpers.bff_session import auth_app as _auth_app
from tests.helpers.bff_session import bff_settings as _settings
from tests.helpers.bff_session import live_session as _session
from tests.helpers.bff_session import sessioned_client as _sessioned_client


def _mock_sage(
    recorder: list[httpx.Request], *, json_body: dict | None = None
) -> httpx.AsyncClient:
    def handler(request: httpx.Request) -> httpx.Response:
        recorder.append(request)
        return httpx.Response(200, json=json_body if json_body is not None else {"ok": True})

    return httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://sage.test")


def _raising_sage(exc: httpx.RequestError) -> httpx.AsyncClient:
    """A SAGE client whose every upstream call raises the given httpx transport
    error, standing in for a SAGE that times out or is unreachable."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise exc.__class__(str(exc) or "boom", request=request)

    return httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://sage.test")


def _client(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://bff.test")


async def test_app_001_boots_without_sage_in_process():
    """The standalone app starts up (runs its lifespan) without ever creating a
    vault registry -- SAGE is not in this process.

    Anti-coincidental-pass: a lifespan that aliased the SAGE vault registry
    (as the co-located app does) would leave `vault_registry` set.
    """
    app = create_bff_app(stack_config=SageCoreConfig(profile="cloud"))
    async with app.router.lifespan_context(app):
        assert getattr(app.state, "vault_registry", None) is None


async def test_app_002_health_endpoint_is_green():
    """`GET /health` returns the constant store-free liveness envelope."""
    app = create_bff_app(stack_config=SageCoreConfig(profile="cloud"))
    async with app.router.lifespan_context(app):
        async with _client(app) as client:
            response = await client.get("/health")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert isinstance(body["version"], str) and body["version"]


async def test_app_003_spa_index_served_at_root(tmp_path):
    """`GET /` serves the SPA `index.html`.

    Anti-coincidental-pass: a missing/misdirected static mount would not return
    the index markup.
    """
    (tmp_path / "index.html").write_text("<!doctype html><title>CAS SPA</title>")
    app = create_bff_app(spa_dir=tmp_path, stack_config=SageCoreConfig(profile="cloud"))

    async with _client(app) as client:
        response = await client.get("/")

    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert "CAS SPA" in response.text


async def test_app_004_spa_deep_link_served(tmp_path):
    """`GET /documents` (a client route, not a file) returns the SPA shell so
    client-side routing resolves.

    Anti-coincidental-pass: a plain file-only static mount would 404 here; the
    catch-all is what makes a deep link resolve to `index.html`.
    """
    (tmp_path / "index.html").write_text("<!doctype html><title>CAS SPA</title>")
    app = create_bff_app(spa_dir=tmp_path, stack_config=SageCoreConfig(profile="cloud"))

    async with _client(app) as client:
        response = await client.get("/documents")

    assert response.status_code == 200
    assert "CAS SPA" in response.text


def test_app_005_existing_routers_mounted():
    """The standalone app mounts the scan/ingest and auth routers plus health."""
    app = create_bff_app(stack_config=SageCoreConfig(profile="cloud"))
    paths = {getattr(route, "path", None) for route in app.routes}

    for expected in (
        "/app/scan",
        "/app/ingest",
        "/app/auth/login",
        "/app/auth/callback",
        "/app/auth/me",
        "/app/auth/logout",
        "/health",
    ):
        assert expected in paths


async def test_app_006_proxy_forwards_with_obo_token():
    """A logged-in `/sage_vaults/*` request is reverse-proxied to SAGE and the
    upstream call carries the user's delegated bearer.

    Anti-coincidental-pass: a proxy that dropped the token, or short-circuited
    instead of forwarding, would not produce the captured upstream bearer.
    """
    app = create_bff_app(stack_config=SageCoreConfig(profile="cloud"))
    recorder: list[httpx.Request] = []
    oidc = _StubOidc("tok-xyz")
    settings = _settings()
    store = InMemorySessionStore()
    await store.create_session(_session())
    app.state.bff_auth = BffAuthContext(settings=settings, oidc=oidc, store=store)
    app.state.sage_transport = HttpSageTransport(
        ObOSageClient("http://sage.test", oidc, client=_mock_sage(recorder, json_body={"stat": 1}))
    )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://bff.test",
        cookies={settings.session_cookie_name: "sid-1"},
    ) as client:
        response = await client.get("/sage_vaults/cas/stats")

    assert response.status_code == 200
    assert response.json() == {"stat": 1}
    assert len(recorder) == 1
    assert recorder[0].headers["authorization"] == "Bearer tok-xyz"
    assert recorder[0].url.path == "/sage_vaults/cas/stats"


async def test_app_007_proxy_rejects_without_a_session():
    """An unauthenticated `/sage_vaults/*` request is refused and SAGE is never
    reached.

    Anti-coincidental-pass: a proxy that forwarded before checking the session
    would record an upstream call.
    """
    app = create_bff_app(stack_config=SageCoreConfig(profile="cloud"))
    recorder: list[httpx.Request] = []
    oidc = _StubOidc()
    store = InMemorySessionStore()
    app.state.bff_auth = BffAuthContext(settings=_settings(), oidc=oidc, store=store)
    app.state.sage_transport = HttpSageTransport(
        ObOSageClient("http://sage.test", oidc, client=_mock_sage(recorder))
    )

    async with _client(app) as client:
        response = await client.get("/sage_vaults/cas/stats")  # no session cookie

    assert response.status_code == 401
    assert recorder == []


async def test_app_007b_proxy_without_auth_configured_returns_503():
    """With no auth context (a deployment missing its identity coordinates) the
    proxy answers the structured `auth_not_configured` 503 rather than failing
    opaquely -- the app still boots and serves the SPA and health probe."""
    app = create_bff_app(stack_config=SageCoreConfig(profile="cloud"))

    async with _client(app) as client:
        response = await client.get("/sage_vaults/cas/stats")

    assert response.status_code == 503
    assert response.json()["code"] == "auth_not_configured"


async def test_app_008_scan_ingest_is_profile_bounded():
    """In the standalone app (no vault registry) the local-filesystem scan
    route returns the typed `local_profile_only` error, not a 500 / AttributeError.

    Anti-coincidental-pass: the pre-change code read `app.state.vault_registry`
    unguarded, raising `AttributeError` -> 500 here. The request carries a
    signed-in session, so it reaches the route past the session requirement.
    """
    app = await _auth_app(with_session=True)

    async with _sessioned_client(app) as client:
        response = await client.post("/app/scan", json={"vault_id": "cas", "directory": "/tmp"})

    assert response.status_code == 501
    assert response.json()["code"] == "local_profile_only"


async def test_app_009_proxy_forwards_bare_collection_get(tmp_path):
    """A logged-in bare `GET /sage_vaults` (list vaults) is reverse-proxied to
    SAGE's canonical collection path -- not swallowed by the SPA catch-all.

    Anti-coincidental-pass: the SPA bundle is mounted here, reproducing the
    production topology. Before the bare-collection route existed, `/sage_vaults`
    (no trailing segment) missed the `/sage_vaults/{path:path}` route and fell
    through to the SPA catch-all, returning `200` HTML and never touching the
    transport -- `recorder` would stay empty and `response.json()` would raise on
    the HTML body. Asserting the upstream path is exactly `/sage_vaults` (no
    trailing slash) also kills a fix that forwarded `/sage_vaults/`.
    """
    (tmp_path / "index.html").write_text("<!doctype html><title>CAS SPA</title>")
    app = create_bff_app(spa_dir=tmp_path, stack_config=SageCoreConfig(profile="cloud"))
    recorder: list[httpx.Request] = []
    oidc = _StubOidc("tok-xyz")
    settings = _settings()
    store = InMemorySessionStore()
    await store.create_session(_session())
    app.state.bff_auth = BffAuthContext(settings=settings, oidc=oidc, store=store)
    app.state.sage_transport = HttpSageTransport(
        ObOSageClient(
            "http://sage.test", oidc, client=_mock_sage(recorder, json_body=[{"vault_id": "cas"}])
        )
    )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://bff.test",
        cookies={settings.session_cookie_name: "sid-1"},
    ) as client:
        response = await client.get("/sage_vaults")

    assert response.status_code == 200
    assert response.json() == [{"vault_id": "cas"}]
    assert len(recorder) == 1
    assert recorder[0].method == "GET"
    assert recorder[0].url.path == "/sage_vaults"
    assert recorder[0].headers["authorization"] == "Bearer tok-xyz"


async def test_app_010_proxy_forwards_bare_collection_post(tmp_path):
    """A logged-in bare `POST /sage_vaults` (create vault) is reverse-proxied to
    SAGE's canonical collection path with the request body forwarded.

    Anti-coincidental-pass: the bare path falling to the SPA catch-all records no
    upstream POST (`recorder` empty); a forwarder that dropped the body would not
    round-trip the config payload captured upstream.
    """
    (tmp_path / "index.html").write_text("<!doctype html><title>CAS SPA</title>")
    app = create_bff_app(spa_dir=tmp_path, stack_config=SageCoreConfig(profile="cloud"))
    recorder: list[httpx.Request] = []
    oidc = _StubOidc("tok-xyz")
    settings = _settings()
    store = InMemorySessionStore()
    await store.create_session(_session())
    app.state.bff_auth = BffAuthContext(settings=settings, oidc=oidc, store=store)
    app.state.sage_transport = HttpSageTransport(
        ObOSageClient(
            "http://sage.test", oidc, client=_mock_sage(recorder, json_body={"vault_id": "new"})
        )
    )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://bff.test",
        cookies={settings.session_cookie_name: "sid-1"},
    ) as client:
        response = await client.post("/sage_vaults", json={"config": {"vault_id": "new"}})

    assert response.status_code == 200
    assert response.json() == {"vault_id": "new"}
    assert len(recorder) == 1
    assert recorder[0].method == "POST"
    assert recorder[0].url.path == "/sage_vaults"
    assert json.loads(recorder[0].content) == {"config": {"vault_id": "new"}}


async def test_app_011_proxy_rejects_bare_collection_without_a_session(tmp_path):
    """An unauthenticated bare `GET /sage_vaults` is refused with the structured
    JSON `auth_required` (401) -- never HTML -- and SAGE is never reached.

    Anti-coincidental-pass: with the SPA bundle mounted, the pre-change code let
    the unauthenticated bare call fall to the SPA catch-all and return `200` HTML,
    not a `401`. The content-type check is the direct guard against the SPA shell
    silently satisfying the SPA's `listVaults()` JSON parse.
    """
    (tmp_path / "index.html").write_text("<!doctype html><title>CAS SPA</title>")
    app = create_bff_app(spa_dir=tmp_path, stack_config=SageCoreConfig(profile="cloud"))
    recorder: list[httpx.Request] = []
    oidc = _StubOidc()
    store = InMemorySessionStore()
    app.state.bff_auth = BffAuthContext(settings=_settings(), oidc=oidc, store=store)
    app.state.sage_transport = HttpSageTransport(
        ObOSageClient("http://sage.test", oidc, client=_mock_sage(recorder))
    )

    async with _client(app) as client:
        response = await client.get("/sage_vaults")  # no session cookie

    assert response.status_code == 401
    assert response.json()["code"] == "auth_required"
    assert "text/html" not in response.headers["content-type"]
    assert recorder == []


async def test_app_012_proxy_maps_upstream_timeout_to_structured_504():
    """When the upstream SAGE call times out, the proxy returns a structured
    `504` envelope -- never an opaque `500` with a traceback.

    Anti-coincidental-pass: without the transport-exception guard in
    `_forward_to_sage`, the `httpx.ReadTimeout` propagates out of the ASGI app
    (ASGITransport re-raises by default) instead of becoming a `504`, so the
    request never yields the structured envelope this asserts.
    """
    app = create_bff_app(stack_config=SageCoreConfig(profile="cloud"))
    oidc = _StubOidc("tok-xyz")
    settings = _settings()
    store = InMemorySessionStore()
    await store.create_session(_session())
    app.state.bff_auth = BffAuthContext(settings=settings, oidc=oidc, store=store)
    app.state.sage_transport = HttpSageTransport(
        ObOSageClient("http://sage.test", oidc, client=_raising_sage(httpx.ReadTimeout("slow")))
    )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://bff.test",
        cookies={settings.session_cookie_name: "sid-1"},
    ) as client:
        response = await client.get("/sage_vaults/cas/stats")

    assert response.status_code == 504
    body = response.json()
    assert body["code"] == "sage_upstream_timeout"
    assert body["message"]


async def test_app_013_proxy_maps_upstream_transport_error_to_structured_502():
    """When the upstream SAGE call fails with a non-timeout transport error, the
    proxy returns a structured `502` envelope -- distinct from the `504` a
    timeout gets, since `httpx.TimeoutException` subclasses `TransportError` and
    must be matched first.

    Anti-coincidental-pass: without the transport-exception guard the
    `httpx.ConnectError` propagates unhandled; a guard that caught
    `TransportError` before `TimeoutException` would mis-map a timeout to `502`.
    """
    app = create_bff_app(stack_config=SageCoreConfig(profile="cloud"))
    oidc = _StubOidc("tok-xyz")
    settings = _settings()
    store = InMemorySessionStore()
    await store.create_session(_session())
    app.state.bff_auth = BffAuthContext(settings=settings, oidc=oidc, store=store)
    app.state.sage_transport = HttpSageTransport(
        ObOSageClient("http://sage.test", oidc, client=_raising_sage(httpx.ConnectError("down")))
    )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://bff.test",
        cookies={settings.session_cookie_name: "sid-1"},
    ) as client:
        response = await client.get("/sage_vaults/cas/stats")

    assert response.status_code == 502
    body = response.json()
    assert body["code"] == "sage_upstream_unavailable"
    assert body["message"]


def _streaming_sage(recorder: list[httpx.Request]) -> httpx.AsyncClient:
    """A SAGE stand-in that answers the content route with a chunked body and
    download headers, and any other path with a JSON body."""

    def handler(request: httpx.Request) -> httpx.Response:
        recorder.append(request)
        if request.url.path.endswith("/content"):

            async def chunks():
                yield b"raw-"
                yield b"bytes"

            return httpx.Response(
                200,
                content=chunks(),
                headers={
                    "Content-Type": "application/pdf",
                    "Content-Disposition": 'attachment; filename="x.pdf"',
                },
            )
        return httpx.Response(200, json={"ok": True})

    return httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://sage.test")


async def _signed_in_app(transport) -> tuple:
    """A cloud-profile BFF app with a signed-in session and the given transport."""
    app = create_bff_app(stack_config=SageCoreConfig(profile="cloud"))
    settings = _settings()
    store = InMemorySessionStore()
    await store.create_session(_session())
    app.state.bff_auth = BffAuthContext(settings=settings, oidc=_StubOidc("tok-xyz"), store=store)
    app.state.sage_transport = transport
    return app, settings


async def test_app_014_proxy_streams_content_route():
    """A logged-in GET on the document content route is relayed as a stream:
    body intact, download headers (Content-Type, Content-Disposition) relayed,
    delegated bearer attached upstream.

    Anti-coincidental-pass: the header-relay assertions are load-bearing --
    dropping Content-Disposition in the proxy's header filtering would break
    the browser download even though the bytes round-trip.
    """
    recorder: list[httpx.Request] = []
    oidc = _StubOidc("tok-xyz")
    app = create_bff_app(stack_config=SageCoreConfig(profile="cloud"))
    settings = _settings()
    store = InMemorySessionStore()
    await store.create_session(_session())
    app.state.bff_auth = BffAuthContext(settings=settings, oidc=oidc, store=store)
    app.state.sage_transport = HttpSageTransport(
        ObOSageClient("http://sage.test", oidc, client=_streaming_sage(recorder))
    )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://bff.test",
        cookies={settings.session_cookie_name: "sid-1"},
    ) as client:
        response = await client.get("/sage_vaults/cas/documents/d1/content")

    assert response.status_code == 200
    assert response.content == b"raw-bytes"
    assert response.headers["content-type"] == "application/pdf"
    assert response.headers["content-disposition"] == 'attachment; filename="x.pdf"'
    assert len(recorder) == 1
    assert recorder[0].headers["authorization"] == "Bearer tok-xyz"


class _SpyTransport(SageTransport):
    """Wraps a real transport, recording which port method each call took."""

    def __init__(self, inner: SageTransport) -> None:
        self._inner = inner
        self.calls: list[str] = []

    async def request(self, method, path, **kwargs):
        self.calls.append(f"request {method} {path}")
        return await self._inner.request(method, path, **kwargs)

    async def stream(self, method, path, **kwargs):
        self.calls.append(f"stream {method} {path}")
        return await self._inner.stream(method, path, **kwargs)


async def test_app_015_only_content_route_streams():
    """The streaming branch is scoped to GET on the document content route:
    other paths -- and non-GET methods on a content-shaped path -- keep the
    buffered `request()` port method.

    Anti-coincidental-pass: a bare `/content` suffix match or a method-agnostic
    branch would route the POST (c) through `stream()`.
    """
    recorder: list[httpx.Request] = []
    oidc = _StubOidc("tok-xyz")
    spy = _SpyTransport(
        HttpSageTransport(ObOSageClient("http://sage.test", oidc, client=_streaming_sage(recorder)))
    )
    app, settings = await _signed_in_app(spy)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://bff.test",
        cookies={settings.session_cookie_name: "sid-1"},
    ) as client:
        await client.get("/sage_vaults/cas/documents/d1/content")  # (a) streams
        await client.get("/sage_vaults/cas/documents/d1")  # (b) buffered
        await client.post("/sage_vaults/cas/documents/d1/content")  # (c) buffered

    assert spy.calls == [
        "stream GET /sage_vaults/cas/documents/d1/content",
        "request GET /sage_vaults/cas/documents/d1",
        "request POST /sage_vaults/cas/documents/d1/content",
    ]


async def test_app_016_streaming_route_requires_session():
    """An unauthenticated GET on the content route is refused with the
    structured 401 and SAGE is never reached -- the streaming branch sits after
    the session gate.

    Anti-coincidental-pass: a streaming branch placed before the session check
    would record an upstream call.
    """
    recorder: list[httpx.Request] = []
    oidc = _StubOidc()
    app = create_bff_app(stack_config=SageCoreConfig(profile="cloud"))
    store = InMemorySessionStore()
    app.state.bff_auth = BffAuthContext(settings=_settings(), oidc=oidc, store=store)
    app.state.sage_transport = HttpSageTransport(
        ObOSageClient("http://sage.test", oidc, client=_streaming_sage(recorder))
    )

    async with _client(app) as client:
        response = await client.get("/sage_vaults/cas/documents/d1/content")  # no cookie

    assert response.status_code == 401
    assert response.json()["code"] == "auth_required"
    assert recorder == []


async def test_app_017_stream_open_failures_map_to_504_and_502():
    """A transport failure while opening the stream (before any headers) maps
    to the same structured envelopes as the buffered path: timeout -> 504
    `sage_upstream_timeout`, other transport error -> 502
    `sage_upstream_unavailable`.

    Anti-coincidental-pass: an unwrapped `stream()` call lets the httpx error
    propagate as an opaque 500 (ASGITransport re-raises), never yielding the
    structured envelope; catching `TransportError` before `TimeoutException`
    would mis-map the timeout to 502.
    """
    for exc, status, code in (
        (httpx.ConnectTimeout("slow"), 504, "sage_upstream_timeout"),
        (httpx.ConnectError("down"), 502, "sage_upstream_unavailable"),
    ):
        oidc = _StubOidc("tok-xyz")
        app, settings = await _signed_in_app(
            HttpSageTransport(ObOSageClient("http://sage.test", oidc, client=_raising_sage(exc)))
        )

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://bff.test",
            cookies={settings.session_cookie_name: "sid-1"},
        ) as client:
            response = await client.get("/sage_vaults/cas/documents/d1/content")

        assert response.status_code == status
        assert response.json()["code"] == code


class _CredentialCloseRecorder:
    """Async stand-in for ``close_postgres_credential`` that counts its calls."""

    def __init__(self) -> None:
        self.calls = 0

    async def __call__(self) -> None:
        self.calls += 1


async def test_app_018_shutdown_closes_postgres_credential(monkeypatch):
    """The standalone backend releases the process-wide async Entra credential at
    shutdown. The hosted profile builds it in ``_initialize_bff_auth`` for the
    session store's managed-identity pool, so a clean shutdown must close it or it
    leaks its aiohttp session as an ``Unclosed client session`` warning -- the same
    hazard the co-located SAGE app's lifespan closes.

    Anti-coincidental-pass: the unmodified lifespan closes the transport and the
    session store but never the credential; ``cred.calls`` stays 0 without the
    wiring. ``_initialize_bff_auth`` is stubbed to a hermetic no-op (no Postgres,
    no identity provider), so the assertion isolates the shutdown close -- which is
    unconditional -- from whether a credential was actually built.
    """

    async def _noop_bff_auth(app, stack_cfg):
        app.state.bff_auth = None

    monkeypatch.setattr("sage.app._initialize_bff_auth", _noop_bff_auth)
    cred = _CredentialCloseRecorder()
    monkeypatch.setattr("sage.storage.postgres.managed_identity.close_postgres_credential", cred)
    app = create_bff_app(stack_config=SageCoreConfig(profile="cloud"))

    async with app.router.lifespan_context(app):
        pass

    assert cred.calls == 1


# ---------------------------------------------------------------------------
# APP-019 -- APP-024: every non-exempt route requires a signed-in session
# ---------------------------------------------------------------------------

#: The fields each application-backend operation declares. The unsessioned
#: refusal is the same for every request shape, so it names none of them, where
#: ``unknown_parameter`` would. The names are published in the specification;
#: the property held is that the refusal does not vary with the request.
_DECLARED_FIELDS = {
    "/app/scan": ("directory", "max_depth"),
    "/app/ingest": ("files", "dry_run", "infer_edges"),
}

_SCAN_BODY = {"vault_id": "cas", "directory": "/tmp"}
_INGEST_BODY = {
    "vault_id": "cas",
    "files": [{"file_path": "/tmp/x.md", "source_type": "markdown"}],
}


@pytest.mark.parametrize(
    ("path", "body", "params"),
    [
        ("/app/scan", _SCAN_BODY, {}),
        ("/app/scan", _SCAN_BODY, {"bogus_q": "1"}),
        ("/app/scan", {**_SCAN_BODY, "bogus_field_x": 1}, {}),
        ("/app/scan", {"directory": "/tmp"}, {}),
        ("/app/ingest", _INGEST_BODY, {}),
        ("/app/ingest", _INGEST_BODY, {"bogus_q": "1"}),
        ("/app/ingest", {**_INGEST_BODY, "bogus_field_x": 1}, {}),
        ("/app/ingest", {"files": []}, {}),
    ],
    ids=[
        "scan_well_formed",
        "scan_query_parameter",
        "scan_body_field",
        "scan_invalid_body",
        "ingest_well_formed",
        "ingest_query_parameter",
        "ingest_body_field",
        "ingest_invalid_body",
    ],
)
async def test_app_019_scan_ingest_refuse_without_a_session(path, body, params):
    """Without a session, scan and ingest answer `auth_required` 401 before the
    request-name refusal, body validation, or the profile boundary run.

    Anti-coincidental-pass: ungated, the well-formed rows answer the
    `local_profile_only` 501, the undeclared-name rows the `unknown_parameter`
    400, and the invalid-body rows a validation 400. A session requirement that
    ran after the request-name refusal would still answer those rows with 400s
    whose `valid_params` enumerates the declared fields, which the name check
    below rejects.
    """
    app = await _auth_app(with_session=False)

    async with _client(app) as client:
        response = await client.post(path, params=params, json=body)

    assert response.status_code == 401, response.text
    assert response.json()["code"] == "auth_required"
    assert "valid_params" not in response.text
    for field in _DECLARED_FIELDS[path]:
        assert field not in response.text, (field, response.text)


@pytest.mark.parametrize("cookie", ["unknown-session", "sid-expired"])
async def test_app_019b_unknown_or_expired_session_cookie_is_refused(cookie):
    """A cookie naming no live session is refused like no cookie at all.

    Anti-coincidental-pass: a requirement that checked only for the cookie's
    presence would let both rows through to the 501.
    """
    app = await _auth_app(with_session=False)
    expired = Session(
        session_id="sid-expired",
        subject="user-1",
        claims={},
        token_cache="cache-blob",
        expires_at=time.time() - 1,
    )
    await app.state.bff_auth.store.create_session(expired)

    async with _sessioned_client(app, cookie) as client:
        response = await client.post("/app/scan", json=_SCAN_BODY)

    assert response.status_code == 401
    assert response.json()["code"] == "auth_required"


async def test_app_020_scan_refusal_matches_the_proxy_refusal():
    """The application-backend routes and the SAGE proxy refuse an unsessioned
    request with the same envelope.

    Anti-coincidental-pass: a refusal authored separately from the proxy's
    would drift in its message and fail the equality.
    """
    app = await _auth_app(with_session=False)

    async with _client(app) as client:
        scan = await client.post("/app/scan", json=_SCAN_BODY)
        proxied = await client.get("/sage_vaults/cas/stats")

    assert scan.status_code == proxied.status_code == 401
    assert scan.json() == proxied.json()


async def test_app_021_scan_without_auth_configured_returns_503():
    """With sign-in unconfigured, scan answers `auth_not_configured` 503, as the
    proxy does, rather than reaching the profile boundary.

    Anti-coincidental-pass: a requirement that passed the request through when
    no auth context exists would answer the `local_profile_only` 501.
    """
    app = create_bff_app(stack_config=SageCoreConfig(profile="cloud"))

    async with _client(app) as client:
        response = await client.post("/app/scan", json=_SCAN_BODY)

    assert response.status_code == 503
    assert response.json()["code"] == "auth_not_configured"


async def test_app_022_undecodable_body_names_no_declared_field():
    """An unsessioned request whose body is not JSON never reaches the route, and
    its answer names no declared field.

    The framework decodes a JSON body before it resolves dependencies, so this
    answer may precede the session requirement; the test holds only what that
    answer may disclose, whichever check produces it.

    Anti-coincidental-pass: an answer that reached the route would be the 501;
    one that enumerated the operation's fields would carry their names.
    """
    app = await _auth_app(with_session=False)

    async with _client(app) as client:
        response = await client.post(
            "/app/scan", content=b"{", headers={"content-type": "application/json"}
        )

    assert 400 <= response.status_code < 500, response.text
    assert response.status_code != 501
    for field in _DECLARED_FIELDS["/app/scan"]:
        assert field not in response.text, (field, response.text)


#: Routes of the standalone app that answer without a signed-in session, each
#: with the reason. Every other route must depend on ``require_session``.
SESSION_EXEMPT_ROUTES: dict[str, str] = {
    "/app/auth/login": "begins sign-in, so it cannot require a session",
    "/app/auth/callback": "completes sign-in and opens the session",
    "/app/auth/me": "reports whether a session exists",
    "/app/auth/logout": "ends a session, and is harmless without one",
    "/health": "container liveness probe, constant and store-free",
    "/{spa_path:path}": "the SPA shell, which renders the sign-in surface",
    "/assets": "the SPA's static bundle",
    "/openapi.json": "the generated schema of the published operations",
    "/docs": "interactive documentation of the published operations",
    "/docs/oauth2-redirect": "interactive documentation of the published operations",
    "/redoc": "interactive documentation of the published operations",
}


def _requires_session(route: object) -> bool:
    """Whether a route's dependency tree includes ``require_session``."""
    dependant = getattr(route, "dependant", None)
    if dependant is None:
        return False
    pending = [dependant]
    while pending:
        current = pending.pop()
        if current.call is require_session:
            return True
        pending.extend(current.dependencies)
    return False


def test_app_023_every_route_is_session_gated_or_exempt(tmp_path):
    """Every route the standalone app serves either requires a signed-in session
    or is on the exemption list, and every exemption names an ungated route the
    app serves. A route later added to the application-backend router inherits
    the requirement, and one added elsewhere fails here until it is gated or
    exempted with a reason.

    Anti-coincidental-pass: the SPA bundle and its assets directory are staged so
    the catch-all and static mount exist; without them their exemptions would be
    stale and fail the second check rather than be exercised.
    """
    (tmp_path / "index.html").write_text("<!doctype html>")
    (tmp_path / "assets").mkdir()
    app = create_bff_app(spa_dir=tmp_path, stack_config=SageCoreConfig(profile="cloud"))

    # One entry per route object, never keyed by path alone: two routes can share
    # a path, and a dict would let the later one's classification mask the other.
    census = [(route.path, _requires_session(route)) for route in app.routes]
    signatures = [(r.path, frozenset(getattr(r, "methods", None) or ())) for r in app.routes]
    assert len(signatures) == len(set(signatures)), f"routes sharing path and methods: {signatures}"

    ungated = sorted(p for p, gated in census if not gated and p not in SESSION_EXEMPT_ROUTES)
    assert not ungated, f"routes neither session-gated nor exempt: {ungated}"
    stale = sorted(
        p
        for p in SESSION_EXEMPT_ROUTES
        if not any(path == p for path, _ in census) or any(path == p and g for path, g in census)
    )
    assert not stale, f"exemptions naming a gated or absent route: {stale}"
    assert all(gated for path, gated in census if path in ("/app/scan", "/app/ingest"))


def test_app_023b_gate_detector_has_teeth():
    """The detector distinguishes a gated route from an ungated one, so the gate
    above is not satisfied vacuously."""
    probe = FastAPI()

    @probe.get("/open")
    async def _open() -> None:
        return None

    @probe.get("/gated", dependencies=[Depends(require_session)])
    async def _gated() -> None:
        return None

    classified = {route.path: _requires_session(route) for route in probe.routes}
    assert classified["/open"] is False
    assert classified["/gated"] is True


def test_app_024_co_located_router_is_not_session_gated(tmp_path):
    """The co-located application includes the same router without the session
    requirement, and the router itself does not carry it: the requirement is
    attached where the standalone app includes the router.

    Anti-coincidental-pass: attaching the requirement to the router, or to each
    route, would also pass APP-019 and APP-023, but would gate the co-located
    local profile, which has no identity to present.
    """
    from app.backend.router import router as app_backend_router
    from sage.app import create_app
    from sage.config import VaultConfig
    from tests.app.test_app_backend import _make_vault_config_dict

    assert all(d.dependency is not require_session for d in app_backend_router.dependencies)

    config = VaultConfig.model_validate(_make_vault_config_dict(tmp_path, "example_vault", "Ex"))
    co_located = create_app(config=config)
    routes = {getattr(r, "path", None): r for r in co_located.routes}
    for path in ("/app/scan", "/app/ingest"):
        assert path in routes
        assert not _requires_session(routes[path]), path
