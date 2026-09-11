"""Read-only verification of an isolated PostgreSQL restore target.

A configured backup is not a demonstrated restore. This module runs in an
operator-dispatched in-network job against a throwaway server produced by an
Azure point-in-time or geo restore, and reports what that server actually
contains: workload tables with their row counts and ordered content hashes, the
workload roles and their grants, the required extensions, and two sentinel
documents that bracket the chosen recovery point.

Three properties are structural rather than incidental. The session is read-only
from its first statement, established as a connection option so no later
statement can relax it. The serving server is refused by address before any
connection is attempted, so a misconfigured dispatch cannot reach the database of
record. And recovery-point fidelity is asserted in both directions: the sentinel
written before the point must be present and the one written after it must be
absent, so a restore that silently returned a different point cannot pass.

Postgres is the storage port's sole durable store (CAS-ADR-042), so a restored
server holding the graph and content state is the whole recovery surface; there
is no second store to reconcile against.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Callable, Protocol

import psycopg
from psycopg import sql
from psycopg.conninfo import make_conninfo

from sage.maintenance.postgres_migration import PostgresStore, fingerprint

REPORT_PREFIX = "CAS_RESTORE_VERIFY_REPORT "

# Set as a connection option rather than a statement: it is in force before the
# first query and cannot be cleared by anything the session goes on to run.
READ_ONLY_OPTIONS = "-c default_transaction_read_only=on"

REQUIRED_EXTENSIONS = ("pgstattuple", "vector")

# What the cloud bootstrap grants every workload identity. A restore that lost
# either one leaves the applications unable to open their own database.
REQUIRED_DATABASE_PRIVILEGES = ("CONNECT", "CREATE")

_SETTING_KEYS = (
    "PG_FQDN",
    "PG_SERVING_FQDN",
    "PG_DATABASE",
    "PG_ADMIN_USER",
    "SAGE_DB_ROLE",
    "BFF_DB_ROLE",
    "PG_EXPECTED_MAJOR",
    "PG_RESTORE_POINT",
    "PG_SENTINEL_SCHEMA",
    "PG_SENTINEL_BEFORE_ID",
    "PG_SENTINEL_AFTER_ID",
)


class VerifyStore(Protocol):
    identity: str
    major: int

    def snapshot(self) -> dict[str, Any]: ...
    def extensions(self) -> list[tuple[str, str, str]]: ...
    def role_report(self, roles: tuple[str, ...]) -> dict[str, Any]: ...
    def document_present(self, schema: str, document_id: str) -> bool: ...
    def close(self) -> None: ...


@dataclass(frozen=True)
class Settings:
    """Exactly the values the verification needs; never the whole environment."""

    host: str
    serving_host: str
    database: str
    admin_user: str
    sage_role: str
    bff_role: str
    expected_major: int
    restore_point: str
    sentinel_schema: str
    sentinel_before: str
    sentinel_after: str

    @property
    def roles(self) -> tuple[str, str]:
        return (self.sage_role, self.bff_role)


def settings_from_environ(environ: Mapping[str, str]) -> Settings:
    missing = [key for key in _SETTING_KEYS if not environ.get(key)]
    if missing:
        raise ValueError("missing restore verification settings: " + ", ".join(missing))
    host = environ["PG_FQDN"].strip()
    serving = environ["PG_SERVING_FQDN"].strip()
    if host.casefold() == serving.casefold():
        raise ValueError(
            "refusing to verify against the serving server; the target must be an "
            "isolated restore server distinct from the database of record"
        )
    major = int(environ["PG_EXPECTED_MAJOR"])
    return Settings(
        host=host,
        serving_host=serving,
        database=environ["PG_DATABASE"],
        admin_user=environ["PG_ADMIN_USER"],
        sage_role=environ["SAGE_DB_ROLE"],
        bff_role=environ["BFF_DB_ROLE"],
        expected_major=major,
        restore_point=environ["PG_RESTORE_POINT"],
        sentinel_schema=environ["PG_SENTINEL_SCHEMA"],
        sentinel_before=environ["PG_SENTINEL_BEFORE_ID"],
        sentinel_after=environ["PG_SENTINEL_AFTER_ID"],
    )


def read_only_conninfo(settings: Settings) -> str:
    return make_conninfo(
        host=settings.host,
        dbname=settings.database,
        user=settings.admin_user,
        sslmode="require",
        options=READ_ONLY_OPTIONS,
    )


class RestoreVerifyStore(PostgresStore):
    """A migration store narrowed to the two extra reads verification needs."""

    def role_report(self, roles: tuple[str, ...]) -> dict[str, Any]:
        report: dict[str, Any] = {}
        for role in roles:
            if self.conn.execute(
                "SELECT NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname=%s)", (role,)
            ).fetchone()[0]:
                report[role] = {"exists": False}
                continue
            privileges = [
                privilege
                for privilege in REQUIRED_DATABASE_PRIVILEGES
                if self.conn.execute(
                    "SELECT has_database_privilege(%s, current_database(), %s)",
                    (role, privilege),
                ).fetchone()[0]
            ]
            report[role] = {
                "exists": True,
                "database_privileges": privileges,
                "pgstattuple_execute": self._can_execute_pgstattuple(role),
            }
        return report

    def _can_execute_pgstattuple(self, role: str) -> bool:
        # A restore that lost the extension makes the privilege unaskable:
        # ``has_function_privilege`` raises on a signature that does not resolve.
        # Report it as not held rather than crashing, so the missing extension is
        # reported as the finding it is instead of aborting the whole run.
        with self.conn.transaction():
            try:
                return self.conn.execute(
                    "SELECT has_function_privilege(%s, 'pgstattuple(regclass)', 'EXECUTE')",
                    (role,),
                ).fetchone()[0]
            except psycopg.errors.UndefinedFunction:
                return False

    def document_present(self, schema: str, document_id: str) -> bool:
        # A primary-key lookup, deliberately not a text scan: a sentinel id
        # quoted inside another document's body must not answer this question.
        return self.conn.execute(
            sql.SQL("SELECT EXISTS (SELECT 1 FROM {} WHERE id = %s)").format(
                sql.Identifier(schema, "documents")
            ),
            (document_id,),
        ).fetchone()[0]


def _sentinel_outcome(
    store: VerifyStore, settings: Settings, failures: list[str]
) -> dict[str, Any]:
    outcome: dict[str, Any] = {"schema": settings.sentinel_schema}
    for label, document_id in (
        ("before", settings.sentinel_before),
        ("after", settings.sentinel_after),
    ):
        try:
            present = bool(store.document_present(settings.sentinel_schema, document_id))
        except psycopg.Error as error:
            failures.append(
                f"sentinel lookup failed in schema {settings.sentinel_schema!r}: "
                f"{type(error).__name__}"
            )
            outcome[label] = {"id": document_id, "present": None}
            continue
        outcome[label] = {"id": document_id, "present": present}
    return outcome


def verify(store: VerifyStore, settings: Settings) -> dict[str, Any]:
    """Run every check against one restore target and assemble the report."""
    started = time.monotonic()
    failures: list[str] = []

    if store.major != settings.expected_major:
        failures.append(
            f"server major {store.major} differs from the expected major {settings.expected_major}"
        )

    snapshot = store.snapshot()
    tables = {
        name: {"count": value["count"], "hash": value["hash"]}
        for name, value in sorted(snapshot["tables"].items())
    }
    if not tables:
        failures.append("restore target holds no workload tables")

    extensions = [list(row) for row in store.extensions()]
    installed = {row[0] for row in extensions}
    for name in REQUIRED_EXTENSIONS:
        if name not in installed:
            failures.append(f"required extension {name!r} is not installed")

    roles = store.role_report(settings.roles)
    for name, detail in roles.items():
        if not detail.get("exists"):
            failures.append(f"workload role {name!r} is absent from the restore target")
            continue
        held = set(detail.get("database_privileges", ()))
        for privilege in REQUIRED_DATABASE_PRIVILEGES:
            if privilege not in held:
                failures.append(f"workload role {name!r} lacks database {privilege}")
        if not detail.get("pgstattuple_execute"):
            failures.append(f"workload role {name!r} lacks EXECUTE on pgstattuple")

    owned = {row[1] for row in snapshot.get("schemas", ())}
    if settings.sage_role not in owned:
        failures.append(f"workload role {settings.sage_role!r} owns no workload schema")

    sentinels = _sentinel_outcome(store, settings, failures)

    if sentinels.get("before", {}).get("present") is False:
        status = "recovery_point_undershoot"
    elif sentinels.get("after", {}).get("present") is True:
        status = "recovery_point_overshoot"
    elif failures:
        status = "failed"
    else:
        status = "verified"

    return {
        "status": status,
        "server": store.identity,
        "server_major": store.major,
        "expected_major": settings.expected_major,
        "restore_point": settings.restore_point,
        "tables": tables,
        "table_count": len(tables),
        "row_total": sum(value["count"] for value in tables.values()),
        "snapshot_fingerprint": fingerprint(snapshot),
        "roles": roles,
        "extensions": extensions,
        "sentinels": sentinels,
        "failures": failures,
        "elapsed_seconds": round(time.monotonic() - started, 3),
    }


def emit(report: dict[str, Any]) -> None:
    """One sanitized line, so a log collector can select the whole record."""
    print(REPORT_PREFIX + json.dumps(report, sort_keys=True, default=str))


def run(
    environ: Mapping[str, str],
    store_factory: Callable[..., VerifyStore],
) -> tuple[int, dict[str, Any]]:
    """Resolve settings, verify, and return an exit code with the report.

    Settings are resolved *before* the factory is called, so a dispatch naming
    the serving server is refused without a connection ever being opened.
    """
    settings = settings_from_environ(environ)
    store = store_factory(read_only_conninfo(settings), settings.roles)
    try:
        report = verify(store, settings)
    finally:
        store.close()
    return (0 if report["status"] == "verified" else 1), report


def main(argv: list[str] | None = None) -> int:
    if argv:
        raise ValueError("the restore verification takes no arguments")
    from sage.storage.postgres.managed_identity import (
        POSTGRES_AAD_SCOPE,
        close_postgres_credential,
        get_postgres_credential,
    )

    with asyncio.Runner() as runner:

        def access_token() -> str:
            return runner.run(get_postgres_credential().get_token(POSTGRES_AAD_SCOPE)).token

        def factory(conninfo: str, roles: tuple[str, ...]) -> VerifyStore:
            return RestoreVerifyStore(
                conninfo,
                roles,
                password=access_token(),
                token_provider=access_token,
            )

        try:
            code, report = run(os.environ, factory)
        finally:
            runner.run(close_postgres_credential())
    emit(report)
    return code


if __name__ == "__main__":
    sys.exit(main())
