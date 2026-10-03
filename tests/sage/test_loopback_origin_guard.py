"""Loopback admission for a server that authenticates no one.

A SAGE process running without authentication trusts every request it
receives, so the only thing standing between it and an arbitrary web page is
where the request came from. ``sage.app.admission_guarded`` wraps the served
application in ``LoopbackOriginGuard`` whenever authentication is disabled:
the request must name a loopback host, and must not carry a foreign
``Origin`` or ``Sec-Fetch-Site: cross-site``. With authentication enabled the
guard is not installed at all -- the bearer token is the admission control
there, and a proxied deployment legitimately arrives under a public host name.

Invariants
----------

LG-001  A foreign ``Host`` is refused on ``/mcp``, ``/mcp_maint`` and a REST
        route, with 403 and the published ``http_error`` envelope.
LG-002  A cross-site multipart POST to ``documents:batch`` is refused even
        though it names a loopback host.
LG-003  Loopback hosts, with no ``Origin`` or a loopback one, reach the REST
        surface and complete an MCP ``initialize`` (positive control).
LG-004  The cross-site refusal covers every method; ``Origin: null`` is
        refused; a same-origin request passes.
LG-005  With authentication enabled the guard is absent: a foreign host
        reaches the authentication layer and is answered by it.
LG-006  Host and origin parsing accepts exactly the three loopback names.
LG-007  A refusal is answered before the wrapped application runs.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml
from fastapi.testclient import TestClient

from sage.app import admission_guarded, create_app
from sage.auth import (
    AuthenticatedPrincipal,
    AuthError,
    LoopbackOriginGuard,
    NoAuthValidator,
    is_loopback_host,
    is_loopback_origin,
)
from sage.config import SageCoreConfig, StackAuthConfig
from tests.sage.test_wire_shape_conformance import SAGE_CORE_SPEC_PATH, _validator_for

_LOOPBACK_BASE = "http://127.0.0.1:8000"

_INITIALIZE: dict[str, Any] = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "sage-tests", "version": "1.0"},
    },
}

_MCP_HEADERS = {
    "Accept": "application/json, text/event-stream",
    "Content-Type": "application/json",
}


def _assert_refused(resp) -> None:
    assert resp.status_code == 403, (resp.status_code, resp.text)
    body = resp.json()
    spec = yaml.safe_load(SAGE_CORE_SPEC_PATH.read_text())
    _validator_for(spec, "ErrorResponse").validate(body)
    assert body["code"] == "http_error", body
    assert body.get("detail") is None
    assert body["message"]


@pytest.fixture
def client(tmp_path: Path):
    """The served application for an unauthenticated local deployment."""
    app = admission_guarded(create_app(vault_root=tmp_path, stack_config=SageCoreConfig()))
    with TestClient(app, base_url=_LOOPBACK_BASE) as test_client:
        yield test_client


# ---------------------------------------------------------------------------
# LG-001 -- foreign Host on every surface
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("path", ["/mcp", "/mcp_maint"])
def test_lg_001_foreign_host_refused_on_mcp_mounts(client: TestClient, path: str) -> None:
    resp = client.post(
        path, json=_INITIALIZE, headers={**_MCP_HEADERS, "Host": "attacker.example:8000"}
    )
    _assert_refused(resp)


def test_lg_001_foreign_host_refused_on_rest(client: TestClient) -> None:
    resp = client.get("/sage_vaults", headers={"Host": "attacker.example"})
    _assert_refused(resp)


def test_lg_001_foreign_host_refused_on_health(client: TestClient) -> None:
    resp = client.get("/health", headers={"Host": "192.168.1.20:8000"})
    _assert_refused(resp)


# ---------------------------------------------------------------------------
# LG-002 -- cross-site multipart ingest
# ---------------------------------------------------------------------------


def test_lg_002_cross_site_multipart_batch_refused(client: TestClient) -> None:
    resp = client.post(
        "/sage_vaults/test/documents:batch",
        files={"files": ("note.md", b"# Note\n", "text/markdown")},
        headers={"Origin": "https://attacker.example", "Sec-Fetch-Site": "cross-site"},
    )
    _assert_refused(resp)


def test_lg_002_foreign_origin_alone_refused(client: TestClient) -> None:
    resp = client.post(
        "/sage_vaults/test/documents:batch",
        files={"files": ("note.md", b"# Note\n", "text/markdown")},
        headers={"Origin": "https://attacker.example"},
    )
    _assert_refused(resp)


def test_lg_002_cross_site_fetch_metadata_alone_refused(client: TestClient) -> None:
    resp = client.post(
        "/sage_vaults/test/documents:batch",
        files={"files": ("note.md", b"# Note\n", "text/markdown")},
        headers={"Sec-Fetch-Site": "cross-site"},
    )
    _assert_refused(resp)


# ---------------------------------------------------------------------------
# LG-003 -- positive control
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("host", ["127.0.0.1:8000", "localhost:8000", "[::1]:8000", "localhost"])
def test_lg_003_loopback_host_reaches_rest(client: TestClient, host: str) -> None:
    resp = client.get("/sage_vaults", headers={"Host": host})
    assert resp.status_code == 200, resp.text
    resp = client.get("/health", headers={"Host": host})
    assert resp.status_code == 200, resp.text


@pytest.mark.parametrize(
    "origin", [None, "http://127.0.0.1:8000", "http://localhost:5173", "http://[::1]:8000"]
)
def test_lg_003_loopback_origin_completes_mcp_initialize(
    client: TestClient, origin: str | None
) -> None:
    headers = dict(_MCP_HEADERS)
    if origin is not None:
        headers["Origin"] = origin
        headers["Sec-Fetch-Site"] = "same-origin"
    resp = client.post("/mcp", json=_INITIALIZE, headers=headers)
    assert resp.status_code == 200, resp.text
    assert "serverInfo" in resp.text


def test_lg_003_loopback_batch_reaches_the_route(client: TestClient) -> None:
    """The same request LG-002 refuses is routed once it comes from loopback.

    With no vault named ``test`` the route answers its own refusal, which proves
    the guard let the request through to the application.
    """
    resp = client.post(
        "/sage_vaults/test/documents:batch",
        files={"files": ("note.md", b"# Note\n", "text/markdown")},
        headers={"Origin": "http://localhost:5173", "Sec-Fetch-Site": "same-site"},
    )
    assert resp.status_code != 403, resp.text
    assert resp.json().get("code") != "http_error", resp.text


# ---------------------------------------------------------------------------
# LG-004 -- every method, null origin, same-origin
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("value", ["cross-site", "Cross-Site", " cross-site "])
def test_lg_004_cross_site_get_refused(client: TestClient, value: str) -> None:
    resp = client.get("/sage_vaults", headers={"Sec-Fetch-Site": value})
    _assert_refused(resp)


def test_lg_004_foreign_origin_get_refused(client: TestClient) -> None:
    resp = client.get("/sage_vaults", headers={"Origin": "https://attacker.example"})
    _assert_refused(resp)


def test_lg_004_null_origin_refused(client: TestClient) -> None:
    resp = client.post("/mcp", json=_INITIALIZE, headers={**_MCP_HEADERS, "Origin": "null"})
    _assert_refused(resp)


@pytest.mark.parametrize("site", ["same-origin", "same-site", "none"])
def test_lg_004_non_cross_site_fetch_metadata_passes(client: TestClient, site: str) -> None:
    resp = client.get("/sage_vaults", headers={"Sec-Fetch-Site": site})
    assert resp.status_code == 200, resp.text


# ---------------------------------------------------------------------------
# LG-005 -- authentication enabled: guard absent
# ---------------------------------------------------------------------------


class _RejectAll:
    async def validate(self, token: str | None) -> AuthenticatedPrincipal:
        raise AuthError(401, "invalid_request", "A bearer token is required.")


def test_lg_005_guard_absent_when_auth_enabled(monkeypatch, tmp_path: Path) -> None:
    def fake(auth_config):
        if auth_config is None or not auth_config.enabled:
            return NoAuthValidator()
        return _RejectAll()

    monkeypatch.setattr("sage.mcp_init.build_auth_validator", fake)
    cfg = SageCoreConfig(auth=StackAuthConfig(enabled=True, tenant_id="tid", audience="api://s"))
    fastapi_app = create_app(vault_root=tmp_path, stack_config=cfg)

    served = admission_guarded(fastapi_app)

    assert served is fastapi_app
    with TestClient(served, base_url="https://sage.example.org") as c:
        resp = c.get("/sage_vaults", headers={"Origin": "https://attacker.example"})
    assert resp.status_code == 401
    assert resp.headers["www-authenticate"].startswith("Bearer")


def test_lg_005_guard_installed_when_auth_disabled(tmp_path: Path) -> None:
    served = admission_guarded(create_app(vault_root=tmp_path, stack_config=SageCoreConfig()))
    assert isinstance(served, LoopbackOriginGuard)


# ---------------------------------------------------------------------------
# LG-006 -- parsing
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value",
    ["127.0.0.1", "127.0.0.1:8000", "localhost", "LOCALHOST:80", "[::1]", "[::1]:8000"],
)
def test_lg_006_loopback_hosts_accepted(value: str) -> None:
    assert is_loopback_host(value)


@pytest.mark.parametrize(
    "value",
    [
        None,
        "",
        "attacker.example",
        "localhost.attacker.example",
        "127.0.0.1.nip.io",
        "127.0.0.2",
        "0.0.0.0",  # noqa: S104 -- a Host value under test, never bound
        "::1",
        "[::1]x",
        "[::1]:",
        "localhost:",
        "localhost:80:80",
        "localhost:8o",
        "user@localhost",
        "[::ffff:127.0.0.1]",
    ],
)
def test_lg_006_non_loopback_hosts_refused(value: str | None) -> None:
    assert not is_loopback_host(value)


@pytest.mark.parametrize(
    "value",
    ["http://127.0.0.1:8000", "http://localhost", "https://localhost:5173", "http://[::1]:8000"],
)
def test_lg_006_loopback_origins_accepted(value: str) -> None:
    assert is_loopback_origin(value)


@pytest.mark.parametrize(
    "value",
    [
        "null",
        "",
        "https://attacker.example",
        "http://localhost.attacker.example",
        "file://localhost",
        "http://localhost/path",
        "localhost:8000",
        "http://user@localhost",
    ],
)
def test_lg_006_non_loopback_origins_refused(value: str) -> None:
    assert not is_loopback_origin(value)


# ---------------------------------------------------------------------------
# LG-007 -- refusal precedes the wrapped application
# ---------------------------------------------------------------------------


async def _drive(guard: LoopbackOriginGuard, scope: dict) -> list[dict]:
    sent: list[dict] = []

    async def receive() -> dict:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: dict) -> None:
        sent.append(message)

    await guard(scope, receive, send)
    return sent


@pytest.mark.parametrize(
    "headers",
    [
        [(b"host", b"attacker.example")],
        [],
        [(b"host", b"127.0.0.1:8000"), (b"origin", b"https://attacker.example")],
        [(b"host", b"127.0.0.1:8000"), (b"sec-fetch-site", b"cross-site")],
    ],
)
async def test_lg_007_refusal_never_reaches_inner_app(headers) -> None:
    reached = {"v": False}

    async def inner(scope, receive, send):
        reached["v"] = True

    scope = {"type": "http", "method": "POST", "path": "/mcp", "headers": headers}
    sent = await _drive(LoopbackOriginGuard(inner), scope)

    assert reached["v"] is False
    assert sent[0]["status"] == 403


async def test_lg_007_lifespan_passes_through() -> None:
    seen: list[str] = []

    async def inner(scope, receive, send):
        seen.append(scope["type"])

    await LoopbackOriginGuard(inner)({"type": "lifespan"}, None, None)
    assert seen == ["lifespan"]


async def test_lg_007_foreign_websocket_is_closed_before_inner_app() -> None:
    reached = {"v": False}

    async def inner(scope, receive, send):
        reached["v"] = True

    scope = {"type": "websocket", "path": "/ws", "headers": [(b"host", b"attacker.example")]}
    sent = await _drive(LoopbackOriginGuard(inner), scope)

    assert reached["v"] is False
    assert sent == [{"type": "websocket.close", "code": 1008}]


async def test_lg_007_loopback_websocket_reaches_inner_app() -> None:
    reached = {"v": False}

    async def inner(scope, receive, send):
        reached["v"] = True

    scope = {"type": "websocket", "path": "/ws", "headers": [(b"host", b"localhost:8000")]}
    await _drive(LoopbackOriginGuard(inner), scope)
    assert reached["v"] is True
