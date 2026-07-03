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

from typing import Any

import pytest
from fastapi.testclient import TestClient

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
    OpenAPI conformance gate validates.
    """
    app = create_app()
    paths = set(app.openapi().get("paths", {}))
    for mount, _surface in MCP_HTTP_MOUNTS:
        offenders = {p for p in paths if p == mount or p.startswith(mount + "/")}
        assert not offenders, f"MCP mount leaked into OpenAPI: {sorted(offenders)}"
