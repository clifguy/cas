"""Opt-in real 16->17 restore; supply two disposable-test maintenance DSNs.

Never points at the configured SAGE database. Each endpoint gets a newly created
throwaway database; identities and data are isolated from every other test.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import Iterator
from pathlib import Path

import psycopg
import pytest
from psycopg import sql

from sage.maintenance.postgres_migration import PostgresStore, run_migration
from sage.storage.postgres.graph_store import PostgresGraphStore
from sage.storage.postgres.pool import pool_from_conninfo
from sage.storage.postgres.schema import schema_statements
from tests.helpers.pg_isolation import derive_throwaway_dbname, rewrite_dsn_dbname


async def initialize_graph(conninfo: str, schema: str) -> None:
    pool = pool_from_conninfo(conninfo, search_path=f"{schema},public")
    await pool.open()
    try:
        store = PostgresGraphStore(pool)
        await store.initialize()
        await store.close()
    finally:
        await pool.close()


@pytest.fixture
def databases() -> Iterator[tuple[PostgresStore, PostgresStore, str]]:
    dsns = [
        os.environ.get("SAGE_MIGRATION_TEST_SOURCE_DSN"),
        os.environ.get("SAGE_MIGRATION_TEST_TARGET_DSN"),
    ]
    if not all(dsns):
        pytest.skip("requires isolated 16 and 17 maintenance DSNs for the cross-major rehearsal")
    role = "migration-" + uuid.uuid4().hex[:12]
    names = [derive_throwaway_dbname(), derive_throwaway_dbname()]
    stores = []
    created = []
    try:
        for dsn, name in zip(dsns, names, strict=True):
            with psycopg.connect(dsn, autocommit=True) as admin:
                admin.execute(sql.SQL("CREATE ROLE {} LOGIN").format(sql.Identifier(role)))
                admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
                created.append((dsn, name))
            target = rewrite_dsn_dbname(dsn, name)
            with psycopg.connect(target, autocommit=True) as conn:
                conn.execute(
                    sql.SQL("GRANT CONNECT, CREATE ON DATABASE {} TO {}").format(
                        sql.Identifier(name), sql.Identifier(role)
                    )
                )
            stores.append(PostgresStore(target, (role,)))
        source, target = stores
        assert (source.major, target.major) == (16, 17), "rehearsal must actually cross 16 -> 17"
        conn = source.conn
        for schema in ("vault_alpha", "vault_beta"):
            for statement in schema_statements(schema):
                conn.execute(statement)
            asyncio.run(initialize_graph(source.conninfo, schema))
            conn.execute(
                sql.SQL(
                    """INSERT INTO {}.documents (id,
                        title,
                        source_type,
                        source_path,
                        source_content_hash,
                        created_by,
                        created_at,
                        last_modified_by,
                        adapter_version,
                        updated_at,
                        metadata_confirmed,
                        is_chain_head)
                        VALUES (%s,
                        %s,
                        %s,
                        %s,
                        %s,
                        %s,
                        %s,
                        %s,
                        '1.0',
                        '2026-01-02',
                        true,
                        false),
                        (%s,
                        %s,
                        %s,
                        %s,
                        %s,
                        %s,
                        %s,
                        %s,
                        '1.0',
                        '2026-01-02',
                        true,
                        true)"""
                ).format(sql.Identifier(schema)),
                (
                    "doc-old",
                    "Old",
                    "markdown",
                    "old.md",
                    "sha256:old",
                    "human",
                    "2026-01-01",
                    "human",
                    "doc-new",
                    "New",
                    "markdown",
                    "new.md",
                    "sha256:new",
                    "human",
                    "2026-01-02",
                    "human",
                ),
            )
            conn.execute(
                sql.SQL(
                    """INSERT INTO {}.edges (id,
                        source_id,
                        target_id,
                        edge_type,
                        created_at,
                        rationale)
                        VALUES ('edge',
                        'doc-new',
                        'doc-old',
                        'supersedes',
                        '2026-01-02',
                        'human decision')"""
                ).format(sql.Identifier(schema))
            )
            conn.execute(
                sql.SQL(
                    """INSERT INTO {}.staging_edges (id,
                        source_id,
                        target_id,
                        edge_type,
                        inference_evidence,
                        confidence_tier,
                        created_at)
                        VALUES ('pending',
                        'doc-new',
                        'doc-old',
                        'references',
                        'curated staging',
                        2,
                        '2026-01-02')"""
                ).format(sql.Identifier(schema))
            )
            conn.execute(
                sql.SQL(
                    """INSERT INTO {}.chunks (document_id,
                        heading_path,
                        indexed_structure,
                        content,
                        chunk_index,
                        embedding)
                        VALUES ('doc-new',
                        'Heading',
                        'Index',
                        'Body',
                        0,
                        %s::vector)"""
                ).format(sql.Identifier(schema)),
                ("[" + ",".join(["0.1"] * 768) + "]",),
            )
            conn.execute(
                sql.SQL(
                    "INSERT INTO {}.edges "
                    "(id,source_id,target_id,edge_type,created_at,retracted_edge_id) "
                    "VALUES ('reference','doc-new','doc-old','references','2026-01-02',NULL), "
                    "('retraction','doc-new',NULL,'retracts','2026-01-03','reference')"
                ).format(sql.Identifier(schema))
            )
        conn.execute("CREATE SCHEMA azure_archive")
        conn.execute("CREATE TABLE azure_archive.audit (entry text)")
        conn.execute("INSERT INTO azure_archive.audit VALUES ('ordinary workload data')")
        conn.execute("CREATE SCHEMA bff")
        conn.execute(
            "CREATE TABLE bff.sessions "
            "(id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY, payload jsonb)"
        )
        conn.execute('INSERT INTO bff.sessions (payload) VALUES (\'{"refresh":"sentinel"}\')')
        conn.execute(sql.SQL("ALTER TABLE bff.sessions OWNER TO {}").format(sql.Identifier(role)))
        conn.execute(sql.SQL("ALTER SCHEMA bff OWNER TO {}").format(sql.Identifier(role)))
        yield source, target, role
    finally:
        for store in stores:
            store.close()
        for dsn, name in created:
            with psycopg.connect(dsn, autocommit=True) as admin:
                admin.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name)))
                admin.execute(sql.SQL("DROP ROLE {}").format(sql.Identifier(role)))


def test_cross_major_restore_preserves_state(
    databases: tuple[PostgresStore, PostgresStore, str], tmp_path: Path
) -> None:
    source, target, role = databases
    assert target.snapshot()["tables"] == {}
    other_role = "other-" + uuid.uuid4().hex[:12]
    source.conn.execute(sql.SQL("CREATE ROLE {} LOGIN").format(sql.Identifier(other_role)))
    try:
        source.conn.execute(
            sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(
                sql.Identifier(source.database), sql.Identifier(other_role)
            )
        )
        with pytest.raises(ValueError, match="unaccounted"):
            source.fence()
    finally:
        source.conn.execute(
            sql.SQL("REVOKE CONNECT ON DATABASE {} FROM {}").format(
                sql.Identifier(source.database), sql.Identifier(other_role)
            )
        )
        source.conn.execute(sql.SQL("DROP ROLE {}").format(sql.Identifier(other_role)))
    observer = psycopg.connect(source.conninfo, autocommit=True)
    try:
        observer.execute("SELECT 1")
        report = run_migration(source, target, tmp_path / "copy.dump", "roundtrip", 16, 17)
        with pytest.raises(psycopg.OperationalError):
            observer.execute("SELECT 1")
    finally:
        observer.close()
    assert report["status"] == "verified"
    assert source.snapshot() == target.snapshot()
    for schema in ("vault_alpha", "vault_beta"):
        assert report["tables"][f"{schema}.documents"] == 2
        assert report["tables"][f"{schema}.edges"] == 3
        assert report["tables"][f"{schema}.staging_edges"] == 1
        assert report["tables"][f"{schema}.chunks"] == 1
    assert report["tables"]["bff.sessions"] == 1
    assert "azure_archive.audit" in report["tables"]
    assert report["tables"]["azure_archive.audit"] == 1
    for store in (source, target):
        assert store.conn.execute(
            "SELECT has_database_privilege(%s,current_database(),'CREATE')", (role,)
        ).fetchone()[0]
    assert (
        target.conn.execute(
            "SELECT retracted_edge_id FROM vault_beta.edges WHERE id='retraction'"
        ).fetchone()[0]
        == "reference"
    )

    assert (
        source.conn.execute(
            "SELECT has_database_privilege(%s,current_database(),'CONNECT')", (role,)
        ).fetchone()[0]
        is False
    )
    assert (
        target.conn.execute("SELECT rationale FROM vault_alpha.edges WHERE id='edge'").fetchone()[0]
        == "human decision"
    )
    assert (
        target.conn.execute(
            "SELECT pg_get_userbyid(relowner) FROM pg_class WHERE oid='bff.sessions'::regclass"
        ).fetchone()[0]
        == role
    )
    for store in (source, target):
        for schema in ("vault_alpha", "vault_beta"):
            with store.conn.transaction(force_rollback=True):
                store.conn.execute(
                    sql.SQL("SET LOCAL search_path TO {},public").format(sql.Identifier(schema))
                )
                assert store.conn.execute(
                    "SELECT is_chain_head FROM documents WHERE id='doc-new'"
                ).fetchone()[0]
                store.conn.execute(
                    "INSERT INTO edges (id,source_id,target_id,edge_type,created_at) "
                    "VALUES ('behavior','doc-old','doc-new','supersedes','2026-01-04')"
                )
                assert (
                    store.conn.execute(
                        "SELECT is_chain_head FROM documents WHERE id='doc-new'"
                    ).fetchone()[0]
                    is False
                )
    # Default grants and extension versions are part of the same resume proof.
    source.conn.execute(
        sql.SQL("ALTER DEFAULT PRIVILEGES IN SCHEMA bff GRANT SELECT ON TABLES TO {}").format(
            sql.Identifier(role)
        )
    )
    with pytest.raises(ValueError, match="reconciliation"):
        run_migration(source, target, tmp_path / "grant-drift.dump", "roundtrip", 16, 17)
    source.conn.execute(
        sql.SQL("ALTER DEFAULT PRIVILEGES IN SCHEMA bff REVOKE SELECT ON TABLES FROM {}").format(
            sql.Identifier(role)
        )
    )
    old_version = target.conn.execute(
        "SELECT extversion FROM pg_extension WHERE extname='vector'"
    ).fetchone()[0]
    target.conn.execute("UPDATE pg_extension SET extversion='0.0-probe' WHERE extname='vector'")
    with pytest.raises(ValueError, match="reconciliation"):
        run_migration(source, target, tmp_path / "extension-drift.dump", "roundtrip", 16, 17)
    target.conn.execute(
        "UPDATE pg_extension SET extversion=%s WHERE extname='vector'", (old_version,)
    )
    # A retry compares state and the durable checkpoint, and must not restore again.
    report2 = run_migration(source, target, tmp_path / "resume.dump", "roundtrip", 16, 17)
    assert report2 == report
    assert not (tmp_path / "resume.dump").exists()
    target.conn.execute("UPDATE vault_alpha.edges SET rationale='changed without changing count'")
    with pytest.raises(ValueError, match="reconciliation"):
        run_migration(source, target, tmp_path / "bad.dump", "roundtrip", 16, 17)


@pytest.mark.parametrize(
    "definition",
    [
        "INCREMENT BY 2",
        "START WITH 9",
        "MINVALUE 0",
        "MAXVALUE 999",
        "CACHE 2",
        "CYCLE",
        "AS integer",
    ],
)
def test_resume_rejects_sequence_definition_drift(
    databases: tuple[PostgresStore, PostgresStore, str], tmp_path: Path, definition: str
) -> None:
    source, target, _ = databases
    report = run_migration(source, target, tmp_path / "copy.dump", "sequence", 16, 17)
    before = target.conn.execute("SELECT last_value, is_called FROM bff.sessions_id_seq").fetchone()
    target.conn.execute(sql.SQL("ALTER SEQUENCE bff.sessions_id_seq " + definition))
    assert (
        target.conn.execute("SELECT last_value, is_called FROM bff.sessions_id_seq").fetchone()
        == before
    )
    with pytest.raises(ValueError, match="resume reconciliation failed"):
        run_migration(source, target, tmp_path / "retry.dump", "sequence", 16, 17)
    assert target.read_checkpoint() == report
    assert not (tmp_path / "retry.dump").exists()


@pytest.fixture
def non_superuser_databases() -> Iterator[tuple[PostgresStore, PostgresStore, str, str]]:
    from psycopg.conninfo import conninfo_to_dict, make_conninfo

    dsns = [
        os.environ.get(k)
        for k in ("SAGE_MIGRATION_TEST_SOURCE_DSN", "SAGE_MIGRATION_TEST_TARGET_DSN")
    ]
    if not all(dsns):
        pytest.skip("requires isolated 16 and 17 maintenance DSNs")
    nonce = uuid.uuid4().hex[:12]
    administrator, workload = "admin_" + nonce, "workload_" + nonce
    password = "disposable-test-only"
    stores, created = [], []
    try:
        for dsn in dsns:
            name = derive_throwaway_dbname()
            with psycopg.connect(dsn, autocommit=True) as setup:
                setup.execute(
                    sql.SQL("CREATE ROLE {} LOGIN CREATEROLE PASSWORD {}").format(
                        sql.Identifier(administrator), sql.Literal(password)
                    )
                )
                setup.execute(
                    sql.SQL("GRANT pg_read_all_data TO {}").format(sql.Identifier(administrator))
                )
                setup.execute(
                    sql.SQL("CREATE DATABASE {} OWNER {}").format(
                        sql.Identifier(name), sql.Identifier(administrator)
                    )
                )
                created.append((dsn, name))
            connection = conninfo_to_dict(rewrite_dsn_dbname(dsn, name))
            connection.pop("password", None)
            connection["user"] = administrator
            store = PostgresStore(make_conninfo(**connection), (workload,), password=password)
            stores.append(store)
            store.conn.execute(sql.SQL("CREATE ROLE {} LOGIN").format(sql.Identifier(workload)))
            store.conn.execute(
                sql.SQL("GRANT CONNECT, CREATE ON DATABASE {} TO {}").format(
                    sql.Identifier(name), sql.Identifier(workload)
                )
            )
            assert (
                store.conn.execute(
                    "SELECT rolsuper FROM pg_roles WHERE rolname=current_user"
                ).fetchone()[0]
                is False
            )
        with psycopg.connect(rewrite_dsn_dbname(*created[0]), autocommit=True) as setup:
            setup.execute(sql.SQL("SET ROLE {}").format(sql.Identifier(workload)))
            setup.execute("CREATE SCHEMA workload")
            setup.execute("CREATE TABLE workload.canonical (id serial PRIMARY KEY, rationale text)")
            setup.execute("INSERT INTO workload.canonical (rationale) VALUES ('human decision')")
        yield *stores, administrator, workload
    finally:
        for store in stores:
            store.close()
        for dsn, name in created:
            with psycopg.connect(dsn, autocommit=True) as setup:
                setup.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name)))
                for role in (workload, administrator):
                    setup.execute(sql.SQL("DROP ROLE {}").format(sql.Identifier(role)))


def test_non_superuser_restore_permission_preflight(
    non_superuser_databases: tuple[PostgresStore, PostgresStore, str, str], tmp_path: Path
) -> None:
    source, target, administrator, workload = non_superuser_databases
    owners = source.restore_owners()
    assert workload in owners
    # No implicit SET ROLE privilege follows from CREATEROLE on PostgreSQL 16+.
    with pytest.raises(ValueError, match="restore ownership privileges"):
        target.assert_restore_privileges(owners)
    assert source.conn.execute(
        "SELECT has_database_privilege(%s,current_database(),'CONNECT')", (workload,)
    ).fetchone()[0]
    assert target.snapshot()["tables"] == {}
    for set_option, inherit_option in ((False, True), (True, False)):
        target.conn.execute(
            sql.SQL("GRANT {} TO {} WITH SET {}, INHERIT {}").format(
                sql.Identifier(workload),
                sql.Identifier(administrator),
                sql.Literal(set_option),
                sql.Literal(inherit_option),
            )
        )
        with pytest.raises(ValueError, match="restore ownership privileges"):
            target.assert_restore_privileges(owners)
    target.conn.execute(
        sql.SQL("GRANT {} TO {} WITH SET TRUE, INHERIT TRUE").format(
            sql.Identifier(workload), sql.Identifier(administrator)
        )
    )
    target.assert_restore_privileges(owners)
    report = run_migration(source, target, tmp_path / "copy.dump", "non-superuser", 16, 17)
    assert report["status"] == "verified"
    assert source.snapshot() == target.snapshot()
    assert (
        target.conn.execute("SELECT rationale FROM workload.canonical").fetchone()[0]
        == "human decision"
    )
    assert (
        target.conn.execute(
            "SELECT pg_get_userbyid(relowner) FROM pg_class "
            "WHERE oid='workload.canonical'::regclass"
        ).fetchone()[0]
        == workload
    )
    assert not source.conn.execute(
        "SELECT has_database_privilege(%s,current_database(),'CONNECT')", (workload,)
    ).fetchone()[0]


@pytest.mark.parametrize(
    "drift",
    ["trigger_enabled", "trigger_definition", "routine_body", "routine_acl", "routine_owner"],
)
def test_resume_rejects_graph_executable_drift(
    databases: tuple[PostgresStore, PostgresStore, str], tmp_path: Path, drift: str
) -> None:
    source, target, role = databases
    report = run_migration(source, target, tmp_path / "copy.dump", "graph-executable", 16, 17)
    before = target.snapshot()
    statements = {
        "trigger_enabled": "ALTER TABLE vault_alpha.edges DISABLE TRIGGER "
        "trg_tier3_chain_head_on_supersedes",
        "trigger_definition": "CREATE OR REPLACE TRIGGER trg_tier3_chain_head_on_supersedes "
        "AFTER INSERT ON vault_alpha.edges FOR EACH ROW "
        "WHEN (NEW.edge_type = 'references') "
        "EXECUTE FUNCTION vault_alpha.trg_fn_chain_head_on_supersedes()",
        "routine_body": "CREATE OR REPLACE FUNCTION vault_alpha.trg_fn_chain_head_on_supersedes() "
        "RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RETURN NEW; END; $$",
        "routine_acl": "REVOKE EXECUTE ON FUNCTION "
        "vault_alpha.trg_fn_chain_head_on_supersedes() FROM PUBLIC",
    }
    if drift == "routine_owner":
        target.conn.execute(
            sql.SQL(
                "ALTER FUNCTION vault_alpha.trg_fn_chain_head_on_supersedes() OWNER TO {}"
            ).format(sql.Identifier(role))
        )
    else:
        target.conn.execute(statements[drift])
    after = target.snapshot()
    assert {name: (table["count"], table["hash"]) for name, table in before["tables"].items()} == {
        name: (table["count"], table["hash"]) for name, table in after["tables"].items()
    }
    with pytest.raises(ValueError, match="resume reconciliation failed"):
        run_migration(source, target, tmp_path / "retry.dump", "graph-executable", 16, 17)
    assert target.read_checkpoint() == report
    assert not (tmp_path / "retry.dump").exists()


def test_snapshot_refuses_unsupported_workload_aggregate(
    databases: tuple[PostgresStore, PostgresStore, str],
) -> None:
    source, _, _ = databases
    source.conn.execute(
        "CREATE AGGREGATE vault_alpha.custom_sum(bigint) (SFUNC=int8pl, STYPE=bigint, INITCOND='0')"
    )
    with pytest.raises(ValueError, match="unsupported workload routine kind"):
        source.snapshot()


def test_migration_refuses_destination_with_only_a_workload_function(
    databases: tuple[PostgresStore, PostgresStore, str], tmp_path: Path
) -> None:
    source, target, role = databases
    target.conn.execute(
        "CREATE FUNCTION public.existing() RETURNS integer LANGUAGE sql AS 'SELECT 1'"
    )
    with pytest.raises(ValueError, match="target contains existing workload objects"):
        run_migration(source, target, tmp_path / "copy.dump", "existing-routine", 16, 17)
    assert source.conn.execute(
        "SELECT has_database_privilege(%s,current_database(),'CONNECT')", (role,)
    ).fetchone()[0]
    assert target.conn.execute("SELECT public.existing()").fetchone()[0] == 1


@pytest.mark.parametrize("drift", ["version", "schema", "missing"])
def test_extension_preflight_rejects_parity_drift_without_fencing(
    databases: tuple[PostgresStore, PostgresStore, str], drift: str
) -> None:
    source, target, role = databases
    target.conn.execute("CREATE EXTENSION vector")
    target.conn.execute("CREATE EXTENSION pgstattuple")
    source.assert_extensions_match(target)
    if drift == "version":
        target.conn.execute("UPDATE pg_extension SET extversion='0.0.0' WHERE extname='vector'")
    elif drift == "schema":
        target.conn.execute("CREATE SCHEMA extensions")
        target.conn.execute("ALTER EXTENSION vector SET SCHEMA extensions")
    else:
        target.conn.execute("DROP EXTENSION vector")
    with pytest.raises(ValueError, match="required extension parity"):
        source.assert_extensions_match(target)
    assert source.conn.execute(
        "SELECT has_database_privilege(%s,current_database(),'CONNECT')", (role,)
    ).fetchone()[0]
    assert target.snapshot()["tables"] == {}
    assert target.read_checkpoint() is None


def test_fence_preflight_preserves_grants_and_existing_session(
    databases: tuple[PostgresStore, PostgresStore, str],
) -> None:
    source, _, role = databases
    with psycopg.connect(source.conninfo, autocommit=True) as observer:
        pid = observer.execute("SELECT pg_backend_pid()").fetchone()[0]
        before = observer.execute(
            "SELECT datacl::text FROM pg_database WHERE datname=current_database()"
        ).fetchone()
        source.assert_fence_privileges()
        assert observer.execute("SELECT pg_backend_pid()").fetchone()[0] == pid
        assert (
            observer.execute(
                "SELECT datacl::text FROM pg_database WHERE datname=current_database()"
            ).fetchone()
            == before
        )
        assert observer.execute(
            "SELECT has_database_privilege(%s,current_database(),'CONNECT')", (role,)
        ).fetchone()[0]


def test_fence_preflight_rejects_other_login_and_rolls_back(
    databases: tuple[PostgresStore, PostgresStore, str],
) -> None:
    source, _, role = databases
    extra = "extra_" + uuid.uuid4().hex[:12]
    source.conn.execute(sql.SQL("CREATE ROLE {} LOGIN").format(sql.Identifier(extra)))
    try:
        source.conn.execute(
            sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(
                sql.Identifier(source.database), sql.Identifier(extra)
            )
        )
        before = source.conn.execute(
            "SELECT datacl::text FROM pg_database WHERE datname=current_database()"
        ).fetchone()
        with pytest.raises(ValueError, match="unaccounted login roles"):
            source.assert_fence_privileges()
        assert (
            source.conn.execute(
                "SELECT datacl::text FROM pg_database WHERE datname=current_database()"
            ).fetchone()
            == before
        )
    finally:
        source.conn.execute(
            sql.SQL("REVOKE ALL ON DATABASE {} FROM {}").format(
                sql.Identifier(source.database), sql.Identifier(extra)
            )
        )
        source.conn.execute(sql.SQL("DROP ROLE {}").format(sql.Identifier(extra)))


def test_fence_preflight_checks_signal_permission_without_terminating(
    non_superuser_databases: tuple[PostgresStore, PostgresStore, str, str],
) -> None:
    source, _, administrator, workload = non_superuser_databases
    before = source.conn.execute(
        "SELECT datacl::text FROM pg_database WHERE datname=current_database()"
    ).fetchone()
    with pytest.raises(ValueError, match="terminate workload sessions"):
        source.assert_fence_privileges()
    assert (
        source.conn.execute(
            "SELECT datacl::text FROM pg_database WHERE datname=current_database()"
        ).fetchone()
        == before
    )
    source.conn.execute(
        sql.SQL("GRANT {} TO {} WITH INHERIT TRUE").format(
            sql.Identifier(workload), sql.Identifier(administrator)
        )
    )
    source.assert_fence_privileges()
    assert (
        source.conn.execute(
            "SELECT datacl::text FROM pg_database WHERE datname=current_database()"
        ).fetchone()
        == before
    )


def test_capacity_report_observes_job_scratch(
    databases: tuple[PostgresStore, PostgresStore, str], tmp_path: Path
) -> None:
    import shutil

    source, _, _ = databases
    expected = source.conn.execute("SELECT pg_database_size(current_database())").fetchone()[0]
    report = source.capacity_report(tmp_path)
    assert report["database_bytes"] == expected
    assert 0 < report["scratch_free_bytes"] <= shutil.disk_usage(tmp_path).total
