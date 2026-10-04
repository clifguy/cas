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

A session ends at its absolute expiry or once it has gone unused for the idle
limit, whichever comes first. Expired records are deleted as new ones are
written, so neither table grows with abandoned sign-ins or lapsed sessions.
The durable binding stores the token cache -- which holds the user's refresh
token -- encrypted under a key derived from the client secret.
"""

from __future__ import annotations

import base64
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, replace
from typing import Any

DEFAULT_IDLE_SECONDS = 8 * 60 * 60
# A read refreshes a session's last-seen time only once it is this old, so an
# active session costs at most one write per interval rather than one per request.
TOUCH_INTERVAL_SECONDS = 60

_KEY_DERIVATION_INFO = b"cas-bff-token-cache-v1"


@dataclass
class Session:
    """A signed-in user's durable server-side session.

    ``last_seen_at`` is when the session was last used; ``None`` on a session
    being created means now.
    """

    session_id: str
    subject: str
    claims: dict[str, Any]
    token_cache: str
    expires_at: float
    last_seen_at: float | None = None


@dataclass
class PendingLogin:
    """An in-flight authorization-code flow awaiting its callback."""

    state: str
    flow: dict[str, Any]
    expires_at: float


def _now() -> float:
    return time.time()


class TokenCacheCipher:
    """Authenticated encryption of a serialized token cache.

    The key is derived with HKDF-SHA256 from the confidential client's secret,
    so the stored caches are unreadable without the secret, and rotating the
    secret retires every stored session.
    """

    def __init__(self, secret: str) -> None:
        from cryptography.fernet import Fernet
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.kdf.hkdf import HKDF

        key = HKDF(
            algorithm=hashes.SHA256(), length=32, salt=None, info=_KEY_DERIVATION_INFO
        ).derive(secret.encode())
        self._fernet = Fernet(base64.urlsafe_b64encode(key))

    def encrypt(self, plaintext: str) -> str:
        """Encrypt ``plaintext`` to a URL-safe text token."""
        return self._fernet.encrypt(plaintext.encode()).decode()

    def decrypt(self, ciphertext: str) -> str:
        """Decrypt a token from :meth:`encrypt`.

        Raises ``ValueError`` when the token was not produced under this key or
        has been altered.
        """
        from cryptography.fernet import InvalidToken

        try:
            return self._fernet.decrypt(ciphertext.encode()).decode()
        except InvalidToken as exc:
            raise ValueError("token cache does not decrypt under this key") from exc


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
        """Persist a pre-login flow record keyed by its ``state``.

        Also deletes pre-login records that have expired.
        """

    @abstractmethod
    async def take_pending(self, state: str) -> PendingLogin | None:
        """Pop the pre-login record for ``state`` (single-use), or ``None``."""

    @abstractmethod
    async def create_session(self, session: Session) -> None:
        """Persist a new signed-in session.

        Also deletes sessions past their absolute expiry or the idle limit.
        """

    @abstractmethod
    async def get_session(self, session_id: str) -> Session | None:
        """Return the live session for ``session_id``, or ``None``.

        ``None`` when the session is absent, past its absolute expiry, or idle
        beyond the limit. A returned session counts as used: its last-seen time
        is refreshed once it is older than the touch interval.
        """

    @abstractmethod
    async def update_token_cache(self, session_id: str, token_cache: str) -> None:
        """Replace the session's serialized token cache (no error if absent)."""

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

    def __init__(self, *, idle_seconds: float = DEFAULT_IDLE_SECONDS) -> None:
        self._idle_seconds = idle_seconds
        self._pending: dict[str, PendingLogin] = {}
        self._sessions: dict[str, Session] = {}

    def _is_live(self, session: Session, now: float) -> bool:
        if session.expires_at < now:
            return False
        return session.last_seen_at is None or session.last_seen_at >= now - self._idle_seconds

    async def open(self) -> None:
        return None

    async def close(self) -> None:
        self._pending.clear()
        self._sessions.clear()

    async def put_pending(self, pending: PendingLogin) -> None:
        now = _now()
        self._pending = {
            state: record for state, record in self._pending.items() if record.expires_at >= now
        }
        self._pending[pending.state] = pending

    async def take_pending(self, state: str) -> PendingLogin | None:
        pending = self._pending.pop(state, None)
        if pending is None or pending.expires_at < _now():
            return None
        return pending

    async def create_session(self, session: Session) -> None:
        now = _now()
        self._sessions = {
            session_id: existing
            for session_id, existing in self._sessions.items()
            if self._is_live(existing, now)
        }
        if session.last_seen_at is None:
            session = replace(session, last_seen_at=now)
        self._sessions[session.session_id] = session

    async def get_session(self, session_id: str) -> Session | None:
        session = self._sessions.get(session_id)
        now = _now()
        if session is None or not self._is_live(session, now):
            return None
        if session.last_seen_at is None or session.last_seen_at < now - TOUCH_INTERVAL_SECONDS:
            session.last_seen_at = now
        return session

    async def update_token_cache(self, session_id: str, token_cache: str) -> None:
        session = self._sessions.get(session_id)
        if session is not None:
            session.token_cache = token_cache

    async def delete_session(self, session_id: str) -> None:
        self._sessions.pop(session_id, None)


