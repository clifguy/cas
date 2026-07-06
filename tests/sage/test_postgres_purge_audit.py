"""The Postgres purge-audit sink: the durable audit-first record (CAS-ADR-029).

The purge tooling appends its audit record to a ``purge_audit`` table in the
vault's own Postgres schema, created on demand at first append (it is a
maintenance artifact, deliberately absent from the canonical request-surface
bootstrap). These tests run the real write against a disposable schema; they
skip without ``SAGE_TEST_PG_DSN``.
"""

import os
from datetime import datetime, timezone

import pytest

pytest.importorskip("psycopg")


def _record(name: str, *, batch_id=None, chain_id=None) -> dict:
    record = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "operation": "purge_document",
        "document_id": f"deadbeef_{name}",
        "title": f"Doc {name}",
        "source_path": f"/x/{name}.md",
        "source_content_hash": "sha256:0",
        "doc_type": "ticket",
        "reason": "wrong-vault",
    }
    if batch_id is not None:
        record["batch_id"] = batch_id
    if chain_id is not None:
        record["chain_id"] = chain_id
    return record


@pytest.fixture
def audit_schema(pg_dsn):
    """A disposable schema standing in for a vault's schema; dropped after."""
    import asyncio

    import psycopg

    from sage.storage.postgres.schema import assert_disposable_target

    schema = assert_disposable_target("sage_test_audit_" + os.urandom(3).hex())

    async def _run(sql: str) -> None:
        async with await psycopg.AsyncConnection.connect(pg_dsn, autocommit=True) as conn:
            await conn.execute(sql)

    asyncio.run(_run(f'CREATE SCHEMA "{schema}"'))
    try:
        yield schema
    finally:
        asyncio.run(_run(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))  # noqa: S608


async def _fetch_rows(pg_dsn: str, schema: str) -> list[tuple]:
    import psycopg

    async with await psycopg.AsyncConnection.connect(pg_dsn) as conn:
        cur = await conn.execute(
            f"SELECT operation, document_id, reason, batch_id, chain_id, ts "  # noqa: S608
            f'FROM "{schema}".purge_audit ORDER BY id'
        )
        return await cur.fetchall()


def _sink(pg_dsn: str, schema: str, monkeypatch):
    from sage.storage.postgres.pool import PostgresConnectionParams
    from sage.storage.postgres.purge_audit import PostgresPurgeAuditSink
    from tests.sage.conftest import stack_postgres_config_from_dsn

    cfg = stack_postgres_config_from_dsn(pg_dsn, monkeypatch)
    params = PostgresConnectionParams(
        host=cfg.host,
        port=cfg.port,
        database=cfg.database,
        user=cfg.user,
        search_path=f"{schema},public",
    )
    return PostgresPurgeAuditSink(params, schema=schema)


async def test_append_creates_table_and_inserts_row(pg_dsn, audit_schema, monkeypatch):
    """First append auto-creates ``purge_audit`` in the vault schema and lands
    exactly one row whose columns match the record, grouping ids NULL."""
    sink = _sink(pg_dsn, audit_schema, monkeypatch)

    await sink.append(_record("one"))

    rows = await _fetch_rows(pg_dsn, audit_schema)
    assert len(rows) == 1
    operation, document_id, reason, batch_id, chain_id, ts = rows[0]
    assert operation == "purge_document"
    assert document_id == "deadbeef_one"
    assert reason == "wrong-vault"
    assert batch_id is None
    assert chain_id is None
    assert ts.tzinfo is not None


async def test_appends_are_idempotent_across_table_creation(pg_dsn, audit_schema, monkeypatch):
    """Chain/batch purges append many rows: the repeat ``CREATE TABLE IF NOT
    EXISTS`` is a no-op, both rows land, and the grouping id round-trips on the
    record that carries it while staying NULL on the one that omits it."""
    sink = _sink(pg_dsn, audit_schema, monkeypatch)

    await sink.append(_record("one", chain_id="CHAIN-1"))
    await sink.append(_record("two"))

    rows = await _fetch_rows(pg_dsn, audit_schema)
    assert len(rows) == 2
    assert rows[0][4] == "CHAIN-1"
    assert rows[1][4] is None


async def test_provisioner_factory_binds_the_vault_schema(pg_dsn, audit_schema, monkeypatch):
    """``provisioner.purge_audit_sink(vault_id)`` wires a sink whose appends
    land in that vault's schema — the factory wiring, not just the ctor."""
    from sage.storage_binding import PostgresVaultStorageProvisioner
    from tests.sage.conftest import stack_postgres_config_from_dsn

    provisioner = PostgresVaultStorageProvisioner(
        stack_postgres_config_from_dsn(pg_dsn, monkeypatch)
    )
    sink = provisioner.purge_audit_sink(audit_schema)

    await sink.append(_record("via-factory", batch_id="BATCH-1"))

    rows = await _fetch_rows(pg_dsn, audit_schema)
    assert len(rows) == 1
    assert rows[0][1] == "deadbeef_via-factory"
    assert rows[0][3] == "BATCH-1"
