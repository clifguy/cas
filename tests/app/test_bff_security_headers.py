"""Security response headers of the standalone backend-for-frontend (CAS-ADR-042).

Every response the backend sends carries a page content-security policy, a
no-sniff directive, a referrer policy and frame protection, and over HTTPS a
transport-security policy. The headers wrap the whole application, so the
responses the framework itself produces for an unhandled exception carry them
too.

Test IDs follow SEC-NNN; the specification is ``bff_security_headers_tests.md``.
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI

from app.backend.asgi import create_bff_app
from app.backend.auth.sage_client import ObOSageClient
from app.backend.transport import HttpSageTransport
from sage.config import SageCoreConfig
from tests.helpers.bff_session import StubOidc, auth_app
from tests.helpers.bff_session import sessioned_client as _sessioned_client

PAGE_POLICY = (
    "default-src 'self'; script-src 'self'; object-src 'none'; "
    "base-uri 'none'; frame-ancestors 'none'"
)
HSTS = "max-age=31536000; includeSubDomains"
_COMMON = {
    "x-content-type-options": "nosniff",
    "referrer-policy": "no-referrer",
    "x-frame-options": "DENY",
}

_INDEX = b"<!doctype html><title>spa-shell</title>"
_ASSET = b"console.log('asset');"


def _stage_bundle(root: Path) -> Path:
    bundle = root / "bundle"
    (bundle / "assets").mkdir(parents=True)
    (bundle / "index.html").write_bytes(_INDEX)
    (bundle / "assets" / "app.js").write_bytes(_ASSET)
    return bundle


def _client(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://bff.test",
    )


def _assert_page_headers(response: httpx.Response, *, enforce: bool = False) -> None:
    for name, value in _COMMON.items():
        assert response.headers.get(name) == value, (response.url, name)
    enforced = response.headers.get("content-security-policy")
    report_only = response.headers.get("content-security-policy-report-only")
    if enforce:
        assert enforced == PAGE_POLICY, response.url
        assert report_only is None, response.url
    else:
        assert report_only == PAGE_POLICY, response.url
        assert enforced is None, response.url


def _sage(status: int = 200, headers: dict[str, str] | None = None, json: object = None):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/content"):

            async def chunks():
                yield b"raw-bytes"

            return httpx.Response(status, content=chunks(), headers=headers or {})
        return httpx.Response(status, json=json or {"ok": True}, headers=headers or {})

    return httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://sage.test")


async def _proxying_app(sage: httpx.AsyncClient) -> FastAPI:
    app = await auth_app(with_session=True)
    app.state.sage_transport = HttpSageTransport(
        ObOSageClient("http://sage.test", StubOidc(), client=sage)
    )
    return app


async def test_sec_001_spa_and_health_responses_carry_page_headers(tmp_path):
    """The shell, a client route, an asset and the health probe carry the headers."""
    app = create_bff_app(spa_dir=_stage_bundle(tmp_path), stack_config=SageCoreConfig())
    async with _client(app) as client:
        for path, body in (
            ("/", _INDEX),
            ("/graph/explorer", _INDEX),
            ("/assets/app.js", _ASSET),
            ("/health", None),
        ):
            response = await client.get(path)
            assert response.status_code == 200, path
            if body is not None:
                assert response.content == body, path
            _assert_page_headers(response)


@pytest.mark.parametrize(
    "path", ["/sage_vaults/cas/stats", "/sage_vaults/cas/documents/0123abcd_doc/content"]
)
async def test_sec_002_proxied_responses_carry_page_headers(path):
    """Buffered and streamed proxied responses carry the headers."""
    app = await _proxying_app(_sage())
    async with _sessioned_client(app) as client:
        response = await client.get(path)
    assert response.status_code == 200
    assert response.content in (b"raw-bytes", b'{"ok":true}')
    _assert_page_headers(response)


async def test_sec_003_error_responses_carry_page_headers():
    """A proxy refusal, an unsessioned refusal and a relayed SAGE error carry the headers."""
    app = await _proxying_app(_sage(404, json={"code": "document_not_found", "message": "x"}))
    async with _sessioned_client(app) as client:
        refused = await client.get("/sage_vaults/cas/%2e/stats")
        relayed = await client.get("/sage_vaults/cas/documents/0123abcd_doc")
    async with _client(app) as anonymous:
        unsessioned = await anonymous.get("/sage_vaults/cas/stats")

    assert refused.status_code == 404 and refused.json()["code"] == "route_not_found"
    assert relayed.status_code == 404 and relayed.json()["code"] == "document_not_found"
    assert unsessioned.status_code == 401, unsessioned.text
    for response in (refused, relayed, unsessioned):
        _assert_page_headers(response)


async def test_sec_004_unhandled_exception_response_carries_page_headers(tmp_path):
    """The framework's own 500 for an unhandled exception carries the headers.

    No SPA bundle is mounted, so its catch-all cannot answer the probe route.
    """
    app = create_bff_app(spa_dir=tmp_path / "no-bundle", stack_config=SageCoreConfig())

    async def _boom() -> None:
        raise RuntimeError("unhandled")

    app.add_api_route("/boom", _boom, methods=["GET"])
    async with _client(app) as client:
        response = await client.get("/boom")
    assert response.status_code == 500
    _assert_page_headers(response)


@pytest.mark.parametrize(("profile", "expected"), [("cloud", HSTS), ("local", None)])
async def test_sec_005_hsts_only_under_the_cloud_profile(profile, expected):
    """Transport security is sent under the cloud profile and not under local."""
    app = create_bff_app(stack_config=SageCoreConfig(profile=profile))
    async with _client(app) as client:
        response = await client.get("/health")
    assert response.headers.get("strict-transport-security") == expected
    _assert_page_headers(response)


@pytest.mark.parametrize(
    ("env", "argument", "enforce"),
    [
        ("1", None, True),
        ("true", None, True),
        ("0", None, False),
        (None, None, False),
        (None, True, True),
        ("1", False, False),
    ],
)
async def test_sec_006_enforcement_switch(monkeypatch, env, argument, enforce):
    """The page policy is enforced when switched on, and report-only otherwise."""
    if env is None:
        monkeypatch.delenv("CAS_BFF_CSP_ENFORCE", raising=False)
    else:
        monkeypatch.setenv("CAS_BFF_CSP_ENFORCE", env)
    app = create_bff_app(stack_config=SageCoreConfig(), csp_enforce=argument)
    async with _client(app) as client:
        response = await client.get("/health")
    _assert_page_headers(response, enforce=enforce)


@pytest.mark.parametrize("enforce", [False, True])
async def test_sec_007_sage_delivery_headers_survive_the_proxy(monkeypatch, enforce):
    """SAGE's sandbox policy on raw bytes is relayed, not replaced; absent, defaults apply.

    Enforcing mode is the case that discriminates: there the page policy and
    SAGE's policy share a header name, so a wrapper that overwrote a present
    header would replace the sandbox with the weaker page policy.
    """
    monkeypatch.setenv("CAS_BFF_CSP_ENFORCE", "1" if enforce else "0")
    sandboxed = await _proxying_app(
        _sage(headers={"content-security-policy": "sandbox", "x-content-type-options": "nosniff"})
    )
    async with _sessioned_client(sandboxed) as client:
        response = await client.get("/sage_vaults/cas/documents/0123abcd_doc/content")
    assert response.headers.get_list("content-security-policy") == ["sandbox"]
    if not enforce:
        assert response.headers.get("content-security-policy-report-only") == PAGE_POLICY
    assert response.headers.get_list("x-content-type-options") == ["nosniff"]

    plain = await _proxying_app(_sage())
    async with _sessioned_client(plain) as client:
        response = await client.get("/sage_vaults/cas/documents/0123abcd_doc/content")
    _assert_page_headers(response, enforce=enforce)
