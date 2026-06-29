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

import contextlib
import os
import subprocess
import sys
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Final

import pytest

from sage.storage.postgres import cloud_bootstrap as cb

REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[2]

_SAGE_ROLE: Final[str] = "id-sage-prod"
_BFF_ROLE: Final[str] = "id-cas-bff-prod"


# ---------------------------------------------------------------------------
# Recording fake connection + factory
#
# Mimics the slice of the psycopg async connection the bootstrap uses:
# ``await conn.execute(sql, params)`` returning a cursor with an async
# ``fetchone``. The pg_roles existence probe resolves against a configurable
# set of already-present roles, so both the create path and the converged
# re-run path are reachable without a server.
#
# The bootstrap opens *two* connections via a ``connect(database)`` factory: one
# to the ``postgres`` maintenance database for principal creation and grants, one
# to the application database for extensions. ``_RecordingFactory`` is that
# factory; it records every connection it opens and the database it targeted, so
# a test can recover the connection made to a given database and assert which
# statements ran on it.
# ---------------------------------------------------------------------------


class _RecordingCursor:
    def __init__(self, row: tuple | None) -> None:
        self._row = row

    async def fetchone(self) -> tuple | None:
        return self._row


class _RecordingConn:
    def __init__(
        self,
        database: str,
        existing_roles: tuple[str, ...] = (),
        withheld_extensions: tuple[str, ...] = (),
    ) -> None:
        self.database = database
        self.existing = set(existing_roles)
        # Extensions to report *absent* from ``pg_extension`` even after a
        # ``CREATE EXTENSION`` ran for them -- the injected "the create
        # succeeded but did not land in this database" case the bootstrap's
        # post-create verification must catch.
        self.withheld = set(withheld_extensions)
        self.created_extensions: set[str] = set()
        self.calls: list[tuple[str, tuple | None]] = []

    async def execute(self, sql: str, params: tuple | None = None) -> _RecordingCursor:
        self.calls.append((sql, params))
        if "pg_roles" in sql:
            role = params[0] if params else None
            return _RecordingCursor((1,) if role in self.existing else None)
        if "CREATE EXTENSION" in sql and '"' in sql:
            # Record the extension a CREATE ran for so the presence probe can
            # report it present -- mirroring the live no-op-once-present semantics.
            self.created_extensions.add(sql.split('"')[1])
            return _RecordingCursor(None)
        if "pg_extension" in sql:
            ext = params[0] if params else None
            present = ext in self.created_extensions and ext not in self.withheld
            return _RecordingCursor((1,) if present else None)
        return _RecordingCursor(None)

    @property
    def sql(self) -> list[str]:
        return [s for s, _ in self.calls]


class _RecordingFactory:
    """A ``connect(database)`` factory recording every connection it opens.

    Each call yields a fresh :class:`_RecordingConn` bound to ``database`` and
    appends it to :attr:`opened`, so a test can recover the connection made to a
    given database and assert the statements that ran on it.
    """

    def __init__(
        self,
        existing_roles: tuple[str, ...] = (),
        withheld_extensions: tuple[str, ...] = (),
    ) -> None:
        self._existing = existing_roles
        self._withheld = withheld_extensions
        self.opened: list[_RecordingConn] = []

    def __call__(self, database: str) -> contextlib.AbstractAsyncContextManager[_RecordingConn]:
        conn = _RecordingConn(database, self._existing, self._withheld)
        self.opened.append(conn)
        return self._cm(conn)

    @contextlib.asynccontextmanager
    async def _cm(self, conn: _RecordingConn) -> AsyncIterator[_RecordingConn]:
        yield conn

    def conn_for(self, database: str) -> _RecordingConn:
        """The single connection opened against ``database`` (asserts exactly one)."""
        matches = [c for c in self.opened if c.database == database]
        assert len(matches) == 1, (
            f"expected exactly one connection to {database!r}; got {len(matches)} "
            f"(opened: {[c.database for c in self.opened]})"
        )
        return matches[0]


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


def test_create_principal_statement_casts_role_to_text() -> None:
    """The bound role parameter is cast to ``::text`` so Postgres resolves the
    pgaadauth_create_principal(text, boolean, boolean) overload instead of failing
    on an ``unknown``-typed parameter.
    """
    sql = cb.create_principal_statement()
    assert sql == "SELECT pgaadauth_create_principal(%s::text, false, false)"
    assert "%s::text" in sql


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
# Orchestration against the recording fake factory
#
# The bootstrap opens two connections via the factory: principal creation and
# the database-scoped grant run on the connection to the ``postgres``
# maintenance database (where the ``pgaadauth_*`` functions live); extension
# creation runs on the connection to the application database (extensions are
# per-database). These checks assert that routing, the idempotent re-run, and
# the least-privilege posture.
# ---------------------------------------------------------------------------


