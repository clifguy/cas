"""Protocol gate for ``deploy/mcp_preflight_probe.py``.

The probe drives a real JSON-RPC handshake over the SAGE MCP Streamable HTTP
transport (``POST <mount>`` with the JSON-RPC message as the request body and
the response in the POST body). Two layers prove it:

* **Faithful** -- run the probe (as a subprocess, the production caller path)
  against the *real* ``build_partitioned_server(...).streamable_http_app()``
  served by uvicorn-in-thread. This is the anti-divergence guard: a probe
  hand-fitted to a fake's framing would pass the stub tests below yet fail
  here against the genuine mcp transport.
* **Trap** -- a controllable threaded HTTP stub reproduces the pathologies a
  real server will not produce on demand (a surface that blanket-200s an
  unknown method, a malformed ``initialize`` result, a 401, and -- the class
  this probe exists to catch -- redirects on POST). Each must make the probe
  FAIL, proving every assertion in the probe is load-bearing, not decorative.

The redirect traps carry the sharpest teeth. A ``307`` on ``POST <mount>`` is
exactly how a trailing-slash-redirecting backend presents to a standards MCP
client; the probe must name it as a failure rather than report a reachable
surface. The ``303`` variant guards the probe's own HTTP client: urllib's
default redirect handler silently converts a redirected POST into a GET on
301/302/303, so a probe that follows redirects would fetch some 200-shaped
page and credit the handshake. The probe must refuse every 3xx explicitly.

The trap layer is standard-library only and always runs; the faithful layer is
skipped only where ``sage`` / ``uvicorn`` cannot be imported.
"""

from __future__ import annotations

import http.client
import http.server
import json
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.parse
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Final

import pytest

_REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[2]
_PROBE: Final[Path] = _REPO_ROOT / "deploy" / "mcp_preflight_probe.py"

try:  # the faithful layer needs the app; the trap layer does not
    from contextlib import AsyncExitStack, asynccontextmanager

    import uvicorn
    from starlette.applications import Starlette

    from sage.mcp_server import build_partitioned_server

    _REAL_SERVER = True
except Exception:  # noqa: BLE001 -- absence just skips the faithful layer
    _REAL_SERVER = False

_NEEDS_REAL = pytest.mark.skipif(
    not _REAL_SERVER, reason="real-server layer needs sage + uvicorn importable"
)


# --------------------------------------------------------------------------- #
# Probe invocation (subprocess -- the production caller path)                 #
# --------------------------------------------------------------------------- #
def _run_probe(
    base_url: str,
    mount: str,
    mode: str = "roundtrip",
    timeout: str = "10",
    token: str = "",
) -> subprocess.CompletedProcess[str]:
    """Run the probe under a fresh interpreter with an isolated environment.

    Only ``PATH`` (and an explicit ``AUTH_TOKEN`` when asked) are passed, so a
    real token in the developer's shell cannot leak in. The probe is stdlib-only,
    so the test interpreter runs it unchanged.
    """
    env = {"PATH": os.environ.get("PATH", "")}
    if token:
        env["AUTH_TOKEN"] = token
    return subprocess.run(
        [
            sys.executable,
            str(_PROBE),
            "--base-url",
            base_url,
            "--mount",
            mount,
            "--mode",
            mode,
            "--timeout",
            timeout,
        ],
        capture_output=True,
        text=True,
        env=env,
        timeout=int(float(timeout)) + 15,
    )


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# --------------------------------------------------------------------------- #
# Faithful layer: the real MCP Streamable HTTP transport, uvicorn-in-thread   #
# --------------------------------------------------------------------------- #
@contextmanager
def _serve_real() -> Iterator[str]:
    """Serve the real ordinary + maintenance streamable transports.

    Mirrors the production wiring in ``sage.app._mount_partitioned_mcp``: each
    partitioned server's ``streamable_http_path`` is its full mount path, the
    resulting exact-path routes live on one parent app, and the parent
    lifespan runs every session manager (a mounted sub-app's own lifespan
    never runs, so the parent must).
    """
    servers = []
    routes = []
    for path, surface in (("/mcp", "sage"), ("/mcp_admin", "sage_admin")):
        server = build_partitioned_server(surface)
        server.settings.streamable_http_path = path
        routes.extend(server.streamable_http_app().routes)
        servers.append(server)

    @asynccontextmanager
    async def _lifespan(_app: Starlette):
        async with AsyncExitStack() as stack:
            for server in servers:
                await stack.enter_async_context(server.session_manager.run())
            yield

    app = Starlette(routes=routes, lifespan=_lifespan)
    port = _free_port()
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 20
    while not getattr(server, "started", False):
        if time.monotonic() > deadline:
            raise RuntimeError("uvicorn did not start within 20s")
        time.sleep(0.05)
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=5)


