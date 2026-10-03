"""Per-tool-call logging in the SAGE MCP server.

Verifies the three-way distinction `_LoggingFastMCP.call_tool` draws
between tool outcomes in the console log:

- success → one INFO line (`mcp tool: <name>`), no WARNING, no ERROR
- envelope-error → one INFO line plus one WARNING line
  (`mcp tool error: <name> (<error_kind>)`), no ERROR; the result is
  returned to the caller unchanged
- unexpected exception → one INFO line plus one ERROR line
  (`mcp tool failed: <name> (reference <id>)`) with traceback, and the
  caller receives a generic `internal_error` envelope carrying only the
  reference

The envelope-error test exercises the *production* return shape that
FastMCP's `_convert_to_content` produces: SAGE tool dicts are
JSON-serialized and wrapped in `[TextContent(text=<json>)]` before they
reach `_LoggingFastMCP.call_tool`. A separate test covers the defensive
raw-dict path so the helper's two branches are both pinned.
"""

from __future__ import annotations

import json
import logging
import re
import traceback

import pytest
from mcp.server.fastmcp.exceptions import ToolError
from mcp.types import TextContent
from pydantic import BaseModel, ValidationError

from sage.mcp_server import _LoggingFastMCP


async def test_call_tool_logs_name_on_success(caplog, monkeypatch):
    mcp = _LoggingFastMCP("test")

    async def fake_super_call(self, name, arguments):
        return {"echoed": name, "args": arguments}

    monkeypatch.setattr("mcp.server.fastmcp.FastMCP.call_tool", fake_super_call)

    with caplog.at_level(logging.INFO, logger="sage.mcp_server"):
        result = await mcp.call_tool("search", {"vault_id": "x"})

    assert result == {"echoed": "search", "args": {"vault_id": "x"}}

    info_records = [
        rec
        for rec in caplog.records
        if rec.name == "sage.mcp_server" and rec.levelno == logging.INFO
    ]
    assert any(rec.getMessage() == "mcp tool: search" for rec in info_records)

    elevated_records = [
        rec
        for rec in caplog.records
        if rec.name == "sage.mcp_server" and rec.levelno >= logging.WARNING
    ]
    assert not elevated_records, "plain-success path must not emit WARNING or ERROR records"


async def test_call_tool_logs_warning_on_envelope_wrapped_in_text_content(caplog, monkeypatch):
    """Production shape: SAGE dict envelopes are wrapped in [TextContent(text=<json>)].

    This mirrors what FastMCP's `_convert_to_content` produces from a SAGE
    tool's `{"error": "<code>", "message": "..."}` return. The first cut
    exercised only the raw-dict mock and missed this shape — the
    smoke test against the running server caught the gap.
    """
    mcp = _LoggingFastMCP("test")

    envelope = {"error": "internal_error", "message": "validation failed: bad id"}
    wrapped = [TextContent(type="text", text=json.dumps(envelope))]

    async def fake_super_call(self, name, arguments):
        return wrapped

    monkeypatch.setattr("mcp.server.fastmcp.FastMCP.call_tool", fake_super_call)

    with caplog.at_level(logging.INFO, logger="sage.mcp_server"):
        result = await mcp.call_tool("get_document", {"document_id": "nope"})

    assert result is wrapped, "result must pass through unchanged"

    sage_records = [rec for rec in caplog.records if rec.name == "sage.mcp_server"]

    info_messages = [rec.getMessage() for rec in sage_records if rec.levelno == logging.INFO]
    assert "mcp tool: get_document" in info_messages

    warning_records = [rec for rec in sage_records if rec.levelno == logging.WARNING]
    assert warning_records, "expected a WARNING log on TextContent-wrapped envelope"
    assert warning_records[0].getMessage() == "mcp tool error: get_document (internal_error)"

    error_records = [rec for rec in sage_records if rec.levelno == logging.ERROR]
    assert not error_records


