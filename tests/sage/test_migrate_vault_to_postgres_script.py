"""Tests for the vault-to-Postgres operator entrypoint (CAS-ADR-042).

Two layers:

- Wiring tests monkeypatch the provisioning, pool, store, and migration seams
  in ``scripts/migrate_vault_to_postgres.py`` to pin the call order — target
  provisioning must complete over a plain connection *before* the
  pgvector-configured pool opens, and a dry-run must never touch the target.
  No server required.
- The fresh-database test provisions a throwaway database from
  ``SAGE_TEST_PG_DSN`` and proves the provision-then-pool sequence serves
  connections on a target that starts with no extensions at all. Skips without
  a configured test server or without ``CREATE DATABASE`` privilege.
"""

from __future__ import annotations

import copy
import io
import uuid
from pathlib import Path

import pytest
import yaml

from sage.migration.vault_to_postgres import (
    AbstractReconciliation,
    ChainHeadReconciliation,
    ChunkReconciliation,
    DocumentReconciliation,
    EdgeReconciliation,
    IdSetReconciliation,
    VaultMigrationReport,
)
from scripts import migrate_vault_to_postgres as script

# ---------------------------------------------------------------------------
# Wiring harness (no server)
# ---------------------------------------------------------------------------


def _stub_report(*, executed: bool) -> VaultMigrationReport:
    return VaultMigrationReport(
        vault_id="wired",
        executed=executed,
        documents=DocumentReconciliation(source_count=1, target_count=1 if executed else 0),
        edges=EdgeReconciliation(source_count=0, target_count=0),
        chain_heads=ChainHeadReconciliation(source_count=1, target_count=1 if executed else 0),
        chunks=ChunkReconciliation(source_total=0, target_total=0),
        abstracts=AbstractReconciliation(total_documents=1, source_coverage=1, target_coverage=1),
        users=IdSetReconciliation(source_count=0, target_count=0),
        staging_edges=IdSetReconciliation(source_count=0, target_count=0),
    )


@pytest.fixture
def wired_calls(monkeypatch, tmp_path, minimal_vault_config_dict) -> list[str]:
    """Wire ``_migrate_one``'s collaborators to recorders; return the call log."""
    vault_dir = tmp_path / "wired"
    (vault_dir / "sources").mkdir(parents=True)
    (vault_dir / "brain").mkdir()
    cfg = copy.deepcopy(minimal_vault_config_dict)
    cfg["vault"]["id"] = "wired"
    cfg["vault"]["storage_root"] = str(vault_dir / "sources")
    cfg["vault"]["brain_root"] = str(vault_dir / "brain")
    config_path = vault_dir / "vault_config.yaml"
    config_path.write_text(yaml.safe_dump(cfg))

    calls: list[str] = []

    monkeypatch.setattr(script, "config_path_for_vault", lambda vault_id: config_path)

    async def fake_provision(dsn, schema, extensions):
        calls.append("provision")

    monkeypatch.setattr(script, "_provision_target", fake_provision)

    class _FakePool:
        async def open(self):
            calls.append("pool.open")

        async def close(self):
            calls.append("pool.close")

    monkeypatch.setattr(script, "_build_target_pool", lambda dsn, schema: _FakePool())

    async def fake_migrate_vault(**kwargs):
        calls.append("migrate_vault")
        return _stub_report(executed=kwargs["execute"])

    monkeypatch.setattr(script, "migrate_vault", fake_migrate_vault)

    class _FakeSourceGraph:
        def __init__(self, path: Path):
            pass

        async def initialize(self):
            pass

        async def close(self):
            pass

    monkeypatch.setattr(script, "SqliteGraphStore", _FakeSourceGraph)
    monkeypatch.setattr(script, "LanceDBContentStore", lambda root: object())
    monkeypatch.setattr(script, "PostgresGraphStore", lambda pool: object())
    monkeypatch.setattr(script, "PostgresContentStore", lambda pool: object())
    return calls


async def test_execute_provisions_before_pool_open(wired_calls):
    rc = await script._migrate_one(
        "wired", execute=True, dsn="postgresql:///throwaway", report_dir=None, out=io.StringIO()
    )
    assert rc == 0
    assert "provision" in wired_calls and "pool.open" in wired_calls
    assert wired_calls.index("provision") < wired_calls.index("pool.open")
    assert wired_calls.index("pool.open") < wired_calls.index("migrate_vault")


async def test_dry_run_touches_no_target(wired_calls):
    rc = await script._migrate_one(
        "wired", execute=False, dsn="postgresql:///throwaway", report_dir=None, out=io.StringIO()
    )
    assert rc == 0
    assert "provision" not in wired_calls
    assert "pool.open" not in wired_calls
    assert "migrate_vault" in wired_calls


# ---------------------------------------------------------------------------
# Fresh database (real Postgres; skips without SAGE_TEST_PG_DSN / privilege)
# ---------------------------------------------------------------------------


@pytest.fixture
def fresh_pg_database(pg_dsn) -> str:
    """A DSN to a freshly created, extensionless database; dropped at teardown."""
    psycopg = pytest.importorskip("psycopg")
    from psycopg.conninfo import conninfo_to_dict, make_conninfo

    name = f"sage_test_db_{uuid.uuid4().hex[:8]}"
    with psycopg.connect(pg_dsn, autocommit=True) as conn:
        try:
            conn.execute(f'CREATE DATABASE "{name}"')
        except psycopg.errors.InsufficientPrivilege:
            pytest.skip("test role lacks CREATE DATABASE privilege")

    parsed = conninfo_to_dict(pg_dsn)
    parsed["dbname"] = name
    yield make_conninfo(**{k: v for k, v in parsed.items() if v is not None})

    with psycopg.connect(pg_dsn, autocommit=True) as conn:
        conn.execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
            "WHERE datname = %s AND pid <> pg_backend_pid()",
            (name,),
        )
        conn.execute(f'DROP DATABASE IF EXISTS "{name}"')


async def test_provisioned_pool_serves_vector_connection_on_fresh_database(fresh_pg_database):
    """Provisioning over a plain connection must precede the configured pool.

    The pool's per-connection configure hook registers the pgvector type, so a
    pool opened against a database without the ``vector`` extension can never
    serve a connection. Provision-first makes a virgin target usable.
    """
    schema = "sage_test_fresh"
    await script._provision_target(fresh_pg_database, schema, ["vector", "pgstattuple"])

    pool = script._build_target_pool(fresh_pg_database, schema)
    await pool.open()
    try:
        async with pool.connection() as conn:
            cur = await conn.execute("SELECT 1")
            assert (await cur.fetchone())[0] == 1
            # Tables landed in the provisioned schema on the search path.
            cur = await conn.execute("SELECT count(*) FROM documents")
            assert (await cur.fetchone())[0] == 0
    finally:
        await pool.close()