@_NEEDS_REAL
def test_roundtrip_against_real_streamable_mount() -> None:
    """initialize + tools/list + unknown-method negative control against the
    genuine ``/mcp`` streamable transport -- the divergence guard.
    """
    with _serve_real() as base:
        proc = _run_probe(base, "/mcp", "roundtrip")
    assert proc.returncode == 0, f"stdout={proc.stdout!r} stderr={proc.stderr!r}"
    assert "initialize=ok" in proc.stdout
    assert "tools_list=ok" in proc.stdout
    assert "negctrl=error" in proc.stdout


@_NEEDS_REAL
def test_handshake_against_real_admin_mount() -> None:
    """A handshake on the genuine ``/mcp_admin`` maintenance mount completes --
    the shape an authenticated maintenance call takes through the edge.
    """
    with _serve_real() as base:
        proc = _run_probe(base, "/mcp_admin", "handshake")
    assert proc.returncode == 0, f"stdout={proc.stdout!r} stderr={proc.stderr!r}"
    assert "initialize=ok" in proc.stdout


@_NEEDS_REAL
@pytest.mark.parametrize("mount", ["/mcp", "/mcp_admin"])
def test_streamable_post_accepts_non_loopback_host(mount: str) -> None:
    """A non-loopback Host (as a proxying edge forwards to the backend) must
    not be rejected with 421 on the initialize POST.

    The MCP SDK auto-enables DNS-rebinding Host validation for a loopback bind
    host; its allow-list 421s every non-loopback (proxied) Host. SAGE disables
    that check (CAS-ADR-034: the edge boundary is the JWT/identity layer), so a
    forged non-loopback Host must complete the handshake. This is the faithful
    guard the in-process and loopback-only checks could never catch -- the bug
    shipped precisely because every prior probe carried a loopback Host.
    """
    body = json.dumps(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "host-guard", "version": "1.0"},
            },
        }
    ).encode()
    with _serve_real() as base:
        parts = urllib.parse.urlsplit(base)
        conn = http.client.HTTPConnection(parts.hostname, parts.port, timeout=10)
        try:
            conn.putrequest("POST", mount, skip_host=True)
            conn.putheader("Host", "sage.example.com")
            conn.putheader("Accept", "application/json, text/event-stream")
            conn.putheader("Content-Type", "application/json")
            conn.putheader("Content-Length", str(len(body)))
            conn.endheaders()
            conn.send(body)
            status = conn.getresponse().status
        finally:
            conn.close()
    assert status != 421, f"non-loopback Host rejected with {status} on POST {mount}"
    assert status == 200


# --------------------------------------------------------------------------- #
# Trap layer: a controllable HTTP stub                                        #
# --------------------------------------------------------------------------- #
Responder = Callable[[dict[str, Any]], "dict[str, Any] | None"]


