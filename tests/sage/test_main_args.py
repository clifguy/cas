"""Tests for sage/__main__.py argument parsing and vault root resolution.

Covers:
  - The parser accepts no positional args (vaults are discovered, not listed).
  - --migrate is removed from the parser (migration moved to sage.migrate).
  - Vault-root resolution: flag → SAGE_VAULT_ROOT env → ~/sage_vaults default.
  - UVICORN_LOG_CONFIG surfaces sage.* records at INFO without depending on
    upstream side effects (e.g., mcp's configure_logging RichHandler install).
"""

from __future__ import annotations

import logging
import logging.config
from pathlib import Path

import pytest

from sage.__main__ import UVICORN_LOG_CONFIG, _build_parser, _resolve_vault_root

# ---------------------------------------------------------------------------
# Surface 3a: parser shape
# ---------------------------------------------------------------------------


def test_parser_accepts_no_positional_args():
    """#14a: With discovery, the parser must not require positional config paths."""
    parser = _build_parser()

    # Should parse cleanly with no args. Today this raises SystemExit because
    # the parser still requires positional config_paths.
    args = parser.parse_args([])

    # Sanity: the parser should expose vault_root (either default-None or a Path).
    assert hasattr(args, "vault_root")


def test_parser_rejects_migrate_flag(capsys):
    """#14b: --migrate is removed; the parser should reject it as unrecognized."""
    parser = _build_parser()

    with pytest.raises(SystemExit):
        parser.parse_args(["--migrate"])

    err = capsys.readouterr().err
    # The error must specifically be "unrecognized arguments". An "argument
    # required" error (which today's parser emits for missing positionals)
    # is not the same thing and would let the test pass coincidentally.
    assert "unrecognized arguments" in err.lower()


def test_parser_accepts_vault_root_flag():
    """The new --vault-root flag is recognized."""
    parser = _build_parser()

    args = parser.parse_args(["--vault-root", "/tmp/vaults"])

    assert str(args.vault_root) == "/tmp/vaults"


# ---------------------------------------------------------------------------
# Surface 3b: vault root resolution
# ---------------------------------------------------------------------------


class _Args:
    """Lightweight stand-in for argparse.Namespace."""

    def __init__(self, vault_root=None):
        self.vault_root = vault_root


def test_resolve_default_when_no_flag_no_env():
    """#15: Default resolves to ~/sage_vaults when neither flag nor env is set."""
    result = _resolve_vault_root(_Args(vault_root=None), env={})

    assert result == Path.home() / "sage_vaults"


def test_resolve_uses_env_var(tmp_path):
    """#16: SAGE_VAULT_ROOT env var is honored when no flag given."""
    result = _resolve_vault_root(_Args(vault_root=None), env={"SAGE_VAULT_ROOT": str(tmp_path)})

    assert result == tmp_path


def test_resolve_flag_overrides_env(tmp_path):
    """#17: --vault-root flag wins over SAGE_VAULT_ROOT env var."""
    flag_path = tmp_path / "from_flag"
    env_path = tmp_path / "from_env"

    result = _resolve_vault_root(
        _Args(vault_root=flag_path), env={"SAGE_VAULT_ROOT": str(env_path)}
    )

    assert result == flag_path


# ---------------------------------------------------------------------------
# Surface 3c: UVICORN_LOG_CONFIG console-logging contract
# ---------------------------------------------------------------------------


@pytest.fixture
def _isolated_logging_state():
    """Snapshot and restore root + ``sage`` logger state across a test.

    ``logging.config.dictConfig`` mutates global logger state. Without
    isolation, this test would either depend on or corrupt the state
    other tests in the same process rely on (Python's logging module
    has no built-in transaction primitive).
    """
    root = logging.getLogger()
    saved_root_handlers = list(root.handlers)
    saved_root_level = root.level
    saved_root_filters = list(root.filters)

    sage_logger = logging.getLogger("sage")
    saved_sage_handlers = list(sage_logger.handlers)
    saved_sage_level = sage_logger.level
    saved_sage_propagate = sage_logger.propagate

    yield

    root.handlers = saved_root_handlers
    root.level = saved_root_level
    root.filters = saved_root_filters
    sage_logger.handlers = saved_sage_handlers
    sage_logger.level = saved_sage_level
    sage_logger.propagate = saved_sage_propagate


def test_uvicorn_log_config_surfaces_sage_info_records(_isolated_logging_state):
    """``sage.mcp_server`` is reachable at INFO with a console handler.

    The per-tool log line ``mcp tool: <name>`` emitted by
    ``_LoggingFastMCP.call_tool`` is the only success-path signal that
    surfaces the called tool's name in the SAGE console. Its visibility
    must NOT depend on a third-party side effect — specifically the
    RichHandler install in ``mcp.server.fastmcp.utilities.logging
    .configure_logging``, which is invoked from ``FastMCP.__init__`` but
    is outside SAGE's substrate boundary. This test asserts the
    visibility contract holds against ``UVICORN_LOG_CONFIG`` alone.

    Pre-test: clear the root logger to mimic a process that has not had
    upstream's ``configure_logging`` run. If the contract holds only
    when that upstream side effect has fired, the test catches the
    drift and the SAGE console silently drops every successful tool
    call's INFO line.
    """
    root = logging.getLogger()
    root.handlers = []
    root.setLevel(logging.WARNING)

    logging.config.dictConfig(UVICORN_LOG_CONFIG)

    target = logging.getLogger("sage.mcp_server")
    assert target.getEffectiveLevel() <= logging.INFO, (
        f"sage.mcp_server effective level is "
        f"{logging.getLevelName(target.getEffectiveLevel())}; INFO records "
        f"would be dropped before reaching any handler"
    )

    # Walk up the parent chain (stop at the first logger that doesn't
    # propagate) and assert at least one handler is reachable. Either a
    # handler directly on sage / sage.mcp_server or a handler on root
    # satisfies the contract; both make INFO records visible.
    current: logging.Logger | None = target
    found = False
    while current is not None:
        if current.handlers:
            found = True
            break
        if not current.propagate:
            break
        current = current.parent
    assert found, (
        "no handler is reachable from sage.mcp_server via the configured "
        "propagation chain; UVICORN_LOG_CONFIG must attach a handler to "
        "either the sage logger or the root"
    )
