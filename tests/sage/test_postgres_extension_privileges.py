"""Real provider-owned extension ACLs through seed and migration restore."""

from __future__ import annotations

import os
import time
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import make_conninfo

from sage.maintenance.postgres_migration import PostgresStore, run_migration
from sage.maintenance.postgres_rehearsal import RehearsalDatabases, execute_rehearsal
from tests.helpers.pg_isolation import derive_throwaway_dbname, rewrite_dsn_dbname


@dataclass
class ProviderFixture:
    dsns: list[str]
    stores: list[PostgresStore]
    administrator: str
    apps: tuple[str, ...]

    include_vector: bool = False

    def install(self, index: int, name: str) -> None:
        # This is the provider's extension-install boundary, never the restoring identity.
        with psycopg.connect(rewrite_dsn_dbname(self.dsns[index], name), autocommit=True) as conn:
            extensions = ("vector", "pgstattuple") if self.include_vector else ("pgstattuple",)
            for extension in extensions:
                conn.execute(sql.SQL("CREATE EXTENSION {}").format(sql.Identifier(extension)))
            conn.execute(
                "UPDATE pg_extension SET extowner=(SELECT oid FROM pg_roles "
                "WHERE rolname=%s) WHERE extname IN ('vector','pgstattuple')",
                (self.administrator,),
            )
            for signature, extension in conn.execute(
                "SELECT p.oid::regprocedure::text, e.extname FROM pg_proc p "
                "JOIN pg_depend d ON d.classid='pg_proc'::regclass AND d.objid=p.oid "
                "AND d.deptype='e' JOIN pg_extension e ON e.oid=d.refobjid "
                "WHERE e.extname IN ('vector','pgstattuple')"
            ).fetchall():
                owner = "azuresu" if extension == "pgstattuple" else self.administrator
                conn.execute(
                    sql.SQL("ALTER FUNCTION {} OWNER TO {}").format(
                        sql.SQL(signature), sql.Identifier(owner)
                    )
                )
                if extension == "pgstattuple":
                    conn.execute(
                        sql.SQL(
                            "GRANT EXECUTE ON FUNCTION {} TO azure_pg_admin WITH GRANT OPTION"
                        ).format(sql.SQL(signature))
                    )

    def connect_admin(self, index: int) -> psycopg.Connection:
        return psycopg.connect(
            rewrite_dsn_dbname(self.dsns[index], self.stores[index].database), autocommit=True
        )


@pytest.fixture
def provider(request: pytest.FixtureRequest) -> Iterator[ProviderFixture]:
    dsns = [
        os.environ.get(key)
        for key in ("SAGE_MIGRATION_TEST_SOURCE_DSN", "SAGE_MIGRATION_TEST_TARGET_DSN")
    ]
    if not all(dsns):
        pytest.skip("requires disposable PostgreSQL 16 and 17 maintenance endpoints")
    mode = getattr(request, "param", None)
    catalog = mode in {"catalog", "catalog_bare"}
    if catalog:
        dsns = dsns[:1]
    nonce = uuid.uuid4().hex[:12]
    administrator = "restore_" + nonce
    apps = (
        "PUBLIC" if getattr(request, "param", None) == "quoted_public" else "sage ; " + nonce,
        'bff " ' + nonce,
    )
    if mode == "catalog_bare":
        apps = ()
    names = [derive_throwaway_dbname() for _ in dsns]
    fixture = ProviderFixture(dsns, [], administrator, apps, include_vector=mode == "rehearsal")
    created = []
    try:
        for index, (dsn, name) in enumerate(zip(dsns, names, strict=True)):
            with psycopg.connect(dsn, autocommit=True) as conn:
                # Fixed provider names must be exclusively fixture-owned. Existing names
                # fail creation; never adopt or change a shared cluster's provider roles.
                conn.execute("CREATE ROLE azuresu")
                conn.execute("CREATE ROLE azure_pg_admin")
                conn.execute(
                    sql.SQL("CREATE ROLE {} LOGIN CREATEDB").format(sql.Identifier(administrator))
                )
                conn.execute(
                    sql.SQL("GRANT azure_pg_admin TO {}").format(sql.Identifier(administrator))
                )
                for app in apps:
                    conn.execute(sql.SQL("CREATE ROLE {} LOGIN").format(sql.Identifier(app)))
                conn.execute(
                    sql.SQL("CREATE DATABASE {} OWNER {}").format(
                        sql.Identifier(name), sql.Identifier(administrator)
                    )
                )
                created.append((dsn, name))
            fixture.install(index, name)
            store = PostgresStore(
                make_conninfo(rewrite_dsn_dbname(dsn, name), user=administrator), apps
            )
            fixture.stores.append(store)
            assert store.conn.execute(
                "SELECT rolsuper FROM pg_roles WHERE rolname=current_user"
            ).fetchone() == (False,)
        source = fixture.stores[0]
        assert tuple(store.major for store in fixture.stores) == ((16,) if catalog else (16, 17))
        if not catalog:
            source.conn.execute("CREATE SCHEMA ordinary")
            source.conn.execute("CREATE TABLE ordinary.data (value text)")
            source.conn.execute("INSERT INTO ordinary.data VALUES ('retained')")
            source.conn.execute(
                "CREATE FUNCTION ordinary.pg_relpages(regclass) RETURNS bigint "
                "LANGUAGE SQL AS 'SELECT 42::bigint'"
            )
            for app in apps:
                source.conn.execute(
                    sql.SQL("GRANT SELECT ON ordinary.data TO {}").format(sql.Identifier(app))
                )
                source.conn.execute(
                    sql.SQL(
                        "ALTER DEFAULT PRIVILEGES IN SCHEMA ordinary GRANT SELECT ON TABLES TO {}"
                    ).format(sql.Identifier(app))
                )
                source.conn.execute(
                    sql.SQL(
                        "GRANT EXECUTE ON FUNCTION ordinary.pg_relpages(regclass) "
                        "TO {} WITH GRANT OPTION"
                    ).format(sql.Identifier(app))
                )
        if mode in {"full", "rehearsal", "quoted_public", "catalog"}:
            source.conn.execute("SET ROLE azure_pg_admin")
            for app in apps:
                source.conn.execute(
                    sql.SQL(
                        "GRANT EXECUTE ON FUNCTION public.pgstattuple(regclass), "
                        "public.pgstattuple(text) TO {}"
                    ).format(sql.Identifier(app))
                )
            source.conn.execute("RESET ROLE")
        yield fixture
    finally:
        for store in fixture.stores:
            store.close()
        for dsn, name in created:
            with psycopg.connect(dsn, autocommit=True) as conn:
                conn.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name)))
                for role in (*apps, administrator, "azure_pg_admin", "azuresu"):
                    conn.execute(sql.SQL("DROP ROLE {}").format(sql.Identifier(role)))


