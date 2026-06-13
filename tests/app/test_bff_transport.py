"""Tests for the backend-for-frontend -> SAGE transport seam (CAS-ADR-042).

The seam is one port (`SageTransport`) with two profile-selected bindings: an
in-process binding that dispatches against the co-located SAGE app, and an HTTP
binding that reaches SAGE over the wire carrying the user's delegated bearer.
These tests pin the port contract, the two bindings' distinct behavior, their
parity on a read, and the profile-registry selection.

Test IDs follow TX-NNN (Transport).
"""

from __future__ import annotations

import time

import httpx
import pytest
from fastapi import FastAPI, Request

from app.backend.auth.oidc import AuthError
from app.backend.auth.sage_client import ObOSageClient
from app.backend.auth.session_store import Session
from app.backend.transport import (
    HttpSageTransport,
    InProcessSageTransport,
    SageResponse,
    SageTransport,
    resolve_bff_transport,
)
from sage import profiles
from sage.api.errors import SAGEError
from sage.config import SageCoreConfig


def _session() -> Session:
    return Session(
        session_id="sid-1",
        subject="user-1",
        claims={"name": "Test User"},
        token_cache="cache-blob",
        expires_at=time.time() + 3600,
    )


class _StubOidc:
    """Minimal OidcService: mint a fixed token from the cache, or refuse.

    ``token=None`` models a session that cannot produce a delegated token, so
    ``acquire_sage_token`` raises before any HTTP traffic.
    """

    def __init__(self, token: str | None = "delegated-token") -> None:  # noqa: S107 -- test fixture token, not a real secret
        self._token = token
        self.calls: list[str] = []

    def acquire_sage_token(self, token_cache: str) -> str:
        self.calls.append(token_cache)
        if self._token is None:
            raise AuthError("no delegated token available")
        return self._token


def _mock_sage(
    recorder: list[httpx.Request],
    *,
    status: int = 200,
    json_body: dict | None = None,
) -> httpx.MockTransport:
    """An httpx transport that records every request and returns a fixed body."""

    def handler(request: httpx.Request) -> httpx.Response:
        recorder.append(request)
        return httpx.Response(status, json=json_body if json_body is not None else {"ok": True})

    return httpx.MockTransport(handler)


def _recording_sage_app(received: dict) -> FastAPI:
    """A tiny in-process SAGE app recording the Authorization header it sees."""
    app = FastAPI()

    @app.get("/sage_vaults/{vault}/stats")
    async def stats(vault: str, request: Request) -> dict:
        received["authorization"] = request.headers.get("authorization")
        received["vault"] = vault
        return {"vault": vault, "via": "in-process"}

    return app


def _shared_sage_app() -> FastAPI:
    """A SAGE app both bindings target, to assert read parity."""
    app = FastAPI()

    @app.get("/sage_vaults/{vault}/stats")
    async def stats(vault: str) -> dict:
        return {"vault": vault, "value": 42}

    return app


def test_tx_001_abc_rejects_incomplete_binding():
    """A `SageTransport` subclass that does not implement `request` cannot be
    instantiated.

    Anti-coincidental-pass: if `request` lost its `@abstractmethod`, the
    incomplete subclass would instantiate and this would not raise.
    """

    class Incomplete(SageTransport):
        pass

    with pytest.raises(TypeError):
        Incomplete()  # type: ignore[abstract]


def test_tx_002_both_bindings_satisfy_port():
    """Both real bindings are `SageTransport` subclasses and instantiate."""
    http = HttpSageTransport(ObOSageClient("http://sage.test", _StubOidc()))
    inproc = InProcessSageTransport(FastAPI())
    assert isinstance(http, SageTransport)
    assert isinstance(inproc, SageTransport)


async def test_tx_003_http_binding_attaches_delegated_bearer():
    """The HTTP binding carries `Authorization: Bearer <token>` on the call.

    Anti-coincidental-pass: drop the header (or skip `acquire_sage_token`) and
    the captured upstream request would carry no bearer.
    """
    recorder: list[httpx.Request] = []
    client = httpx.AsyncClient(transport=_mock_sage(recorder), base_url="http://sage.test")
    transport = HttpSageTransport(
        ObOSageClient("http://sage.test", _StubOidc("tok-abc"), client=client)
    )

    response = await transport.request("GET", "/sage_vaults/cas/stats", session=_session())

    assert isinstance(response, SageResponse)
    assert response.status_code == 200
    assert len(recorder) == 1
    assert recorder[0].headers["authorization"] == "Bearer tok-abc"


