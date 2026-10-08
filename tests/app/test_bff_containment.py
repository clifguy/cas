"""Path and header containment of the standalone backend-for-frontend (CAS-ADR-042).

The backend serves the files of its SPA bundle and nothing else, forwards only
paths beneath ``/sage_vaults`` to SAGE, exchanges only allowlisted headers with
SAGE, and publishes no interactive documentation page. Path cases a normalizing
HTTP client would rewrite before sending run against a real uvicorn server with
raw request targets.

Test IDs follow CTN-NNN; the specification is ``bff_containment_tests.md``.
"""

from __future__ import annotations

import http.client
import json
from pathlib import Path
from urllib.parse import quote, urlsplit

import httpx
import pytest
from fastapi import FastAPI

from app.backend.asgi import create_bff_app
from app.backend.auth.config import BffAuthContext
from app.backend.auth.sage_client import ObOSageClient
from app.backend.auth.session_store import InMemorySessionStore
from app.backend.transport import HttpSageTransport
from sage.config import SageCoreConfig
from tests.deploy._stub_server import serve_uvicorn
from tests.helpers.bff_session import SESSION_ID, StubOidc, auth_app, bff_settings, live_session
from tests.helpers.bff_session import sessioned_client as _sessioned_client

_SENTINEL = b"sentinel-outside-the-bundle"
_INDEX = b"<!doctype html><title>spa-shell</title>"
_TOP_FILE = b"top-level-bundle-file"
_ASSET = b"console.log('asset');"


def _stage_bundle(root: Path) -> tuple[Path, Path]:
    """Stage an SPA bundle under ``root/bundle`` and a sentinel file beside it."""
    bundle = root / "bundle"
    (bundle / "assets").mkdir(parents=True)
    (bundle / "index.html").write_bytes(_INDEX)
    (bundle / "robots.txt").write_bytes(_TOP_FILE)
    (bundle / "assets" / "app.js").write_bytes(_ASSET)
    sentinel = root / "sentinel.txt"
    sentinel.write_bytes(_SENTINEL)
    return bundle, sentinel


def _raw_get(base_url: str, target: str, headers: dict[str, str] | None = None):
    """Send ``GET target`` byte-for-byte, with no client-side path normalization."""
    parts = urlsplit(base_url)
    conn = http.client.HTTPConnection(parts.hostname, parts.port, timeout=10)
    try:
        conn.request("GET", target, headers=headers or {})
        response = conn.getresponse()
        return response.status, response.read()
    finally:
        conn.close()


def _escape_targets(sentinel: Path) -> list[str]:
    """Request targets that each name ``sentinel``, one directory above the bundle."""
    absolute = str(sentinel)
    return [
        "/../sentinel.txt",
        "/%2e%2e/sentinel.txt",
        "/..%2fsentinel.txt",
        "/%2e%2e%2fsentinel.txt",
        "/" + absolute,
        "/%2F" + quote(absolute.lstrip("/"), safe=""),
        "/%00",
        "/assets/..%2f..%2fsentinel.txt",
    ]


def test_ctn_001_spa_serves_only_bundle_files(tmp_path):
    """Over real uvicorn, no request target serves a file outside the bundle.

    Anti-coincidental-pass: the same server serves a real top-level bundle file
    and an asset byte-exact and returns the shell for a client route, so the
    refusals are not a server that serves nothing.
    """
    bundle, sentinel = _stage_bundle(tmp_path)
    app = create_bff_app(spa_dir=bundle, stack_config=SageCoreConfig())

    with serve_uvicorn(app) as base_url:
        assert _raw_get(base_url, "/robots.txt") == (200, _TOP_FILE)
        assert _raw_get(base_url, "/assets/app.js") == (200, _ASSET)
        assert _raw_get(base_url, "/documents/some-id") == (200, _INDEX)

        for target in _escape_targets(sentinel):
            status, body = _raw_get(base_url, target)
            assert _SENTINEL not in body, target
            if target.startswith("/assets/"):
                # The static mount refuses on its own terms; the catch-all never sees it.
                assert status == 404, (target, status, body)
                continue
            assert status == 404, (target, status, body)
            assert json.loads(body)["code"] == "route_not_found", (target, body)


def test_ctn_002_symlink_out_of_the_bundle_is_not_served(tmp_path):
    """A symlink in the bundle pointing outside it yields the shell, not the target.

    Anti-coincidental-pass: an ordinary in-bundle file is served by the same app.
    """
    bundle, sentinel = _stage_bundle(tmp_path)
    (bundle / "linked.txt").symlink_to(sentinel)
    app = create_bff_app(spa_dir=bundle, stack_config=SageCoreConfig())

    with serve_uvicorn(app) as base_url:
        assert _raw_get(base_url, "/robots.txt") == (200, _TOP_FILE)
        status, body = _raw_get(base_url, "/linked.txt")

    assert _SENTINEL not in body
    assert (status, body) == (200, _INDEX)


