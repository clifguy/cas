"""Gate for ``tests/deploy/_stub_server.py`` -- the shared loopback stub servers.

The defect these helpers exist to prevent is a race: a port chosen by binding a
throwaway socket and closing it can be taken by a sibling worker before the real
server rebinds. A rare race cannot be gated by a passing test -- a green parallel
run is equally consistent with the bug -- so every assertion here is structural.
The load-bearing ones observe *how many times a port is bound* and *which socket
object ends up serving*, both of which distinguish holding a reservation from
sampling a free port regardless of how often the race would actually fire.
"""

from __future__ import annotations

import http.server
import re
import socket
import urllib.request
from collections.abc import Iterator
from pathlib import Path
from typing import Any, Final

import pytest

from tests.deploy._stub_server import (
    AdoptingThreadingHTTPServer,
    reserved_loopback_socket,
    serve_threaded,
    serve_uvicorn,
)

_DEPLOY_TESTS: Final[Path] = Path(__file__).resolve().parent
_TESTS_ROOT: Final[Path] = _DEPLOY_TESTS.parent

#: The three stub modules the shared helper exists for.
_STUB_MODULES: Final[tuple[str, ...]] = (
    "test_cloud_preflight.py",
    "test_sharepoint_validate.py",
    "test_mcp_preflight_probe.py",
)


class _EchoHandler(http.server.BaseHTTPRequestHandler):
    """Answers every GET with its own path, so a probe can prove it arrived."""

    def log_message(self, *_a: object) -> None:
        return

    def do_GET(self) -> None:  # noqa: N802 (BaseHTTPRequestHandler API)
        body = self.path.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def _bind_fails(port: int) -> bool:
    """Whether a fresh socket (no ``SO_REUSEADDR``) is refused that loopback port."""
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        probe.bind(("127.0.0.1", port))
    except OSError:
        return True
    else:
        return False
    finally:
        probe.close()


def _accepts_connections(port: int) -> bool:
    """Whether a TCP connection to that loopback port completes.

    The direct read of this property, ``getsockopt(SOL_SOCKET, SO_ACCEPTCONN)``,
    raises ``Protocol not available`` on macOS, so the property is observed rather
    than queried: the kernel completes the handshake from a listening socket's
    backlog with no ``accept`` call, and refuses outright on one merely bound.
    """
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=2):
            return True
    except OSError:
        return False


@pytest.fixture
def recorded_binds(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[tuple[Any, ...]]]:
    """Record every ``socket.bind`` address in this process, from any thread."""
    binds: list[tuple[Any, ...]] = []
    original = socket.socket.bind

    def _recording_bind(self: socket.socket, address: Any) -> None:
        binds.append(tuple(address) if isinstance(address, tuple) else (address,))
        original(self, address)

    monkeypatch.setattr(socket.socket, "bind", _recording_bind)
    yield binds


def _assert_single_ephemeral_bind(binds: list[tuple[Any, ...]]) -> None:
    """One loopback bind asking for an ephemeral port, and no rebind anywhere.

    Two clauses, because either alone is non-discriminating. The first is filtered
    to loopback so an unrelated bind elsewhere cannot pollute the count -- but that
    filter is itself an escape hatch: sampling a port on ``127.0.0.1`` and rebinding
    it on ``0.0.0.0`` reopens the window while leaving the filtered count at one.
    The second clause is therefore unfiltered, and rejects a bind naming a concrete
    port on *any* host, which is what a rebind must always do.
    """
    loopback = [b for b in binds if b and b[0] == "127.0.0.1"]
    assert loopback == [("127.0.0.1", 0)], f"expected a single ephemeral bind, got {loopback}"
    rebinds = [b for b in binds if len(b) > 1 and b[1] != 0]
    assert not rebinds, f"a bind naming a concrete port reopens the window: {rebinds}"


# --------------------------------------------------------------------------- #
# The reservation primitive                                                    #
# --------------------------------------------------------------------------- #
def test_reserved_socket_holds_its_port_while_open() -> None:
    """The port is unavailable to anyone else for as long as the reservation lives.

    This is the property the old bind-read-close idiom lacked: the port number it
    handed back was free the instant it was returned. The post-exit control proves
    the refusal above comes from the hold rather than from a malformed probe.

    The reservation is also asserted to be *listening*, not merely bound. Binding
    alone holds the port, so without this clause the ``listen`` call is inert and
    could be deleted with every test still green -- while a consumer handed the
    socket would inherit one that refuses connections until it listens itself.
    """
    with reserved_loopback_socket() as sock:
        port = sock.getsockname()[1]
        assert _bind_fails(port), "a held reservation must refuse a second binder"
        assert _accepts_connections(port), "the reservation must be listening, not merely bound"
    assert not _bind_fails(port), "the port must be bindable once the reservation is released"


# --------------------------------------------------------------------------- #
# Socket adoption                                                              #
# --------------------------------------------------------------------------- #
def test_adopting_server_serves_on_the_reserved_socket() -> None:
    """The server serves on the *same socket object* that made the reservation.

    Identity, not port equality, is the discriminator: a server that rebound a
    fresh socket to the reserved port would satisfy every numeric assertion while
    reopening exactly the window this helper closes.
    """
    with reserved_loopback_socket() as sock:
        server = AdoptingThreadingHTTPServer(sock, _EchoHandler)
        try:
            assert server.socket is sock
            assert server.server_port == sock.getsockname()[1]
            assert server.server_address[0] == "127.0.0.1"
        finally:
            server.server_close()


