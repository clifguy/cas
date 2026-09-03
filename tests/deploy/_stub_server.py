"""Loopback stub servers whose selecting socket is their serving socket.

A stub server needs an ephemeral port, and the obvious way to get one -- bind a
throwaway socket to port 0, read the number back, close it, then bind the real
server to that number -- leaves the port free between the close and the rebind.
Under a parallel test run a sibling worker's identical dance can be handed the
same number and win the second bind, so a probe reaches the wrong worker's stub
and fails against a response it was never meant to see.

The helpers here close that window by construction: ``reserved_loopback_socket``
binds *and holds* the socket, and both server shapes serve on that same socket
object rather than re-creating one. The port is therefore never observable in a
free state, which makes the guarantee checkable by reading the code rather than
by hoping a parallel run stays green.
"""

from __future__ import annotations

import http.server
import socket
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover -- typing only
    from collections.abc import Awaitable, MutableMapping

    ASGIApp = Callable[
        [MutableMapping[str, Any], Callable[[], Awaitable[Any]], Callable[..., Awaitable[None]]],
        Awaitable[None],
    ]

#: How long a stub server's thread is given to wind down before the caller
#: stops waiting on it. Every server here is a daemon thread, so a straggler
#: cannot outlive the interpreter.
_JOIN_TIMEOUT = 5.0


@contextmanager
def reserved_loopback_socket() -> Iterator[socket.socket]:
    """Bind and listen on an ephemeral loopback port, holding it until exit.

    The yielded socket *is* the reservation: while it is open no other socket
    can bind that port, so a caller may read the port number and act on it
    without racing anyone. Callers hand this socket to a server rather than
    closing it and rebinding -- that is the whole point.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind(("127.0.0.1", 0))
        sock.listen(128)
        yield sock
    finally:
        # A server that adopted the socket has usually closed it already;
        # closing twice is a no-op.
        sock.close()


class AdoptingThreadingHTTPServer(http.server.ThreadingHTTPServer):
    """A ``ThreadingHTTPServer`` that serves on a socket bound before it existed.

    ``socketserver.TCPServer.__init__`` always constructs its own socket, even
    with ``bind_and_activate=False``; overriding ``server_bind`` is what lets
    the pre-bound one take its place without leaving the constructor's socket
    behind as a leaked descriptor.
    """

    def __init__(
        self,
        sock: socket.socket,
        handler_cls: type[http.server.BaseHTTPRequestHandler],
    ) -> None:
        self._reserved = sock
        super().__init__(sock.getsockname()[:2], handler_cls)

    def server_bind(self) -> None:
        """Adopt the reserved socket instead of binding a fresh one."""
        self.socket.close()
        self.socket = self._reserved
        host, port = self.socket.getsockname()[:2]
        self.server_address = (host, port)
        self.server_name = socket.getfqdn(host)
        self.server_port = port


@contextmanager
def serve_threaded(
    handler_factory: Callable[[str], type[http.server.BaseHTTPRequestHandler]],
) -> Iterator[str]:
    """Run a threaded stub HTTP server; yield its ``http://127.0.0.1:<port>``.

    ``handler_factory`` receives that base URL and returns the handler class to
    serve with, so a stub that must advertise its own identity in a response
    body or header can close over the URL at class-definition time. A handler
    with no such need ignores the argument.
    """
    with reserved_loopback_socket() as sock:
        base_url = f"http://127.0.0.1:{sock.getsockname()[1]}"
        server = AdoptingThreadingHTTPServer(sock, handler_factory(base_url))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield base_url
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=_JOIN_TIMEOUT)


@contextmanager
def serve_uvicorn(app: ASGIApp, *, startup_timeout: float = 30.0) -> Iterator[str]:
    """Serve an ASGI app with uvicorn-in-thread; yield its ``http://127.0.0.1:<port>``.

    ``uvicorn.Server.run`` accepts already-open sockets and binds nothing of its
    own when given them, so the reservation -- bound and listening -- carries
    straight through to the serving socket.

    Waiting on ``started`` is what keeps a caller from racing startup: a startup
    that never completes surfaces as a named timeout rather than as a hung
    request, and a thread that dies before reporting started (a lifespan failure,
    an import error in the app) is reported the moment it does rather than after
    the full timeout.
    """
    import uvicorn

    with reserved_loopback_socket() as sock:
        port = sock.getsockname()[1]
        config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
        server = uvicorn.Server(config)
        thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
        thread.start()
        deadline = time.monotonic() + startup_timeout
        while not getattr(server, "started", False):
            if not thread.is_alive():
                raise RuntimeError("uvicorn thread exited before reporting started")
            if time.monotonic() > deadline:
                raise RuntimeError(f"uvicorn did not start within {startup_timeout}s")
            time.sleep(0.05)
        try:
            yield f"http://127.0.0.1:{port}"
        finally:
            server.should_exit = True
            thread.join(timeout=_JOIN_TIMEOUT)