def acl(conn: psycopg.Connection, signature: str) -> list[tuple]:
    return conn.execute(
        "SELECT pg_get_userbyid(a.grantor), CASE WHEN a.grantee=0 THEN 'PUBLIC' "
        "ELSE pg_get_userbyid(a.grantee) END, a.privilege_type, a.is_grantable "
        "FROM pg_proc p, aclexplode(coalesce(p.proacl,acldefault('f',p.proowner))) a "
        "WHERE p.oid=%s::regprocedure ORDER BY 1,2,3,4",
        (signature,),
    ).fetchall()


@pytest.mark.parametrize("provider", ["catalog_bare"], indirect=True)
def test_provider_grant_cycle_is_the_captured_non_superuser_failure(
    provider: ProviderFixture,
) -> None:
    source = provider.stores[0]
    assert ("azuresu", "azure_pg_admin", "EXECUTE", True) in acl(
        source.conn, "public.pg_relpages(regclass)"
    )
    with pytest.raises(psycopg.Error, match="grant options cannot be granted back") as error:
        source.conn.execute(
            "GRANT ALL ON FUNCTION public.pg_relpages(regclass) TO azure_pg_admin WITH GRANT OPTION"
        )
    assert error.value.sqlstate == "0LP01"


def assert_permissions(source: PostgresStore, target: PostgresStore, apps: tuple[str, ...]) -> None:
    for signature in (
        "public.pgstattuple(regclass)",
        "public.pgstattuple(text)",
        "public.pg_relpages(regclass)",
        "ordinary.pg_relpages(regclass)",
    ):
        assert acl(target.conn, signature) == acl(source.conn, signature)
    for app in apps:
        assert target.conn.execute(
            "SELECT has_function_privilege(%s, 'public.pgstattuple(regclass)','EXECUTE')", (app,)
        ).fetchone() == (True,)
        assert target.conn.execute(
            "SELECT has_table_privilege(%s,'ordinary.data','SELECT')", (app,)
        ).fetchone() == (True,)
    routine_grants = acl(target.conn, "ordinary.pg_relpages(regclass)")
    for app in apps:
        assert any(grant[1:] == (app, "EXECUTE", True) for grant in routine_grants)
    with target.conn.transaction(force_rollback=True):
        target.conn.execute("CREATE TABLE ordinary.future_permissions (value text)")
        for app in apps:
            assert target.conn.execute(
                "SELECT has_table_privilege(%s,'ordinary.future_permissions','SELECT')", (app,)
            ).fetchone() == (True,)
    assert target.conn.execute("SELECT value FROM ordinary.data").fetchall() == [("retained",)]
    assert target.snapshot() == source.snapshot()


@pytest.mark.parametrize("provider", ["full"], indirect=True)
def test_migration_restores_provider_and_application_permissions(
    provider: ProviderFixture, tmp_path: Path
) -> None:
    source, target = provider.stores
    report = run_migration(source, target, tmp_path / "copy.dump", "provider", 16, 17)
    assert report["status"] == "verified"
    assert_permissions(source, target, provider.apps)
    assert target.read_checkpoint() == report


@pytest.mark.parametrize("provider", ["full"], indirect=True)
def test_extension_application_grant_option_survives_restore(
    provider: ProviderFixture, tmp_path: Path
) -> None:
    source, target = provider.stores
    source.conn.execute("SET ROLE azure_pg_admin")
    source.conn.execute(
        sql.SQL(
            "GRANT EXECUTE ON FUNCTION public.pgstattuple(regclass) TO {} WITH GRANT OPTION"
        ).format(sql.Identifier(provider.apps[1]))
    )
    source.conn.execute("RESET ROLE")
    run_migration(source, target, tmp_path / "copy.dump", "provider", 16, 17)
    assert ("azure_pg_admin", provider.apps[1], "EXECUTE", True) in acl(
        target.conn, "public.pgstattuple(regclass)"
    )
    assert_permissions(source, target, provider.apps)


@pytest.mark.parametrize("provider", ["rehearsal"], indirect=True)
def test_seed_and_migration_restore_preserve_serving_permissions(
    provider: ProviderFixture, tmp_path: Path
) -> None:
    source, target = provider.stores

    class PreparedDatabases(RehearsalDatabases):
        def create(self) -> None:
            super().create()
            for index, name in enumerate(self.names):
                provider.install(index, name)

    admin = PreparedDatabases(source, target, "g17", "r602")
    before = source.snapshot()
    original_acl = acl(source.conn, "public.pgstattuple(regclass)")
    try:
        report = execute_rehearsal(
            admin, tmp_path, image="test:fixed", job_started=time.monotonic()
        )
        assert report["status"] == "rehearsal_verified"
        clones = [admin.connect(index) for index in (0, 1)]
        try:
            assert_permissions(*clones, provider.apps)
        finally:
            for clone in clones:
                clone.close()
        assert source.snapshot() == before
        assert acl(source.conn, "public.pgstattuple(regclass)") == original_acl
        assert target.read_checkpoint() is None
        assert target.snapshot()["tables"] == {}
    finally:
        admin.cleanup()
    for store, name in zip(provider.stores, admin.names, strict=True):
        assert (
            store.conn.execute("SELECT 1 FROM pg_database WHERE datname=%s", (name,)).fetchone()
            is None
        )


@pytest.mark.parametrize(
    "drift,provider",
    [("application", "catalog"), ("provider", "catalog_bare"), ("owner", "catalog_bare")],
    indirect=["provider"],
)
def test_extension_permission_drift_changes_snapshot(provider: ProviderFixture, drift: str) -> None:
    source = provider.stores[0]
    before = source.snapshot()
    with provider.connect_admin(0) as conn:
        if drift == "application":
            conn.execute("SET ROLE azure_pg_admin")
            conn.execute(
                sql.SQL("REVOKE EXECUTE ON FUNCTION public.pgstattuple(regclass) FROM {}").format(
                    sql.Identifier(provider.apps[0])
                )
            )
        elif drift == "provider":
            conn.execute(
                "REVOKE GRANT OPTION FOR EXECUTE ON FUNCTION public.pg_relpages(regclass) "
                "FROM azure_pg_admin CASCADE"
            )
        else:
            conn.execute(
                sql.SQL("ALTER FUNCTION public.pg_relpages(regclass) OWNER TO {}").format(
                    sql.Identifier(provider.administrator)
                )
            )
    assert source.snapshot() != before, (
        "extension permission drift must participate in reconciliation"
    )