def test_adoption_disposes_the_constructor_socket(monkeypatch: pytest.MonkeyPatch) -> None:
    """``TCPServer.__init__`` always makes a socket; adoption must close that one.

    Leaving it open leaks a descriptor per stub server, and closing the *reserved*
    one instead would drop the reservation -- so both halves are asserted.
    """
    closed: list[socket.socket] = []
    original = socket.socket.close

    def _recording_close(self: socket.socket) -> None:
        closed.append(self)
        original(self)

    with reserved_loopback_socket() as sock:
        monkeypatch.setattr(socket.socket, "close", _recording_close)
        server = AdoptingThreadingHTTPServer(sock, _EchoHandler)
        monkeypatch.undo()
        try:
            assert len(closed) == 1, f"expected one disposal during construction, got {closed}"
            assert closed[0] is not sock, "the reserved socket must survive adoption"
        finally:
            server.server_close()


# --------------------------------------------------------------------------- #
# The threaded HTTP stub                                                       #
# --------------------------------------------------------------------------- #
def test_serve_threaded_binds_exactly_once(recorded_binds: list[tuple[Any, ...]]) -> None:
    """One loopback bind, and it asks for an ephemeral port.

    The idiom this replaces bound twice -- ``("127.0.0.1", 0)`` to sample a port,
    then ``("127.0.0.1", <that port>)`` to serve on it -- with the race living
    between them. A second bind of any kind, and a bind naming a specific port in
    particular, is the signature of that window reopening -- on any host, not only
    on loopback.
    """
    with serve_threaded(lambda _base: _EchoHandler):
        pass
    _assert_single_ephemeral_bind(recorded_binds)


def test_serve_threaded_yields_a_loopback_base_url_that_answers() -> None:
    """The yielded URL has the shape every caller substitutes into probe targets,
    and a request to it reaches the handler."""
    with serve_threaded(lambda _base: _EchoHandler) as base:
        assert re.fullmatch(r"http://127\.0\.0\.1:\d+", base), base
        with urllib.request.urlopen(base + "/ping", timeout=5) as response:  # noqa: S310
            assert response.read() == b"/ping"


def test_handler_factory_receives_the_yielded_base_url() -> None:
    """The factory's argument is the URL the caller gets back.

    A stub that advertises its own identity (the ``{{BASE_URL}}`` seam) writes that
    URL into response bodies and headers; if the two ever diverged, a stub would
    point probes at a server other than itself.
    """
    seen: list[str] = []

    def _factory(base_url: str) -> type[http.server.BaseHTTPRequestHandler]:
        seen.append(base_url)
        return _EchoHandler

    with serve_threaded(_factory) as base:
        assert seen == [base]


# --------------------------------------------------------------------------- #
# The uvicorn stub                                                             #
# --------------------------------------------------------------------------- #
async def _hello_app(scope: Any, receive: Any, send: Any) -> None:
    """A minimal ASGI app: 200 ``ok`` on any HTTP request."""
    assert scope["type"] == "http"
    await send(
        {
            "type": "http.response.start",
            "status": 200,
            "headers": [(b"content-type", b"text/plain")],
        }
    )
    await send({"type": "http.response.body", "body": b"ok"})


def test_serve_uvicorn_serves_on_the_reserved_socket(
    recorded_binds: list[tuple[Any, ...]],
) -> None:
    """uvicorn is handed the reservation and binds nothing of its own.

    Passing the socket through is what makes this windowless; a helper that let
    uvicorn bind its own port would still answer this request, so the bind count
    -- not the response -- is the assertion that discriminates.
    """
    pytest.importorskip("uvicorn")
    with serve_uvicorn(_hello_app, startup_timeout=20.0) as base:
        assert re.fullmatch(r"http://127\.0\.0\.1:\d+", base), base
        with urllib.request.urlopen(base + "/", timeout=10) as response:  # noqa: S310
            assert response.read() == b"ok"
    _assert_single_ephemeral_bind(recorded_binds)


# --------------------------------------------------------------------------- #
# The shared-helper gate                                                       #
# --------------------------------------------------------------------------- #
def test_no_test_module_picks_its_own_port() -> None:
    """No test module anywhere reads a port back out of a socket it bound itself.

    ``getsockname`` is the name-independent tell: a module that renames its port
    picker still has to read the number off a socket. This module is exempt because
    it is the one place the reservation primitive is exercised directly.

    The walk spans the whole suite rather than ``tests/deploy/`` alone -- the stub
    servers live there today, but a scan scoped to where the problem happened to
    surface would not see the next one appear next door.
    """
    modules = sorted(_TESTS_ROOT.glob("**/test_*.py"))
    assert len(modules) > len(list(_DEPLOY_TESTS.glob("test_*.py"))), (
        "the walk must reach beyond tests/deploy/ -- a glob matching nothing passes vacuously"
    )
    offenders = [
        str(path.relative_to(_TESTS_ROOT))
        for path in modules
        if path.name != Path(__file__).name and "getsockname(" in path.read_text(encoding="utf-8")
    ]
    assert not offenders, f"these modules pick their own port instead of reserving one: {offenders}"


@pytest.mark.parametrize("module", _STUB_MODULES)
def test_stub_modules_import_the_shared_helper(module: str) -> None:
    """Removal guard on the import, and no more than that.

    Asserting the import line is present does not establish that a module stands
    its servers up through the helper -- it could import and still hand-roll one.
    The teeth are in ``test_no_test_module_picks_its_own_port``; this guards
    against the import quietly disappearing.
    """
    source = (_DEPLOY_TESTS / module).read_text(encoding="utf-8")
    assert "from tests.deploy._stub_server import" in source, module
