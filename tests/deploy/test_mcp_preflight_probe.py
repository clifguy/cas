"""Protocol gate for ``deploy/mcp_preflight_probe.py``.

The probe drives a real JSON-RPC handshake over the SAGE MCP HTTP+SSE transport
(``GET <mount>/sse`` -> ``endpoint`` event -> ``POST <mount>/messages/`` ->
result on the stream). Two layers prove it:

* **Faithful** -- run the probe (as a subprocess, the production caller path)
  against the *real* ``build_partitioned_server(...).sse_app()`` served by
  uvicorn-in-thread. This is the anti-divergence guard: a probe hand-fitted to a
  fake's frame format would pass the stub tests below yet fail here against the
  genuine mcp transport.
* **Trap** -- a controllable threaded HTTP+SSE stub reproduces the pathologies a
  real server will not produce on demand (a surface that blanket-200s an unknown
  method, a missing ``endpoint`` event, a malformed ``initialize`` result, a 401
  on the stream open). Each must make the probe FAIL -- proving every assertion
  in the probe is load-bearing, not decorative.

The trap layer is standard-library only and always runs; the faithful layer is
skipped only where ``sage`` / ``uvicorn`` cannot be imported.
"""

from __future__ import annotations

import http.client
import http.server
import json
import os
import queue
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
    import uvicorn
    from starlette.applications import Starlette
    from starlette.routing import Mount

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
# Faithful layer: the real MCP SSE transport, uvicorn-in-thread               #
# --------------------------------------------------------------------------- #
@contextmanager
def _serve_real() -> Iterator[str]:
    """Mount the real ordinary + maintenance MCP SSE apps and serve them."""
    routes = [
        Mount(path, app=build_partitioned_server(surface).sse_app())
        for path, surface in (("/mcp", "sage"), ("/mcp_admin", "sage_admin"))
    ]
    app = Starlette(routes=routes)
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
def test_roundtrip_against_real_mcp_mount() -> None:
    """initialize + tools/list + unknown-method negative control against the
    genuine ``/mcp`` SSE transport -- the divergence guard.
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


# --------------------------------------------------------------------------- #
# Trap layer: a controllable HTTP+SSE stub                                    #
# --------------------------------------------------------------------------- #
Responder = Callable[[dict[str, Any]], "dict[str, Any] | None"]


def _result(rid: Any, result: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": rid, "result": result}


def _error(rid: Any) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": rid, "error": {"code": -32601, "message": "Method not found"}}


_SERVER_INFO = {"protocolVersion": "2024-11-05", "capabilities": {}, "serverInfo": {"name": "stub"}}


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
def sse_stub(
    responder: Responder,
    *,
    send_endpoint: bool = True,
    sse_status: int = 200,
    sse_body: str = "",
) -> Iterator[str]:
    """A threaded HTTP+SSE stub: ``GET <mount>/sse`` opens a stream (optionally
    emitting the ``endpoint`` event), ``POST <mount>/messages/`` is acked 202 and
    its ``responder``-computed reply is pushed back on the open stream.

    When ``sse_status`` is non-200, the GET returns that status with ``sse_body``
    as the response body, modelling an edge that rejects the handshake with a
    diagnostic message (e.g. a 421 ``Invalid Host header``).
    """
    port = _free_port()
    stop = threading.Event()
    outbox: queue.Queue[dict[str, Any] | None] = queue.Queue()

    class _Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *_a: object) -> None:
            return

        def _event(self, event: str, data: str) -> None:
            try:
                self.wfile.write(f"event: {event}\ndata: {data}\n\n".encode())
                self.wfile.flush()
            except OSError:
                pass

        def do_GET(self) -> None:  # noqa: N802
            if sse_status != 200:
                body = sse_body.encode()
                self.send_response(sse_status)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                if body:
                    self.wfile.write(body)
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            mount = self.path.split("?", 1)[0].rsplit("/sse", 1)[0]
            if send_endpoint:
                self._event("endpoint", f"{mount}/messages/?session_id=stub")
            while not stop.is_set():
                try:
                    msg = outbox.get(timeout=0.1)
                except queue.Empty:
                    continue
                if msg is None:
                    break
                self._event("message", json.dumps(msg))

        def do_POST(self) -> None:  # noqa: N802
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b""
            self.send_response(202)
            self.end_headers()
            try:
                req = json.loads(raw)
            except ValueError:
                return
            if req.get("id") is None:
                return  # a notification draws no response
            reply = responder(req)
            if reply is not None:
                outbox.put(reply)

    server = http.server.ThreadingHTTPServer(("127.0.0.1", port), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        stop.set()
        outbox.put(None)
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_probe_passes_against_good_stub() -> None:
    """Positive control for the trap layer: a well-behaved surface must PASS, so
    the FAIL assertions below are credited to the pathology, not the stub harness.
    """
    with sse_stub(_good) as base:
        proc = _run_probe(base, "/mcp", "roundtrip")
    assert proc.returncode == 0, f"stdout={proc.stdout!r} stderr={proc.stderr!r}"
    assert "negctrl=error" in proc.stdout


def test_probe_fails_when_surface_blanket_200s() -> None:
    """The negative-control guard: an unknown method that returns a result (no
    JSON-RPC error) means the surface is not discriminating -> the probe FAILs.
    """
    with sse_stub(_blanket_200) as base:
        proc = _run_probe(base, "/mcp", "roundtrip")
    assert proc.returncode != 0, "a non-discriminating surface must fail the probe"
    assert "negctrl=fail" in proc.stdout, proc.stdout


def test_probe_fails_when_no_endpoint_event() -> None:
    """No ``endpoint`` event -> the probe cannot derive a session URL -> FAIL on
    timeout (it must read the real event, not assume the path).
    """
    with sse_stub(_good, send_endpoint=False) as base:
        proc = _run_probe(base, "/mcp", "roundtrip", timeout="3")
    assert proc.returncode != 0
    assert "no_endpoint_event" in proc.stdout, proc.stdout


def test_probe_fails_when_initialize_result_malformed() -> None:
    """A 200-shaped initialize result lacking ``serverInfo`` must not be credited
    as a completed handshake.
    """
    with sse_stub(_malformed_init) as base:
        proc = _run_probe(base, "/mcp", "roundtrip")
    assert proc.returncode != 0
    assert "initialize=fail" in proc.stdout, proc.stdout


def test_probe_fails_on_401() -> None:
    """A 401 on the stream open is an auth failure, not reachability -> FAIL."""
    with sse_stub(_good, sse_status=401) as base:
        proc = _run_probe(base, "/mcp", "roundtrip", timeout="3")
    assert proc.returncode != 0
    assert "sse_status=401" in proc.stdout, proc.stdout


def test_probe_emits_response_body_on_http_error() -> None:
    """On an HTTP error opening the SSE stream, the probe surfaces the response
    *body*, not just the status code -- so an edge rejection (e.g. a 421
    ``Invalid Host header``) is self-diagnosing from the verdict line alone.
    """
    with sse_stub(_good, sse_status=421, sse_body="Invalid Host header") as base:
        proc = _run_probe(base, "/mcp", "roundtrip", timeout="3")
    assert proc.returncode != 0
    assert "sse_status=421" in proc.stdout, proc.stdout
    assert "Invalid Host header" in proc.stdout, proc.stdout


@_NEEDS_REAL
@pytest.mark.parametrize("mount", ["/mcp", "/mcp_admin"])
def test_sse_handshake_accepts_non_loopback_host(mount: str) -> None:
    """A non-loopback Host (as APIM/ACA forwards to the backend) must not be
    rejected with 421 on the SSE handshake.

    The MCP SDK auto-enables DNS-rebinding Host validation for a loopback bind
    host; its allow-list 421s every non-loopback (proxied) Host. SAGE disables
    that check (CAS-ADR-034: the edge boundary is the JWT/identity layer), so a
    forged non-loopback Host must complete the handshake. The faithful guard the
    in-process and loopback-only checks could never catch -- the bug shipped
    precisely because every prior probe carried a loopback Host. Only the
    handshake status is read; the long-lived SSE body is left unread so the open
    stream cannot block the test.
    """
    with _serve_real() as base:
        parts = urllib.parse.urlsplit(base)
        conn = http.client.HTTPConnection(parts.hostname, parts.port, timeout=10)
        try:
            conn.putrequest("GET", f"{mount}/sse", skip_host=True)
            conn.putheader("Host", "sage.example.com")
            conn.putheader("Accept", "text/event-stream")
            conn.endheaders()
            status = conn.getresponse().status
        finally:
            conn.close()
    assert status != 421, f"non-loopback Host rejected with {status} on {mount}/sse"
    assert status == 200
