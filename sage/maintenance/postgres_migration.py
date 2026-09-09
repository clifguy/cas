"""Offline, whole-database logical migration between explicitly named servers.

Runs only in an operator-dispatched in-VNet job. The source's workload CONNECT
privileges stay revoked on success *and* failure; releasing that fence is a
separate operation. A verified checkpoint permits a retry only against identical
source and target content. Dumps and tokens never appear in the job report.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Protocol

import psycopg
from psycopg import sql
from psycopg.conninfo import make_conninfo
from psycopg.types.json import Jsonb


@dataclass(frozen=True)
class RestoreManifest:
    """Catalog state read in the dump snapshot, bound to the resulting archive."""

    archive_sha256: str
    permissions: dict[str, Any]


def archive_digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


class MigrationStore(Protocol):
    identity: str
    major: int

    def snapshot(self) -> dict[str, Any]: ...
    def fence(self) -> None: ...
    def assert_quiescent(self) -> None: ...
    def dump(self, path: Path) -> RestoreManifest: ...
    def restore(self, path: Path, manifest: RestoreManifest) -> None: ...
    def read_checkpoint(self) -> dict[str, Any] | None: ...
    def save_checkpoint(self, value: dict[str, Any]) -> None: ...


def fingerprint(value: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def run_migration(
    source: MigrationStore,
    target: MigrationStore,
    archive: Path,
    run_id: str,
    source_major: int,
    target_major: int,
) -> dict[str, Any]:
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", run_id):
        raise ValueError("invalid migration run identity")
    if source.identity == target.identity:
        raise ValueError("source and target must be distinct")
    if (source.major, target.major) != (source_major, target_major):
        raise ValueError("unexpected source or target major")
    expected = {
        "run_id": run_id,
        "source": source.identity,
        "target": target.identity,
        "source_major": source_major,
        "target_major": target_major,
    }
    checkpoint = target.read_checkpoint()
    if checkpoint is not None and any(checkpoint.get(k) != v for k, v in expected.items()):
        raise ValueError("stale migration checkpoint")
    before_target = target.snapshot()
    if checkpoint is None and (
        before_target["tables"] or before_target["sequences"] or before_target.get("routines")
    ):
        raise ValueError("target contains existing workload objects")
    source.fence()
    source.assert_quiescent()
    before = source.snapshot()
    if not before["tables"]:
        raise ValueError("source has no workload tables")
    if checkpoint is not None:
        if checkpoint.get("fingerprint") != fingerprint(before) or before != before_target:
            raise ValueError("resume reconciliation failed")
        return checkpoint
    if archive.exists():
        raise ValueError("archive path already exists")
    manifest = source.dump(archive)
    if not archive.is_file() or archive.stat().st_size == 0:
        raise ValueError("dump produced no archive")
    source.assert_quiescent()
    if source.snapshot() != before:
        raise ValueError("source changed during snapshot")
    target.restore(archive, manifest)
    after = target.snapshot()
    source.assert_quiescent()
    if source.snapshot() != before or after != before:
        raise ValueError("reconciliation failed")
    with archive.open("rb") as stream:
        archive_hash = hashlib.file_digest(stream, "sha256").hexdigest()
    report = {
        **expected,
        "status": "verified",
        "permission_policy": "catalog-acl-v1-owner-maintain",
        "fingerprint": fingerprint(before),
        "archive_sha256": archive_hash,
        "tables": {name: value["count"] for name, value in before["tables"].items()},
    }
    target.save_checkpoint(report)
    return report


# System/extension namespaces are not portable workload data. All other schemas,
# including the BFF and stack registry, are included rather than a vault allowlist.
_SCHEMA_PREDICATE = """n.nspname !~ '^pg_'
AND n.nspname NOT IN ('information_schema', '_cas_migration')"""


def canonical_relation_acl(acl: str, owner: str, table: str, major: int) -> sql.Composed:
    """Compare grants semantically, with one explicit owner-only 16-to-17 rule.

    PostgreSQL 17 adds MAINTAIN when restoring the owner's former ALL grant.
    Only the unchanged owner's non-grantable self-grant beside all seven prior
    table privileges is equivalent. Other recipients and grant options remain
    observable. Column expressions are internal constants, never caller SQL.
    """
    return sql.SQL("""CASE WHEN {acl} IS NULL THEN NULL ELSE (
        SELECT coalesce(jsonb_agg(jsonb_build_array(pg_get_userbyid(a.grantor),
            CASE WHEN a.grantee=0 THEN NULL ELSE pg_get_userbyid(a.grantee) END,
            a.privilege_type, a.is_grantable)
            ORDER BY pg_get_userbyid(a.grantor), a.grantee=0,
                pg_get_userbyid(a.grantee), a.privilege_type, a.is_grantable), '[]'::jsonb)
        FROM aclexplode({acl}) a WHERE NOT (
            {major}=17 AND {table} AND a.privilege_type='MAINTAIN'
            AND a.grantor={owner} AND a.grantee={owner} AND NOT a.is_grantable
            AND (SELECT count(*) FROM aclexplode({acl}) own
                WHERE own.grantor={owner} AND own.grantee={owner} AND NOT own.is_grantable
                AND own.privilege_type IN
                  ('SELECT','INSERT','UPDATE','DELETE','TRUNCATE','REFERENCES','TRIGGER'))=7
        )) END""").format(
        acl=sql.SQL(acl), owner=sql.SQL(owner), table=sql.SQL(table), major=sql.Literal(major)
    )


class PostgresStore:
    def __init__(
        self,
        conninfo: str,
        app_roles: tuple[str, ...],
        *,
        password: str = "",
        token_provider: Callable[[], str] | None = None,
    ) -> None:
        self.conninfo = conninfo
        self.password = password
        self.token_provider = token_provider
        self.app_roles = app_roles
        self.conn = psycopg.connect(
            conninfo, password=password or None, autocommit=True, connect_timeout=20
        )
        self.major = self.conn.info.server_version // 10000
        address, port, database = self.conn.execute(
            "SELECT inet_server_addr()::text, inet_server_port(), current_database()"
        ).fetchone()
        self.identity = f"{address}:{port}/{database}"
        self.database = database

    def close(self) -> None:
        self.conn.close()

    def extensions(self) -> list[tuple[str, str, str]]:
        return self.conn.execute(
            "SELECT e.extname, e.extversion, n.nspname FROM pg_extension e "
            "JOIN pg_namespace n ON e.extnamespace=n.oid "
            "WHERE e.extname IN ('vector', 'pgstattuple') ORDER BY 1"
        ).fetchall()

    def assert_extensions_match(self, target: PostgresStore) -> None:
        source_extensions, target_extensions = self.extensions(), target.extensions()
        if {row[0] for row in source_extensions} != {
            "vector",
            "pgstattuple",
        } or source_extensions != target_extensions:
            raise ValueError(
                "required extension parity differs; have an authorized administrator align "
                "installed versions and schemas before downtime, then rerun preparation"
            )

    def capacity_report(self, directory: Path) -> dict[str, int]:
        return {
            "database_bytes": self.conn.execute(
                "SELECT pg_database_size(current_database())"
            ).fetchone()[0],
            "scratch_free_bytes": shutil.disk_usage(directory).free,
        }

    def extension_privileges(self) -> list[dict[str, Any]]:
        rows = self.conn.execute("""
            SELECT e.extname, e.extversion, n.nspname, p.proname,
                pg_get_function_identity_arguments(p.oid),
                format('%I(%s)', p.proname, pg_get_function_arguments(p.oid)),
                pg_get_userbyid(p.proowner),
                coalesce((SELECT jsonb_agg(jsonb_build_array(
                    pg_get_userbyid(a.grantor),
                    CASE WHEN a.grantee=0 THEN NULL ELSE pg_get_userbyid(a.grantee) END,
                    a.privilege_type, a.is_grantable)
                    ORDER BY pg_get_userbyid(a.grantor), a.grantee=0,
                             pg_get_userbyid(a.grantee), a.privilege_type, a.is_grantable)
                  FROM aclexplode(coalesce(p.proacl, acldefault('f',p.proowner))) a), '[]'::jsonb)
            FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace
            JOIN pg_depend d ON d.classid='pg_proc'::regclass AND d.objid=p.oid
                AND d.objsubid=0 AND d.refclassid='pg_extension'::regclass AND d.deptype='e'
            JOIN pg_extension e ON e.oid=d.refobjid
            WHERE p.prokind='f' ORDER BY e.extname,n.nspname,p.proname,5
        """).fetchall()
        keys = ("extension", "version", "schema", "name", "arguments", "toc_name", "owner", "acl")
        return [dict(zip(keys, row, strict=True)) for row in rows]

    def snapshot(self, *, permissions_only: bool = False) -> dict[str, Any]:
        self.conn.execute("SET timezone = 'UTC'")
        self.conn.execute("SET search_path = pg_catalog, public")
        schemas = self.conn.execute(
            sql.SQL(
                "SELECT n.nspname, pg_get_userbyid(n.nspowner), n.nspacl::text "
                "FROM pg_namespace n WHERE {} ORDER BY 1"
            ).format(sql.SQL(_SCHEMA_PREDICATE))
        ).fetchall()
        # Exclude extension-owned relations; their presence/version is checked separately.
        relations = self.conn.execute(
            sql.SQL(
                """SELECT n.nspname, c.relname, c.relkind,
                    pg_get_userbyid(c.relowner), {acl}
            FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
            WHERE {predicate} AND c.relkind IN ('r','p','v','m','f','S')
            AND NOT EXISTS (SELECT 1 FROM pg_depend d WHERE d.classid='pg_class'::regclass
              AND d.objid=c.oid AND d.deptype='e') ORDER BY 1,2"""
            ).format(
                predicate=sql.SQL(_SCHEMA_PREDICATE),
                acl=canonical_relation_acl("c.relacl", "c.relowner", "c.relkind='r'", self.major),
            )
        ).fetchall()
        column_grants = self.conn.execute(
            sql.SQL("""SELECT n.nspname, c.relname, column_attr.attname, {acl}
                FROM pg_attribute column_attr JOIN pg_class c ON c.oid=column_attr.attrelid
                JOIN pg_namespace n ON n.oid=c.relnamespace
                WHERE {predicate} AND c.relkind IN ('r','p','v','m','f')
                AND column_attr.attnum>0 AND NOT column_attr.attisdropped
                AND column_attr.attacl IS NOT NULL
                AND NOT EXISTS (SELECT 1 FROM pg_depend d
                    WHERE d.classid='pg_class'::regclass AND d.objid=c.oid AND d.deptype='e')
                ORDER BY 1,2,3""").format(
                predicate=sql.SQL(_SCHEMA_PREDICATE),
                acl=canonical_relation_acl("column_attr.attacl", "c.relowner", "FALSE", self.major),
            )
        ).fetchall()
        extensions = self.extensions()
        default_grants = self.conn.execute(
            sql.SQL(
                "SELECT pg_get_userbyid(d.defaclrole), n.nspname, d.defaclobjtype, "
                "{acl} FROM pg_default_acl d "
                "LEFT JOIN pg_namespace n ON n.oid=d.defaclnamespace "
                "WHERE d.defaclnamespace=0 OR ({predicate}) ORDER BY 1,2,3"
            ).format(
                predicate=sql.SQL(_SCHEMA_PREDICATE),
                acl=canonical_relation_acl(
                    "d.defaclacl", "d.defaclrole", "d.defaclobjtype='r'", self.major
                ),
            )
        ).fetchall()
        routines = self.conn.execute(
            sql.SQL("""SELECT n.nspname, p.proname,
                pg_get_function_identity_arguments(p.oid), p.prokind,
                CASE WHEN p.prokind IN ('f','p') THEN pg_get_functiondef(p.oid) END,
                pg_get_userbyid(p.proowner), p.proacl::text
                FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace
                WHERE {} AND NOT EXISTS (
                  SELECT 1 FROM pg_depend d WHERE d.classid='pg_proc'::regclass
                    AND d.objid=p.oid AND d.deptype='e') ORDER BY 1,2,3""").format(
                sql.SQL(_SCHEMA_PREDICATE)
            )
        ).fetchall()
        if any(row[3] not in ("f", "p") for row in routines):
            raise ValueError("unsupported workload routine kind")
        result: dict[str, Any] = {
            "schemas": schemas,
            "tables": {},
            "sequences": {},
            "extensions": extensions,
            "default_grants": default_grants,
            "column_grants": column_grants,
            "routines": routines,
            "extension_privileges": self.extension_privileges(),
        }
        if permissions_only:
            return {**result, "relations": relations}
        for namespace, name, kind, owner, acl in relations:
            qualified = sql.Identifier(namespace, name)
            key = f"{namespace}.{name}"
            if kind == "S":
                row = self.conn.execute(
                    sql.SQL("SELECT last_value, is_called FROM {}").format(qualified)
                ).fetchone()
                definition = self.conn.execute(
                    "SELECT seqtypid::regtype::text, seqstart, seqincrement, seqmax, "
                    "seqmin, seqcache, seqcycle FROM pg_sequence WHERE seqrelid=%s::regclass",
                    (qualified.as_string(self.conn),),
                ).fetchone()
                result["sequences"][key] = {
                    "state": row,
                    "definition": definition,
                    "owner": owner,
                    "acl": acl,
                }
                continue
            if kind != "r":
                raise ValueError(f"unsupported workload relation kind {kind!r}: {key}")
            digest = hashlib.sha256()
            count = 0
            with self.conn.transaction():
                with self.conn.cursor(name="migration_rows") as cursor:
                    cursor.execute(
                        sql.SQL(
                            "SELECT to_jsonb(t)::text FROM {} t "
                            'ORDER BY to_jsonb(t)::text COLLATE "C"'
                        ).format(qualified)
                    )
                    for (row,) in cursor:
                        digest.update(row.encode())
                        digest.update(b"\n")
                        count += 1
            regclass = qualified.as_string(self.conn)
            columns = self.conn.execute(
                """SELECT a.attname, format_type(a.atttypid,a.atttypmod), a.attnotnull,
                a.attgenerated, a.attidentity, pg_get_expr(d.adbin,d.adrelid)
                FROM pg_attribute a LEFT JOIN pg_attrdef d
                ON (a.attrelid=d.adrelid AND a.attnum=d.adnum)
                WHERE a.attrelid=%s::regclass AND a.attnum>0 AND NOT a.attisdropped
                ORDER BY a.attnum""",
                (regclass,),
            ).fetchall()
            indexes = self.conn.execute(
                "SELECT pg_get_indexdef(indexrelid) FROM pg_index "
                "WHERE indrelid=%s::regclass ORDER BY 1",
                (regclass,),
            ).fetchall()
            constraints = self.conn.execute(
                "SELECT conname, pg_get_constraintdef(oid) FROM pg_constraint "
                "WHERE conrelid=%s::regclass ORDER BY 1",
                (regclass,),
            ).fetchall()
            triggers = self.conn.execute(
                "SELECT tgname, pg_get_triggerdef(oid), tgenabled FROM pg_trigger "
                "WHERE tgrelid=%s::regclass AND NOT tgisinternal ORDER BY tgname",
                (regclass,),
            ).fetchall()
            result["tables"][key] = {
                "count": count,
                "hash": digest.hexdigest(),
                "owner": owner,
                "acl": acl,
                "columns": columns,
                "indexes": indexes,
                "constraints": constraints,
                "triggers": triggers,
            }
        return result

    def restore_owners(self) -> list[str]:
        # Shared ownership dependencies include routines/types as well as tables.
        # Namespace/relation owners also cover pinned roles absent from pg_shdepend.
        return [
            row[0]
            for row in self.conn.execute(
                sql.SQL("""SELECT DISTINCT pg_get_userbyid(owner) FROM (
                SELECT refobjid AS owner FROM pg_shdepend
                WHERE dbid=(SELECT oid FROM pg_database WHERE datname=current_database())
                AND refclassid='pg_authid'::regclass AND deptype='o'
                AND classid<>'pg_extension'::regclass
                AND NOT EXISTS (SELECT 1 FROM pg_depend d
                    WHERE d.classid=pg_shdepend.classid AND d.objid=pg_shdepend.objid
                    AND d.deptype='e')
                UNION SELECT n.nspowner FROM pg_namespace n WHERE {predicate}
                UNION SELECT c.relowner FROM pg_class c JOIN pg_namespace n
                  ON n.oid=c.relnamespace WHERE {predicate}
                UNION SELECT defaclrole FROM pg_default_acl
            ) owners ORDER BY 1""").format(predicate=sql.SQL(_SCHEMA_PREDICATE))
            ).fetchall()
        ]

    def assert_restore_privileges(self, owners: list[str]) -> None:
        if not self.conn.execute(
            "SELECT has_database_privilege(current_database(),'CREATE')"
        ).fetchone()[0]:
            raise ValueError("restore ownership privileges require database CREATE")
        for owner in owners:
            row = self.conn.execute(
                "SELECT pg_has_role(current_user,oid,'SET'), "
                "pg_has_role(current_user,oid,'USAGE') FROM pg_roles WHERE rolname=%s",
                (owner,),
            ).fetchone()
            # pg_restore changes owners before COPY. Both ownership transfer and
            # inherited owner access are necessary; CREATEROLE alone is not enough.
            if row is None or not all(row):
                raise ValueError(f"restore ownership privileges missing for role {owner!r}")

    def _revoke_connect(self) -> None:
        if not self.app_roles:
            raise ValueError("no workload roles supplied for fencing")
        current_user = self.conn.execute("SELECT current_user").fetchone()[0]
        if current_user in self.app_roles:
            raise ValueError("migration administrator cannot be a workload role")
        with self.conn.transaction():
            self.conn.execute(
                sql.SQL("REVOKE CONNECT ON DATABASE {} FROM PUBLIC").format(
                    sql.Identifier(self.database)
                )
            )
            for role in self.app_roles:
                row = self.conn.execute(
                    "SELECT rolsuper FROM pg_roles WHERE rolname=%s", (role,)
                ).fetchone()
                if row is None or row[0]:
                    raise ValueError("workload role absent or superuser")
                self.conn.execute(
                    sql.SQL("REVOKE CONNECT ON DATABASE {} FROM {}").format(
                        sql.Identifier(self.database), sql.Identifier(role)
                    )
                )
                allowed = self.conn.execute(
                    "SELECT has_database_privilege(%s,%s,'CONNECT')", (role, self.database)
                ).fetchone()[0]
                if allowed:
                    raise ValueError("workload retains CONNECT through inherited privileges")

    def assert_fence_privileges(self) -> None:
        # Rehearse the real ACL changes, but never disconnect a serving session.
        with self.conn.transaction(force_rollback=True):
            self.conn.execute("SET LOCAL lock_timeout = '5s'")
            self._revoke_connect()
            self._assert_connect_fenced()
            superuser, signal = self.conn.execute(
                "SELECT rolsuper, pg_has_role(current_user,'pg_signal_backend','USAGE') "
                "FROM pg_roles WHERE rolname=current_user"
            ).fetchone()
            session_roles = {
                row[0]
                for row in self.conn.execute(
                    "SELECT DISTINCT usename FROM pg_stat_activity "
                    "WHERE datname=current_database() AND pid<>pg_backend_pid() "
                    "AND backend_type='client backend'"
                ).fetchall()
            }
            for role in set(self.app_roles) | session_roles:
                row = self.conn.execute(
                    "SELECT rolsuper, pg_has_role(current_user,oid,'USAGE') "
                    "FROM pg_roles WHERE rolname=%s",
                    (role,),
                ).fetchone()
                if row is None or (not superuser and (row[0] or not (signal or row[1]))):
                    raise ValueError("source privileges cannot terminate workload sessions")
            if not self.conn.execute(
                "SELECT has_function_privilege("
                "'pg_catalog.pg_terminate_backend(integer,bigint)','EXECUTE')"
            ).fetchone()[0]:
                raise ValueError("source privileges cannot terminate workload sessions")
            if self.conn.execute(
                "SELECT count(*) FROM pg_prepared_xacts WHERE database=current_database()"
            ).fetchone()[0]:
                raise ValueError("prepared transactions remain")

    def fence(self) -> None:
        self._revoke_connect()
        self.conn.execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
            "WHERE datname=current_database() "
            "AND pid<>pg_backend_pid() AND backend_type='client backend'"
        )
        self.assert_quiescent()

    def _assert_connect_fenced(self) -> None:
        for role in self.app_roles:
            if self.conn.execute(
                "SELECT has_database_privilege(%s,%s,'CONNECT')", (role, self.database)
            ).fetchone()[0]:
                raise ValueError("workload CONNECT fence is not held")
        unaccounted = self.conn.execute(
            "SELECT count(*) FROM pg_roles WHERE rolcanlogin AND NOT rolsuper "
            "AND rolname <> current_user "
            "AND has_database_privilege(rolname,current_database(),'CONNECT')"
        ).fetchone()[0]
        if unaccounted:
            raise ValueError("unaccounted login roles retain source CONNECT")

    def assert_quiescent(self) -> None:
        self._assert_connect_fenced()
        writers = self.conn.execute(
            "SELECT count(*) FROM pg_stat_activity WHERE datname=current_database() "
            "AND pid<>pg_backend_pid() AND backend_type='client backend'"
        ).fetchone()[0]
        prepared = self.conn.execute(
            "SELECT count(*) FROM pg_prepared_xacts WHERE database=current_database()"
        ).fetchone()[0]
        if writers or prepared:
            raise ValueError("writers or prepared transactions remain")

    def _command(self, argv: list[str]) -> bytes:
        env = {
            **os.environ,
            "PGPASSWORD": self.token_provider() if self.token_provider else self.password,
            "PGCONNECT_TIMEOUT": "20",
        }
        # Internal argv builders use libpq conninfo and no shell interpretation.
        result = subprocess.run(argv, env=env, capture_output=True, timeout=3000)  # noqa: S603
        if result.returncode:
            # pg_restore can echo row contents on failure. Keep logs free of database data.
            raise RuntimeError(f"{Path(argv[0]).name} failed with exit {result.returncode}")
        return result.stdout

    def dump(self, path: Path, *, client_dir: Path | None = None) -> RestoreManifest:
        def client(name: str) -> str:
            return str(client_dir / name) if client_dir else name

        with self.conn.transaction():
            self.conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            permissions = self.snapshot(permissions_only=True)
            snapshot = self.conn.execute("SELECT pg_export_snapshot()").fetchone()[0]
            self._command(
                [
                    client("pg_dump"),
                    "--format=custom",
                    "--exclude-schema=_cas_migration",
                    "--snapshot",
                    snapshot,
                    "--file",
                    str(path),
                    "--dbname",
                    self.conninfo,
                ]
            )
        path.chmod(0o600)
        self._command([client("pg_restore"), "--list", str(path)])
        return RestoreManifest(archive_digest(path), permissions)

    @staticmethod
    def _managed_privileges(manifest: RestoreManifest) -> list[dict[str, Any]]:
        managed = []
        baseline = {
            ("azuresu", "azuresu", "EXECUTE", False),
            ("azuresu", "pg_stat_scan_tables", "EXECUTE", False),
            ("azuresu", "azure_pg_admin", "EXECUTE", True),
        }
        for function in manifest.permissions["extension_privileges"]:
            if function["extension"] != "pgstattuple" or function["owner"] != "azuresu":
                continue
            grants = {tuple(grant) for grant in function["acl"]}
            provider = {grant for grant in grants if grant[0] == "azuresu"}
            if provider != baseline or any(
                grant[0] != "azure_pg_admin"
                or grant[1] in {"azuresu", "azure_pg_admin"}
                or grant[2] != "EXECUTE"
                for grant in grants - baseline
            ):
                raise ValueError("unexpected provider extension permissions")
            managed.append(function)
        return managed

    def _restore_list(self, listing: bytes, managed: list[dict[str, Any]]) -> str:
        # Match the complete catalog-derived archive descriptor. A name-only match
        # cannot establish extension ownership or distinguish overloaded functions.
        descriptors = {
            f"ACL {function['schema']} FUNCTION {function['toc_name']} {function['owner']}": 0
            for function in managed
        }
        if len(descriptors) != len(managed) or any(
            "\n" in item or "\r" in item for item in descriptors
        ):
            raise ValueError("ambiguous provider ACL archive identity")
        result = []
        for line in listing.decode("utf-8").splitlines():
            entry = re.fullmatch(r"[0-9]+; [0-9]+ [0-9]+ (.*)", line)
            if entry and entry[1] in descriptors:
                descriptors[entry[1]] += 1
                line = ";" + line
            result.append(line)
        if any(count != 1 for count in descriptors.values()):
            raise ValueError("provider ACL archive mapping is incomplete or ambiguous")
        return "\n".join(result) + "\n"

    def _replay_managed_privileges(self, managed: list[dict[str, Any]]) -> None:
        current = {
            (f["extension"], f["schema"], f["name"], f["arguments"]): f
            for f in self.extension_privileges()
        }
        with self.conn.transaction():
            self.conn.execute("SET LOCAL statement_timeout = '3000s'")
            for function in managed:
                key = tuple(function[k] for k in ("extension", "schema", "name", "arguments"))
                target = current.get(key)
                if target is None or any(
                    target[k] != function[k] for k in ("extension", "version", "owner", "toc_name")
                ):
                    raise ValueError("provider extension identity differs on target")
                expected = {tuple(g) for g in function["acl"]}
                actual = {tuple(g) for g in target["acl"]}
                # Provider-owned grants are retained in place, never replayed by the
                # migration role. Any unexpected grant is a mismatch, not cleanup authority.
                if {g for g in actual if g[0] == "azuresu"} != {
                    g for g in expected if g[0] == "azuresu"
                } or not actual <= expected:
                    raise ValueError("provider extension permissions differ on target")
                for grantor, recipient, privilege, grantable in sorted(
                    expected - actual,
                    key=lambda grant: (
                        grant[0],
                        grant[1] is not None,
                        grant[1] or "",
                        grant[2],
                        grant[3],
                    ),
                ):
                    self.conn.execute(sql.SQL("SET LOCAL ROLE {}").format(sql.Identifier(grantor)))
                    self.conn.execute(
                        sql.SQL("GRANT EXECUTE ON FUNCTION {}.{}({}) TO {}{}").format(
                            sql.Identifier(function["schema"]),
                            sql.Identifier(function["name"]),
                            sql.SQL(function["arguments"]),
                            sql.SQL("PUBLIC") if recipient is None else sql.Identifier(recipient),
                            sql.SQL(" WITH GRANT OPTION" if grantable else ""),
                        )
                    )
                    self.conn.execute("RESET ROLE")

    def restore(
        self, path: Path, manifest: RestoreManifest, *, client_dir: Path | None = None
    ) -> None:
        if archive_digest(path) != manifest.archive_sha256:
            raise ValueError("restore archive does not match permission snapshot")
        managed = self._managed_privileges(manifest)
        client = str(client_dir / "pg_restore") if client_dir else "pg_restore"
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent, suffix=".restore-list"
        ) as selection:
            extra = []
            if managed:
                selection.write(
                    self._restore_list(self._command([client, "--list", str(path)]), managed)
                )
                selection.flush()
                extra = ["--use-list", selection.name]
            self._command(
                [
                    client,
                    "--exit-on-error",
                    "--single-transaction",
                    *extra,
                    "--dbname",
                    self.conninfo,
                    str(path),
                ]
            )
        self._replay_managed_privileges(managed)
        if self.snapshot(permissions_only=True) != manifest.permissions:
            raise ValueError("restore permission reconciliation failed")

    def read_checkpoint(self) -> dict[str, Any] | None:
        if (
            self.conn.execute("SELECT to_regclass('_cas_migration.checkpoint')").fetchone()[0]
            is None
        ):
            return None
        rows = self.conn.execute("SELECT body FROM _cas_migration.checkpoint").fetchall()
        if len(rows) != 1:
            raise ValueError("invalid checkpoint cardinality")
        return rows[0][0]

    def save_checkpoint(self, value: dict[str, Any]) -> None:
        with self.conn.transaction():
            self.conn.execute("CREATE SCHEMA IF NOT EXISTS _cas_migration")
            self.conn.execute("REVOKE ALL ON SCHEMA _cas_migration FROM PUBLIC")
            self.conn.execute("CREATE TABLE _cas_migration.checkpoint (body jsonb NOT NULL)")
            self.conn.execute("INSERT INTO _cas_migration.checkpoint VALUES (%s)", (Jsonb(value),))


def main(argv: list[str] | None = None) -> int:
    arguments = sys.argv[1:] if argv is None else argv
    if arguments and arguments[0] in ("rehearse", "cleanup-rehearsal"):
        from sage.maintenance.postgres_rehearsal import main as rehearsal_main

        return rehearsal_main(arguments)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("preflight", "migrate"), nargs="?", default="preflight")
    mode = parser.parse_args(argv).mode
    from sage.storage.postgres.managed_identity import (
        POSTGRES_AAD_SCOPE,
        close_postgres_credential,
        get_postgres_credential,
    )

    required = [
        "PG_SOURCE_FQDN",
        "PG_TARGET_FQDN",
        "PG_DATABASE",
        "PG_ADMIN_USER",
        "SAGE_DB_ROLE",
        "BFF_DB_ROLE",
        "PG_MIGRATION_RUN_ID",
        "PG_SOURCE_MAJOR",
        "PG_TARGET_MAJOR",
    ]
    missing = [key for key in required if not os.environ.get(key)]
    if missing:
        raise ValueError("missing migration settings: " + ", ".join(missing))
    from sage.storage.postgres.cloud_bootstrap import _run as bootstrap_target

    roles = (os.environ["SAGE_DB_ROLE"], os.environ["BFF_DB_ROLE"])
    stores: list[PostgresStore] = []
    with asyncio.Runner() as runner:

        def access_token() -> str:
            return runner.run(get_postgres_credential().get_token(POSTGRES_AAD_SCOPE)).token

        try:
            for key in ("PG_SOURCE_FQDN", "PG_TARGET_FQDN"):
                token = access_token()
                conninfo = make_conninfo(
                    host=os.environ[key],
                    dbname=os.environ["PG_DATABASE"],
                    user=os.environ["PG_ADMIN_USER"],
                    sslmode="require",
                )
                stores.append(
                    PostgresStore(
                        conninfo,
                        roles,
                        password=token,
                        token_provider=access_token,
                    )
                )
            if stores[0].identity == stores[1].identity:
                raise ValueError("source and target must be distinct")
            if (stores[0].major, stores[1].major) != (
                int(os.environ["PG_SOURCE_MAJOR"]),
                int(os.environ["PG_TARGET_MAJOR"]),
            ):
                raise ValueError("unexpected source or target major")
            target_snapshot = stores[1].snapshot()
            if stores[1].read_checkpoint() is None and (
                target_snapshot["tables"]
                or target_snapshot["sequences"]
                or target_snapshot.get("routines")
            ):
                raise ValueError("target contains existing workload objects")
            runner.run(bootstrap_target({**os.environ, "PG_FQDN": os.environ["PG_TARGET_FQDN"]}))
            stores[1].assert_restore_privileges(stores[0].restore_owners())
            stores[0].assert_extensions_match(stores[1])
            stores[0].assert_fence_privileges()
            capacity = stores[0].capacity_report(Path(tempfile.gettempdir()))
            if mode == "preflight":
                print(
                    json.dumps(
                        {
                            "status": "preflight_verified",
                            "capacity_estimate": capacity,
                            "source": stores[0].identity,
                            "target": stores[1].identity,
                        }
                    )
                )
                return 0
            with tempfile.TemporaryDirectory(prefix="postgres-migration-") as directory:
                report = run_migration(
                    *stores,
                    Path(directory) / "snapshot.dump",
                    os.environ["PG_MIGRATION_RUN_ID"],
                    int(os.environ["PG_SOURCE_MAJOR"]),
                    int(os.environ["PG_TARGET_MAJOR"]),
                )
                print(json.dumps({**report, "capacity_estimate": capacity}, sort_keys=True))
        finally:
            for store in stores:
                store.close()
            runner.run(close_postgres_credential())
    return 0


if __name__ == "__main__":
    sys.exit(main())