async def test_bootstrap_creates_absent_principals() -> None:
    """When neither role exists yet, the bootstrap enrols each managed identity as
    a database role on the maintenance connection and pre-creates the extensions on
    the application connection.
    """
    factory = _RecordingFactory(existing_roles=())
    await cb.bootstrap_cloud_postgres(factory, database="sage", app_roles=[_SAGE_ROLE, _BFF_ROLE])

    created = _create_principal_roles(factory.conn_for("postgres"))
    assert created == [_SAGE_ROLE, _BFF_ROLE], f"both roles must be created; got {created}"
    app_sql = factory.conn_for("sage").sql
    assert any('CREATE EXTENSION IF NOT EXISTS "vector"' in s for s in app_sql)
    assert any('CREATE EXTENSION IF NOT EXISTS "pgstattuple"' in s for s in app_sql)


async def test_bootstrap_create_principal_casts_param_to_text() -> None:
    """The create-principal statement the bootstrap issues binds the role with an
    explicit ``::text`` cast -- proving ``ensure_principal`` uses the cast builder,
    not just that the builder is correct in isolation.
    """
    factory = _RecordingFactory(existing_roles=())
    await cb.bootstrap_cloud_postgres(factory, database="sage", app_roles=[_SAGE_ROLE])
    create_sql = [s for s in factory.conn_for("postgres").sql if "pgaadauth_create_principal" in s]
    assert create_sql, "a create-principal statement must be issued"
    assert all("%s::text" in s for s in create_sql), (
        f"the create-principal call must cast the role param to ::text; got {create_sql}"
    )


async def test_bootstrap_idempotent_when_principals_exist() -> None:
    """Re-running converges: when both roles already exist, no create-principal is
    issued (which would error on the live server), yet the grants and extension
    creates -- all IF-NOT-EXISTS / re-grantable -- still run, and nothing raises.
    """
    factory = _RecordingFactory(existing_roles=(_SAGE_ROLE, _BFF_ROLE))
    await cb.bootstrap_cloud_postgres(factory, database="sage", app_roles=[_SAGE_ROLE, _BFF_ROLE])

    admin = factory.conn_for("postgres")
    assert _create_principal_roles(admin) == [], (
        "no principal must be re-created on a converged run"
    )
    grants = [s for s in admin.sql if s.startswith("GRANT")]
    assert len(grants) == 2, f"both grants must still run on re-run; got {grants}"
    assert any('CREATE EXTENSION IF NOT EXISTS "vector"' in s for s in factory.conn_for("sage").sql)


async def test_bootstrap_grants_create_on_database_each_role() -> None:
    """Each role receives CONNECT + CREATE on the database -- the privilege its
    self-bootstrap needs to create and own its schema(s) -- on the maintenance
    connection.
    """
    factory = _RecordingFactory(existing_roles=())
    await cb.bootstrap_cloud_postgres(factory, database="sage", app_roles=[_SAGE_ROLE, _BFF_ROLE])
    admin_sql = factory.conn_for("postgres").sql
    for role in (_SAGE_ROLE, _BFF_ROLE):
        assert any(
            s == f'GRANT CONNECT, CREATE ON DATABASE "sage" TO "{role}"' for s in admin_sql
        ), f"missing CONNECT, CREATE ON DATABASE grant for {role}"


async def test_bootstrap_never_grants_admin_role() -> None:
    """No statement the bootstrap emits -- on either connection, first run or
    re-run -- grants the broad admin role or superuser.
    """
    for existing in ((), (_SAGE_ROLE, _BFF_ROLE)):
        factory = _RecordingFactory(existing_roles=existing)
        await cb.bootstrap_cloud_postgres(
            factory, database="sage", app_roles=[_SAGE_ROLE, _BFF_ROLE]
        )
        for conn in factory.opened:
            for sql in conn.sql:
                lowered = sql.lower()
                assert "azure_pg_admin" not in lowered, f"admin-role grant leaked: {sql}"
                assert "superuser" not in lowered, f"superuser grant leaked: {sql}"


# ---------------------------------------------------------------------------
# Connection routing -- the fix: principal/grant work and extension work land on
# the right database's connection.
# ---------------------------------------------------------------------------


