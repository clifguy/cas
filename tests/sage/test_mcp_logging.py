"""T-0061: per-tool-call logging in the SAGE MCP server.

Verifies that `_LoggingFastMCP.call_tool` emits one INFO line per
dispatched tool call (success path) and one ERROR line with exception
info plus a re-raise (failure path), while otherwise delegating
unchanged to FastMCP.
"""

from __future__ import annotations

import logging

import pytest

from sage.mcp_server import _LoggingFastMCP


async def test_call_tool_logs_name_on_success(caplog, monkeypatch):
    mcp = _LoggingFastMCP("test")

    async def fake_super_call(self, name, arguments):
        return {"echoed": name, "args": arguments}

    monkeypatch.setattr("mcp.server.fastmcp.FastMCP.call_tool", fake_super_call)

    with caplog.at_level(logging.INFO, logger="sage.mcp_server"):
        result = await mcp.call_tool("sage_discover", {"vault_id": "x"})

    assert result == {"echoed": "sage_discover", "args": {"vault_id": "x"}}

    info_records = [
        rec
        for rec in caplog.records
        if rec.name == "sage.mcp_server" and rec.levelno == logging.INFO
    ]
    assert any(rec.getMessage() == "mcp tool: sage_discover" for rec in info_records)


async def test_call_tool_logs_failure_and_reraises(caplog, monkeypatch):
    mcp = _LoggingFastMCP("test")

    class Boom(RuntimeError):
        pass

    async def fake_super_call(self, name, arguments):
        raise Boom("kaboom")

    monkeypatch.setattr("mcp.server.fastmcp.FastMCP.call_tool", fake_super_call)

    with caplog.at_level(logging.INFO, logger="sage.mcp_server"):
        with pytest.raises(Boom, match="kaboom"):
            await mcp.call_tool("sage_get_document", {})

    sage_records = [rec for rec in caplog.records if rec.name == "sage.mcp_server"]
    info_messages = [rec.getMessage() for rec in sage_records if rec.levelno == logging.INFO]
    assert "mcp tool: sage_get_document" in info_messages

    error_records = [rec for rec in sage_records if rec.levelno == logging.ERROR]
    assert error_records, "expected an ERROR log on tool failure"
    assert error_records[0].getMessage() == "mcp tool failed: sage_get_document"
    assert error_records[0].exc_info is not None
    assert error_records[0].exc_info[0] is Boom
