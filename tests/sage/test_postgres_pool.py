"""Postgres async driver + pool (CAS-ADR-042).

Pure tests cover the libpq-kwargs composition and the pool-factory wiring with no
server; the PG-touching tests (which skip when SAGE_TEST_PG_DSN is unset) prove
the pool opens, registers pgvector on pooled connections, and serves concurrent
acquirers against a real Postgres -- the concurrency a single-writer embedded
store could not.
"""

from __future__ import annotations

import asyncio

import pytest

from sage.storage.postgres.pool import (
    PASSWORD_ENV_VAR,
    PostgresConnectionParams,
    build_conn_kwargs,
    configure_connection,
    create_pool,
)

# ---------------------------------------------------------------------------
# Pure: build_conn_kwargs
# ---------------------------------------------------------------------------


def test_build_conn_kwargs_socket_form_omits_host_and_user():
    """A null host/user composes a socket connection: neither key is emitted, so
    libpq falls back to the local socket and the OS user (peer auth)."""
    kwargs = build_conn_kwargs(PostgresConnectionParams(), environ={})
    assert kwargs == {"dbname": "sage", "port": "5432"}


def test_build_conn_kwargs_tcp_form_includes_host_user_sslmode():
    """A TCP/hosted endpoint emits host, port, user, and sslmode."""
    params = PostgresConnectionParams(
        host="db.example", port=6432, database="sage_cloud", user="svc", sslmode="require"
    )
    kwargs = build_conn_kwargs(params, environ={})
    assert kwargs["host"] == "db.example"
    assert kwargs["port"] == "6432"
    assert kwargs["dbname"] == "sage_cloud"
    assert kwargs["user"] == "svc"
    assert kwargs["sslmode"] == "require"


def test_build_conn_kwargs_password_only_from_env():
    """The password is sourced exclusively from $SAGE_PG_PASSWORD -- absent
    without it, present with it. Guards the no-credential-in-config invariant.
    """
    params = PostgresConnectionParams(host="db.example", user="svc")
    assert "password" not in build_conn_kwargs(params, environ={})
    kwargs = build_conn_kwargs(params, environ={PASSWORD_ENV_VAR: "s3cret"})
    assert kwargs["password"] == "s3cret"


def test_build_conn_kwargs_search_path_to_options():
    """search_path is passed through the libpq `options` startup parameter."""
    params = PostgresConnectionParams(search_path="sage_test_abcd,public")
    kwargs = build_conn_kwargs(params, environ={})
    assert kwargs["options"] == "-c search_path=sage_test_abcd,public"


def test_params_carry_no_password_field():
    """The pool params dataclass cannot carry a credential."""
    fields = set(PostgresConnectionParams.__dataclass_fields__)
    assert "password" not in fields
    assert "dsn" not in fields


# ---------------------------------------------------------------------------
# Pool factory + configure hook (no server)
# ---------------------------------------------------------------------------


async def test_configure_connection_registers_pgvector(monkeypatch):
    """The pool's configure hook registers the pgvector type on the connection.

    Anti-coincidental-pass: a recorder replaces register_vector_async; the hook
    must call it with the connection. If the hook stopped registering, the
    recorder would be empty.
    """
    pgv = pytest.importorskip("pgvector.psycopg")

    calls: list[object] = []

    async def _recorder(conn):
        calls.append(conn)

    monkeypatch.setattr(pgv, "register_vector_async", _recorder)
    sentinel = object()
    await configure_connection(sentinel)
    assert calls == [sentinel]


def test_create_pool_returns_unopened_pool_with_vector_configure():
    """create_pool builds an unopened AsyncConnectionPool wired to the pgvector
    configure hook.

    Anti-coincidental-pass: the assertion introspects the psycopg_pool configure
    callback, so wiring configure=None would fail it. The pool is built unopened
    (an async pool is opened by the caller, not in __init__).
    """
    pytest.importorskip("psycopg")
    pytest.importorskip("psycopg_pool")
    from psycopg_pool import AsyncConnectionPool

    pool = create_pool(PostgresConnectionParams(host="db.invalid", database="sage"))
    assert isinstance(pool, AsyncConnectionPool)
    assert pool._configure is configure_connection
    assert pool._opened is False


# ---------------------------------------------------------------------------
# PG-touching (skip without SAGE_TEST_PG_DSN)
# ---------------------------------------------------------------------------


async def test_pool_opens_serves_and_closes(pg_pool):
    """A pooled connection executes a query and the pool closes cleanly."""
    async with pg_pool.connection() as conn:
        cur = await conn.execute("SELECT 1")
        assert (await cur.fetchone())[0] == 1


async def test_pgvector_round_trips_through_pooled_connection(pg_pool):
    """A vector value round-trips through a pooled connection -- proving pgvector
    is registered on connections the pool hands out, not just a bare connect.
    """
    async with pg_pool.connection() as conn:
        cur = await conn.execute("SELECT %s::vector", ([1.0, 2.0, 3.0],))
        row = await cur.fetchone()
        assert [float(x) for x in row[0]] == [1.0, 2.0, 3.0]


async def test_pool_serves_concurrent_acquirers(pg_pool):
    """The pool serves several connections concurrently -- the multi-writer
    concurrency the embedded single-writer store could not.
    """

    async def _one(i: int) -> int:
        async with pg_pool.connection() as conn:
            cur = await conn.execute("SELECT %s::int", (i,))
            return (await cur.fetchone())[0]

    results = await asyncio.gather(*[_one(i) for i in range(5)])
    assert sorted(results) == [0, 1, 2, 3, 4]