async def test_principals_and_grants_route_to_maintenance_database() -> None:
    """Principal creation and the database-scoped grant issue against the
    ``postgres`` maintenance database -- where the ``pgaadauth_*`` functions live --
    and no extension statement leaks onto that connection.
    """
    factory = _RecordingFactory(existing_roles=())
    await cb.bootstrap_cloud_postgres(factory, database="sage", app_roles=[_SAGE_ROLE, _BFF_ROLE])
    admin = factory.conn_for(cb.MAINTENANCE_DATABASE)
    assert _create_principal_roles(admin) == [_SAGE_ROLE, _BFF_ROLE]
    assert [s for s in admin.sql if s.startswith("GRANT")] == [
        f'GRANT CONNECT, CREATE ON DATABASE "sage" TO "{_SAGE_ROLE}"',
        f'GRANT CONNECT, CREATE ON DATABASE "sage" TO "{_BFF_ROLE}"',
    ]
    assert not any("CREATE EXTENSION" in s for s in admin.sql), (
        f"no extension may run on the maintenance connection; got {admin.sql}"
    )


async def test_extensions_route_to_application_database() -> None:
    """Extension creation issues against the application database (extensions are
    per-database), and no principal or grant statement leaks onto that connection.
    """
    factory = _RecordingFactory(existing_roles=())
    await cb.bootstrap_cloud_postgres(factory, database="sage", app_roles=[_SAGE_ROLE, _BFF_ROLE])
    app = factory.conn_for("sage")
    assert any('CREATE EXTENSION IF NOT EXISTS "vector"' in s for s in app.sql)
    assert any('CREATE EXTENSION IF NOT EXISTS "pgstattuple"' in s for s in app.sql)
    assert not any("pgaadauth_create_principal" in s for s in app.sql), (
        f"no principal creation may run on the application connection; got {app.sql}"
    )
    assert not any(s.startswith("GRANT") for s in app.sql), (
        f"no grant may run on the application connection; got {app.sql}"
    )


async def test_bootstrap_opens_two_connections_one_per_database() -> None:
    """The bootstrap opens exactly two connections -- one to the maintenance
    database and one to the application database -- never collapsing back to a
    single connection (the defect this fix corrects).
    """
    factory = _RecordingFactory(existing_roles=())
    await cb.bootstrap_cloud_postgres(factory, database="sage", app_roles=[_SAGE_ROLE])
    assert sorted(c.database for c in factory.opened) == ["postgres", "sage"]


# ---------------------------------------------------------------------------
# Extension self-verification -- the pre-created set must actually land in the
# application database (the one the workload connects to). A create that does not
# land would otherwise surface only later, as an InsufficientPrivilege when the
# unprivileged workload retries CREATE EXTENSION; this check fails the bootstrap
# loud instead.
# ---------------------------------------------------------------------------


async def test_bootstrap_verifies_extensions_on_application_connection() -> None:
    """After creating the extensions, the bootstrap probes ``pg_extension`` on the
    *application* connection for each one -- proving they actually landed in the
    database the workload connects to, not merely that a CREATE was issued.
    """
    factory = _RecordingFactory(existing_roles=())
    await cb.bootstrap_cloud_postgres(factory, database="sage", app_roles=[_SAGE_ROLE, _BFF_ROLE])
    app = factory.conn_for("sage")
    probed = [params[0] for sql, params in app.calls if "pg_extension" in sql and params]
    assert "vector" in probed and "pgstattuple" in probed, (
        f"both extensions must be verified present on the app connection; probed {probed}"
    )


async def test_extension_verification_does_not_touch_maintenance_connection() -> None:
    """The presence check runs only on the application connection -- the database
    the workload connects to. It must never probe the maintenance connection, whose
    extension state is irrelevant to the workload (and where the workload never runs).
    """
    factory = _RecordingFactory(existing_roles=())
    await cb.bootstrap_cloud_postgres(factory, database="sage", app_roles=[_SAGE_ROLE, _BFF_ROLE])
    assert not any("pg_extension" in s for s in factory.conn_for("postgres").sql), (
        "the extension presence check must not run on the maintenance connection"
    )


async def test_bootstrap_fails_loud_when_extension_absent_in_app_database() -> None:
    """If an extension create does not land in the application database -- the exact
    scoping/effectiveness failure that left the workload's CREATE EXTENSION facing an
    absent ``vector`` (InsufficientPrivilege) -- the bootstrap raises, naming the
    database and the missing extension, instead of completing silently.
    """
    factory = _RecordingFactory(existing_roles=(), withheld_extensions=("vector",))
    with pytest.raises(RuntimeError, match=r"vector") as excinfo:
        await cb.bootstrap_cloud_postgres(
            factory, database="sage", app_roles=[_SAGE_ROLE, _BFF_ROLE]
        )
    message = str(excinfo.value)
    assert "vector" in message and "sage" in message, (
        f"the error must name the absent extension and the database; got {message!r}"
    )


def test_maintenance_database_is_builtin_postgres() -> None:
    """The maintenance database the principal work targets is the fixed Azure
    Flexible Server built-in ``postgres`` -- not the application database.
    """
    assert cb.MAINTENANCE_DATABASE == "postgres"


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