@pytest.mark.parametrize("drift", ["provider", "unexpected_recipient", "owner"])
def test_restore_rejects_unexpected_target_permissions(
    provider: ProviderFixture, tmp_path: Path, drift: str
) -> None:
    source, target = provider.stores
    with provider.connect_admin(1) as conn:
        if drift == "provider":
            conn.execute(
                "REVOKE GRANT OPTION FOR EXECUTE ON FUNCTION public.pg_relpages(regclass) "
                "FROM azure_pg_admin"
            )
        elif drift == "unexpected_recipient":
            conn.execute("SET ROLE azure_pg_admin")
            conn.execute("GRANT EXECUTE ON FUNCTION public.pg_relpages(regclass) TO PUBLIC")
        else:
            conn.execute(
                sql.SQL("ALTER FUNCTION public.pg_relpages(regclass) OWNER TO {}").format(
                    sql.Identifier(provider.administrator)
                )
            )
    with pytest.raises(ValueError, match="provider extension"):
        run_migration(source, target, tmp_path / "copy.dump", "provider", 16, 17)
    assert target.read_checkpoint() is None
    for app in provider.apps:
        assert source.conn.execute(
            "SELECT has_database_privilege(%s,current_database(),'CONNECT')", (app,)
        ).fetchone() == (False,)


