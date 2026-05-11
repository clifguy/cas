"""Tests for ASGI middleware in sage.app.

Covers _GracefulSSEMiddleware, which suppresses two known-benign tracebacks
from the SSE transport: uvicorn-shutdown double-response RuntimeError and
client-cancellation anyio.ClosedResourceError.
"""

import logging

import pytest
from anyio import ClosedResourceError

from sage.app import _extract_session_id, _GracefulSSEMiddleware


def _http_scope(query_string: bytes = b"") -> dict:
    return {
        "type": "http",
        "method": "POST",
        "path": "/messages/",
        "query_string": query_string,
        "headers": [],
    }


async def _noop_receive() -> dict:
    return {"type": "http.disconnect"}


async def _noop_send(_message: dict) -> None:
    return None


def _make_app(exc: BaseException | None):
    """Build a tiny ASGI callable that either returns cleanly or raises ``exc``."""

    async def _app(scope, receive, send):
        if exc is not None:
            raise exc

    return _app


async def test_closed_resource_error_is_suppressed(caplog):
    middleware = _GracefulSSEMiddleware(_make_app(ClosedResourceError()))
    caplog.set_level(logging.DEBUG, logger="sage.app")

    await middleware(_http_scope(b"session_id=abc-123"), _noop_receive, _noop_send)

    info_records = [r for r in caplog.records if r.levelno == logging.INFO]
    assert len(info_records) == 1
    assert "SSE writer closed by client cancellation" in info_records[0].getMessage()
    assert "abc-123" in info_records[0].getMessage()


async def test_closed_resource_error_without_session_id(caplog):
    middleware = _GracefulSSEMiddleware(_make_app(ClosedResourceError()))
    caplog.set_level(logging.DEBUG, logger="sage.app")

    await middleware(_http_scope(b""), _noop_receive, _noop_send)

    info_records = [r for r in caplog.records if r.levelno == logging.INFO]
    assert len(info_records) == 1
    msg = info_records[0].getMessage()
    assert "SSE writer closed by client cancellation" in msg
    assert "session_id" not in msg


async def test_shutdown_runtime_error_still_suppressed(caplog):
    exc = RuntimeError("Unexpected ASGI message 'http.response.start' sent ...")
    middleware = _GracefulSSEMiddleware(_make_app(exc))
    caplog.set_level(logging.DEBUG, logger="sage.app")

    await middleware(_http_scope(), _noop_receive, _noop_send)

    debug_records = [
        r
        for r in caplog.records
        if r.levelno == logging.DEBUG and "Suppressed SSE shutdown RuntimeError" in r.getMessage()
    ]
    assert len(debug_records) == 1


async def test_unrelated_runtime_error_propagates():
    middleware = _GracefulSSEMiddleware(_make_app(RuntimeError("something unrelated")))
    with pytest.raises(RuntimeError, match="something unrelated"):
        await middleware(_http_scope(), _noop_receive, _noop_send)


async def test_unrelated_exception_propagates():
    middleware = _GracefulSSEMiddleware(_make_app(ValueError("boom")))
    with pytest.raises(ValueError, match="boom"):
        await middleware(_http_scope(), _noop_receive, _noop_send)


async def test_non_http_scope_passes_through():
    """Lifespan and websocket scopes should bypass the middleware entirely."""
    called = {"n": 0}

    async def _inner(scope, receive, send):
        called["n"] += 1

    middleware = _GracefulSSEMiddleware(_inner)
    await middleware({"type": "lifespan"}, _noop_receive, _noop_send)
    assert called["n"] == 1


def test_extract_session_id_present():
    scope = _http_scope(b"session_id=xyz-456&other=ignored")
    assert _extract_session_id(scope) == "xyz-456"


def test_extract_session_id_absent():
    assert _extract_session_id(_http_scope(b"")) is None
    assert _extract_session_id(_http_scope(b"other=ignored")) is None


def test_extract_session_id_handles_malformed_query_string():
    scope = _http_scope(b"\xff\xfe")
    assert _extract_session_id(scope) is None