class PostgresSessionStore(SessionStore):
    """Durable store over Postgres, schema-isolated under its own namespace.

    Reuses the storage engine's libpq connection composition, but builds its own
    pool without the pgvector type hook -- the session tables carry no vector
    columns. Schema and table DDL is idempotent so a fresh replica reconciles
    rather than fails on an already-provisioned database, including one
    provisioned before a column or index was added. The token cache is stored
    encrypted by ``cipher``; a row that does not decrypt under it (written under
    a rotated secret) reads as absent and is deleted.
    """

    def __init__(
        self,
        conninfo: str,
        *,
        cipher: TokenCacheCipher,
        schema: str = "cas_bff",
        idle_seconds: float = DEFAULT_IDLE_SECONDS,
        connection_class=None,
    ) -> None:
        from sage.storage.postgres.schema import validate_schema_name

        validate_schema_name(schema)
        self._conninfo = conninfo
        self._cipher = cipher
        self._schema = schema
        self._idle_seconds = idle_seconds
        self._connection_class = connection_class
        self._pool: Any = None

    def _ddl(self) -> list[str]:
        schema = self._schema
        return [
            f'CREATE SCHEMA IF NOT EXISTS "{schema}"',  # noqa: S608 -- identifier validated
            f'CREATE TABLE IF NOT EXISTS "{schema}"."sessions" ('  # noqa: S608
            "session_id text PRIMARY KEY, subject text NOT NULL, claims jsonb NOT NULL, "
            "token_cache text NOT NULL, expires_at double precision NOT NULL)",
            # A row from before idle tracking defaults to never seen, so it reads
            # as idle and is swept.
            f'ALTER TABLE "{schema}"."sessions" '  # noqa: S608
            "ADD COLUMN IF NOT EXISTS last_seen_at double precision NOT NULL DEFAULT 0",
            f'CREATE INDEX IF NOT EXISTS "sessions_expires_at_idx" '  # noqa: S608
            f'ON "{schema}"."sessions" (expires_at)',
            f'CREATE TABLE IF NOT EXISTS "{schema}"."pending_logins" ('  # noqa: S608
            "state text PRIMARY KEY, flow jsonb NOT NULL, expires_at double precision NOT NULL)",
            f'CREATE INDEX IF NOT EXISTS "pending_logins_expires_at_idx" '  # noqa: S608
            f'ON "{schema}"."pending_logins" (expires_at)',
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
            await conn.execute("DELETE FROM pending_logins WHERE expires_at < %s", (_now(),))
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

        now = _now()
        last_seen_at = session.last_seen_at if session.last_seen_at is not None else now
        async with self._pool.connection() as conn:
            await conn.execute(
                "DELETE FROM sessions WHERE expires_at < %s OR last_seen_at < %s",
                (now, now - self._idle_seconds),
            )
            await conn.execute(
                "INSERT INTO sessions "
                "(session_id, subject, claims, token_cache, expires_at, last_seen_at) "
                "VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT (session_id) DO UPDATE SET "
                "subject = EXCLUDED.subject, claims = EXCLUDED.claims, "
                "token_cache = EXCLUDED.token_cache, expires_at = EXCLUDED.expires_at, "
                "last_seen_at = EXCLUDED.last_seen_at",
                (
                    session.session_id,
                    session.subject,
                    Jsonb(session.claims),
                    self._cipher.encrypt(session.token_cache),
                    session.expires_at,
                    last_seen_at,
                ),
            )

    async def get_session(self, session_id: str) -> Session | None:
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                "SELECT subject, claims, token_cache, expires_at, last_seen_at FROM sessions "
                "WHERE session_id = %s",
                (session_id,),
            )
            row = await cur.fetchone()
        if row is None:
            return None
        subject, claims, stored_cache, expires_at, last_seen_at = row
        now = _now()
        if expires_at < now or last_seen_at < now - self._idle_seconds:
            return None
        try:
            token_cache = self._cipher.decrypt(stored_cache)
        except ValueError:
            await self.delete_session(session_id)
            return None
        if last_seen_at < now - TOUCH_INTERVAL_SECONDS:
            async with self._pool.connection() as conn:
                await conn.execute(
                    "UPDATE sessions SET last_seen_at = %s WHERE session_id = %s",
                    (now, session_id),
                )
            last_seen_at = now
        return Session(
            session_id=session_id,
            subject=subject,
            claims=claims,
            token_cache=token_cache,
            expires_at=expires_at,
            last_seen_at=last_seen_at,
        )

    async def update_token_cache(self, session_id: str, token_cache: str) -> None:
        async with self._pool.connection() as conn:
            await conn.execute(
                "UPDATE sessions SET token_cache = %s WHERE session_id = %s",
                (self._cipher.encrypt(token_cache), session_id),
            )

    async def delete_session(self, session_id: str) -> None:
        async with self._pool.connection() as conn:
            await conn.execute("DELETE FROM sessions WHERE session_id = %s", (session_id,))
