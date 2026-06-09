"""Async Postgres driver + connection pool for the SAGE storage engine (CAS-ADR-042).

The driver the store adapters share: psycopg3 over a ``psycopg_pool``
``AsyncConnectionPool``, with the pgvector type registered on every pooled
connection so embedding columns round-trip as Python sequences. Concurrency is
served by Postgres itself, so this pool has no single-writer ceiling.

The driver imports are lazy so this module stays importable without psycopg
installed, and the connection parameters are a local dataclass rather than the
stack-config model, so this module -- under ``sage.storage`` -- depends on no
upstream SAGE layer. The translation from stack config to these parameters lives
in the wiring layer (the provisioning CLI and the eventual storage binding), not
here. The password is never carried in configuration: it is read from the
environment, mirroring how the hosted abstraction provider reads its key.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

# Environment variable carrying the Postgres password for a TCP/hosted endpoint.
# A local unix-socket connection authenticates the OS user by peer and needs no
# password, so this is typically unset on the on-box target.
PASSWORD_ENV_VAR = "SAGE_PG_PASSWORD"  # noqa: S105 -- env-var name, not a secret


@dataclass(frozen=True)
class PostgresConnectionParams:
    """Connection parameters for the async pool.

    A superset of the stack-config ``postgres`` block: it adds the optional
    ``search_path`` (used by the test harness to bind a disposable schema) and
    leaves the password out entirely -- that is read from the environment by
    :func:`build_conn_kwargs`.
    """

    host: str | None = None
    port: int = 5432
    database: str = "sage"
    user: str | None = None
    sslmode: str | None = None
    search_path: str | None = None
    min_pool_size: int = 1
    max_pool_size: int = 10


def build_conn_kwargs(
    params: PostgresConnectionParams,
    environ: dict[str, str] | None = None,
) -> dict[str, str]:
    """Compose libpq connection keyword arguments from ``params`` and the env.

    Pure string composition (no driver import). A null ``host`` is omitted so
    libpq falls back to the local unix socket; a null ``user`` is omitted so
    libpq falls back to the operating-system user. The password is read only from
    ``$SAGE_PG_PASSWORD`` -- never from ``params`` -- so no credential can be
    sourced from configuration. ``search_path``, when set, is passed through the
    libpq ``options`` startup parameter.
    """
    env = os.environ if environ is None else environ
    kwargs: dict[str, str] = {"dbname": params.database, "port": str(params.port)}
    if params.host is not None:
        kwargs["host"] = params.host
    if params.user is not None:
        kwargs["user"] = params.user
    if params.sslmode is not None:
        kwargs["sslmode"] = params.sslmode
    password = env.get(PASSWORD_ENV_VAR)
    if password:
        kwargs["password"] = password
    if params.search_path is not None:
        kwargs["options"] = f"-c search_path={params.search_path}"
    return kwargs


async def configure_connection(conn) -> None:
    """Pool connection-configure hook: register the pgvector type.

    Runs once per connection the pool opens, so ``vector`` columns adapt to and
    from Python sequences on every pooled connection, not just a bare connect.
    """
    from pgvector.psycopg import register_vector_async

    await register_vector_async(conn)


def _build_pool(conninfo: str, *, min_size: int, max_size: int):
    from psycopg_pool import AsyncConnectionPool

    # open=False: an async pool is opened with ``await pool.open()`` (or an
    # ``async with`` block) by the caller; it cannot be opened in __init__.
    return AsyncConnectionPool(
        conninfo,
        min_size=min_size,
        max_size=max_size,
        configure=configure_connection,
        open=False,
    )


def create_pool(params: PostgresConnectionParams):
    """Build (unopened) an ``AsyncConnectionPool`` from connection parameters.

    The configuration-driven factory the store adapters use. The returned pool
    is not yet open; the caller opens it with ``await pool.open()`` or an
    ``async with`` block.
    """
    from psycopg.conninfo import make_conninfo

    return _build_pool(
        make_conninfo(**build_conn_kwargs(params)),
        min_size=params.min_pool_size,
        max_size=params.max_pool_size,
    )


def pool_from_conninfo(
    conninfo: str,
    *,
    search_path: str | None = None,
    min_size: int = 1,
    max_size: int = 10,
):
    """Build (unopened) an ``AsyncConnectionPool`` from a raw libpq conninfo/URL.

    The DSN-driven factory the storage test harness uses: it preserves any
    password embedded in ``conninfo`` and layers a ``search_path`` over it (to
    bind a disposable schema) without routing through the env-only password rule
    of :func:`build_conn_kwargs`.
    """
    from psycopg.conninfo import conninfo_to_dict, make_conninfo

    parsed = conninfo_to_dict(conninfo)
    if search_path is not None:
        parsed["options"] = f"-c search_path={search_path}"
    return _build_pool(make_conninfo(**parsed), min_size=min_size, max_size=max_size)
