"""The HTTP MCP mounts decline the standalone GET event stream.

Under ``stateless_http=True`` each JSON-RPC request is served by a transport
built for that request alone, holding no session id and no event store. A
standalone GET therefore opens a stream nothing can ever be written to: it
emits no bytes and stays open until the client abandons it. The Streamable
HTTP specification's answer for an endpoint that offers no such stream is
``405 Method Not Allowed``, and that is what these tests pin.

The decline is decided on the method, before the transport's ``Accept``
negotiation runs. That ordering is the substance of the change rather than an
implementation detail: a client sending ``Accept: text/event-stream`` and a
client sending ``Accept: */*`` reach the same endpoint for the same reason,
and both deserve the same answer. Pinning it here keeps a later refactor from
reintroducing a ``406`` for one of them.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from sage.app import (
    EVENT_STREAM_DECLINED_MESSAGE,
    MCP_HTTP_MOUNTS,
    _event_stream_decline_route,
    create_app,
)
from sage.mcp_server import build_partitioned_server

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

_POST_HEADERS = {
    "Accept": "application/json, text/event-stream",
    "Content-Type": "application/json",
}


@pytest.fixture
def client(tmp_path: Path):
    with TestClient(create_app(vault_root=tmp_path)) as test_client:
        yield test_client


@pytest.mark.parametrize(("path", "surface"), MCP_HTTP_MOUNTS)
def test_standalone_get_is_declined(client: TestClient, path: str, surface: str) -> None:
    """A GET that asks for an event stream is refused as a method, not negotiated."""
    resp = client.get(path, headers={"Accept": "text/event-stream"}, follow_redirects=False)

    assert resp.status_code == 405, (
        f"GET {path} answered {resp.status_code}; a 200 here is the dead stream this "
        "change exists to remove, and a 406 means the decline ran after Accept negotiation"
    )
    assert "POST" in (resp.headers.get("allow") or ""), (
        f"GET {path} returned 405 without naming POST in Allow: {resp.headers.get('allow')!r}"
    )
    body = resp.json()
    assert body.get("jsonrpc") == "2.0", f"non-JSON-RPC decline body on {path}: {body!r}"
    assert body.get("error", {}).get("message") == EVENT_STREAM_DECLINED_MESSAGE


# Two clients observed at startup send the first and the fourth of these. The
# rest bracket them: one that names both types, and two that name none.
_ACCEPT_VALUES = [
    "text/event-stream",
    "application/json, text/event-stream",
    "text/event-stream, application/json",
    "*/*",
    "application/json",
    None,
]


@pytest.mark.parametrize(("path", "surface"), MCP_HTTP_MOUNTS)
@pytest.mark.parametrize("accept", _ACCEPT_VALUES)
def test_standalone_get_is_declined_regardless_of_accept(
    client: TestClient, path: str, surface: str, accept: str | None
) -> None:
    """Every Accept value gets 405 -- never 406, never 200.

    The ``application/json`` and ``*/*`` arms are the discriminator against an
    implementation that declines *after* the transport's Accept check, which
    would leave those two answering 406.
    """
    headers = {} if accept is None else {"Accept": accept}
    resp = client.get(path, headers=headers, follow_redirects=False)

    assert resp.status_code == 405, (
        f"GET {path} with Accept={accept!r} answered {resp.status_code}, not 405"
    )
    assert resp.json().get("error", {}).get("message") == EVENT_STREAM_DECLINED_MESSAGE


@pytest.mark.parametrize(("path", "surface"), MCP_HTTP_MOUNTS)
def test_declined_get_is_a_bounded_body_not_a_stream(
    client: TestClient, path: str, surface: str
) -> None:
    """The decline completes; it does not become the stream it refuses.

    Regression guard for the user-visible defect: the previous 200 answered
    with ``text/event-stream`` and then emitted nothing, so the request only
    ended when the client gave up.
    """
    resp = client.get(path, headers={"Accept": "text/event-stream"}, follow_redirects=False)

    content_type = resp.headers.get("content-type", "")
    assert "text/event-stream" not in content_type, (
        f"GET {path} still answered with an event stream: {content_type!r}"
    )
    assert content_type.startswith("application/json")
    assert resp.headers.get("content-length") == str(len(resp.content))


@pytest.mark.parametrize(("path", "surface"), MCP_HTTP_MOUNTS)
def test_post_still_reaches_the_transport(client: TestClient, path: str, surface: str) -> None:
    """The GET-only route must not shadow the method that carries every call."""
    resp = client.post(path, json=_INITIALIZE, headers=_POST_HEADERS, follow_redirects=False)

    assert resp.status_code == 200, (
        f"POST {path} answered {resp.status_code}; a 405 here means the decline route "
        "matched every method instead of GET alone"
    )
    assert resp.json()["result"]["serverInfo"]["name"] == surface


@pytest.mark.parametrize(("path", "surface"), MCP_HTTP_MOUNTS)
def test_delete_still_reaches_the_transport(client: TestClient, path: str, surface: str) -> None:
    """DELETE keeps the transport's own answer rather than the decline route's.

    The transport answers DELETE with its own 405 under stateless operation, so
    the status alone cannot tell the two apart. The decline message is the
    discriminator, and the absence of ``Allow: POST`` corroborates it.
    """
    resp = client.request(
        "DELETE", path, headers={"Accept": "application/json"}, follow_redirects=False
    )

    assert resp.json().get("error", {}).get("message") != EVENT_STREAM_DECLINED_MESSAGE, (
        f"DELETE {path} was answered by the GET decline route, which must match GET alone"
    )


@pytest.mark.parametrize(("path", "surface"), MCP_HTTP_MOUNTS)
def test_decline_requires_a_stateless_server(path: str, surface: str) -> None:
    """The decline is licensed by statelessness, and says so when it is not.

    A stateful server mints a session id and can hold an event store, so it may
    legitimately serve the stream this route refuses. Should the transport
    settings ever change, the mounter must fail loudly rather than keep
    refusing a stream the server could now serve.
    """
    server = build_partitioned_server(surface)
    assert server.settings.stateless_http is True
    # Starlette pairs HEAD with GET on its own. POST must not be among them:
    # that is the set membership the transport's reachability depends on.
    assert _event_stream_decline_route(server, path).methods == {"GET", "HEAD"}

    server.settings.stateless_http = False
    with pytest.raises(RuntimeError, match="stateless"):
        _event_stream_decline_route(server, path)
