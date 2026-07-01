"""Idempotent admin-side bootstrap for the cloud Postgres endpoint (CAS-ADR-042).

The cloud Postgres Flexible Server is provisioned Entra-only -- password
authentication is disabled -- and each workload (the SAGE infra server and the
CAS BFF) authenticates as *itself*, presenting a managed-identity Entra access
token as the libpq password. Two prerequisites must exist on the live server
before that works, and neither can be expressed as declarative ARM: a **database
role per managed identity** (created with ``pgaadauth_create_principal``) and the
**extensions** the storage adapters require (``vector`` for the content store's
embedding column; ``pgstattuple``, untrusted, for bloat measurement). Both are
data-plane SQL that must run *as the server's Entra administrator*.

This module is that bootstrap. Run once per environment from inside the VNet (the
server has no public endpoint) as the administrator identity, it enrols each
workload identity as a least-privilege role granted only ``CONNECT, CREATE ON
DATABASE`` -- enough for the workload to create and own its own schema(s) at
startup, never the broad ``azure_pg_admin`` role -- and pre-creates the
extensions. It does not create application tables: each workload self-bootstraps
its own owned schema (SAGE one per vault, the BFF its session schema), so
admin-owned tables would break that ownership. Pre-creating the extensions as
admin is what lets those self-bootstraps' ``CREATE EXTENSION IF NOT EXISTS``
calls succeed as privilege-free no-ops.

The work spans two admin connections, because the SQL is not all addressable from
one database. The ``pgaadauth_*`` administration functions exist only in the
Flexible Server's built-in ``postgres`` maintenance database, so principal
creation and the database-scoped grant run there; extensions are per-database
objects, so they are created on a connection to the application database itself.

Re-running converges: every extension create is ``IF NOT EXISTS``, every grant is
re-grantable, and role creation is guarded by an existence check so it is never
re-issued (which would error). The pure statement builders carry no driver
import so they stay testable without a server; the psycopg and Entra imports are
deferred to :func:`_run` so the module imports cleanly in any deployment profile.
"""

from __future__ import annotations

import asyncio
import os
import re
import sys
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Final

from sage.storage.postgres.schema import (
    DEFAULT_EXTENSIONS,
    validate_extension,
    validate_schema_name,
)

# The ``pgaadauth_*`` administration functions live only in the Flexible Server's
# built-in ``postgres`` maintenance database, so principal creation and the
# database-scoped grant must run against it -- not the application database, where
# those functions do not exist. The name is a fixed Azure built-in.
MAINTENANCE_DATABASE: Final[str] = "postgres"

# Managed-identity role names are Azure user-assigned-identity names: lowercase
# letters, digits, and hyphens, starting with a letter (e.g. ``id-sage-prod``).
# A role name is interpolated into the GRANT as a quoted identifier (it cannot be
# a bind parameter), so it is validated against this strict allowlist first --
# the same discipline the schema module applies to schema and extension names.
_ROLE_NAME_RE: Final[re.Pattern[str]] = re.compile(r"^[a-z][a-z0-9-]{1,126}$")


def validate_role_name(name: str) -> str:
    """Return ``name`` if it is a safe managed-identity role identifier, else raise."""
    if not isinstance(name, str) or not _ROLE_NAME_RE.match(name):
        raise ValueError(
            f"role name {name!r} is not a safe managed-identity identifier "
            "(lowercase letters, digits, and hyphens; must start with a letter)"
        )
    return name


def _ordered_extensions(extensions: tuple[str, ...] | list[str]) -> list[str]:
    """Validated extension list with ``vector`` guaranteed first, duplicates dropped.

    ``vector`` underpins the content store's embedding column, so it is created
    regardless of the supplied list; the remainder follow in order. Mirrors the
    ordering the schema module's per-vault bootstrap applies.
    """
    ordered = ["vector"]
    seen = {"vector"}
    for ext in extensions:
        validate_extension(ext)
        if ext not in seen:
            seen.add(ext)
            ordered.append(ext)
    return ordered


def extension_statements(
    extensions: tuple[str, ...] | list[str] = DEFAULT_EXTENSIONS,
) -> list[str]:
    """Idempotent ``CREATE EXTENSION`` statements the admin pre-creates.

    Defaults to the canonical schema-module extension set (``vector`` +
    ``pgstattuple``). Each is ``IF NOT EXISTS`` so re-running is a no-op, and the
    untrusted ``pgstattuple`` is created here -- as the administrator -- because
    the unprivileged application roles cannot create it.
    """
    return [f'CREATE EXTENSION IF NOT EXISTS "{ext}"' for ext in _ordered_extensions(extensions)]


