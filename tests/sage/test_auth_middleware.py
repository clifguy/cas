"""Auth-middleware wiring tests (CAS-ADR-042).

Verify that ``create_app`` installs the resolved token validator as a single
ASGI middleware that guards the REST routes and the mounted MCP sub-apps
uniformly: disabled config is a pass-through (the local deployment is
unaffected), enabled config rejects an unauthenticated request on every
surface with an identical challenge, the liveness probe stays exempt, and a
valid token passes through. Enabled-auth wiring uses a stub validator (the
JWT logic itself is covered by test_auth_validator); the stub is installed
through the same factory monkeypatch seam the binding honors.

No-token requests to the MCP mounts are answered by the middleware before the
SSE sub-app runs, so these never open a streaming connection.
"""

from __future__ import annotations

from httpx import ASGITransport, AsyncClient

from sage.app import create_app
from sage.auth import (
    SCOPE_PRINCIPAL_KEY,
    AuthenticatedPrincipal,
    AuthError,
    AuthMiddleware,
    NoAuthValidator,
)
from sage.config import SageCoreConfig, StackAuthConfig

_VALID = "good-token"
_DISABLED = SageCoreConfig()
_ENABLED = SageCoreConfig(
    auth=StackAuthConfig(enabled=True, tenant_id="tid", audience="api://sage")
)


class _StubValidator:
    """Accepts exactly the sentinel token; rejects everything else as 401."""

    async def validate(self, token):
        if token == _VALID:
            return AuthenticatedPrincipal(subject="user-1", scopes=frozenset({"Sage.Access"}))
        raise AuthError(401, "invalid_token", "bad or missing token")


def _install_stub(monkeypatch) -> None:
    """Route the auth-seam factory to the stub when enabled, NoAuth otherwise."""

    def fake(auth_config):
        if auth_config is None or not auth_config.enabled:
            return NoAuthValidator()
        return _StubValidator()

    monkeypatch.setattr("sage.mcp_init.build_auth_validator", fake)


def _client(app) -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test", timeout=5.0)


# --------------------------------------------------------------------------
# Integration: create_app installs the middleware across surfaces
# --------------------------------------------------------------------------


async def test_c1_disabled_is_pass_through() -> None:
    """Auth absent: an unauthenticated request behaves as it does today."""
    app = create_app(stack_config=_DISABLED)
    async with _client(app) as c:
        resp = await c.get("/openapi.json")
    assert resp.status_code == 200
    assert "www-authenticate" not in resp.headers


async def test_c2_rest_enforced(monkeypatch) -> None:
    _install_stub(monkeypatch)
    app = create_app(stack_config=_ENABLED)
    async with _client(app) as c:
        resp = await c.get("/openapi.json")
    assert resp.status_code == 401
    assert resp.headers["www-authenticate"].startswith("Bearer")


async def test_c3_rest_valid_token_passes(monkeypatch) -> None:
    _install_stub(monkeypatch)
    app = create_app(stack_config=_ENABLED)
    async with _client(app) as c:
        resp = await c.get("/openapi.json", headers={"Authorization": f"Bearer {_VALID}"})
    assert resp.status_code == 200


async def test_c4_health_is_exempt(monkeypatch) -> None:
    """The liveness probe answers 200 with no token even when auth is on."""
    _install_stub(monkeypatch)
    app = create_app(stack_config=_ENABLED)
    async with _client(app) as c:
        resp = await c.get("/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


async def test_c5_mcp_mount_enforced(monkeypatch) -> None:
    _install_stub(monkeypatch)
    app = create_app(stack_config=_ENABLED)
    async with _client(app) as c:
        resp = await c.get("/mcp")
    assert resp.status_code == 401
    assert resp.headers["www-authenticate"].startswith("Bearer")


async def test_c6_mcp_admin_mount_enforced(monkeypatch) -> None:
    _install_stub(monkeypatch)
    app = create_app(stack_config=_ENABLED)
    async with _client(app) as c:
        resp = await c.get("/mcp_admin")
    assert resp.status_code == 401


async def test_c8_authorization_uniform_across_surfaces(monkeypatch) -> None:
    """The same no-token request yields an identical challenge on each surface.

    Directly encodes AC1: authorization is uniform regardless of surface. If
    /mcp_admin were exempted or ran a different policy, the responses would
    diverge.
    """
    _install_stub(monkeypatch)
    app = create_app(stack_config=_ENABLED)
    seen = set()
    async with _client(app) as c:
        for path in ("/sage_vaults/test_vault/users", "/mcp", "/mcp_admin"):
            r = await c.get(path)
            seen.add((r.status_code, r.headers.get("www-authenticate")))
    assert len(seen) == 1, seen
    (status, challenge) = next(iter(seen))
    assert status == 401
    assert challenge is not None and challenge.startswith("Bearer")


# --------------------------------------------------------------------------
# Middleware unit: pass-through vs short-circuit (hang-free, covers /mcp)
# --------------------------------------------------------------------------


async def _run(mw: AuthMiddleware, scope: dict) -> tuple[list, dict]:
    sent: list = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        sent.append(message)

    await mw(scope, receive, send)
    return sent, scope


async def test_middleware_passes_valid_token_and_attaches_principal() -> None:
    """On a /mcp path a valid token reaches the inner app with its principal."""
    called = {}

    async def inner(scope, receive, send):
        called["principal"] = scope.get(SCOPE_PRINCIPAL_KEY)
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    mw = AuthMiddleware(inner, validator=_StubValidator(), exempt_paths=frozenset({"/health"}))
    scope = {"type": "http", "path": "/mcp", "headers": [(b"authorization", b"Bearer good-token")]}
    sent, _ = await _run(mw, scope)
    assert called["principal"].subject == "user-1"
    assert sent[0]["status"] == 200


async def test_middleware_short_circuits_missing_token_before_inner() -> None:
    """A missing token is rejected at the middleware; the inner app never runs."""
    inner_called = {"v": False}

    async def inner(scope, receive, send):
        inner_called["v"] = True

    mw = AuthMiddleware(inner, validator=_StubValidator(), exempt_paths=frozenset({"/health"}))
    scope = {"type": "http", "path": "/mcp", "headers": []}
    sent, _ = await _run(mw, scope)
    assert inner_called["v"] is False
    assert sent[0]["status"] == 401
    header_names = {name for name, _ in sent[0]["headers"]}
    assert b"www-authenticate" in header_names
