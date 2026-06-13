"""Externalized session/token store for the backend-for-frontend.

A signed-in user's server-side state -- the identity-provider claims and the
serialized token cache used to mint delegated downstream tokens -- lives in a
durable store keyed by an opaque session id (the value carried in the session
cookie). Externalizing it to the relational store means a revision shift or a
scale-out does not drop logins: any replica can serve any session.

Two bindings implement one contract. :class:`PostgresSessionStore` is the
durable binding the hosted profile uses; :class:`InMemorySessionStore` is a
process-local binding for tests and any single-process use. The store also
holds the short-lived pre-login records -- the in-flight authorization-code
flow keyed by its ``state`` -- so the callback can validate the round-trip.
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any


@dataclass
class Session:
    """A signed-in user's durable server-side session."""

    session_id: str
    subject: str
    claims: dict[str, Any]
    token_cache: str
    expires_at: float


@dataclass
class PendingLogin:
    """An in-flight authorization-code flow awaiting its callback."""

    state: str
    flow: dict[str, Any]
    expires_at: float


def _now() -> float:
    return time.time()


class SessionStore(ABC):
    """Port for the externalized session and pre-login store."""

    @abstractmethod
    async def open(self) -> None:
        """Prepare the store (provision schema, open the pool)."""

    @abstractmethod
    async def close(self) -> None:
        """Release any held resources."""

    @abstractmethod
    async def put_pending(self, pending: PendingLogin) -> None:
        """Persist a pre-login flow record keyed by its ``state``."""

    @abstractmethod
    async def take_pending(self, state: str) -> PendingLogin | None:
        """Pop the pre-login record for ``state`` (single-use), or ``None``."""

    @abstractmethod
    async def create_session(self, session: Session) -> None:
        """Persist a new signed-in session."""

    @abstractmethod
    async def get_session(self, session_id: str) -> Session | None:
        """Return the live session for ``session_id``, or ``None`` if absent/expired."""

    @abstractmethod
    async def delete_session(self, session_id: str) -> None:
        """Delete the session for ``session_id`` (no error if already absent)."""


class SessionService:
    """Cookie-facing read/terminate operations over a :class:`SessionStore`."""

    def __init__(self, store: SessionStore, settings: Any) -> None:
        self._store = store
        self._settings = settings

    async def read(self, session_id: str | None) -> Session | None:
        """Resolve a cookie value to a live session, or ``None``."""
        if not session_id:
            return None
        return await self._store.get_session(session_id)

    async def terminate(self, session_id: str | None) -> None:
        """End the session named by the cookie value, if any."""
        if session_id:
            await self._store.delete_session(session_id)


class InMemorySessionStore(SessionStore):
    """Process-local store for tests and single-process use."""

    def __init__(self) -> None:
        self._pending: dict[str, PendingLogin] = {}
        self._sessions: dict[str, Session] = {}

    async def open(self) -> None:
        return None

    async def close(self) -> None:
        self._pending.clear()
        self._sessions.clear()

    async def put_pending(self, pending: PendingLogin) -> None:
        self._pending[pending.state] = pending

    async def take_pending(self, state: str) -> PendingLogin | None:
        pending = self._pending.pop(state, None)
        if pending is None or pending.expires_at < _now():
            return None
        return pending

    async def create_session(self, session: Session) -> None:
        self._sessions[session.session_id] = session

    async def get_session(self, session_id: str) -> Session | None:
        session = self._sessions.get(session_id)
        if session is None or session.expires_at < _now():
            return None
        return session

    async def delete_session(self, session_id: str) -> None:
        self._sessions.pop(session_id, None)


