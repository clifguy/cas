"""T-0063: drop uvicorn.access records for /mcp/messages/.

Verifies that the ``_DropMcpMessagesAccessLogs`` filter (a) drops
records whose request path starts with ``/mcp/messages/``, (b) keeps
records for any other path, and (c) is defensive against unexpected
``record.args`` shapes (returns True so unrelated logs are never
accidentally dropped). Also confirms the filter is wired into
``UVICORN_LOG_CONFIG`` for the ``uvicorn.access`` logger.
"""

from __future__ import annotations

import logging

from sage.__main__ import UVICORN_LOG_CONFIG, _DropMcpMessagesAccessLogs


def _access_record(args: tuple | None) -> logging.LogRecord:
    return logging.LogRecord(
        name="uvicorn.access",
        level=logging.INFO,
        pathname="",
        lineno=0,
        msg='%s - "%s %s HTTP/%s" %d',
        args=args,
        exc_info=None,
    )


def test_filter_drops_mcp_messages_path():
    f = _DropMcpMessagesAccessLogs()
    rec = _access_record(("127.0.0.1:49260", "POST", "/mcp/messages/?session_id=abc", "1.1", 202))
    assert f.filter(rec) is False


def test_filter_keeps_other_paths():
    f = _DropMcpMessagesAccessLogs()
    for path in ("/docs", "/mcp/sse/", "/api/something", "/"):
        rec = _access_record(("127.0.0.1:49260", "GET", path, "1.1", 200))
        assert f.filter(rec) is True, f"should keep path {path!r}"


def test_filter_keeps_records_with_unexpected_args():
    f = _DropMcpMessagesAccessLogs()

    assert f.filter(_access_record(None)) is True
    assert f.filter(_access_record(("a", "b"))) is True
    assert f.filter(_access_record(("a", "b", 123))) is True


def test_filter_wired_into_uvicorn_access_logger():
    assert "drop_mcp_messages" in UVICORN_LOG_CONFIG["filters"]
    access_cfg = UVICORN_LOG_CONFIG["loggers"]["uvicorn.access"]
    assert "drop_mcp_messages" in access_cfg["filters"]
