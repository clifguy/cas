"""Transport gate for the HTTP MCP mounts: exact-path Streamable HTTP.

A standards MCP client (the claude.ai connector, Claude Desktop) speaks the
Streamable HTTP transport: it POSTs JSON-RPC directly to the mount URL — the
byte-exact, slash-less path the edge's protected-resource metadata advertises
as the OAuth resource. These tests pin the contract that broke once in
production: a POST to exactly ``/mcp`` (no trailing slash) must be served by
the transport, not answered with a ``307`` trailing-slash redirect.

The redirect regression is structural: a Starlette ``Mount`` can never match
the exact mount path (its regex requires the trailing slash), so the parent
router's ``redirect_slashes`` answers 307 — which MCP clients do not follow
on POST. The transport must therefore hang off an exact-path ``Route`` on the
parent router. ``follow_redirects=False`` on every request here is
load-bearing: the test client follows redirects by default, which would mask
the exact failure this file exists to catch.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from starlette.types import Message, Receive, Scope, Send

from sage.app import MCP_HTTP_MOUNTS, create_app

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

_HEADERS = {
    "Accept": "application/json, text/event-stream",
    "Content-Type": "application/json",
}


@pytest.mark.parametrize(("path", "surface"), MCP_HTTP_MOUNTS)
def test_post_exact_mount_serves_initialize(path: str, surface: str) -> None:
    """POST to the exact mount path completes the MCP initialize handshake.

    Asserts the response is a JSON-RPC result naming the partitioned surface —
    not merely a 200 — so a 200-shaped non-MCP body (an error page, a proxy
    default) cannot be credited as a served transport.
    """
    app = create_app()
    with TestClient(app) as client:
        resp = client.post(path, json=_INITIALIZE, headers=_HEADERS, follow_redirects=False)
        assert resp.status_code == 200, (
            f"POST {path} answered {resp.status_code}, not 200 — a 3xx here is the "
            "trailing-slash redirect regression (a Mount cannot match the exact path)"
        )
        msg = resp.json()
        assert msg.get("jsonrpc") == "2.0", f"non-JSON-RPC body on {path}: {msg!r}"
        result = msg.get("result")
        assert isinstance(result, dict) and isinstance(result.get("serverInfo"), dict), (
            f"initialize on {path} returned no serverInfo: {msg!r}"
        )
        assert result["serverInfo"]["name"] == surface


@pytest.mark.parametrize(("path", "surface"), MCP_HTTP_MOUNTS)
def test_post_exact_mount_does_not_redirect(path: str, surface: str) -> None:
    """No 3xx of any flavor on the exact mount path.

    Separate from the 200 assertion above so a future regression reads as
    'the mount redirects again', not as a generic handshake failure.
    """
    app = create_app()
    with TestClient(app) as client:
        resp = client.post(path, json=_INITIALIZE, headers=_HEADERS, follow_redirects=False)
        assert resp.status_code not in range(300, 400), (
            f"POST {path} redirected ({resp.status_code} -> "
            f"{resp.headers.get('location', '?')}); MCP clients do not follow POST redirects"
        )


def test_mcp_mounts_absent_from_openapi() -> None:
    """The raw transport routes stay out of the documented OpenAPI surface.

    FastAPI skips non-APIRoute entries when generating the schema; this pins
    that behavior so the MCP endpoints never leak into the REST contract the
    OpenAPI conformance gate validates. Both entries at a mount path are
    covered: the transport route and the GET-only route that declines the
    standalone event stream.
    """
    app = create_app()
    paths = set(app.openapi().get("paths", {}))
    mounted = [r for r in app.router.routes if getattr(r, "path", None) in dict(MCP_HTTP_MOUNTS)]
    # Positive control: absence from OpenAPI means nothing if nothing is
    # mounted. Each mount contributes a transport route and a decline route.
    assert len(mounted) == 2 * len(MCP_HTTP_MOUNTS), (
        f"expected two routes per mount, found {[getattr(r, 'path', None) for r in mounted]}"
    )
    for mount, _surface in MCP_HTTP_MOUNTS:
        offenders = {p for p in paths if p == mount or p.startswith(mount + "/")}
        assert not offenders, f"MCP mount leaked into OpenAPI: {sorted(offenders)}"


@pytest.mark.parametrize(("path", "surface"), MCP_HTTP_MOUNTS)
def test_transport_statuses_reach_access_filter(
    path: str, surface: str, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Feed raw app response messages through Uvicorn's real logging producer.

    Anti-coincidental-pass: the three routine requests are asserted to emit no
    access record, which on its own would also hold if no record ever reached
    the logger -- an unwired access logger, a handler that dropped everything,
    a filter that suppressed unconditionally. The fourth request is the control
    that separates those: an unexpected route under the mount must still print,
    so the empty list above means "suppressed" rather than "never arrived".
    """
    import asyncio
    import logging
    from http import HTTPStatus
    from unittest.mock import Mock

    import h11
    from uvicorn.protocols.http.flow_control import FlowControl
    from uvicorn.protocols.http.h11_impl import RequestResponseCycle

    from sage.__main__ import _DropMcpAccessLogs

    app = create_app(vault_root=tmp_path)
    logger = logging.Logger("uvicorn.access", logging.INFO)
    logger.addHandler(caplog.handler)
    logger.addFilter(_DropMcpAccessLogs())
    statuses: list[int] = []

    async def observed_app(scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            return await app(scope, receive, send)
        transport = Mock()
        cycle = RequestResponseCycle(
            scope=scope,
            conn=h11.Connection(h11.SERVER),
            transport=transport,
            flow=FlowControl(transport),
            logger=logging.getLogger("uvicorn.error"),
            access_logger=logger,
            access_log=True,
            default_headers=[],
            message_event=asyncio.Event(),
            on_response=lambda: None,
        )

        async def observed_send(message: Message) -> None:
            if message["type"] == "http.response.start":
                statuses.append(message["status"])
            await cycle.send(message)
            await send(message)

        await app(scope, receive, observed_send)

    with TestClient(observed_app) as client:
        response = client.post(path, json=_INITIALIZE, headers=_HEADERS, follow_redirects=False)
        assert response.status_code == 200
        assert response.json()["result"]["serverInfo"]["name"] == surface
        response = client.post(
            path,
            json={"jsonrpc": "2.0", "method": "notifications/initialized"},
            headers=_HEADERS,
            follow_redirects=False,
        )
        assert response.status_code == 202
        response = client.get(path, headers={"Accept": "application/json"}, follow_redirects=False)
        assert response.status_code == 405
        # Control: an unexpected route, which must stay visible.
        response = client.get(f"{path}/nope", follow_redirects=False)
        assert response.status_code == 404

    assert statuses == [
        HTTPStatus.OK,
        HTTPStatus.ACCEPTED,
        HTTPStatus.METHOD_NOT_ALLOWED,
        HTTPStatus.NOT_FOUND,
    ]
    assert statuses[0] is HTTPStatus.OK
    assert statuses[1] is HTTPStatus.ACCEPTED
    # Every one of the first three is routine startup traffic, so none reaches
    # the console: the POSTs are dropped outright and the declined GET is
    # counted into the once-a-minute summary instead of printed per attempt.
    # The unexpected route survives, proving the records do arrive.
    records = [r for r in caplog.records if r.name == "uvicorn.access"]
    assert [(r.args[1], r.args[2], r.args[4]) for r in records] == [("GET", f"{path}/nope", 404)]
