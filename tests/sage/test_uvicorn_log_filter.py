"""Access logging retains failures and bounds expected discovery diagnostics."""

import logging
import logging.config
from http import HTTPStatus

import pytest

from sage.__main__ import UVICORN_LOG_CONFIG, _DropMcpAccessLogs


def _access_record(path="/mcp", method="POST", status=200):
    return logging.LogRecord(
        "uvicorn.access",
        logging.INFO,
        "",
        0,
        '%s - "%s %s HTTP/%s" %d',
        ("127.0.0.1:1234", method, path, "1.1", status),
        None,
    )


class _StatusInt(int):
    """An integer status supplied by a transport other than HTTPStatus."""


@pytest.mark.parametrize(
    "status", [200, 202, 299, HTTPStatus.OK, HTTPStatus.ACCEPTED, _StatusInt(200)]
)
@pytest.mark.parametrize("path", ["/mcp", "/mcp_maint", "/mcp?x=1", "/mcp_maint?x=1"])
def test_routine_success_is_quiet(path, status):
    assert not _DropMcpAccessLogs().filter(_access_record(path, status=status))


@pytest.mark.parametrize(
    "status",
    [
        199,
        300,
        301,
        307,
        400,
        401,
        403,
        404,
        405,
        406,
        429,
        500,
        503,
        HTTPStatus.MOVED_PERMANENTLY,
        HTTPStatus.TEMPORARY_REDIRECT,
        HTTPStatus.BAD_REQUEST,
        HTTPStatus.UNAUTHORIZED,
        HTTPStatus.FORBIDDEN,
        HTTPStatus.NOT_FOUND,
        HTTPStatus.METHOD_NOT_ALLOWED,
        HTTPStatus.NOT_ACCEPTABLE,
        HTTPStatus.TOO_MANY_REQUESTS,
        HTTPStatus.INTERNAL_SERVER_ERROR,
        HTTPStatus.SERVICE_UNAVAILABLE,
    ],
)
def test_mcp_non_success_remains_visible(status):
    assert _DropMcpAccessLogs().filter(_access_record(status=status))


@pytest.mark.parametrize("method", ["GET", "DELETE", "HEAD", "OPTIONS", "PUT"])
@pytest.mark.parametrize("status", [200, HTTPStatus.OK])
def test_unexpected_method_remains_visible(method, status):
    assert _DropMcpAccessLogs().filter(_access_record(method=method, status=status))


@pytest.mark.parametrize(
    "path", ["/mcp_admin", "/mcp_admin?x=1", "/mcpfoo", "/mcp/anything", "/mcp_maint/x", "/docs"]
)
@pytest.mark.parametrize("status", [200, HTTPStatus.OK])
def test_other_paths_remain_visible(path, status):
    assert _DropMcpAccessLogs().filter(_access_record(path, status=status))


@pytest.mark.parametrize(
    "args",
    [
        None,
        {"status": 200},
        ("a", 123, "/mcp", "1.1", 200),
        ("a", "b"),
        ("a", "POST", "/mcp"),
        ("a", "POST", 123, "1.1", 200),
        ("a", "POST", "/mcp", "1.1", "200"),
    ],
)
def test_malformed_record_is_not_suppressed(args):
    rec = _access_record()
    rec.args = args
    assert _DropMcpAccessLogs().filter(rec)


@pytest.mark.parametrize("status", [None, "200", 200.0, True, False])
def test_malformed_status_remains_visible(status: object) -> None:
    assert _DropMcpAccessLogs().filter(_access_record(status=status))


# Independent examples include both root and supported mount placement forms.
_DISCOVERY = [
    "/.well-known/oauth-protected-resource",
    "/.well-known/oauth-protected-resource/mcp",
    "/mcp/.well-known/oauth-protected-resource",
    "/.well-known/oauth-authorization-server/mcp_maint",
    "/.well-known/openid-configuration/mcp_maint",
    "/mcp_maint/.well-known/openid-configuration",
]


@pytest.mark.parametrize("path", _DISCOVERY)
@pytest.mark.parametrize("status", [404, HTTPStatus.NOT_FOUND])
def test_expected_discovery_requires_auth_disabled(path, caplog, status):
    caplog.set_level(logging.DEBUG, logger="sage.discovery")
    assert _DropMcpAccessLogs(auth_enabled=True).filter(_access_record(path, "GET", status))
    assert not _DropMcpAccessLogs(auth_enabled=False).filter(_access_record(path, "GET", status))
    assert any(r.levelno == logging.DEBUG and path in r.getMessage() for r in caplog.records)


