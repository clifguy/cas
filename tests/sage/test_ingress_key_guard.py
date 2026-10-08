"""Gateway ingress key: SAGE answers only requests its gateway forwarded.

A deployment whose container ingress is reachable from the internet fronts it
with a gateway that validates callers. ``IngressKeyGuard`` makes that gateway
the only way in: when the environment supplies an ingress key, every request
must carry it in the ingress-key header, which only the gateway knows and
injects. The liveness probe stays open. With no key configured -- the on-box
profile, or a deployment whose key is not yet loaded -- the guard is absent.

Invariants
----------

IG-001  With a key configured, a request without the header, with an empty
        one or with a wrong one is refused with 403 and the published
        ``http_error`` envelope, on a REST route, the schema document, the
        transfer upload and download routes and the MCP mount -- including
        the routes that need no bearer token.
IG-002  The configured key admits the request (positive control).
IG-003  The liveness probe answers without the key.
IG-004  With no key, or an empty one, configured the guard is absent.
IG-005  The guard answers before authentication: a request without the key is
        refused 403 even where a bearer token would be required, and the
        right key reaches the authentication layer.
IG-006  A refusal is answered before the wrapped application runs, and the
        key is not echoed in the response.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from sage.app import create_app
from sage.auth import (
    INGRESS_KEY_ENV_VAR,
    INGRESS_KEY_HEADER,
    AuthError,
    IngressKeyGuard,
    NoAuthValidator,
)
from sage.config import SageCoreConfig, StackAuthConfig
from tests.sage.test_wire_shape_conformance import SAGE_CORE_SPEC_PATH, _validator_for

_KEY = "k" * 64


def _assert_refused(resp) -> None:
    assert resp.status_code == 403, (resp.status_code, resp.text)
    body = resp.json()
    spec = yaml.safe_load(SAGE_CORE_SPEC_PATH.read_text())
    _validator_for(spec, "ErrorResponse").validate(body)
    assert body["code"] == "http_error", body
    assert _KEY not in resp.text


@pytest.fixture
def keyed_client(monkeypatch, tmp_path: Path):
    monkeypatch.setenv(INGRESS_KEY_ENV_VAR, _KEY)
    app = create_app(vault_root=tmp_path, stack_config=SageCoreConfig())
    with TestClient(app, base_url="http://127.0.0.1:8000") as client:
        yield client


_GUARDED_REQUESTS = [
    ("GET", "/sage_vaults"),
    ("GET", "/openapi.json"),
    ("PUT", "/upload"),
    ("GET", "/download/abc"),
    ("POST", "/mcp"),
]


@pytest.mark.parametrize(("method", "path"), _GUARDED_REQUESTS)
@pytest.mark.parametrize("presented", [None, "", "wrong", _KEY[:-1], _KEY + "x"])
def test_ig_001_missing_or_wrong_key_is_refused(keyed_client, method, path, presented) -> None:
    headers = {} if presented is None else {INGRESS_KEY_HEADER: presented}
    _assert_refused(keyed_client.request(method, path, headers=headers))


def test_ig_002_configured_key_admits(keyed_client) -> None:
    for path in ("/sage_vaults", "/openapi.json"):
        resp = keyed_client.get(path, headers={INGRESS_KEY_HEADER: _KEY})
        assert resp.status_code == 200, (path, resp.text)
    lower = keyed_client.get("/openapi.json", headers={INGRESS_KEY_HEADER.lower(): _KEY})
    assert lower.status_code == 200, "header names are case-insensitive"


def test_ig_003_liveness_probe_needs_no_key(keyed_client) -> None:
    assert keyed_client.get("/health").status_code == 200


@pytest.mark.parametrize("configured", [None, ""])
def test_ig_004_guard_absent_without_a_key(monkeypatch, tmp_path: Path, configured) -> None:
    if configured is None:
        monkeypatch.delenv(INGRESS_KEY_ENV_VAR, raising=False)
    else:
        monkeypatch.setenv(INGRESS_KEY_ENV_VAR, configured)
    app = create_app(vault_root=tmp_path, stack_config=SageCoreConfig())
    assert not any(m.cls is IngressKeyGuard for m in app.user_middleware)
    with TestClient(app, base_url="http://127.0.0.1:8000") as client:
        assert client.get("/openapi.json").status_code == 200


class _RejectAll:
    async def validate(self, token):
        raise AuthError(401, "invalid_token", "no token accepted")


def test_ig_005_guard_answers_before_authentication(monkeypatch, tmp_path: Path) -> None:
    def fake(auth_config):
        if auth_config is None or not auth_config.enabled:
            return NoAuthValidator()
        return _RejectAll()

    monkeypatch.setattr("sage.mcp_init.build_auth_validator", fake)
    monkeypatch.setenv(INGRESS_KEY_ENV_VAR, _KEY)
    cfg = SageCoreConfig(auth=StackAuthConfig(enabled=True, tenant_id="tid", audience="api://s"))
    app = create_app(vault_root=tmp_path, stack_config=cfg)
    with TestClient(app, base_url="https://sage.example.org") as client:
        _assert_refused(client.get("/sage_vaults"))
        authed = client.get("/sage_vaults", headers={INGRESS_KEY_HEADER: _KEY})
    assert authed.status_code == 401
    assert authed.headers["www-authenticate"].startswith("Bearer")


async def test_ig_006_refusal_precedes_the_wrapped_application() -> None:
    reached: list[str] = []

    async def inner(scope, receive, send):
        reached.append(scope["path"])

    guard = IngressKeyGuard(inner, key=_KEY)
    sent: list[dict] = []

    async def send(message):
        sent.append(message)

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    scope = {"type": "http", "method": "GET", "path": "/sage_vaults", "headers": []}
    await guard(scope, receive, send)
    assert reached == []
    assert sent[0]["status"] == 403
    assert _KEY.encode() not in b"".join(m.get("body", b"") for m in sent)

    keyed = dict(scope, headers=[(INGRESS_KEY_HEADER.lower().encode(), _KEY.encode())])
    await guard(keyed, receive, send)
    assert reached == ["/sage_vaults"]