async def test_tx_004_http_binding_never_calls_sage_without_a_token():
    """When the session cannot mint a token, the HTTP binding raises and SAGE
    is never reached.

    Anti-coincidental-pass: a binding that issued the request before minting
    the token would record an upstream call.
    """
    recorder: list[httpx.Request] = []
    client = httpx.AsyncClient(transport=_mock_sage(recorder), base_url="http://sage.test")
    transport = HttpSageTransport(ObOSageClient("http://sage.test", _StubOidc(None), client=client))

    with pytest.raises(AuthError):
        await transport.request("GET", "/sage_vaults/cas/stats", session=_session())

    assert recorder == []


async def test_tx_004b_http_binding_requires_a_session():
    """The HTTP binding refuses a call with no session before minting a token
    or reaching SAGE.

    Anti-coincidental-pass: a binding that skipped the session check would call
    `acquire_sage_token` (recorded) and/or reach the mock transport.
    """
    recorder: list[httpx.Request] = []
    client = httpx.AsyncClient(transport=_mock_sage(recorder), base_url="http://sage.test")
    oidc = _StubOidc()
    transport = HttpSageTransport(ObOSageClient("http://sage.test", oidc, client=client))

    with pytest.raises(SAGEError) as excinfo:
        await transport.request("GET", "/sage_vaults/cas/stats", session=None)

    assert excinfo.value.status_code == 401
    assert recorder == []
    assert oidc.calls == []


async def test_tx_005_inprocess_binding_dispatches_without_a_bearer():
    """The in-process binding reaches the co-located SAGE app and carries no
    delegated bearer -- proving it is the direct, in-process path, distinct
    from the OBO/HTTP binding.

    Anti-coincidental-pass: if `InProcessSageTransport` were an alias of the
    HTTP binding it would require a session and attach a bearer, so the
    recorded Authorization header would be non-null (and the no-session call
    would raise).
    """
    received: dict = {}
    transport = InProcessSageTransport(_recording_sage_app(received))

    response = await transport.request("GET", "/sage_vaults/cas/stats")

    assert response.status_code == 200
    assert b"in-process" in response.content
    assert received["vault"] == "cas"
    assert received["authorization"] is None


async def test_tx_006_parity_equivalent_result_for_a_read():
    """Both bindings, pointed at the same logical SAGE, return the same status
    and body for a read.

    Anti-coincidental-pass: pointing one binding at a different SAGE app would
    diverge the bodies; a binding that ignored its target and returned an empty
    body would diverge too.
    """
    sage_app = _shared_sage_app()
    inproc = InProcessSageTransport(sage_app)
    http_client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=sage_app), base_url="http://sage.test"
    )
    http = HttpSageTransport(ObOSageClient("http://sage.test", _StubOidc(), client=http_client))

    via_inproc = await inproc.request("GET", "/sage_vaults/cas/stats")
    via_http = await http.request("GET", "/sage_vaults/cas/stats", session=_session())

    assert via_inproc.status_code == via_http.status_code == 200
    assert via_inproc.content == via_http.content


def test_tx_007_registration_and_profile_resolution():
    """Importing the transport module registers the seam for both profiles, and
    `resolve_bff_transport` selects the binding the active profile names.

    Anti-coincidental-pass: registering only one profile, or swapping the two
    builders, would make a resolution return the wrong binding type.
    """
    assert profiles.LOCAL_PROFILE in profiles.registered_profiles()
    assert profiles.CLOUD_PROFILE in profiles.registered_profiles()

    local_cfg = SageCoreConfig(profile="local")
    cloud_cfg = SageCoreConfig(profile="cloud")

    inproc = resolve_bff_transport(local_cfg, sage_app=FastAPI())
    http = resolve_bff_transport(
        cloud_cfg, oidc_client=ObOSageClient("http://sage.test", _StubOidc())
    )

    assert isinstance(inproc, InProcessSageTransport)
    assert isinstance(http, HttpSageTransport)
