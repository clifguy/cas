"""Tests for log filters in sage.app.

Covers _CancelledNotificationValidationFilter: suppresses the cosmetic
``Failed to validate notification`` WARNING emitted by mcp.shared.session
on client-cancelled long tool calls.
"""

import logging

from sage.app import _CancelledNotificationValidationFilter

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
        "cancelled-notification filter not installed on root logger after importing sage.app"
    )