class PostgresSessionStore(SessionStore):
    """Durable store over Postgres, schema-isolated under its own namespace.

    Reuses the storage engine's libpq connection composition, but builds its own
    pool without the pgvector type hook -- the session tables carry no vector
    columns. Schema and table DDL is idempotent so a fresh replica reconciles
    rather than fails on an already-provisioned database.
    """

    def __init__(self, conninfo: str, *, schema: str = "cas_bff", connection_class=None) -> None:
        from sage.storage.postgres.schema import validate_schema_name

        validate_schema_name(schema)
        self._conninfo = conninfo
        self._schema = schema
        self._connection_class = connection_class
        self._pool: Any = None

    def _ddl(self) -> list[str]:
        schema = self._schema
        return [
            f'CREATE SCHEMA IF NOT EXISTS "{schema}"',  # noqa: S608 -- identifier validated
            f'CREATE TABLE IF NOT EXISTS "{schema}"."sessions" ('  # noqa: S608
            "session_id text PRIMARY KEY, subject text NOT NULL, claims jsonb NOT NULL, "
            "token_cache text NOT NULL, expires_at double precision NOT NULL)",
            f'CREATE TABLE IF NOT EXISTS "{schema}"."pending_logins" ('  # noqa: S608
            "state text PRIMARY KEY, flow jsonb NOT NULL, expires_at double precision NOT NULL)",
        ]

    async def _bootstrap(self) -> None:
        import psycopg

        conn_class = self._connection_class or psycopg.AsyncConnection
        async with await conn_class.connect(self._conninfo, autocommit=True) as conn:
            async with conn.transaction():
                for stmt in self._ddl():
                    await conn.execute(stmt)

    async def open(self) -> None:
        from psycopg.conninfo import conninfo_to_dict, make_conninfo
        from psycopg_pool import AsyncConnectionPool

        await self._bootstrap()
        parsed = conninfo_to_dict(self._conninfo)
        parsed["options"] = f"-c search_path={self._schema},public"
        extra = (
            {} if self._connection_class is None else {"connection_class": self._connection_class}
        )
        self._pool = AsyncConnectionPool(
            make_conninfo(**parsed), min_size=1, max_size=4, open=False, **extra
        )
        await self._pool.open(wait=True, timeout=10)

    async def close(self) -> None:
        if self._pool is not None:
            await self._pool.close()
            self._pool = None

    async def put_pending(self, pending: PendingLogin) -> None:
        from psycopg.types.json import Jsonb

        async with self._pool.connection() as conn:
            await conn.execute(
                "INSERT INTO pending_logins (state, flow, expires_at) VALUES (%s, %s, %s) "
                "ON CONFLICT (state) DO UPDATE SET flow = EXCLUDED.flow, "
                "expires_at = EXCLUDED.expires_at",
                (pending.state, Jsonb(pending.flow), pending.expires_at),
            )

    async def take_pending(self, state: str) -> PendingLogin | None:
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                "DELETE FROM pending_logins WHERE state = %s RETURNING flow, expires_at",
                (state,),
            )
            row = await cur.fetchone()
        if row is None:
            return None
        flow, expires_at = row
        if expires_at < _now():
            return None
        return PendingLogin(state=state, flow=flow, expires_at=expires_at)

    async def create_session(self, session: Session) -> None:
        from psycopg.types.json import Jsonb

        async with self._pool.connection() as conn:
            await conn.execute(
                "INSERT INTO sessions (session_id, subject, claims, token_cache, expires_at) "
                "VALUES (%s, %s, %s, %s, %s) ON CONFLICT (session_id) DO UPDATE SET "
                "subject = EXCLUDED.subject, claims = EXCLUDED.claims, "
                "token_cache = EXCLUDED.token_cache, expires_at = EXCLUDED.expires_at",
                (
                    session.session_id,
                    session.subject,
                    Jsonb(session.claims),
                    session.token_cache,
                    session.expires_at,
                ),
            )

    async def get_session(self, session_id: str) -> Session | None:
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                "SELECT subject, claims, token_cache, expires_at FROM sessions "
                "WHERE session_id = %s",
                (session_id,),
            )
            row = await cur.fetchone()
        if row is None:
            return None
        subject, claims, token_cache, expires_at = row
        if expires_at < _now():
            return None
        return Session(
            session_id=session_id,
            subject=subject,
            claims=claims,
            token_cache=token_cache,
            expires_at=expires_at,
        )

    async def delete_session(self, session_id: str) -> None:
        async with self._pool.connection() as conn:
            await conn.execute("DELETE FROM sessions WHERE session_id = %s", (session_id,))
