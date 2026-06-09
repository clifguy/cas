"""End-to-end dump/restore proof for the Postgres backup runner.

The pure unit tests assert the *commands* are built correctly; only an actual
``pg_dump`` -> drop -> ``pg_restore`` cycle proves the produced archive is
genuinely restorable. This test runs that cycle against a uniquely-named
throwaway database so it never touches working data.

Gating: it skips unless a throwaway Postgres is configured
(``SAGE_TEST_PG_DSN``), ``psycopg`` is importable, the ``pg_dump`` / ``pg_restore``
client binaries are on PATH, and the client major version is at least the
server's (``pg_dump`` refuses to dump a newer server). Locally the Homebrew
``postgresql@17`` client satisfies this; an older CI client self-skips.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import uuid

import pytest

from scripts.backup_postgres import build_dump_argv

psycopg = pytest.importorskip("psycopg")

from psycopg.conninfo import conninfo_to_dict, make_conninfo  # noqa: E402


def _client_major(binary: str) -> int:
    """Major version of a Postgres client binary, e.g. ``pg_dump`` -> 17."""
    out = subprocess.run([binary, "--version"], capture_output=True, text=True, check=True)
    # e.g. "pg_dump (PostgreSQL) 17.10 (Homebrew)" -> 17. Anchor on the
    # "(PostgreSQL) " label so the trailing "(Homebrew)" build tag is ignored.
    match = re.search(r"\(PostgreSQL\)\s+(\d+)", out.stdout)
    if match is None:
        raise ValueError(f"could not parse version from {out.stdout!r}")
    return int(match.group(1))


def _server_major(dsn: str) -> int:
    """Major version of the server behind ``dsn`` (170010 -> 17)."""
    with psycopg.connect(dsn) as conn:
        row = conn.execute("SHOW server_version_num").fetchone()
    return int(row[0]) // 10000


def _conninfo_for_db(dsn: str, dbname: str) -> str:
    """Rewrite ``dsn`` to target a different database, preserving credentials."""
    parsed = conninfo_to_dict(dsn)
    parsed["dbname"] = dbname
    return make_conninfo(**parsed)


def _roundtrip_dsn() -> str:
    """Return the test DSN, or skip when the round-trip cannot run here."""
    dsn = os.environ.get("SAGE_TEST_PG_DSN")
    if not dsn:
        pytest.skip("set SAGE_TEST_PG_DSN to a throwaway Postgres to run the round-trip")
    for binary in ("pg_dump", "pg_restore"):
        if shutil.which(binary) is None:
            pytest.skip(f"{binary} not on PATH")
    if _client_major("pg_dump") < _server_major(dsn):
        pytest.skip("pg_dump client older than server -- cannot dump a newer server")
    return dsn


def test_dump_restore_roundtrip_recovers_row(tmp_path) -> None:
    """A sentinel row survives a full dump -> drop -> restore cycle.

    The mid-test 'absent after drop' assertion is the anti-coincidental guard: it
    proves the final 'present' assertion is the restore's doing, not residual
    state. If ``build_dump_argv`` emitted a non-restorable format, the restore
    would not bring the row back and the test would fail.
    """
    dsn = _roundtrip_dsn()
    throwaway = f"sage_rt_{uuid.uuid4().hex[:8]}"
    dump_path = tmp_path / "roundtrip.dump"

    # CREATE/DROP DATABASE must run outside a transaction -> autocommit.
    admin = psycopg.connect(dsn, autocommit=True)
    try:
        admin.execute(f'CREATE DATABASE "{throwaway}"')
        target = _conninfo_for_db(dsn, throwaway)

        with psycopg.connect(target, autocommit=True) as conn:
            conn.execute("CREATE TABLE rt_probe (id int PRIMARY KEY, marker text)")
            conn.execute("INSERT INTO rt_probe VALUES (1, 'sentinel')")

        # Dump using the runner's own argv builder.
        subprocess.run(build_dump_argv(target, dump_path), check=True, capture_output=True)
        assert dump_path.stat().st_size > 0

        # Drop the table, then confirm it is genuinely gone before restoring.
        with psycopg.connect(target, autocommit=True) as conn:
            conn.execute("DROP TABLE rt_probe")
            assert conn.execute("SELECT to_regclass('rt_probe')").fetchone()[0] is None

        # Restore the custom-format archive and confirm the sentinel returns.
        subprocess.run(
            ["pg_restore", "-d", target, str(dump_path)], check=True, capture_output=True
        )
        with psycopg.connect(target, autocommit=True) as conn:
            marker = conn.execute("SELECT marker FROM rt_probe WHERE id = 1").fetchone()[0]
        assert marker == "sentinel"
    finally:
        admin.execute(f'DROP DATABASE IF EXISTS "{throwaway}" WITH (FORCE)')
        admin.close()