@pytest.mark.parametrize(
    "path,method,status",
    [
        ("/.well-known/oauth-protected-resource/mcp_admin", "GET", 404),
        ("/mcp_admin/.well-known/openid-configuration", "GET", 404),
        ("/.well-known/unknown", "GET", 404),
        ("/.well-known/oauth-protected-resource/mcpx", "GET", 404),
        ("/.well-known/oauth-protected-resource", "POST", 404),
        ("/.well-known/oauth-protected-resource", "GET", 500),
        ("/.well-known/oauth-protected-resource", "GET", 401),
    ],
)
@pytest.mark.parametrize("status_type", [int, HTTPStatus])
def test_discovery_negative_controls(path, method, status, status_type):
    status = status_type(status)
    assert _DropMcpAccessLogs(auth_enabled=False).filter(_access_record(path, method, status))


@pytest.mark.parametrize("status", [404, HTTPStatus.NOT_FOUND])
def test_discovery_counts_are_bounded_and_flushed(caplog, status):
    now = [0.0]
    caplog.set_level(logging.INFO, logger="sage.discovery")
    f = _DropMcpAccessLogs(auth_enabled=False, clock=lambda: now[0])
    for n in range(100):
        assert not f.filter(
            _access_record("/.well-known/oauth-protected-resource?nonce=" + str(n), "GET", status)
        )
    assert f._counts == {"/.well-known/oauth-protected-resource": 99}
    summaries = [r for r in caplog.records if r.levelno == logging.INFO]
    assert all("nonce" not in r.getMessage() for r in summaries)
    assert len(summaries) == 1
    assert "count=1" in summaries[0].getMessage()
    now[0] = 60.0
    f.filter(_access_record("/.well-known/oauth-protected-resource", "GET", status))
    summaries = [r for r in caplog.records if r.levelno == logging.INFO]
    assert len(summaries) == 2
    assert "count=100" in summaries[1].getMessage()
    f.filter(_access_record("/.well-known/oauth-protected-resource", "GET", status))
    f.flush_summary()
    assert "count=1" in caplog.records[-1].getMessage()
    f.flush_summary()
    assert len([r for r in caplog.records if r.levelno == logging.INFO]) == 3


def test_filter_wired_into_uvicorn_access_logger():
    assert "drop_mcp_access" in UVICORN_LOG_CONFIG["filters"]
    assert "drop_mcp_access" in UVICORN_LOG_CONFIG["loggers"]["uvicorn.access"]["filters"]


def test_real_logger_retains_failure_and_filters_success():
    import subprocess
    import sys

    result = subprocess.run(
        [
            sys.executable,
            "-c",
            """
import logging
import logging.config
from sage.__main__ import UVICORN_LOG_CONFIG
logging.config.dictConfig(UVICORN_LOG_CONFIG)
logger = logging.getLogger('uvicorn.access')
for status in (200, 503):
    logger.info('%s - "%s %s HTTP/%s" %d', '127.0.0.1:1234', 'POST', '/mcp', '1.1', status)
""",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    assert "503" in result.stdout
    assert "200" not in result.stdout


@pytest.mark.parametrize("auth_enabled", [False, True])
def test_main_uses_resolved_auth_posture_and_flushes_on_exit(monkeypatch, caplog, auth_enabled):
    from types import SimpleNamespace

    import sage.__main__ as entry

    caplog.set_level(logging.INFO, logger="sage.discovery")
    monkeypatch.setattr(
        entry,
        "create_app",
        lambda **kw: SimpleNamespace(state=SimpleNamespace(auth_enabled=auth_enabled)),
    )
    monkeypatch.setattr("sys.argv", ["sage"])

    def run(app, **kwargs):
        f = kwargs["log_config"]["filters"]["drop_mcp_access"]["()"]()
        record = _access_record("/.well-known/oauth-protected-resource", "GET", 404)
        assert f.filter(record) is auth_enabled
        assert f.filter(record) is auth_enabled
        raise RuntimeError("server stopped")

    monkeypatch.setattr(entry.uvicorn, "run", run)
    with pytest.raises(RuntimeError, match="server stopped"):
        entry.main()
    summaries = [r for r in caplog.records if "Expected discovery 404s" in r.getMessage()]
    assert len(summaries) == (0 if auth_enabled else 2)
