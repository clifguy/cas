"""Drop uvicorn.access records for every MCP mount's JSON-RPC endpoint.

Over the Streamable HTTP transport every JSON-RPC message is a ``POST`` to
the mount path itself, so each logical tool call produces access-log lines
that carry no signal beyond what ``_LoggingFastMCP.call_tool`` already logs
(the tool name lives in the request body, not the URL). Verifies that the
``_DropMcpAccessLogs`` filter (a) drops records whose request path is exactly
a mount path (with or without a query string), (b) keeps records for every
other path — including paths that merely share the mount as a string prefix,
so an unrelated ``/mcpfoo`` route or a stray ``/mcp/anything`` 404 stays
visible, and (c) is defensive against unexpected ``record.args`` shapes
(returns True so unrelated logs are never accidentally dropped). Also
confirms the suppressed paths track the canonical mount list and that the
filter is wired into ``UVICORN_LOG_CONFIG`` for the ``uvicorn.access``
logger.
"""

from __future__ import annotations

import logging

from sage.__main__ import (
    _MCP_MOUNT_PATHS,
    UVICORN_LOG_CONFIG,
    _DropMcpAccessLogs,
)
from sage.app import MCP_HTTP_MOUNTS


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


def test_filter_drops_exact_mount_path():
    f = _DropMcpAccessLogs()
    rec = _access_record(("127.0.0.1:49260", "POST", "/mcp", "1.1", 200))
    assert f.filter(rec) is False


def test_filter_drops_mount_path_with_query():
    f = _DropMcpAccessLogs()
    rec = _access_record(("127.0.0.1:49260", "POST", "/mcp?session_id=abc", "1.1", 200))
    assert f.filter(rec) is False


def test_filter_drops_maintenance_mount_paths():
    f = _DropMcpAccessLogs()
    for path in ("/mcp_maint", "/mcp_admin"):
        rec = _access_record(("127.0.0.1:54722", "POST", path, "1.1", 200))
        assert f.filter(rec) is False, f"should drop {path}"


def test_filter_keeps_other_paths():
    f = _DropMcpAccessLogs()
    for path in (
        "/docs",
        "/mcpfoo",
        "/mcp/anything",
        "/mcp_maint/x",
        "/mcp_admin/x",
        "/api/something",
        "/",
    ):
        rec = _access_record(("127.0.0.1:49260", "GET", path, "1.1", 200))
        assert f.filter(rec) is True, f"should keep path {path!r}"


def test_filter_keeps_records_with_unexpected_args():
    f = _DropMcpAccessLogs()

    assert f.filter(_access_record(None)) is True
    assert f.filter(_access_record(("a", "b"))) is True
    assert f.filter(_access_record(("a", "b", 123))) is True


def test_filter_paths_track_mount_list():
    # The suppressed paths are derived from the canonical mount list, not
    # hardcoded, so adding a mount auto-covers its JSON-RPC endpoint.
    assert _MCP_MOUNT_PATHS == tuple(path for path, _ in MCP_HTTP_MOUNTS)

    f = _DropMcpAccessLogs()
    for path, _surface in MCP_HTTP_MOUNTS:
        for logged in (path, f"{path}?x=1"):
            rec = _access_record(("127.0.0.1:49260", "POST", logged, "1.1", 200))
            assert f.filter(rec) is False, f"should drop {logged}"


def test_filter_wired_into_uvicorn_access_logger():
    assert "drop_mcp_access" in UVICORN_LOG_CONFIG["filters"]
    access_cfg = UVICORN_LOG_CONFIG["loggers"]["uvicorn.access"]
    assert "drop_mcp_access" in access_cfg["filters"]
