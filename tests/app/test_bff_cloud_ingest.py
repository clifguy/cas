"""Cloud-profile bulk-ingest wiring for the standalone BFF (CAS-ADR-042).

Under the hosted profile, path-based ``/app/scan`` and ``/app/ingest`` stay
co-located-only -- they walk a filesystem the standalone process does not
share with SAGE -- so they keep returning the typed ``local_profile_only``
501, now pointing the caller at the upload path. Cloud bulk-ingest is
delivered instead by uploading file content to the SAGE batch-ingest
endpoint, reached through the BFF reverse proxy (which attaches the user's
delegated bearer). The proxy refuses the call without a signed-in session.

The end-to-end hosted path (upload -> proxy -> SAGE batch endpoint -> SSE ->
landed document) is exercised in
tests/sage/test_batch_ingest_endpoint.py::test_b8_cloud_proxy_forwards_upload_to_batch_endpoint,
which has the real SAGE app + vault fixtures.

Test IDs follow APP-NNN (standalone APP).
"""

from __future__ import annotations

import httpx
from fastapi import FastAPI

from app.backend.asgi import create_bff_app
from app.backend.auth.config import BffAuthContext, BffAuthSettings
from app.backend.auth.sage_client import ObOSageClient
from app.backend.auth.session_store import InMemorySessionStore
from app.backend.transport import HttpSageTransport
from sage.config import SageCoreConfig


def _settings() -> BffAuthSettings:
    return BffAuthSettings(
        tenant_id="t",
        client_id="c",
        client_secret="s",  # noqa: S106 -- test fixture, not a real secret
        sage_app_id_uri="api://sage",
        sage_base_url="http://sage.test",
    )


class _StubOidc:
    def acquire_sage_token(self, token_cache: str) -> str:  # noqa: ARG002
        return "delegated-token"  # noqa: S105 -- test fixture token, not a real secret


def _mock_sage(recorder: list[httpx.Request]) -> httpx.AsyncClient:
    def handler(request: httpx.Request) -> httpx.Response:
        recorder.append(request)
        return httpx.Response(200, json={"ok": True})

    return httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://sage.test")


def _client(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://bff.test")


async def test_app_009_cloud_ingest_route_stays_co_located_only():
    """In the hosted profile the path-based ``/app/ingest`` route returns the
    typed ``local_profile_only`` 501 and points the caller at the upload path.

    Anti-coincidental-pass: a route that silently succeeded (or 500'd on the
    absent registry) would not carry the ``local_profile_only`` code; a
    message that lost the upload-path guidance would fail the substring check.
    """
    app = create_bff_app(stack_config=SageCoreConfig(profile="cloud"))

    async with _client(app) as client:
        response = await client.post(
            "/app/ingest",
            json={
                "vault_id": "cas",
                "files": [{"file_path": "/tmp/x.md", "source_type": "markdown"}],
            },
        )

    assert response.status_code == 501
    body = response.json()
    assert body["code"] == "local_profile_only"
    assert "documents:batch" in body["message"], body["message"]


async def test_app_010_cloud_batch_upload_via_proxy_requires_session():
    """A cloud bulk-ingest upload to the SAGE batch endpoint -- reached through
    the BFF reverse proxy -- is refused without a signed-in session, and SAGE
    is never reached.

    Anti-coincidental-pass: a proxy that forwarded before checking the session
    would record an upstream call to the mock SAGE client.
    """
    app = create_bff_app(stack_config=SageCoreConfig(profile="cloud"))
    recorder: list[httpx.Request] = []
    oidc = _StubOidc()
    store = InMemorySessionStore()
    app.state.bff_auth = BffAuthContext(settings=_settings(), oidc=oidc, store=store)
    app.state.sage_transport = HttpSageTransport(
        ObOSageClient("http://sage.test", oidc, client=_mock_sage(recorder))
    )

    async with _client(app) as client:  # no session cookie
        response = await client.post(
            "/sage_vaults/cas/documents:batch",
            files=[("files", ("a.md", b"# A\n\nbody", "text/markdown"))],
            data={"metadata": '{"files": [{"source_type": "markdown"}]}'},
        )

    assert response.status_code == 401
    assert recorder == []
