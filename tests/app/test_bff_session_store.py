"""Tests for the externalized session store.

The in-memory binding is always exercised; the durable Postgres binding runs
only when ``SAGE_TEST_PG_DSN`` is set (mirroring the SAGE Postgres storage
tests). The headline Postgres test is the cross-instance round-trip: a session
written by one store instance is read by a *fresh* instance over the same DSN,
which is what a revision shift or a scale-out reduces to.

The managed-identity path (BFF-MI-001 through BFF-MI-003) is exercised via
mocks so no real Postgres or Azure credential is required.
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

# Presence flag for the class-level skip only. The live DSN each test connects
# with comes from the ``pg_dsn`` fixture, read at runtime after the isolation
# provisioner has rewritten SAGE_TEST_PG_DSN to this process's throwaway db.
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
    def pg_dsn(self, _provision_isolated_test_database) -> str:
        # Read at runtime, not from the module-level PG_DSN captured at import:
        # the isolation provisioner (root conftest) rewrites SAGE_TEST_PG_DSN to
        # this process's throwaway database only after fixtures start, so an
        # import-time capture would connect to the shared maintenance database.
        dsn = os.environ.get("SAGE_TEST_PG_DSN")
        if not dsn:
            pytest.skip("SAGE_TEST_PG_DSN not set")
        return dsn

    @pytest.fixture
    def schema(self) -> str:
        # Disposable, validated lowercase identifier; dropped by each test.
        return "sage_test_bff_" + uuid.uuid4().hex[:12]

    async def test_h1_roundtrip_across_fresh_instances(self, pg_dsn, schema):
        """A session written by one instance is read by a fresh one (scale-out)."""
        writer = PostgresSessionStore(pg_dsn, schema=schema)
        await writer.open()
        try:
            await writer.create_session(_session("sid-1"))
        finally:
            await writer.close()

        reader = PostgresSessionStore(pg_dsn, schema=schema)
        await reader.open()
        try:
            got = await reader.get_session("sid-1")
            assert got is not None
            assert got.subject == "subject-sid-1"
            assert got.claims == {"name": "Test User", "n": 1}
            assert got.token_cache == "cache-blob"
        finally:
            await reader.close()
            await _drop_schema(pg_dsn, schema)

    async def test_h1b_bootstrap_is_idempotent(self, pg_dsn, schema):
        """Opening (and so bootstrapping) twice on one schema is a no-op."""
        first = PostgresSessionStore(pg_dsn, schema=schema)
        await first.open()
        await first.close()
        second = PostgresSessionStore(pg_dsn, schema=schema)
        await second.open()
        try:
            await second.create_session(_session("sid-2"))
            assert (await second.get_session("sid-2")) is not None
        finally:
            await second.close()
            await _drop_schema(pg_dsn, schema)

    async def test_h3_expired_session_is_absent(self, pg_dsn, schema):
        store = PostgresSessionStore(pg_dsn, schema=schema)
        await store.open()
        try:
            await store.create_session(_session("sid-x", ttl=-1.0))
            assert await store.get_session("sid-x") is None
        finally:
            await store.close()
            await _drop_schema(pg_dsn, schema)

    async def test_pending_single_use(self, pg_dsn, schema):
        store = PostgresSessionStore(pg_dsn, schema=schema)
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
            await _drop_schema(pg_dsn, schema)


# ---------------------------------------------------------------------------
# Managed-identity connection_class threading (BFF-MI-001 through BFF-MI-003)
# ---------------------------------------------------------------------------

_FAKE_CONNINFO = "host=test port=5432 dbname=testdb user=testuser"


class TestManagedIdentityConnectionClass:
    """Unit tests for connection_class threading in PostgresSessionStore.

    No real Postgres or Azure credential required: the pool constructor and
    psycopg connect are monkeypatched to sentinels that record which path was
    taken without opening any network connection.
    """

    async def test_bff_mi_001_connection_class_forwarded_to_pool(self, monkeypatch):
        """BFF-MI-001: connection_class is passed to AsyncConnectionPool when set.

        AsyncConnectionPool is a deferred import inside open(), so it must be
        patched on psycopg_pool, not on the session_store module.
        """
        import psycopg_pool

        recorded: list[dict] = []

        class _FakePool:
            def __init__(self, conninfo, *, min_size, max_size, open, **kwargs):
                recorded.append(kwargs)

            async def open(self, *, wait, timeout):
                pass

        monkeypatch.setattr(psycopg_pool, "AsyncConnectionPool", _FakePool)

        class _FakeClass:
            pass

        store = PostgresSessionStore(_FAKE_CONNINFO, connection_class=_FakeClass)
        monkeypatch.setattr(store, "_bootstrap", _async_noop)
        await store.open()

        assert len(recorded) == 1
        assert recorded[0].get("connection_class") is _FakeClass

    async def test_bff_mi_002_no_connection_class_omits_kwarg(self, monkeypatch):
        """BFF-MI-002: connection_class kwarg is absent when not supplied."""
        import psycopg_pool

        recorded: list[dict] = []

        class _FakePool:
            def __init__(self, conninfo, *, min_size, max_size, open, **kwargs):
                recorded.append(kwargs)

            async def open(self, *, wait, timeout):
                pass

        monkeypatch.setattr(psycopg_pool, "AsyncConnectionPool", _FakePool)

        store = PostgresSessionStore(_FAKE_CONNINFO)
        monkeypatch.setattr(store, "_bootstrap", _async_noop)
        await store.open()

        assert len(recorded) == 1
        assert "connection_class" not in recorded[0]

    async def test_bff_mi_003_bootstrap_uses_connection_class_when_set(self, monkeypatch):
        """BFF-MI-003: _bootstrap dispatches to connection_class.connect when provided.

        psycopg.AsyncConnection.connect is patched as a classmethod sentinel
        (same technique as test_postgres_managed_identity.py) so no real
        database is contacted. Raising immediately before the ``async with``
        body is sufficient to distinguish which connect path was taken.
        """
        import psycopg

        base_calls: list[str] = []
        token_calls: list[str] = []

        async def _fake_base_connect(cls, conninfo, **kwargs):
            base_calls.append("base")
            raise RuntimeError("base-connect-sentinel")

        monkeypatch.setattr(psycopg.AsyncConnection, "connect", classmethod(_fake_base_connect))

        class _FakeTokenClass:
            @classmethod
            async def connect(cls, conninfo, **kwargs):
                token_calls.append("token")
                raise RuntimeError("token-connect-sentinel")

        # Cloud branch: connection_class is set → token class must be called.
        cloud_store = PostgresSessionStore(_FAKE_CONNINFO, connection_class=_FakeTokenClass)
        with pytest.raises(RuntimeError, match="token-connect-sentinel"):
            await cloud_store._bootstrap()
        assert token_calls == ["token"]
        assert base_calls == []

        # Local branch: no connection_class → base psycopg.AsyncConnection is called.
        local_store = PostgresSessionStore(_FAKE_CONNINFO)
        with pytest.raises(RuntimeError, match="base-connect-sentinel"):
            await local_store._bootstrap()
        assert base_calls == ["base"]


async def _async_noop(*args, **kwargs):
    return None