def test_dump_permissions_and_archive_share_snapshot(
    provider: ProviderFixture, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, target = provider.stores
    original = source._command
    changed = []

    def concurrent_grant_change(argv: list[str]) -> bytes:
        if Path(argv[0]).name == "pg_dump":
            with provider.connect_admin(0) as conn:
                conn.execute(
                    sql.SQL("REVOKE SELECT ON ordinary.data FROM {}").format(
                        sql.Identifier(provider.apps[0])
                    )
                )
            changed.append(True)
        return original(argv)

    monkeypatch.setattr(source, "_command", concurrent_grant_change)
    path = tmp_path / "copy.dump"
    manifest = source.dump(path)
    assert changed == [True]
    target.restore(path, manifest)
    assert source.conn.execute(
        "SELECT has_table_privilege(%s,'ordinary.data','SELECT')", (provider.apps[0],)
    ).fetchone() == (False,)
    assert target.conn.execute(
        "SELECT has_table_privilege(%s,'ordinary.data','SELECT')", (provider.apps[0],)
    ).fetchone() == (True,)


def test_archive_identity_mismatch_precedes_target_write(
    provider: ProviderFixture, tmp_path: Path
) -> None:
    source, target = provider.stores
    path = tmp_path / "copy.dump"
    manifest = source.dump(path)
    path.write_bytes(path.read_bytes() + b"different archive")
    before = target.snapshot()
    with pytest.raises(ValueError, match="archive does not match"):
        target.restore(path, manifest)
    assert target.snapshot() == before
    assert target.read_checkpoint() is None


def test_same_named_non_extension_function_is_never_exempted(
    provider: ProviderFixture, tmp_path: Path
) -> None:
    source, target = provider.stores
    source.conn.execute(
        "GRANT EXECUTE ON FUNCTION ordinary.pg_relpages(regclass) "
        "TO azure_pg_admin WITH GRANT OPTION"
    )
    expected = acl(source.conn, "ordinary.pg_relpages(regclass)")
    assert (provider.administrator, "azure_pg_admin", "EXECUTE", True) in expected
    report = run_migration(source, target, tmp_path / "copy.dump", "provider", 16, 17)
    assert report["status"] == "verified"
    assert acl(target.conn, "ordinary.pg_relpages(regclass)") == expected
    assert target.conn.execute(
        "SELECT pg_get_userbyid(proowner) FROM pg_proc "
        "WHERE oid='ordinary.pg_relpages(regclass)'::regprocedure"
    ).fetchone() == (provider.administrator,)


@pytest.mark.parametrize("surface", ["table", "default"])
@pytest.mark.parametrize("recipient", ["application", "public"])
def test_resume_rejects_added_maintain_for_non_owner(
    provider: ProviderFixture, tmp_path: Path, recipient: str, surface: str
) -> None:
    source, target = provider.stores
    report = run_migration(source, target, tmp_path / "copy.dump", "provider", 16, 17)
    role = sql.Identifier(provider.apps[0]) if recipient == "application" else sql.SQL("PUBLIC")
    command = (
        "GRANT MAINTAIN ON ordinary.data TO {}"
        if surface == "table"
        else "ALTER DEFAULT PRIVILEGES IN SCHEMA ordinary GRANT MAINTAIN ON TABLES TO {}"
    )
    target.conn.execute(sql.SQL(command).format(role))
    with pytest.raises(ValueError, match="resume reconciliation failed"):
        run_migration(source, target, tmp_path / "retry.dump", "provider", 16, 17)
    assert target.read_checkpoint() == report
    assert not (tmp_path / "retry.dump").exists()


def test_cross_major_global_default_owner_permissions(
    provider: ProviderFixture, tmp_path: Path
) -> None:
    source, target = provider.stores
    source.conn.execute(
        sql.SQL("ALTER DEFAULT PRIVILEGES GRANT SELECT ON TABLES TO {}").format(
            sql.Identifier(provider.apps[0])
        )
    )
    report = run_migration(source, target, tmp_path / "copy.dump", "provider", 16, 17)
    assert report["status"] == "verified"
    assert source.snapshot()["default_grants"] == target.snapshot()["default_grants"]
    target.conn.execute("CREATE TABLE public.future_data (value text)")
    assert target.conn.execute(
        "SELECT has_table_privilege(%s,'public.future_data','SELECT')", (provider.apps[0],)
    ).fetchone() == (True,)
    assert target.conn.execute(
        "SELECT has_table_privilege(%s,'public.future_data','MAINTAIN')", (provider.apps[0],)
    ).fetchone() == (False,)


@pytest.mark.parametrize("provider", ["quoted_public"], indirect=True)
def test_literal_public_role_is_not_public_access(
    provider: ProviderFixture, tmp_path: Path
) -> None:
    source, target = provider.stores
    run_migration(source, target, tmp_path / "copy.dump", "provider", 16, 17)
    for store in (source, target):
        assert store.conn.execute(
            "SELECT EXISTS(SELECT 1 FROM pg_proc p, aclexplode(p.proacl) a "
            "WHERE p.oid='public.pgstattuple(regclass)'::regprocedure AND a.grantee=0)"
        ).fetchone() == (False,)
        assert store.conn.execute(
            "SELECT has_function_privilege(%s,'public.pgstattuple(regclass)','EXECUTE')",
            ("PUBLIC",),
        ).fetchone() == (True,)


@pytest.mark.parametrize("corruption", ["missing", "duplicate"])
def test_archive_acl_mapping_must_be_complete_and_unique(
    provider: ProviderFixture, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, corruption: str
) -> None:
    source, target = provider.stores
    path = tmp_path / "copy.dump"
    manifest = source.dump(path)
    original = target._command
    altered = []

    def ambiguous_listing(argv: list[str]) -> bytes:
        output = original(argv)
        if "--list" in argv:
            lines = output.decode().splitlines()
            matching = [
                line
                for line in lines
                if "ACL public FUNCTION pg_relpages(relname regclass)" in line
            ]
            assert len(matching) == 1
            line = matching[0]
            lines.remove(line) if corruption == "missing" else lines.append(line)
            altered.append(True)
            return ("\n".join(lines) + "\n").encode()
        return output

    monkeypatch.setattr(target, "_command", ambiguous_listing)
    before = target.snapshot()
    with pytest.raises(ValueError, match="mapping is incomplete or ambiguous"):
        target.restore(path, manifest)
    assert altered == [True]
    assert target.snapshot() == before
    assert target.read_checkpoint() is None


def test_unknown_source_provider_grant_is_rejected_before_restore(
    provider: ProviderFixture, tmp_path: Path
) -> None:
    source, target = provider.stores
    with provider.connect_admin(0) as conn:
        conn.execute(
            sql.SQL("GRANT EXECUTE ON FUNCTION public.pg_relpages(regclass) TO {}").format(
                sql.Identifier(provider.apps[0])
            )
        )
    path = tmp_path / "copy.dump"
    manifest = source.dump(path)
    before = target.snapshot()
    with pytest.raises(ValueError, match="provider extension"):
        target.restore(path, manifest)
    assert target.snapshot() == before
    assert target.read_checkpoint() is None


@pytest.mark.parametrize("change", ["grant_option", "partial_owner"])
def test_owner_maintain_exception_does_not_hide_other_owner_grants(
    provider: ProviderFixture, tmp_path: Path, change: str
) -> None:
    source, target = provider.stores
    if change == "partial_owner":
        source.conn.execute(
            sql.SQL("REVOKE INSERT ON ordinary.data FROM {}").format(
                sql.Identifier(provider.administrator)
            )
        )
    if change == "partial_owner":
        # The newer dump client also adds MAINTAIN to this partial owner ACL.
        # It is deliberately outside the ALL-grant equivalence and must be rejected.
        with pytest.raises(ValueError, match="permission reconciliation failed"):
            run_migration(source, target, tmp_path / "copy.dump", "provider", 16, 17)
        assert target.read_checkpoint() is None
        return
    report = run_migration(source, target, tmp_path / "copy.dump", "provider", 16, 17)
    target.conn.execute(
        sql.SQL("GRANT MAINTAIN ON ordinary.data TO {}{}").format(
            sql.Identifier(provider.administrator),
            sql.SQL(" WITH GRANT OPTION" if change == "grant_option" else ""),
        )
    )
    with pytest.raises(ValueError, match="resume reconciliation failed"):
        run_migration(source, target, tmp_path / "retry.dump", "provider", 16, 17)
    assert target.read_checkpoint() == report
    assert not (tmp_path / "retry.dump").exists()


def test_column_grants_preserve_recipient_and_grant_option(
    provider: ProviderFixture, tmp_path: Path
) -> None:
    source, target = provider.stores
    source.conn.execute(
        sql.SQL("GRANT UPDATE (value) ON ordinary.data TO {} WITH GRANT OPTION").format(
            sql.Identifier(provider.apps[0])
        )
    )
    run_migration(source, target, tmp_path / "copy.dump", "column", 16, 17)
    for store in (source, target):
        assert store.conn.execute(
            "SELECT has_column_privilege(%s,'ordinary.data','value','UPDATE WITH GRANT OPTION'), "
            "has_table_privilege(%s,'ordinary.data','UPDATE')",
            (provider.apps[0], provider.apps[0]),
        ).fetchone() == (True, False)
    assert source.snapshot() == target.snapshot()


@pytest.mark.parametrize("stage", ["restore", "resume"])
@pytest.mark.parametrize("change", ["added", "grant_option"])
def test_column_permission_drift_prevents_verification(
    provider: ProviderFixture,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    stage: str,
    change: str,
) -> None:
    source, target = provider.stores
    role = sql.Identifier(provider.apps[0])
    if change == "grant_option":
        source.conn.execute(
            sql.SQL("GRANT UPDATE (value) ON ordinary.data TO {} WITH GRANT OPTION").format(role)
        )
    command = (
        "GRANT UPDATE (value) ON ordinary.data TO {}"
        if change == "added"
        else "REVOKE GRANT OPTION FOR UPDATE (value) ON ordinary.data FROM {}"
    )
    checkpoint = None
    changed = []
    if stage == "resume":
        checkpoint = run_migration(source, target, tmp_path / "copy.dump", "column", 16, 17)
        target.conn.execute(sql.SQL(command).format(role))
        changed.append(True)
    else:
        original = target._command

        def restore_with_column_change(argv: list[str]) -> bytes:
            result = original(argv)
            if Path(argv[0]).name == "pg_restore" and "--dbname" in argv:
                target.conn.execute(sql.SQL(command).format(role))
                changed.append(True)
            return result

        monkeypatch.setattr(target, "_command", restore_with_column_change)
    with pytest.raises(
        ValueError, match="permission reconciliation failed|resume reconciliation failed"
    ):
        run_migration(source, target, tmp_path / "attempt.dump", "column", 16, 17)
    assert changed == [True]
    assert target.read_checkpoint() == checkpoint
    assert source.snapshot() != target.snapshot()


@pytest.fixture
def row_security(provider: ProviderFixture) -> ProviderFixture:
    source = provider.stores[0]
    role = sql.Identifier(provider.apps[0])
    source.conn.execute(sql.SQL("GRANT USAGE ON SCHEMA ordinary TO {}").format(role))
    source.conn.execute(sql.SQL("GRANT INSERT ON ordinary.data TO {}").format(role))
    source.conn.execute("ALTER TABLE ordinary.data ENABLE ROW LEVEL SECURITY")
    source.conn.execute(
        sql.SQL(
            "CREATE POLICY owner_access ON ordinary.data TO {} USING (true) WITH CHECK (true)"
        ).format(sql.Identifier(provider.administrator))
    )
    source.conn.execute(
        sql.SQL(
            "CREATE POLICY app_access ON ordinary.data TO {} "
            "USING (value = 'allowed') WITH CHECK (value = 'allowed')"
        ).format(role)
    )
    return provider


def test_row_security_preserves_application_read_and_write_restrictions(
    row_security: ProviderFixture, tmp_path: Path
) -> None:
    source, target = row_security.stores
    run_migration(source, target, tmp_path / "copy.dump", "security", 16, 17)
    for index in (0, 1):
        with row_security.connect_admin(index) as conn, conn.transaction(force_rollback=True):
            conn.execute(sql.SQL("SET LOCAL ROLE {}").format(sql.Identifier(row_security.apps[0])))
            assert conn.execute("SELECT value FROM ordinary.data").fetchall() == []
            conn.execute("INSERT INTO ordinary.data VALUES ('allowed')")
            assert conn.execute("SELECT value FROM ordinary.data").fetchall() == [("allowed",)]
            with pytest.raises(psycopg.errors.InsufficientPrivilege, match="row-level security"):
                conn.execute("INSERT INTO ordinary.data VALUES ('blocked')")
    assert source.snapshot() == target.snapshot()


@pytest.mark.parametrize("stage", ["restore", "resume"])
@pytest.mark.parametrize(
    "change", ["enabled", "forced", "roles", "using", "check", "command", "mode"]
)
def test_row_security_drift_prevents_verification(
    row_security: ProviderFixture,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    stage: str,
    change: str,
) -> None:
    source, target = row_security.stores
    role = sql.Identifier(row_security.apps[0])
    if change == "command":
        source.conn.execute("DROP POLICY app_access ON ordinary.data")
        source.conn.execute(
            sql.SQL(
                "CREATE POLICY app_access ON ordinary.data FOR SELECT TO {} "
                "USING (value = 'allowed')"
            ).format(role)
        )
    changes = {
        "enabled": "ALTER TABLE ordinary.data DISABLE ROW LEVEL SECURITY",
        "forced": "ALTER TABLE ordinary.data FORCE ROW LEVEL SECURITY",
        "roles": "ALTER POLICY app_access ON ordinary.data TO PUBLIC",
        "using": "ALTER POLICY app_access ON ordinary.data USING (true)",
        "check": "ALTER POLICY app_access ON ordinary.data WITH CHECK (true)",
        "command": "DROP POLICY app_access ON ordinary.data; CREATE POLICY app_access "
        "ON ordinary.data FOR DELETE TO {} USING (value = 'allowed')",
        "mode": "DROP POLICY app_access ON ordinary.data; CREATE POLICY app_access "
        "ON ordinary.data AS RESTRICTIVE TO {} USING (value = 'allowed') "
        "WITH CHECK (value = 'allowed')",
    }
    checkpoint = None
    changed = []

    def apply_change() -> None:
        target.conn.execute(sql.SQL(changes[change]).format(role))
        if change == "forced":
            assert target.conn.execute("SELECT value FROM ordinary.data").fetchall() == [
                ("retained",)
            ]
        changed.append(True)

    if stage == "resume":
        checkpoint = run_migration(source, target, tmp_path / "copy.dump", "security", 16, 17)
        apply_change()
    else:
        original = target._command

        def restore_with_security_change(argv: list[str]) -> bytes:
            result = original(argv)
            if Path(argv[0]).name == "pg_restore" and "--dbname" in argv:
                apply_change()
            return result

        monkeypatch.setattr(target, "_command", restore_with_security_change)
    with pytest.raises(
        ValueError, match="permission reconciliation failed|resume reconciliation failed"
    ):
        run_migration(source, target, tmp_path / "attempt.dump", "security", 16, 17)
    assert changed == [True]
    assert target.read_checkpoint() == checkpoint


@pytest.fixture(params=["enum", "domain", "composite"])
def type_permissions(provider: ProviderFixture, request: pytest.FixtureRequest) -> ProviderFixture:
    source = provider.stores[0]
    definitions = {
        "enum": "CREATE TYPE ordinary.status AS ENUM ('retained')",
        "domain": "CREATE DOMAIN ordinary.status AS text",
        "composite": "CREATE TYPE ordinary.status AS (label text)",
    }
    source.conn.execute(definitions[request.param])
    source.conn.execute("REVOKE ALL ON TYPE ordinary.status FROM PUBLIC")
    source.conn.execute(
        sql.SQL("GRANT USAGE ON TYPE ordinary.status TO {} WITH GRANT OPTION").format(
            sql.Identifier(provider.apps[0])
        )
    )
    source.conn.execute("ALTER TABLE ordinary.data ADD COLUMN state ordinary.status")
    return provider


def test_type_permissions_preserve_application_grants(
    type_permissions: ProviderFixture, tmp_path: Path
) -> None:
    source, target = type_permissions.stores
    run_migration(source, target, tmp_path / "copy.dump", "types", 16, 17)
    for store in (source, target):
        assert store.conn.execute(
            "SELECT has_type_privilege(%s,'ordinary.status','USAGE WITH GRANT OPTION'), "
            "has_type_privilege(%s,'ordinary.status','USAGE')",
            type_permissions.apps,
        ).fetchone() == (True, False)
    assert source.snapshot() == target.snapshot()


@pytest.mark.parametrize("stage", ["restore", "resume"])
@pytest.mark.parametrize("change", ["recipient", "grant_option", "owner"])
def test_type_permission_drift_prevents_verification(
    type_permissions: ProviderFixture,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    stage: str,
    change: str,
) -> None:
    source, target = type_permissions.stores
    if change == "owner":
        source.conn.execute("GRANT CREATE ON SCHEMA ordinary TO azure_pg_admin")
    commands = {
        "recipient": sql.SQL("GRANT USAGE ON TYPE ordinary.status TO {}").format(
            sql.Identifier(type_permissions.apps[1])
        ),
        "grant_option": sql.SQL(
            "REVOKE GRANT OPTION FOR USAGE ON TYPE ordinary.status FROM {}"
        ).format(sql.Identifier(type_permissions.apps[0])),
        "owner": sql.SQL("ALTER TYPE ordinary.status OWNER TO azure_pg_admin"),
    }
    checkpoint = None
    changed = []
    if stage == "resume":
        checkpoint = run_migration(source, target, tmp_path / "copy.dump", "types", 16, 17)
        target.conn.execute(commands[change])
        changed.append(True)
    else:
        original = target._command

        def restore_with_type_change(argv: list[str]) -> bytes:
            result = original(argv)
            if Path(argv[0]).name == "pg_restore" and "--dbname" in argv:
                target.conn.execute(commands[change])
                changed.append(True)
            return result

        monkeypatch.setattr(target, "_command", restore_with_type_change)
    with pytest.raises(
        ValueError, match="permission reconciliation failed|resume reconciliation failed"
    ):
        run_migration(source, target, tmp_path / "attempt.dump", "types", 16, 17)
    assert changed == [True]
    assert target.read_checkpoint() == checkpoint


def test_native_row_type_acl_loss_cannot_be_certified(
    provider: ProviderFixture, tmp_path: Path
) -> None:
    source, target = provider.stores
    source.conn.execute("REVOKE ALL ON TYPE ordinary.data FROM PUBLIC")
    assert source.conn.execute(
        "SELECT has_type_privilege(%s,'ordinary.data','USAGE')", (provider.apps[0],)
    ).fetchone() == (False,)
    with pytest.raises(ValueError, match="permission reconciliation failed"):
        run_migration(source, target, tmp_path / "copy.dump", "row_type", 16, 17)
    assert target.read_checkpoint() is None
    assert target.conn.execute(
        "SELECT has_type_privilege(%s,'ordinary.data','USAGE')", (provider.apps[0],)
    ).fetchone() == (True,)


@pytest.mark.parametrize("stage", ["restore", "resume"])
def test_row_type_permission_drift_prevents_verification(
    provider: ProviderFixture, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stage: str
) -> None:
    source, target = provider.stores
    checkpoint = None
    changed = []
    if stage == "resume":
        checkpoint = run_migration(source, target, tmp_path / "copy.dump", "row_type", 16, 17)
        target.conn.execute("REVOKE ALL ON TYPE ordinary.data FROM PUBLIC")
        changed.append(True)
    else:
        original = target._command

        def restore_with_row_type_change(argv: list[str]) -> bytes:
            result = original(argv)
            if Path(argv[0]).name == "pg_restore" and "--dbname" in argv:
                target.conn.execute("REVOKE ALL ON TYPE ordinary.data FROM PUBLIC")
                changed.append(True)
            return result

        monkeypatch.setattr(target, "_command", restore_with_row_type_change)
    with pytest.raises(
        ValueError, match="permission reconciliation failed|resume reconciliation failed"
    ):
        run_migration(source, target, tmp_path / "attempt.dump", "row_type", 16, 17)
    assert changed == [True]
    assert target.read_checkpoint() == checkpoint
    assert source.conn.execute(
        "SELECT has_type_privilege(%s,'ordinary.data','USAGE')", (provider.apps[0],)
    ).fetchone() == (True,)
    assert target.conn.execute(
        "SELECT has_type_privilege(%s,'ordinary.data','USAGE')", (provider.apps[0],)
    ).fetchone() == (False,)


@pytest.fixture
def permission_object(
    provider: ProviderFixture, surface: str
) -> tuple[ProviderFixture, str, sql.Composable, str, sql.Composable]:
    source, target = provider.stores
    for app in provider.apps:
        source.conn.execute(
            sql.SQL("GRANT CREATE, USAGE ON SCHEMA ordinary TO {}").format(sql.Identifier(app))
        )
    if surface == "large_object":
        oid = source.conn.execute("SELECT lo_from_bytea(0,'private-data'::bytea)").fetchone()[0]
        source.conn.execute("ALTER TABLE ordinary.data ADD COLUMN blob_oid oid")
        source.conn.execute("UPDATE ordinary.data SET blob_oid=%s", (oid,))
        kind, identity, privilege = "LARGE OBJECT", sql.Literal(oid), "SELECT"
        operation = sql.SQL("SELECT lo_get({})").format(sql.Literal(oid))
    elif surface == "language":
        for index in (0, 1):
            with provider.connect_admin(index) as conn:
                conn.execute(
                    sql.SQL("ALTER LANGUAGE plpgsql OWNER TO {}").format(
                        sql.Identifier(provider.administrator)
                    )
                )
        kind, identity, privilege = "LANGUAGE", sql.Identifier("plpgsql"), "USAGE"
        operation = sql.SQL(
            "CREATE FUNCTION ordinary.app_function() RETURNS int "
            "LANGUAGE plpgsql AS $$BEGIN RETURN 1; END$$"
        )
    elif surface == "vector_type":
        for index in (0, 1):
            with provider.connect_admin(index) as conn:
                conn.execute(
                    sql.SQL("ALTER TYPE public.vector OWNER TO {}").format(
                        sql.Identifier(provider.administrator)
                    )
                )
        kind, identity, privilege = "TYPE", sql.Identifier("public", "vector"), "USAGE"
        operation = sql.SQL("CREATE TABLE ordinary.app_vector (embedding public.vector(3))")
    elif surface == "extension_aggregate":
        kind, identity, privilege = "FUNCTION", sql.SQL("public.avg(public.vector)"), "EXECUTE"
        operation = sql.SQL("SELECT public.avg(v) FROM (VALUES ('[1,2,3]'::public.vector)) x(v)")
    else:
        for store in (source, target):
            store.conn.execute("CREATE TABLE public.extension_data (value text)")
            store.conn.execute("INSERT INTO public.extension_data VALUES ('installed')")
            store.conn.execute("ALTER EXTENSION pgstattuple ADD TABLE public.extension_data")
        kind, identity, privilege = "TABLE", sql.Identifier("public", "extension_data"), "SELECT"
        operation = sql.SQL("SELECT value FROM public.extension_data")
    source.conn.execute(sql.SQL("REVOKE ALL ON {} {} FROM PUBLIC").format(sql.SQL(kind), identity))
    source.conn.execute(
        sql.SQL("GRANT {} ON {} {} TO {} WITH GRANT OPTION").format(
            sql.SQL(privilege), sql.SQL(kind), identity, sql.Identifier(provider.apps[0])
        )
    )
    return provider, kind, identity, privilege, operation


_PERMISSION_SURFACES = [
    ("large_object", "full"),
    ("language", "full"),
    ("vector_type", "rehearsal"),
    ("extension_aggregate", "rehearsal"),
    ("extension_relation", "full"),
]


@pytest.mark.parametrize("surface,provider", _PERMISSION_SURFACES, indirect=["provider"])
def test_additional_permissions_preserve_real_application_access(
    permission_object: tuple[ProviderFixture, str, sql.Composable, str, sql.Composable],
    tmp_path: Path,
) -> None:
    provider, _, _, _, operation = permission_object
    source, target = provider.stores
    run_migration(source, target, tmp_path / "copy.dump", "permissions", 16, 17)
    for index in (0, 1):
        with provider.connect_admin(index) as conn:
            for app, allowed in zip(provider.apps, (True, False), strict=True):
                if allowed:
                    with conn.transaction(force_rollback=True):
                        conn.execute(sql.SQL("SET LOCAL ROLE {}").format(sql.Identifier(app)))
                        conn.execute(operation)
                else:
                    with pytest.raises(psycopg.errors.InsufficientPrivilege):
                        with conn.transaction(force_rollback=True):
                            conn.execute(sql.SQL("SET LOCAL ROLE {}").format(sql.Identifier(app)))
                            conn.execute(operation)
    assert source.snapshot() == target.snapshot()


@pytest.mark.parametrize("surface,provider", _PERMISSION_SURFACES, indirect=["provider"])
@pytest.mark.parametrize("stage", ["restore", "resume"])
@pytest.mark.parametrize("change", ["recipient", "grant_option"])
def test_additional_permission_drift_prevents_verification(
    permission_object: tuple[ProviderFixture, str, sql.Composable, str, sql.Composable],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    stage: str,
    change: str,
) -> None:
    provider, kind, identity, privilege, _ = permission_object
    source, target = provider.stores
    command = (
        sql.SQL("GRANT {} ON {} {} TO {}").format(
            sql.SQL(privilege), sql.SQL(kind), identity, sql.Identifier(provider.apps[1])
        )
        if change == "recipient"
        else sql.SQL("REVOKE GRANT OPTION FOR {} ON {} {} FROM {}").format(
            sql.SQL(privilege), sql.SQL(kind), identity, sql.Identifier(provider.apps[0])
        )
    )
    checkpoint = None
    changed = []
    if stage == "resume":
        checkpoint = run_migration(source, target, tmp_path / "copy.dump", "permissions", 16, 17)
        target.conn.execute(command)
        changed.append(True)
    else:
        original = target._command

        def restore_with_permission_change(argv: list[str]) -> bytes:
            result = original(argv)
            if Path(argv[0]).name == "pg_restore" and "--dbname" in argv:
                target.conn.execute(command)
                changed.append(True)
            return result

        monkeypatch.setattr(target, "_command", restore_with_permission_change)
    with pytest.raises(
        ValueError, match="permission reconciliation failed|resume reconciliation failed"
    ):
        run_migration(source, target, tmp_path / "attempt.dump", "permissions", 16, 17)
    assert changed == [True]
    assert target.read_checkpoint() == checkpoint


def test_foreign_wrapper_is_rejected_as_unsupported_before_restore(
    provider: ProviderFixture, tmp_path: Path
) -> None:
    source, target = provider.stores
    with provider.connect_admin(0) as conn:
        conn.execute("CREATE FOREIGN DATA WRAPPER workload_fdw NO HANDLER NO VALIDATOR")
    before = target.snapshot()
    with pytest.raises(ValueError, match="foreign-data wrappers"):
        run_migration(source, target, tmp_path / "copy.dump", "foreign", 16, 17)
    assert target.snapshot() == before
    assert target.read_checkpoint() is None


@pytest.mark.parametrize("surface,provider", [("large_object", "full")], indirect=["provider"])
@pytest.mark.parametrize("stage", ["restore", "resume"])
def test_large_object_content_drift_prevents_verification(
    permission_object: tuple[ProviderFixture, str, sql.Composable, str, sql.Composable],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    stage: str,
) -> None:
    provider, _, _, _, _ = permission_object
    source, target = provider.stores
    oid = source.conn.execute("SELECT blob_oid FROM ordinary.data").fetchone()[0]
    checkpoint = None
    changed = []

    def change_content() -> None:
        target.conn.execute("SELECT lo_put(%s,0,'changed-data'::bytea)", (oid,))
        changed.append(True)

    if stage == "resume":
        checkpoint = run_migration(source, target, tmp_path / "copy.dump", "blob", 16, 17)
        change_content()
    else:
        original = target._command

        def restore_with_change(argv: list[str]) -> bytes:
            result = original(argv)
            if Path(argv[0]).name == "pg_restore" and "--dbname" in argv:
                change_content()
            return result

        monkeypatch.setattr(target, "_command", restore_with_change)
    with pytest.raises(ValueError, match="reconciliation failed"):
        run_migration(source, target, tmp_path / "attempt.dump", "blob", 16, 17)
    assert changed == [True]
    assert target.read_checkpoint() == checkpoint


def test_existing_large_object_rejects_target_before_fencing(
    provider: ProviderFixture,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source, target = provider.stores
    target.conn.execute("SELECT lo_from_bytea(0,'existing'::bytea)")
    before = target.snapshot()
    fenced = []
    monkeypatch.setattr(source, "fence", lambda: fenced.append(True))
    with pytest.raises(ValueError, match="target contains existing workload objects"):
        run_migration(source, target, tmp_path / "copy.dump", "blob", 16, 17)
    assert fenced == []
    assert target.snapshot() == before
    assert target.read_checkpoint() is None


@pytest.mark.parametrize("surface,provider", _PERMISSION_SURFACES[:3], indirect=["provider"])
@pytest.mark.parametrize("stage", ["restore", "resume"])
def test_additional_object_owner_drift_prevents_verification(
    permission_object: tuple[ProviderFixture, str, sql.Composable, str, sql.Composable],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    stage: str,
) -> None:
    provider, kind, identity, _, _ = permission_object
    source, target = provider.stores
    checkpoint = None
    changed = []

    def change_owner() -> None:
        with provider.connect_admin(1) as conn:
            conn.execute(
                sql.SQL("ALTER {} {} OWNER TO {}").format(
                    sql.SQL(kind), identity, sql.Identifier(provider.apps[1])
                )
            )
            if kind == "LARGE OBJECT":
                conn.execute(
                    sql.SQL("GRANT SELECT ON LARGE OBJECT {} TO {}").format(
                        identity, sql.Identifier(provider.administrator)
                    )
                )
        changed.append(True)

    if stage == "resume":
        checkpoint = run_migration(source, target, tmp_path / "copy.dump", "owner", 16, 17)
        change_owner()
    else:
        original = target._command

        def restore_with_change(argv: list[str]) -> bytes:
            result = original(argv)
            if Path(argv[0]).name == "pg_restore" and "--dbname" in argv:
                change_owner()
            return result

        monkeypatch.setattr(target, "_command", restore_with_change)
    with pytest.raises(ValueError, match="reconciliation failed"):
        run_migration(source, target, tmp_path / "attempt.dump", "owner", 16, 17)
    assert changed == [True]
    assert target.read_checkpoint() == checkpoint


def install_trusted_extension(store: PostgresStore, owner: str) -> None:
    store.conn.execute(
        sql.SQL("GRANT CREATE ON DATABASE {} TO {}").format(
            sql.Identifier(store.database), sql.Identifier(owner)
        )
    )
    with store.conn.transaction():
        store.conn.execute(sql.SQL("SET LOCAL ROLE {}").format(sql.Identifier(owner)))
        store.conn.execute("CREATE EXTENSION hstore WITH SCHEMA public")


def trusted_extension_owner(store: PostgresStore) -> str:
    return store.conn.execute(
        "SELECT pg_get_userbyid(extowner) FROM pg_extension WHERE extname='hstore'"
    ).fetchone()[0]


@pytest.fixture
def owned_extension(provider: ProviderFixture, owner_kind: str) -> tuple[ProviderFixture, str]:
    owner = provider.apps[0] if owner_kind == "application" else provider.administrator
    for index in (0, 1):
        with provider.connect_admin(index) as conn:
            conn.execute(
                sql.SQL("GRANT {} TO {}").format(
                    sql.Identifier(provider.apps[0]), sql.Identifier(provider.administrator)
                )
            )
    install_trusted_extension(provider.stores[0], owner)
    assert trusted_extension_owner(provider.stores[0]) == owner
    return provider, owner


@pytest.mark.parametrize("provider", ["rehearsal"], indirect=True)
@pytest.mark.parametrize("owner_kind", ["application", "administrator"])
@pytest.mark.parametrize("flow", ["migration", "rehearsal"])
def test_extension_object_owner_is_preserved_when_prepared(
    owned_extension: tuple[ProviderFixture, str], tmp_path: Path, flow: str
) -> None:
    provider, owner = owned_extension
    source, target = provider.stores
    before = source.snapshot()

    def prepare(store: PostgresStore) -> None:
        if owner != provider.administrator:
            install_trusted_extension(store, owner)

    class PreparedDatabases(RehearsalDatabases):
        def create(self) -> None:
            super().create()
            for index, name in enumerate(self.names):
                provider.install(index, name)
                store = self.connect(index)
                try:
                    prepare(store)
                finally:
                    store.close()

    admin = PreparedDatabases(source, target, "g17", "r604")
    clones = []
    try:
        if flow == "migration":
            prepare(target)
            result = run_migration(source, target, tmp_path / "copy.dump", "owner", 16, 17)
            assert result["status"] == "verified"
            stores = (source, target)
        else:
            result = execute_rehearsal(
                admin, tmp_path, image="test:owner", job_started=time.monotonic()
            )
            assert result["status"] == "rehearsal_verified"
            clones = [admin.connect(index) for index in (0, 1)]
            stores = (source, *clones)
        for store in stores:
            assert trusted_extension_owner(store) == owner
            with store.conn.transaction(force_rollback=True):
                store.conn.execute(sql.SQL("SET LOCAL ROLE {}").format(sql.Identifier(owner)))
                store.conn.execute("ALTER EXTENSION hstore UPDATE")
        assert stores[-1].read_checkpoint()["status"] == "verified"
        assert source.snapshot() == before
    finally:
        for clone in clones:
            clone.close()
        if flow == "rehearsal":
            admin.cleanup()


@pytest.mark.parametrize("provider", ["rehearsal"], indirect=True)
@pytest.mark.parametrize("owner_kind", ["application"])
@pytest.mark.parametrize("flow", ["migration", "rehearsal"])
def test_native_extension_owner_loss_prevents_verification(
    owned_extension: tuple[ProviderFixture, str], tmp_path: Path, flow: str
) -> None:
    provider, owner = owned_extension
    source, target = provider.stores
    before = source.snapshot()
    source.assert_extensions_match(target)
    target.assert_restore_privileges(source.restore_owners())

    class PreparedDatabases(RehearsalDatabases):
        def create(self) -> None:
            super().create()
            for index, name in enumerate(self.names):
                provider.install(index, name)

    admin = PreparedDatabases(source, target, "g17", "r605")
    clones = []
    report = {}
    try:
        with pytest.raises(ValueError, match="permission reconciliation failed"):
            if flow == "migration":
                run_migration(source, target, tmp_path / "copy.dump", "owner_loss", 16, 17)
            else:
                execute_rehearsal(
                    admin, tmp_path, image="test:owner", job_started=time.monotonic(), report=report
                )
        if flow == "migration":
            restored = target
        else:
            assert report["status"] == "failed"
            assert report["stage"] == "seed_restore"
            clones = [admin.connect(index) for index in (0, 1)]
            restored = clones[0]
            assert clones[1].read_checkpoint() is None
        assert trusted_extension_owner(source) == owner
        assert trusted_extension_owner(restored) == provider.administrator
        assert restored.read_checkpoint() is None
        with source.conn.transaction(force_rollback=True):
            source.conn.execute(sql.SQL("SET LOCAL ROLE {}").format(sql.Identifier(owner)))
            source.conn.execute("ALTER EXTENSION hstore UPDATE")
        with pytest.raises(psycopg.errors.InsufficientPrivilege, match="must be owner"):
            with restored.conn.transaction(force_rollback=True):
                restored.conn.execute(sql.SQL("SET LOCAL ROLE {}").format(sql.Identifier(owner)))
                restored.conn.execute("ALTER EXTENSION hstore UPDATE")
        assert source.snapshot() == before
    finally:
        for clone in clones:
            clone.close()
        if flow == "rehearsal":
            admin.cleanup()


@pytest.mark.parametrize("provider", ["rehearsal"], indirect=True)
@pytest.mark.parametrize("owner_kind", ["administrator"])
@pytest.mark.parametrize("stage", ["restore", "resume"])
def test_extension_object_owner_drift_prevents_verification(
    owned_extension: tuple[ProviderFixture, str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    stage: str,
) -> None:
    provider, owner = owned_extension
    source, target = provider.stores
    checkpoint = None
    changed = []

    def change_owner() -> None:
        target.conn.execute("DROP EXTENSION hstore")
        install_trusted_extension(target, provider.apps[0])
        changed.append(True)

    if stage == "resume":
        checkpoint = run_migration(source, target, tmp_path / "copy.dump", "owner_drift", 16, 17)
        change_owner()
    else:
        original = target._command

        def restore_with_change(argv: list[str]) -> bytes:
            result = original(argv)
            if Path(argv[0]).name == "pg_restore" and "--dbname" in argv:
                change_owner()
            return result

        monkeypatch.setattr(target, "_command", restore_with_change)
    with pytest.raises(ValueError, match="reconciliation failed"):
        run_migration(source, target, tmp_path / "attempt.dump", "owner_drift", 16, 17)
    assert changed == [True]
    assert trusted_extension_owner(source) == owner
    assert trusted_extension_owner(target) == provider.apps[0]
    assert target.read_checkpoint() == checkpoint
    assert {k: v for k, v in source.snapshot().items() if k != "extension_objects"} == {
        k: v for k, v in target.snapshot().items() if k != "extension_objects"
    }
