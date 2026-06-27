"""Unit tests for the cloud Postgres role/extension bootstrap (CAS-ADR-042).

Exercises ``sage.storage.postgres.cloud_bootstrap`` -- the idempotent, in-VNet
bootstrap an operator (or a deploy job) runs once per environment as the server's
Entra administrator to enrol each workload's managed identity as a database role
and pre-create the extensions the storage adapters require. The cloud endpoint is
Entra-only and password-disabled, so the workloads authenticate as themselves and
self-bootstrap their own (owned) schemas at startup; this bootstrap supplies only
the admin-level prerequisites those self-bootstraps depend on.

These checks need no live server: the pure statement builders are asserted
directly, and the orchestration is driven against a recording fake connection so
the idempotency (re-run) behaviour and the least-privilege grant are provable
without Postgres. The driver and Entra imports are deferred in the module under
test, so importing it -- and these unit checks -- pull in neither psycopg nor
azure (the final test proves it in a clean subprocess).
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Final

import pytest

from sage.storage.postgres import cloud_bootstrap as cb

REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[2]

_SAGE_ROLE: Final[str] = "id-sage-prod"
_BFF_ROLE: Final[str] = "id-cas-bff-prod"


# ---------------------------------------------------------------------------
# Recording fake connection
#
# Mimics the slice of the psycopg async connection the bootstrap uses:
# ``await conn.execute(sql, params)`` returning a cursor with an async
# ``fetchone``. The pg_roles existence probe resolves against a configurable
# set of already-present roles, so both the create path and the converged
# re-run path are reachable without a server.
# ---------------------------------------------------------------------------


class _RecordingCursor:
    def __init__(self, row: tuple | None) -> None:
        self._row = row

    async def fetchone(self) -> tuple | None:
        return self._row


class _RecordingConn:
    def __init__(self, existing_roles: tuple[str, ...] = ()) -> None:
        self.existing = set(existing_roles)
        self.calls: list[tuple[str, tuple | None]] = []

    async def execute(self, sql: str, params: tuple | None = None) -> _RecordingCursor:
        self.calls.append((sql, params))
        if "pg_roles" in sql:
            role = params[0] if params else None
            return _RecordingCursor((1,) if role in self.existing else None)
        return _RecordingCursor(None)

    @property
    def sql(self) -> list[str]:
        return [s for s, _ in self.calls]


def _create_principal_roles(conn: _RecordingConn) -> list[str]:
    """The role names a ``pgaadauth_create_principal`` call was issued for."""
    return [
        params[0] for sql, params in conn.calls if "pgaadauth_create_principal" in sql and params
    ]


# ---------------------------------------------------------------------------
# Pure statement builders
# ---------------------------------------------------------------------------


def test_extension_statements_idempotent_and_required() -> None:
    """The default extension set creates pgvector and pgstattuple, idempotently,
    with vector first (it underpins the content store's embedding column).
    """
    statements = cb.extension_statements()
    assert 'CREATE EXTENSION IF NOT EXISTS "vector"' in statements
    assert 'CREATE EXTENSION IF NOT EXISTS "pgstattuple"' in statements
    assert all("IF NOT EXISTS" in s for s in statements), "extension creates must be idempotent"
    assert "vector" in statements[0], f"vector must be created first; got {statements}"
    # The default list is the canonical schema-module list, not an ad-hoc copy.
    assert cb.DEFAULT_EXTENSIONS == ("vector", "pgstattuple")


def test_grant_statement_is_least_privilege() -> None:
    """The grant is exactly CONNECT + CREATE on the database and nothing more --
    never the broad azure_pg_admin role or SUPERUSER.
    """
    grant = cb.grant_statement("sage", _SAGE_ROLE)
    assert grant == 'GRANT CONNECT, CREATE ON DATABASE "sage" TO "id-sage-prod"'
    lowered = grant.lower()
    assert "azure_pg_admin" not in lowered
    assert "superuser" not in lowered


def test_validate_role_name_accepts_mi_names() -> None:
    """Managed-identity role names validate; injection payloads and malformed
    names are rejected before any name reaches an interpolated GRANT.
    """
    assert cb.validate_role_name(_SAGE_ROLE) == _SAGE_ROLE
    assert cb.validate_role_name(_BFF_ROLE) == _BFF_ROLE
    for bad in ("bad name", 'r"; DROP ROLE x; --', "", "role;", "Role-Upper", "id_sage"):
        with pytest.raises(ValueError):
            cb.validate_role_name(bad)


# ---------------------------------------------------------------------------
# Orchestration against the recording fake
# ---------------------------------------------------------------------------


async def test_bootstrap_creates_absent_principals() -> None:
    """When neither role exists yet, the bootstrap enrols each managed identity as
    a database role and grants it, after pre-creating the extensions.
    """
    conn = _RecordingConn(existing_roles=())
    await cb.bootstrap_cloud_postgres(conn, database="sage", app_roles=[_SAGE_ROLE, _BFF_ROLE])

    created = _create_principal_roles(conn)
    assert created == [_SAGE_ROLE, _BFF_ROLE], f"both roles must be created; got {created}"
    assert any('CREATE EXTENSION IF NOT EXISTS "vector"' in s for s in conn.sql)
    assert any('CREATE EXTENSION IF NOT EXISTS "pgstattuple"' in s for s in conn.sql)


async def test_bootstrap_idempotent_when_principals_exist() -> None:
    """Re-running converges: when both roles already exist, no create-principal is
    issued (which would error on the live server), yet the grants and extension
    creates -- all IF-NOT-EXISTS / re-grantable -- still run, and nothing raises.
    """
    conn = _RecordingConn(existing_roles=(_SAGE_ROLE, _BFF_ROLE))
    await cb.bootstrap_cloud_postgres(conn, database="sage", app_roles=[_SAGE_ROLE, _BFF_ROLE])

    assert _create_principal_roles(conn) == [], "no principal must be re-created on a converged run"
    grants = [s for s in conn.sql if s.startswith("GRANT")]
    assert len(grants) == 2, f"both grants must still run on re-run; got {grants}"
    assert any('CREATE EXTENSION IF NOT EXISTS "vector"' in s for s in conn.sql)


async def test_bootstrap_grants_create_on_database_each_role() -> None:
    """Each role receives CONNECT + CREATE on the database -- the privilege its
    self-bootstrap needs to create and own its schema(s).
    """
    conn = _RecordingConn(existing_roles=())
    await cb.bootstrap_cloud_postgres(conn, database="sage", app_roles=[_SAGE_ROLE, _BFF_ROLE])
    for role in (_SAGE_ROLE, _BFF_ROLE):
        assert any(
            s == f'GRANT CONNECT, CREATE ON DATABASE "sage" TO "{role}"' for s in conn.sql
        ), f"missing CONNECT, CREATE ON DATABASE grant for {role}"


async def test_bootstrap_never_grants_admin_role() -> None:
    """No statement the bootstrap emits grants the broad admin role or superuser --
    the least-privilege posture holds end to end, on first run and on re-run.
    """
    for existing in ((), (_SAGE_ROLE, _BFF_ROLE)):
        conn = _RecordingConn(existing_roles=existing)
        await cb.bootstrap_cloud_postgres(conn, database="sage", app_roles=[_SAGE_ROLE, _BFF_ROLE])
        for sql in conn.sql:
            lowered = sql.lower()
            assert "azure_pg_admin" not in lowered, f"admin-role grant leaked: {sql}"
            assert "superuser" not in lowered, f"superuser grant leaked: {sql}"


# ---------------------------------------------------------------------------
# Environment parsing
# ---------------------------------------------------------------------------


def test_config_from_env_reads_job_env() -> None:
    """The job config is read entirely from the environment the Bicep job injects;
    a missing required coordinate fails loud rather than connecting to nothing.
    """
    env = {
        "PG_FQDN": "psql.example.private.postgres.database.azure.com",
        "PG_DATABASE": "sage",
        "PG_ADMIN_USER": "id-pg-bootstrap-prod",
        "SAGE_DB_ROLE": _SAGE_ROLE,
        "BFF_DB_ROLE": _BFF_ROLE,
    }
    cfg = cb._config_from_env(env)
    assert cfg.host == env["PG_FQDN"]
    assert cfg.database == "sage"
    assert cfg.admin_user == "id-pg-bootstrap-prod"
    assert cfg.app_roles == (_SAGE_ROLE, _BFF_ROLE)
    assert cfg.extensions == cb.DEFAULT_EXTENSIONS

    # An explicit override list is honoured.
    cfg2 = cb._config_from_env({**env, "PG_EXTENSIONS": "vector, pgstattuple"})
    assert cfg2.extensions == ("vector", "pgstattuple")

    for missing in ("PG_FQDN", "PG_DATABASE", "PG_ADMIN_USER", "SAGE_DB_ROLE", "BFF_DB_ROLE"):
        broken = {k: v for k, v in env.items() if k != missing}
        with pytest.raises(ValueError):
            cb._config_from_env(broken)


# ---------------------------------------------------------------------------
# Lazy-import boundary (anti-coincidental for the deployment-profile guardrails)
# ---------------------------------------------------------------------------


def test_pure_helpers_need_no_driver() -> None:
    """Importing the bootstrap module pulls in neither psycopg nor azure: the
    driver and Entra imports are deferred to ``_run``/``main``, so the module is
    importable (and its pure builders usable) in any profile. A regression that
    hoists those imports to module scope fails this in a clean subprocess.
    """
    code = (
        "import sys\n"
        "import sage.storage.postgres.cloud_bootstrap as cb\n"
        "azure = sorted(m for m in sys.modules if m == 'azure' or m.startswith('azure.'))\n"
        "assert not azure, azure\n"
        "assert 'psycopg' not in sys.modules, 'psycopg imported at module scope'\n"
        "stmts = cb.extension_statements()\n"
        "assert any('vector' in s for s in stmts)\n"
        "g = cb.grant_statement('sage', 'id-sage-prod')\n"
        "assert g.startswith('GRANT CONNECT, CREATE ON DATABASE')\n"
        "cb.validate_role_name('id-cas-bff-prod')\n"
    )
    env = {**os.environ, "PYTHONPATH": str(REPO_ROOT)}
    proc = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        env=env,
        cwd=str(REPO_ROOT),
    )
    assert proc.returncode == 0, f"clean-import check failed:\n{proc.stderr}"