def grant_statement(database: str, role: str) -> str:
    """The least-privilege grant for one workload role: CONNECT + CREATE on the DB.

    ``CREATE ON DATABASE`` lets the workload create and own its own schema(s), so
    table and index rights follow ownership with no further grants. The role is
    never added to ``azure_pg_admin``. Both identifiers are validated before
    interpolation.
    """
    validate_schema_name(database)
    validate_role_name(role)
    return f'GRANT CONNECT, CREATE ON DATABASE "{database}" TO "{role}"'


def pgstattuple_grant_statement(role: str) -> str:
    """Grant one workload role EXECUTE on the untrusted ``pgstattuple`` functions.

    The database-scoped :func:`grant_statement` gives the workload CONNECT + CREATE and
    nothing more, so it can own its schemas -- but that leaves it unable to *execute*
    the untrusted ``pgstattuple`` extension's functions, which the content store uses to
    measure chunks-table bloat (retained dead tuples, free-space fragments). Without this
    grant the first bloat read raises ``InsufficientPrivilege`` and vault statistics fail.

    Both the ``text`` and ``regclass`` overloads are named because the content-store
    queries pass an unknown-typed ``'chunks'`` literal, and either overload may be the one
    Postgres resolves it to. The grant is re-grantable, so a converged re-run is a no-op;
    it must run on the application database (function grants are per-database), where the
    extension's functions live. The role is validated before interpolation -- it is a
    quoted identifier, not a bind parameter. See CAS-ADR-042.
    """
    validate_role_name(role)
    return f'GRANT EXECUTE ON FUNCTION pgstattuple(text), pgstattuple(regclass) TO "{role}"'


def create_principal_statement() -> str:
    """Return the SQL that enrols a managed-identity role as a database principal.

    ``pgaadauth_create_principal(name, isAdmin, isMfa)`` is issued for a plain
    login role -- no admin membership, no MFA. The role name is bound as a
    parameter and cast to ``::text`` so Postgres resolves the
    ``pgaadauth_create_principal(text, boolean, boolean)`` overload; without the
    cast psycopg sends the parameter as ``unknown`` and the overload is
    unresolvable.
    """
    return "SELECT pgaadauth_create_principal(%s::text, false, false)"


async def ensure_principal(conn, role: str) -> bool:
    """Idempotently enrol ``role`` as a database principal; return whether created.

    Checks ``pg_roles`` first so a converged re-run never re-issues
    ``pgaadauth_create_principal`` (which errors when the role already exists).
    The name is bound as a parameter to the catalog probe and the create call.
    """
    validate_role_name(role)
    cursor = await conn.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (role,))
    if await cursor.fetchone() is not None:
        return False
    await conn.execute(create_principal_statement(), (role,))
    return True


async def verify_extensions_present(conn, database: str, extensions) -> None:
    """Confirm each expected extension is present in the connected database.

    The admin pre-creates the untrusted extensions so each workload's own
    ``CREATE EXTENSION IF NOT EXISTS`` is a privilege-free no-op -- but that no-op
    only holds if the extension is actually present in the database the workload
    connects to. This check runs on the application connection itself (the same
    database the workload will use), probing ``pg_extension`` for each expected
    extension and raising if any is absent. A create that did not land therefore
    fails the bootstrap loud -- naming the database and the missing extension(s) --
    instead of surfacing later as an ``InsufficientPrivilege`` when the
    unprivileged workload retries the create against a database where the extension
    was never installed.
    """
    missing: list[str] = []
    for ext in _ordered_extensions(extensions):
        cursor = await conn.execute("SELECT 1 FROM pg_extension WHERE extname = %s", (ext,))
        if await cursor.fetchone() is None:
            missing.append(ext)
    if missing:
        raise RuntimeError(
            f"extension(s) {', '.join(missing)} absent from database {database!r} after "
            "admin pre-creation; the application's CREATE EXTENSION IF NOT EXISTS would "
            "fail with InsufficientPrivilege. Confirm the bootstrap pre-created the "
            "extensions in the same database the application connects to."
        )