def _recording_sage(
    recorder: list[httpx.Request],
    *,
    response_headers: dict[str, str] | None = None,
) -> httpx.AsyncClient:
    """A SAGE stand-in recording each request and answering with ``response_headers``."""

    def handler(request: httpx.Request) -> httpx.Response:
        recorder.append(request)
        if request.url.path.endswith("/content"):

            async def chunks():
                yield b"raw-bytes"

            return httpx.Response(200, content=chunks(), headers=response_headers or {})
        return httpx.Response(200, json={"ok": True}, headers=response_headers or {})

    return httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://sage.test")


async def _proxying_app(sage: httpx.AsyncClient) -> FastAPI:
    app = await auth_app(with_session=True)
    app.state.sage_transport = HttpSageTransport(
        ObOSageClient("http://sage.test", StubOidc(), client=sage)
    )
    return app


_REFUSED_PROXY_PATHS = [
    "/sage_vaults/cas/%2e%2e/%2e%2e/health",
    "/sage_vaults/cas/..%2f..%2fhealth",
    "/sage_vaults/%2e%2e",
    "/sage_vaults/cas/%2e/stats",
    "/sage_vaults/cas//stats",
    "/sage_vaults/cas/stats/",
    "/sage_vaults/",
]


@pytest.mark.parametrize("target", _REFUSED_PROXY_PATHS)
async def test_ctn_003_proxy_refuses_dot_and_empty_segments(target):
    """A dot or empty segment below ``/sage_vaults/`` is refused before SAGE is called."""
    recorder: list[httpx.Request] = []
    app = await _proxying_app(_recording_sage(recorder))

    async with _sessioned_client(app) as client:
        response = await client.get(target)

    assert response.status_code == 404, response.text
    assert response.json()["code"] == "route_not_found"
    assert recorder == []


async def test_ctn_003b_proxy_forwards_an_ordinary_path_exactly():
    """Positive control for CTN-003: an ordinary path reaches SAGE unchanged."""
    recorder: list[httpx.Request] = []
    app = await _proxying_app(_recording_sage(recorder))

    async with _sessioned_client(app) as client:
        response = await client.get("/sage_vaults/cas/documents/0123abcd_doc/open")

    assert response.status_code == 200
    assert [r.url.raw_path for r in recorder] == [b"/sage_vaults/cas/documents/0123abcd_doc/open"]


async def test_ctn_003c_proxy_refuses_a_literal_dot_segment_over_uvicorn():
    """A literal ``..`` sent raw to a real server is refused and never reaches SAGE.

    The lifespan resets the auth context and transport at startup, so both are
    installed once the server is up.
    """
    recorder: list[httpx.Request] = []
    app = create_bff_app(stack_config=SageCoreConfig(profile="cloud"))
    cookie = f"{bff_settings().session_cookie_name}={SESSION_ID}"

    with serve_uvicorn(app) as base_url:
        store = InMemorySessionStore()
        await store.create_session(live_session())
        app.state.bff_auth = BffAuthContext(settings=bff_settings(), oidc=StubOidc(), store=store)
        app.state.sage_transport = HttpSageTransport(
            ObOSageClient("http://sage.test", StubOidc(), client=_recording_sage(recorder))
        )
        ok_status, _ = _raw_get(base_url, "/sage_vaults/cas/stats", {"Cookie": cookie})
        status, body = _raw_get(base_url, "/sage_vaults/cas/../../health", {"Cookie": cookie})

    assert ok_status == 200
    assert status == 404, body
    assert json.loads(body)["code"] == "route_not_found"
    assert [r.url.raw_path for r in recorder] == [b"/sage_vaults/cas/stats"]


async def test_ctn_003d_normalized_path_check_refuses_a_climb_on_its_own(monkeypatch):
    """With the segment check disabled, the final check on the path the HTTP
    client will send still refuses a dot-segment climb, and SAGE is not called.

    Anti-coincidental-pass: the check is reachable only when the segment check
    lets a dot segment through, so this test disables that layer to exercise
    it; without the final check the request reaches SAGE outside the collection.
    """
    from app.backend import proxy

    monkeypatch.setattr(proxy, "_is_dot_or_empty", lambda segment: False)
    recorder: list[httpx.Request] = []
    app = await _proxying_app(_recording_sage(recorder))

    async with _sessioned_client(app) as client:
        response = await client.get("/sage_vaults/cas/%2e%2e/%2e%2e/health")

    assert response.status_code == 404, response.text
    assert response.json()["code"] == "route_not_found"
    assert recorder == []