def _result(rid: Any, result: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": rid, "result": result}


def _error(rid: Any) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": rid, "error": {"code": -32601, "message": "Method not found"}}


_SERVER_INFO = {"protocolVersion": "2025-06-18", "capabilities": {}, "serverInfo": {"name": "stub"}}


def _good(req: dict[str, Any]) -> dict[str, Any]:
    """A well-behaved surface: real handshake, a tool roster, errors on unknown."""
    rid, method = req.get("id"), req.get("method")
    if method == "initialize":
        return _result(rid, dict(_SERVER_INFO))
    if method == "tools/list":
        return _result(rid, {"tools": [{"name": "x"}]})
    return _error(rid)


def _blanket_200(req: dict[str, Any]) -> dict[str, Any]:
    """Handshake + tools/list look healthy, but an unknown method ALSO returns a
    result instead of a JSON-RPC error -- the surface is not discriminating.
    """
    rid, method = req.get("id"), req.get("method")
    if method == "initialize":
        return _result(rid, dict(_SERVER_INFO))
    if method == "tools/list":
        return _result(rid, {"tools": [{"name": "x"}]})
    return _result(rid, {"ok": True})


def _malformed_init(req: dict[str, Any]) -> dict[str, Any]:
    """initialize returns a result with no ``serverInfo`` -- a 200-shaped lie."""
    rid, method = req.get("id"), req.get("method")
    if method == "initialize":
        return _result(rid, {"capabilities": {}})
    if method == "tools/list":
        return _result(rid, {"tools": [{"name": "x"}]})
    return _error(rid)


@contextmanager
def streamable_stub(
    responder: Responder,
    *,
    post_status: int = 200,
    post_body: str = "",
    redirect_location: str | None = None,
) -> Iterator[str]:
    """A threaded HTTP stub speaking the Streamable HTTP shape: ``POST <mount>``
    answers with the ``responder``-computed JSON-RPC reply in the response body;
    a notification (no ``id``) is acknowledged ``202`` with no body.

    When ``post_status`` is not 200, the POST returns that status instead --
    with ``post_body`` as the response body (modelling an edge rejection whose
    body names its own cause, e.g. a 421 ``Invalid Host header``), and, for
    3xx statuses, ``redirect_location`` as the ``Location`` header (modelling
    the trailing-slash-redirecting backend). ``GET /ok`` always answers a
    200-shaped initialize result: the honeypot a redirect-following client
    would land on and wrongly credit.
    """
    port = _free_port()

    class _Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *_a: object) -> None:
            return

        def _send_json(self, payload: dict[str, Any]) -> None:
            body = json.dumps(payload).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:  # noqa: N802
            # The honeypot: what a redirect-following (POST->GET converting)
            # client fetches after a 301/302/303. Looks like a valid handshake.
            self._send_json(_result(1, dict(_SERVER_INFO)))

        def do_POST(self) -> None:  # noqa: N802
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b""
            if post_status != 200:
                body = post_body.encode()
                self.send_response(post_status)
                if redirect_location is not None:
                    self.send_header("Location", redirect_location)
                self.send_header("Content-Type", "text/plain")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                if body:
                    self.wfile.write(body)
                return
            try:
                req = json.loads(raw)
            except ValueError:
                self.send_response(400)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            if req.get("id") is None:
                self.send_response(202)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            reply = responder(req)
            self._send_json(reply if reply is not None else _error(req.get("id")))

    server = http.server.ThreadingHTTPServer(("127.0.0.1", port), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_probe_passes_against_good_stub() -> None:
    """Positive control for the trap layer: a well-behaved surface must PASS, so
    the FAIL assertions below are credited to the pathology, not the stub harness.
    """
    with streamable_stub(_good) as base:
        proc = _run_probe(base, "/mcp", "roundtrip")
    assert proc.returncode == 0, f"stdout={proc.stdout!r} stderr={proc.stderr!r}"
    assert "negctrl=error" in proc.stdout


def test_probe_fails_when_surface_blanket_200s() -> None:
    """The negative-control guard: an unknown method that returns a result (no
    JSON-RPC error) means the surface is not discriminating -> the probe FAILs.
    """
    with streamable_stub(_blanket_200) as base:
        proc = _run_probe(base, "/mcp", "roundtrip")
    assert proc.returncode != 0, "a non-discriminating surface must fail the probe"
    assert "negctrl=fail" in proc.stdout, proc.stdout


def test_probe_fails_when_initialize_result_malformed() -> None:
    """A 200-shaped initialize result lacking ``serverInfo`` must not be credited
    as a completed handshake.
    """
    with streamable_stub(_malformed_init) as base:
        proc = _run_probe(base, "/mcp", "roundtrip")
    assert proc.returncode != 0
    assert "initialize=fail" in proc.stdout, proc.stdout


def test_probe_fails_on_307_post() -> None:
    """A 307 on the initialize POST is the trailing-slash-redirect regression
    presenting exactly as it did live -- the probe must FAIL with a named
    redirect verdict, never report a reachable surface.
    """
    with streamable_stub(_good, post_status=307, redirect_location="/mcp/") as base:
        proc = _run_probe(base, "/mcp", "handshake", timeout="5")
    assert proc.returncode != 0, "a redirecting mount must fail the probe"
    assert "post_status=307" in proc.stdout, proc.stdout
    assert "redirect_refused" in proc.stdout, proc.stdout


def test_probe_fails_on_303_post() -> None:
    """A 303 must FAIL even though urllib's default handler would silently
    convert the redirected POST into a GET -- which here lands on a honeypot
    that answers a perfectly valid-looking initialize result. Catches a probe
    that follows redirects instead of refusing them.
    """
    with streamable_stub(_good, post_status=303, redirect_location="/ok") as base:
        proc = _run_probe(base, "/mcp", "handshake", timeout="5")
    assert proc.returncode != 0, (
        "a 303-redirecting mount must fail the probe; a pass means the probe "
        "followed the redirect (POST->GET) and credited the honeypot response"
    )
    assert "post_status=303" in proc.stdout, proc.stdout
    assert "redirect_refused" in proc.stdout, proc.stdout


def test_probe_fails_on_401() -> None:
    """A 401 on the initialize POST is an auth failure, not reachability -> FAIL."""
    with streamable_stub(_good, post_status=401, post_body="Unauthorized") as base:
        proc = _run_probe(base, "/mcp", "roundtrip", timeout="5")
    assert proc.returncode != 0
    assert "post_status=401" in proc.stdout, proc.stdout


def test_probe_emits_response_body_on_http_error() -> None:
    """On an HTTP error, the probe surfaces the response *body*, not just the
    status code -- so an edge rejection (e.g. a 421 ``Invalid Host header``) is
    self-diagnosing from the verdict line alone.
    """
    with streamable_stub(_good, post_status=421, post_body="Invalid Host header") as base:
        proc = _run_probe(base, "/mcp", "roundtrip", timeout="5")
    assert proc.returncode != 0
    assert "post_status=421" in proc.stdout, proc.stdout
    assert "Invalid Host header" in proc.stdout, proc.stdout