async def bootstrap_cloud_postgres(
    connect,
    *,
    database: str,
    app_roles: tuple[str, ...] | list[str],
    extensions: tuple[str, ...] | list[str] = DEFAULT_EXTENSIONS,
    maintenance_database: str = MAINTENANCE_DATABASE,
) -> None:
    """Run the full admin bootstrap across the maintenance and application databases.

    ``connect(database)`` is a factory returning an async context manager that
    yields an open (autocommit) admin connection to that database. The bootstrap
    opens two: principal creation and the least-privilege CONNECT + CREATE grant run
    on the ``postgres`` maintenance database (where the ``pgaadauth_*`` functions
    live); the extensions are pre-created on the application database (extensions are
    per-database objects), where each workload role is also granted EXECUTE on the
    untrusted ``pgstattuple`` functions the content store reads for bloat measurement
    (a per-database function grant CONNECT + CREATE does not confer). Idempotent
    throughout: safe to re-run against an already-bootstrapped server.
    """
    # Principal creation and the database-scoped grant: against the maintenance
    # database, the only place the ``pgaadauth_*`` functions exist.
    async with connect(maintenance_database) as admin_conn:
        for role in app_roles:
            await ensure_principal(admin_conn, role)
            await admin_conn.execute(grant_statement(database, role))
    # Extensions and their EXECUTE grants: against the application database itself
    # (both extensions and function grants are per-database, so they must run on a
    # connection to it).
    async with connect(database) as app_conn:
        for statement in extension_statements(extensions):
            await app_conn.execute(statement)
        # Verify the pre-created set actually landed in this database -- the one the
        # workload connects to -- so a create that did not take fails here loud
        # rather than as a later InsufficientPrivilege at vault load.
        await verify_extensions_present(app_conn, database, extensions)
        # Grant each workload role EXECUTE on the untrusted pgstattuple functions the
        # content store uses for bloat measurement. CONNECT + CREATE (granted on the
        # maintenance connection) does not carry function EXECUTE, so without this the
        # workload's first bloat read raises InsufficientPrivilege. Only when pgstattuple
        # is actually in the resolved set -- granting execute on a function that was not
        # provisioned would error.
        if "pgstattuple" in _ordered_extensions(extensions):
            for role in app_roles:
                await app_conn.execute(pgstattuple_grant_statement(role))


@dataclass(frozen=True)
class _JobConfig:
    """Bootstrap coordinates resolved from the job environment."""

    host: str
    database: str
    admin_user: str
    app_roles: tuple[str, ...]
    extensions: tuple[str, ...]


def _config_from_env(env: dict[str, str]) -> _JobConfig:
    """Resolve the bootstrap config from the environment the deploy job injects.

    No coordinate is baked into this module: the private-server FQDN, the database,
    the administrator identity to connect as, and the workload role names all
    arrive as environment variables. A missing required variable fails loud.
    """

    def _required(key: str) -> str:
        value = env.get(key)
        if not value:
            raise ValueError(f"missing required environment variable {key!r}")
        return value

    extensions_raw = env.get("PG_EXTENSIONS")
    extensions = (
        tuple(e.strip() for e in extensions_raw.split(",") if e.strip())
        if extensions_raw
        else DEFAULT_EXTENSIONS
    )
    return _JobConfig(
        host=_required("PG_FQDN"),
        database=_required("PG_DATABASE"),
        admin_user=_required("PG_ADMIN_USER"),
        app_roles=(_required("SAGE_DB_ROLE"), _required("BFF_DB_ROLE")),
        extensions=extensions,
    )


async def _run(env: dict[str, str] | None = None) -> None:
    """Connect as the Entra administrator identity and run the bootstrap.

    Authenticates exactly as the cloud storage binding does -- a managed-identity
    Entra token injected as the libpq password -- but as the *administrator*
    identity (``AZURE_CLIENT_ID`` selects it for ``DefaultAzureCredential``), since
    role and untrusted-extension creation are administrator operations. Supplies a
    per-database connection factory so the bootstrap can target the ``postgres``
    maintenance database for principal work and the application database for
    extensions.
    """
    from psycopg.conninfo import make_conninfo

    from sage.storage.postgres.managed_identity import (
        get_postgres_credential,
        make_token_auth_connection_class,
    )
    from sage.storage.postgres.pool import PostgresConnectionParams, build_conn_kwargs

    cfg = _config_from_env(os.environ if env is None else env)
    connection_class = make_token_auth_connection_class(get_postgres_credential())

    @asynccontextmanager
    async def connect(database: str):
        params = PostgresConnectionParams(
            host=cfg.host,
            database=database,
            user=cfg.admin_user,
            sslmode="require",
        )
        # Empty environ: compose no env password into the conninfo -- the token-auth
        # connection class injects a fresh Entra token as the password per connect.
        conninfo = make_conninfo(**build_conn_kwargs(params, {}))
        async with await connection_class.connect(conninfo, autocommit=True) as conn:
            yield conn

    await bootstrap_cloud_postgres(
        connect,
        database=cfg.database,
        app_roles=cfg.app_roles,
        extensions=cfg.extensions,
    )
    print(
        "cloud-postgres bootstrap complete: roles "
        f"{', '.join(cfg.app_roles)} on database {cfg.database!r} "
        f"(principals via the {MAINTENANCE_DATABASE!r} maintenance database; "
        f"extensions: {', '.join(cfg.extensions)})"
    )


def main(argv: list[str] | None = None) -> int:
    asyncio.run(_run())
    return 0


if __name__ == "__main__":
    sys.exit(main())
