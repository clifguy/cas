"""Tests for sage/__main__.py argument parsing and vault root resolution.

Covers:
  - The parser accepts no positional args (vaults are discovered, not listed).
  - --migrate is removed from the parser (migration moved to sage.migrate).
  - Vault-root resolution: flag → SAGE_VAULT_ROOT env → ~/sage_vaults default.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from sage.__main__ import _build_parser, _resolve_vault_root


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
    result = _resolve_vault_root(
        _Args(vault_root=None), env={"SAGE_VAULT_ROOT": str(tmp_path)}
    )

    assert result == tmp_path


def test_resolve_flag_overrides_env(tmp_path):
    """#17: --vault-root flag wins over SAGE_VAULT_ROOT env var."""
    flag_path = tmp_path / "from_flag"
    env_path = tmp_path / "from_env"

    result = _resolve_vault_root(
        _Args(vault_root=flag_path), env={"SAGE_VAULT_ROOT": str(env_path)}
    )

    assert result == flag_path