async def test_ctn_004_encoded_separators_stay_inside_their_segment():
    """An encoded ``/`` or ``?`` reaches SAGE still encoded; a literal ``:`` passes."""
    recorder: list[httpx.Request] = []
    app = await _proxying_app(_recording_sage(recorder))

    async with _sessioned_client(app) as client:
        for target in (
            "/sage_vaults/cas/documents/a%2Fb",
            "/sage_vaults/cas/documents/x%3Fy/open",
            "/sage_vaults/cas/documents:batch",
        ):
            response = await client.get(target)
            assert response.status_code == 200, (target, response.text)

    assert [(r.url.raw_path, r.url.query) for r in recorder] == [
        (b"/sage_vaults/cas/documents/a%2Fb", b""),
        (b"/sage_vaults/cas/documents/x%3Fy/open", b""),
        (b"/sage_vaults/cas/documents:batch", b""),
    ]


_DOCS_PATHS = ("/docs", "/docs/oauth2-redirect", "/redoc", "/openapi.json")


async def test_ctn_005_no_documentation_pages(tmp_path):
    """No documentation route is served; the document is still generated in process.

    Anti-coincidental-pass: with a bundle staged, the documentation paths reach
    the SPA catch-all, so the assertion is on content as well as on routes.
    """
    bundle, _ = _stage_bundle(tmp_path)
    app = create_bff_app(spa_dir=bundle, stack_config=SageCoreConfig(profile="cloud"))

    paths = {getattr(route, "path", None) for route in app.routes}
    assert not paths & set(_DOCS_PATHS)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://bff.test"
    ) as client:
        for path in _DOCS_PATHS:
            response = await client.get(path)
            text = response.text.lower()
            for marker in ("swagger", "redoc", "cdn.jsdelivr", '"openapi"'):
                assert marker not in text, (path, marker)

    assert "paths" in app.openapi()


_ALLOWED_REQUEST = {
    "accept": "application/json",
    "accept-language": "en-GB",
    "content-type": "application/json",
    "user-agent": "spa-test-agent/1.0",
}
_DENIED_REQUEST = {
    "authorization": "Bearer caller-supplied",
    "origin": "https://elsewhere.example",
    "x-forwarded-for": "203.0.113.9",
    "x-custom-probe": "should-not-pass",
}


@pytest.mark.parametrize(
    "path", ["/sage_vaults/cas/stats", "/sage_vaults/cas/documents/0123abcd_doc/content"]
)
async def test_ctn_006_request_headers_are_allowlisted(path):
    """Only allowlisted request headers reach SAGE, on the buffered and streamed path."""
    recorder: list[httpx.Request] = []
    app = await _proxying_app(_recording_sage(recorder))

    async with _sessioned_client(app) as client:
        response = await client.get(path, headers={**_ALLOWED_REQUEST, **_DENIED_REQUEST})

    assert response.status_code == 200
    (upstream,) = recorder
    for name, value in _ALLOWED_REQUEST.items():
        assert upstream.headers.get(name) == value, name
    assert upstream.headers["authorization"] == "Bearer delegated-token"
    for name in ("origin", "x-forwarded-for", "x-custom-probe", "cookie"):
        assert name not in upstream.headers, name


_ALLOWED_RESPONSE = {
    "content-type": "application/pdf",
    "content-disposition": 'attachment; filename="x.pdf"',
    "cache-control": "no-store",
    "www-authenticate": 'Bearer realm="sage"',
    "allow": "GET",
    "content-security-policy": "sandbox",
    "x-content-type-options": "nosniff",
}
_DENIED_RESPONSE = {
    "set-cookie": "upstream=1; Path=/",
    "server": "upstream-server",
    "x-upstream-probe": "should-not-pass",
}


@pytest.mark.parametrize(
    "path", ["/sage_vaults/cas/stats", "/sage_vaults/cas/documents/0123abcd_doc/content"]
)
async def test_ctn_007_response_headers_are_allowlisted(path):
    """Only allowlisted response headers come back, on the buffered and streamed path."""
    recorder: list[httpx.Request] = []
    sage = _recording_sage(recorder, response_headers={**_ALLOWED_RESPONSE, **_DENIED_RESPONSE})
    app = await _proxying_app(sage)

    async with _sessioned_client(app) as client:
        response = await client.get(path)

    assert response.status_code == 200
    for name, value in _ALLOWED_RESPONSE.items():
        assert response.headers.get(name) == value, name
    for name in _DENIED_RESPONSE:
        assert name not in response.headers, name
