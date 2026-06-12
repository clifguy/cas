"""Tests for the externalized session store.

The in-memory binding is always exercised; the durable Postgres binding runs
only when ``SAGE_TEST_PG_DSN`` is set (mirroring the SAGE Postgres storage
tests). The headline Postgres test is the cross-instance round-trip: a session
written by one store instance is read by a *fresh* instance over the same DSN,
which is what a revision shift or a scale-out reduces to.
"""

import os
import time
import uuid

import pytest

from app.backend.auth.session_store import (
    InMemorySessionStore,
    PendingLogin,
    PostgresSessionStore,
    Session,
)

PG_DSN = os.environ.get("SAGE_TEST_PG_DSN")


def _session(session_id: str, *, ttl: float = 100.0) -> Session:
    return Session(
        session_id=session_id,
        subject=f"subject-{session_id}",
        claims={"name": "Test User", "n": 1},
        token_cache="cache-blob",
        expires_at=time.time() + ttl,
    )


# ---------------------------------------------------------------------------
# In-memory binding
# ---------------------------------------------------------------------------


async def test_inmemory_session_roundtrip():
    store = InMemorySessionStore()
    await store.open()
    await store.create_session(_session("sid"))
    got = await store.get_session("sid")
    assert got is not None
    assert got.subject == "subject-sid"
    assert got.claims == {"name": "Test User", "n": 1}
    await store.delete_session("sid")
    assert await store.get_session("sid") is None
    await store.close()


async def test_inmemory_expired_session_is_absent():
    store = InMemorySessionStore()
    await store.open()
    await store.create_session(_session("sid", ttl=-1.0))
    assert await store.get_session("sid") is None
    await store.close()


async def test_inmemory_pending_is_single_use():
    store = InMemorySessionStore()
    await store.open()
    await store.put_pending(PendingLogin(state="st", flow={"f": 1}, expires_at=time.time() + 100))
    taken = await store.take_pending("st")
    assert taken is not None and taken.flow == {"f": 1}
    assert await store.take_pending("st") is None  # single-use
    await store.close()


# ---------------------------------------------------------------------------
# Postgres binding (skipped without SAGE_TEST_PG_DSN)
# ---------------------------------------------------------------------------


async def _drop_schema(dsn: str, schema: str) -> None:
    import psycopg

    async with await psycopg.AsyncConnection.connect(dsn, autocommit=True) as conn:
        await conn.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')  # noqa: S608


@pytest.mark.skipif(not PG_DSN, reason="SAGE_TEST_PG_DSN not set")
class TestPostgresSessionStore:
    @pytest.fixture
    def schema(self) -> str:
        # Disposable, validated lowercase identifier; dropped by each test.
        return "sage_test_bff_" + uuid.uuid4().hex[:12]

    async def test_h1_roundtrip_across_fresh_instances(self, schema):
        """A session written by one instance is read by a fresh one (scale-out)."""
        writer = PostgresSessionStore(PG_DSN, schema=schema)
        await writer.open()
        try:
            await writer.create_session(_session("sid-1"))
        finally:
            await writer.close()

        reader = PostgresSessionStore(PG_DSN, schema=schema)
        await reader.open()
        try:
            got = await reader.get_session("sid-1")
            assert got is not None
            assert got.subject == "subject-sid-1"
            assert got.claims == {"name": "Test User", "n": 1}
            assert got.token_cache == "cache-blob"
        finally:
            await reader.close()
            await _drop_schema(PG_DSN, schema)

    async def test_h1b_bootstrap_is_idempotent(self, schema):
        """Opening (and so bootstrapping) twice on one schema is a no-op."""
        first = PostgresSessionStore(PG_DSN, schema=schema)
        await first.open()
        await first.close()
        second = PostgresSessionStore(PG_DSN, schema=schema)
        await second.open()
        try:
            await second.create_session(_session("sid-2"))
            assert (await second.get_session("sid-2")) is not None
        finally:
            await second.close()
            await _drop_schema(PG_DSN, schema)

    async def test_h3_expired_session_is_absent(self, schema):
        store = PostgresSessionStore(PG_DSN, schema=schema)
        await store.open()
        try:
            await store.create_session(_session("sid-x", ttl=-1.0))
            assert await store.get_session("sid-x") is None
        finally:
            await store.close()
            await _drop_schema(PG_DSN, schema)

    async def test_pending_single_use(self, schema):
        store = PostgresSessionStore(PG_DSN, schema=schema)
        await store.open()
        try:
            await store.put_pending(
                PendingLogin(state="st", flow={"redirect_uri": "x"}, expires_at=time.time() + 100)
            )
            taken = await store.take_pending("st")
            assert taken is not None and taken.flow == {"redirect_uri": "x"}
            assert await store.take_pending("st") is None
        finally:
            await store.close()
            await _drop_schema(PG_DSN, schema)
