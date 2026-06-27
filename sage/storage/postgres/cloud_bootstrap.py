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
server has no public endpoint) as the administrator identity, it pre-creates the
extensions and enrols each workload identity as a least-privilege role granted
only ``CONNECT, CREATE ON DATABASE`` -- enough for the workload to create and own
its own schema(s) at startup, never the broad ``azure_pg_admin`` role. It does
not create application tables: each workload self-bootstraps its own owned schema
(SAGE one per vault, the BFF its session schema), so admin-owned tables would
break that ownership. Pre-creating the extensions as admin is what lets those
self-bootstraps' ``CREATE EXTENSION IF NOT EXISTS`` calls succeed as
privilege-free no-ops.

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
from dataclasses import dataclass
from typing import Final

from sage.storage.postgres.schema import (
    DEFAULT_EXTENSIONS,
    validate_extension,
    validate_schema_name,
)

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
    # pgaadauth_create_principal(name, isAdmin, isMfa): a plain login role, no
    # admin membership, no MFA requirement.
    await conn.execute("SELECT pgaadauth_create_principal(%s, false, false)", (role,))
    return True


async def bootstrap_cloud_postgres(
    conn,
    *,
    database: str,
    app_roles: tuple[str, ...] | list[str],
    extensions: tuple[str, ...] | list[str] = DEFAULT_EXTENSIONS,
) -> None:
    """Run the full admin bootstrap on an open (autocommit) admin connection.

    Pre-creates the extensions, then for each workload identity ensures its
    database role exists and grants it the least-privilege CONNECT + CREATE on the
    database. Idempotent throughout: safe to re-run against an already-bootstrapped
    server.
    """
    for statement in extension_statements(extensions):
        await conn.execute(statement)
    for role in app_roles:
        await ensure_principal(conn, role)
        await conn.execute(grant_statement(database, role))


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
    role and untrusted-extension creation are administrator operations.
    """
    from psycopg.conninfo import make_conninfo

    from sage.storage.postgres.managed_identity import (
        get_postgres_credential,
        make_token_auth_connection_class,
    )
    from sage.storage.postgres.pool import PostgresConnectionParams, build_conn_kwargs

    cfg = _config_from_env(os.environ if env is None else env)
    params = PostgresConnectionParams(
        host=cfg.host,
        database=cfg.database,
        user=cfg.admin_user,
        sslmode="require",
    )
    # Empty environ: compose no env password into the conninfo -- the token-auth
    # connection class injects a fresh Entra token as the password per connect.
    conninfo = make_conninfo(**build_conn_kwargs(params, {}))
    connection_class = make_token_auth_connection_class(get_postgres_credential())
    async with await connection_class.connect(conninfo, autocommit=True) as conn:
        await bootstrap_cloud_postgres(
            conn,
            database=cfg.database,
            app_roles=cfg.app_roles,
            extensions=cfg.extensions,
        )
    print(
        "cloud-postgres bootstrap complete: roles "
        f"{', '.join(cfg.app_roles)} on database {cfg.database!r} "
        f"(extensions: {', '.join(cfg.extensions)})"
    )


def main(argv: list[str] | None = None) -> int:
    asyncio.run(_run())
    return 0


if __name__ == "__main__":
    sys.exit(main())
