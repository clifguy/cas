"""Opt-in real 16->17 restore; supply two disposable-test maintenance DSNs.

Never points at the configured SAGE database. Each endpoint gets a newly created
throwaway database; identities and data are isolated from every other test.
"""

from __future__ import annotations

import os
import uuid
from pathlib import Path

import psycopg
import pytest
from psycopg import sql

from sage.maintenance.postgres_migration import PostgresStore, run_migration
from sage.storage.postgres.schema import schema_statements
from tests.helpers.pg_isolation import derive_throwaway_dbname, rewrite_dsn_dbname


@pytest.fixture
def databases():
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


def test_cross_major_restore_preserves_state(databases, tmp_path: Path) -> None:
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
