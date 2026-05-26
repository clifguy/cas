"""Per-tool-call logging in the SAGE MCP server.

Verifies the three-way distinction `_LoggingFastMCP.call_tool` draws
between tool outcomes in the console log:

- success → one INFO line (`mcp tool: <name>`), no WARNING, no ERROR
  []
- envelope-error → one INFO line plus one WARNING line
  (`mcp tool error: <name> (<error_kind>)`), no ERROR; the result is
  returned to the caller unchanged []
- raised exception → one INFO line plus one ERROR line
  (`mcp tool failed: <name>`) with traceback, and the exception
  re-propagates []

The envelope-error test exercises the *production* return shape that
FastMCP's `_convert_to_content` produces: SAGE tool dicts are
JSON-serialized and wrapped in `[TextContent(text=<json>)]` before they
reach `_LoggingFastMCP.call_tool`. A separate test covers the defensive
raw-dict path so the helper's two branches are both pinned.
"""

from __future__ import annotations

import json
import logging

import pytest
from mcp.types import TextContent

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

    elevated_records = [
        rec
        for rec in caplog.records
        if rec.name == "sage.mcp_server" and rec.levelno >= logging.WARNING
    ]
    assert not elevated_records, "plain-success path must not emit WARNING or ERROR records"


async def test_call_tool_logs_warning_on_envelope_wrapped_in_text_content(caplog, monkeypatch):
    """production shape: SAGE dict envelopes are wrapped in [TextContent(text=<json>)].

    This mirrors what FastMCP's `_convert_to_content` produces from a SAGE
    tool's `{"error": "<code>", "message": "..."}` return. The first cut of
    only exercised the raw-dict mock and missed this shape — the
    smoke test against the running server caught the gap.
    """
    mcp = _LoggingFastMCP("test")

    envelope = {"error": "internal_error", "message": "validation failed: bad id"}
    wrapped = [TextContent(type="text", text=json.dumps(envelope))]

    async def fake_super_call(self, name, arguments):
        return wrapped

    monkeypatch.setattr("mcp.server.fastmcp.FastMCP.call_tool", fake_super_call)

    with caplog.at_level(logging.INFO, logger="sage.mcp_server"):
        result = await mcp.call_tool("sage_get_document", {"document_id": "nope"})

    assert result is wrapped, "result must pass through unchanged"

    sage_records = [rec for rec in caplog.records if rec.name == "sage.mcp_server"]

    info_messages = [rec.getMessage() for rec in sage_records if rec.levelno == logging.INFO]
    assert "mcp tool: sage_get_document" in info_messages

    warning_records = [rec for rec in sage_records if rec.levelno == logging.WARNING]
    assert warning_records, "expected a WARNING log on TextContent-wrapped envelope"
    assert warning_records[0].getMessage() == "mcp tool error: sage_get_document (internal_error)"

    error_records = [rec for rec in sage_records if rec.levelno == logging.ERROR]
    assert not error_records


async def test_call_tool_logs_warning_on_raw_dict_envelope(caplog, monkeypatch):
    """defensive shape: a hypothetical FastMCP that returns the raw dict.

    Not the current production path — FastMCP wraps dicts in TextContent (see
    sibling test). Pinned anyway so the helper's dict branch can't silently
    rot. If a future SDK change starts returning dicts directly, this test
    documents the expected behavior at that layer.
    """
    mcp = _LoggingFastMCP("test")

    async def fake_super_call(self, name, arguments):
        return {"error": "internal_error", "message": "validation failed: bad id"}

    monkeypatch.setattr("mcp.server.fastmcp.FastMCP.call_tool", fake_super_call)

    with caplog.at_level(logging.INFO, logger="sage.mcp_server"):
        result = await mcp.call_tool("sage_get_document", {"document_id": "nope"})

    assert result == {"error": "internal_error", "message": "validation failed: bad id"}

    sage_records = [rec for rec in caplog.records if rec.name == "sage.mcp_server"]

    info_messages = [rec.getMessage() for rec in sage_records if rec.levelno == logging.INFO]
    assert "mcp tool: sage_get_document" in info_messages

    warning_records = [rec for rec in sage_records if rec.levelno == logging.WARNING]
    assert warning_records, "expected a WARNING log on raw-dict envelope"
    assert warning_records[0].getMessage() == "mcp tool error: sage_get_document (internal_error)"

    error_records = [rec for rec in sage_records if rec.levelno == logging.ERROR]
    assert not error_records


async def test_call_tool_no_warning_on_text_content_success(caplog, monkeypatch):
    """Plain success wrapped in TextContent must not trip the envelope check."""
    mcp = _LoggingFastMCP("test")

    success_payload = {"results": [{"id": "abc", "title": "T"}], "total_available": 1}
    wrapped = [TextContent(type="text", text=json.dumps(success_payload))]

    async def fake_super_call(self, name, arguments):
        return wrapped

    monkeypatch.setattr("mcp.server.fastmcp.FastMCP.call_tool", fake_super_call)

    with caplog.at_level(logging.INFO, logger="sage.mcp_server"):
        result = await mcp.call_tool("sage_discover", {"vault_id": "x"})

    assert result is wrapped

    elevated_records = [
        rec
        for rec in caplog.records
        if rec.name == "sage.mcp_server" and rec.levelno >= logging.WARNING
    ]
    assert not elevated_records, "TextContent-wrapped success must not emit WARNING or ERROR"


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
