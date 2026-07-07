"""CLI contract for scripts/bootstrap_postgres.py.

The provisioning script must name its target schema explicitly: storage tenancy
is one schema per vault (CAS-ADR-042) and the shared ``public`` schema carries
extension objects only, so a defaulted target would invite provisioning SAGE
tables into a schema that sits behind every per-vault search_path -- where they
would mask a dropped vault schema instead of letting its queries fail loud.
"""

from __future__ import annotations

import pytest

from scripts.bootstrap_postgres import _parse_args


def test_schema_argument_is_required(capsys):
    """Omitting --schema is a usage error (argparse exit code 2), not a silent
    default into the shared schema.

    Anti-coincidental: a restored ``default="public"`` would make this parse
    succeed, so the SystemExit assertion fails exactly that regression.
    """
    with pytest.raises(SystemExit) as excinfo:
        _parse_args(["--dsn", "postgresql://localhost/throwaway"])
    assert excinfo.value.code == 2
    assert "--schema" in capsys.readouterr().err


def test_explicit_schema_parses():
    """A named schema parses through unchanged (the refusal of 'public' itself
    lives at the DDL assembly point, ``schema_statements``, so every caller --
    not just this script -- inherits it)."""
    args = _parse_args(["--schema", "sage_test_target"])
    assert args.schema == "sage_test_target"
