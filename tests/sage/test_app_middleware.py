"""Tests for ASGI middleware and log filters in sage.app.

Covers:

- _GracefulSSEMiddleware (T-0019): suppresses two known-benign tracebacks
  from the SSE transport: uvicorn-shutdown double-response RuntimeError and
  client-cancellation anyio.ClosedResourceError.
- _CancelledNotificationValidationFilter (T-0022): suppresses the cosmetic
  ``Failed to validate notification`` WARNING emitted by mcp.shared.session
  on client-cancelled long tool calls.
"""

import logging

import pytest
from anyio import ClosedResourceError

from sage.app import (
    _CancelledNotificationValidationFilter,
    _extract_session_id,
    _GracefulSSEMiddleware,
)


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


# ---------------------------------------------------------------------------
# T-0022: _CancelledNotificationValidationFilter
# ---------------------------------------------------------------------------


_CANCELLED_MCPERR_MESSAGE = (
    "Failed to validate notification: . Message was: "
    "method='notifications/cancelled' params={'requestId': 99, "
    "'reason': 'McpError: MCP error -32001: Request timed out'} "
    "jsonrpc='2.0'"
)


def _make_record(msg: str, level: int = logging.WARNING) -> logging.LogRecord:
    return logging.LogRecord(
        name="root",
        level=level,
        pathname=__file__,
        lineno=0,
        msg=msg,
        args=(),
        exc_info=None,
    )


def test_filter_swallows_cancelled_mcperr_warning():
    f = _CancelledNotificationValidationFilter()
    assert f.filter(_make_record(_CANCELLED_MCPERR_MESSAGE)) is False


def test_filter_passes_unrelated_validation_warning():
    f = _CancelledNotificationValidationFilter()
    other = (
        "Failed to validate notification: ValidationError(...). Message was: "
        "method='notifications/progress' params={'progressToken': 'x', 'progress': 0.5}"
    )
    assert f.filter(_make_record(other)) is True


def test_filter_passes_cancelled_without_mcperror_prefix():
    f = _CancelledNotificationValidationFilter()
    msg = (
        "Failed to validate notification: . Message was: "
        "method='notifications/cancelled' params={'requestId': 7, 'reason': 'user pressed Esc'}"
    )
    assert f.filter(_make_record(msg)) is True


def test_filter_passes_unrelated_warning():
    f = _CancelledNotificationValidationFilter()
    assert f.filter(_make_record("Some other warning entirely")) is True


def test_filter_passes_non_warning_records():
    f = _CancelledNotificationValidationFilter()
    # Even a record whose text matches the suppression pattern is passed
    # through at non-WARNING levels — the filter only acts on WARNING.
    assert f.filter(_make_record(_CANCELLED_MCPERR_MESSAGE, level=logging.ERROR)) is True
    assert f.filter(_make_record(_CANCELLED_MCPERR_MESSAGE, level=logging.INFO)) is True


def test_filter_installed_on_root_logger_at_import():
    """Module-load side effect: filter must be attached to root logger."""
    root = logging.getLogger()
    assert any(isinstance(f, _CancelledNotificationValidationFilter) for f in root.filters), (
        "T-0022 filter not installed on root logger after importing sage.app"
    )