async def test_call_tool_logs_warning_on_raw_dict_envelope(caplog, monkeypatch):
    """Defensive shape: a hypothetical FastMCP that returns the raw dict.

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
        result = await mcp.call_tool("get_document", {"document_id": "nope"})

    assert result == {"error": "internal_error", "message": "validation failed: bad id"}

    sage_records = [rec for rec in caplog.records if rec.name == "sage.mcp_server"]

    info_messages = [rec.getMessage() for rec in sage_records if rec.levelno == logging.INFO]
    assert "mcp tool: get_document" in info_messages

    warning_records = [rec for rec in sage_records if rec.levelno == logging.WARNING]
    assert warning_records, "expected a WARNING log on raw-dict envelope"
    assert warning_records[0].getMessage() == "mcp tool error: get_document (internal_error)"

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
        result = await mcp.call_tool("search", {"vault_id": "x"})

    assert result is wrapped

    elevated_records = [
        rec
        for rec in caplog.records
        if rec.name == "sage.mcp_server" and rec.levelno >= logging.WARNING
    ]
    assert not elevated_records, "TextContent-wrapped success must not emit WARNING or ERROR"


def _wire_envelope(result) -> dict:
    """Decode a SAGE error envelope from the production wire shape."""
    assert isinstance(result, list) and len(result) == 1, result
    assert isinstance(result[0], TextContent), result
    return json.loads(result[0].text)


def _assert_generic_internal_error(result, sentinel: str) -> str:
    envelope = _wire_envelope(result)
    assert envelope["error"] == "internal_error", envelope
    assert sentinel not in json.dumps(envelope), envelope
    match = re.fullmatch(r"Internal error \(reference ([0-9a-f]{32})\)\.", envelope["message"])
    assert match, envelope
    assert set(envelope) == {"error", "message"}, envelope
    return match.group(1)


def _failure_record(caplog, tool: str) -> logging.LogRecord:
    sage_records = [rec for rec in caplog.records if rec.name == "sage.mcp_server"]
    info_messages = [rec.getMessage() for rec in sage_records if rec.levelno == logging.INFO]
    assert f"mcp tool: {tool}" in info_messages
    error_records = [rec for rec in sage_records if rec.levelno == logging.ERROR]
    assert len(error_records) == 1, [rec.getMessage() for rec in error_records]
    return error_records[0]


async def test_call_tool_logs_failure_and_returns_generic_envelope(caplog, monkeypatch):
    """An exception escaping the dispatch reaches the caller as a generic envelope.

    The exception's text stays in the server log, under the same reference the
    caller receives, so an operator can find it; the caller sees neither the
    message nor the exception type.
    """
    mcp = _LoggingFastMCP("test")

    class Boom(RuntimeError):
        pass

    async def fake_super_call(self, name, arguments):
        raise Boom("kaboom-sentinel")

    monkeypatch.setattr("mcp.server.fastmcp.FastMCP.call_tool", fake_super_call)

    with caplog.at_level(logging.INFO, logger="sage.mcp_server"):
        result = await mcp.call_tool("get_document", {})

    reference = _assert_generic_internal_error(result, "kaboom-sentinel")
    assert "Boom" not in result[0].text

    record = _failure_record(caplog, "get_document")
    assert record.getMessage() == f"mcp tool failed: get_document (reference {reference})"
    assert record.exc_info is not None
    assert record.exc_info[0] is Boom


async def test_call_tool_wrapped_unexpected_exception_returns_generic_envelope(caplog, monkeypatch):
    """The production shape: FastMCP wraps a tool's exception in a ToolError.

    The ToolError's text embeds the original message, so the generic envelope
    must replace it rather than pass it through.
    """
    mcp = _LoggingFastMCP("test")

    async def fake_super_call(self, name, arguments):
        try:
            raise OSError("secret-path-sentinel")
        except OSError as exc:
            raise ToolError(f"Error executing tool {name}: {exc}") from exc

    monkeypatch.setattr("mcp.server.fastmcp.FastMCP.call_tool", fake_super_call)

    with caplog.at_level(logging.INFO, logger="sage.mcp_server"):
        result = await mcp.call_tool("get_document", {})

    reference = _assert_generic_internal_error(result, "secret-path-sentinel")
    record = _failure_record(caplog, "get_document")
    assert reference in record.getMessage()
    assert record.exc_info is not None
    assert "secret-path-sentinel" in "".join(traceback.format_exception(*record.exc_info))


async def test_call_tool_argument_validation_tool_error_is_not_genericized(monkeypatch):
    """A ToolError caused by argument validation keeps its caller-actionable text."""
    mcp = _LoggingFastMCP("test")

    class _Args(BaseModel):
        limit: int

    async def fake_super_call(self, name, arguments):
        try:
            _Args.model_validate({"limit": "not-a-number"})
        except ValidationError as exc:
            raise ToolError(f"Error executing tool {name}: {exc}") from exc

    monkeypatch.setattr("mcp.server.fastmcp.FastMCP.call_tool", fake_super_call)

    envelope = _wire_envelope(await mcp.call_tool("get_document", {"limit": "x"}))
    assert envelope["error"] == "invalid_parameter", envelope
    assert "limit" in json.dumps(envelope), envelope


async def test_call_tool_unknown_tool_error_is_not_genericized(monkeypatch):
    """A ToolError with no underlying exception is the SDK's own refusal."""
    mcp = _LoggingFastMCP("test")

    async def fake_super_call(self, name, arguments):
        raise ToolError(f"Unknown tool: {name}")

    monkeypatch.setattr("mcp.server.fastmcp.FastMCP.call_tool", fake_super_call)

    with pytest.raises(ToolError, match="Unknown tool: nope"):
        await mcp.call_tool("nope", {})
