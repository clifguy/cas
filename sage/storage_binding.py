"""Durable-storage binding for the SAGE deployment profiles (CAS-ADR-042).

A deployment profile co-binds the adapter ports as one switch; this module
carries the storage half of that bundle for both profiles. The binding object is
a *provisioner*: it opens a vault's graph and content stores together over one
per-vault connection pool, because the two stores co-vary as one binding. The
two profiles share the same provisioner and differ only in how it
authenticates: the local profile reads a password from the environment (or
peer-authenticates a unix socket), the cloud profile injects a per-connection
managed-identity Entra token (``managed_identity=True``) because its endpoint
has password auth disabled.

This module sits in the wiring layer (alongside the service initializer): it
imports the storage adapters and the stack config, and nothing below
``sage.storage`` / ``sage.adapters`` imports it back.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sage.adapters.interfaces import ContentStore, GraphStore
from sage.config import SageCoreConfig, StackPostgresConfig
from sage.instrumentation.timing import NULL_QUERY_TIMER, NullQueryTimer, QueryTimer

# A vault whose Postgres server is reachable opens its pool well inside this
# bound; an unreachable server fails the vault's load loudly at startup
# instead of hanging it.
_POOL_OPEN_TIMEOUT_SECONDS = 10.0


@dataclass
class VaultStorageHandle:
    """One vault's opened durable-storage pair plus the resource backing it.

    ``graph_store`` / ``content_store`` are ``None`` for the slots the caller
    did not request (it injects its own). :meth:`close` releases the backing
    resource the handle owns — the per-vault connection pool. Idempotent;
    closing the stores themselves remains the service teardown's job so
    injected stores keep their existing lifecycle.
    """

    graph_store: GraphStore | None
    content_store: ContentStore | None
    pool: Any | None = None
    _closed: bool = field(default=False, repr=False)

    async def close(self) -> None:
        """Release the handle's backing resource (idempotent)."""
        if self._closed:
            return
        self._closed = True
        if self.pool is not None:
            await self.pool.close()


class VaultStorageProvisioner(ABC):
    """Port for the durable-storage binding: opens one vault's store pair.

    One provisioner instance serves the whole stack; ``open_vault_storage``
    is called once per vault at service initialization. The ``need_graph`` /
    ``need_content`` flags preserve injection precedence — a slot the caller
    fills itself must not be constructed (nor pay its backing cost) here.
    """

    @abstractmethod
    async def open_vault_storage(
        self,
        vault_id: str,
        brain_root: Path,
        *,
        need_graph: bool,
        need_content: bool,
        storage_timer: QueryTimer | NullQueryTimer = NULL_QUERY_TIMER,
        content_timer: QueryTimer | NullQueryTimer = NULL_QUERY_TIMER,
        migrate: bool = False,
    ) -> VaultStorageHandle:
        """Open the requested stores for one vault and return their handle."""


class PostgresVaultStorageProvisioner(VaultStorageProvisioner):
    """The Postgres binding: both stores over one per-vault connection pool.

    A vault's rows live in a Postgres schema named by its vault id, bound via
    the pool's ``search_path``. Opening a vault validates the id as a schema
    identifier, idempotently bootstraps the schema over a plain connection
    (provisioning must precede the pool: the pool's configure hook registers
    the pgvector type, which requires ``vector`` to be installed in the
    database), then opens the pool and constructs both stores over it. The
    returned handle owns the pool; the database itself must already exist (see
    docs/process/postgres-local-runtime.md).

    ``create_extensions`` controls whether the bootstrap issues the
    ``CREATE EXTENSION`` statements. The default creates them (the local
    runtime's connecting role can, and nothing creates them ahead of it). When
    False, the extensions are an out-of-band administrator precondition the
    bootstrap relies on rather than creates -- the pool's pgvector type
    registration only needs ``vector`` *present* in the database, regardless of
    which role installed it.
    """

    def __init__(
        self,
        postgres_config: StackPostgresConfig,
        *,
        connection_class=None,
        read_env_password: bool = True,
        create_extensions: bool = True,
    ) -> None:
        self.postgres_config = postgres_config
        # The cloud (managed-identity) binding supplies a token-auth connection
        # class and turns off the env password: the Entra-only endpoint rejects a
        # password, and the token is injected per-connection by the connection
        # class instead. The local binding leaves both at their defaults.
        self._connection_class = connection_class
        self._read_env_password = read_env_password
        self._conn_environ = None if read_env_password else {}
        self._create_extensions = create_extensions

    def _connection_params(self, search_path: str | None = None):
        from sage.storage.postgres.pool import PostgresConnectionParams

        pg = self.postgres_config
        return PostgresConnectionParams(
            host=pg.host,
            port=pg.port,
            database=pg.database,
            user=pg.user,
            sslmode=pg.sslmode,
            search_path=search_path,
            min_pool_size=pg.min_pool_size,
            max_pool_size=pg.max_pool_size,
        )

    async def _bootstrap(self, vault_id: str) -> None:
        """Idempotently provision the vault's schema over a plain connection."""
        import psycopg
        from psycopg.conninfo import make_conninfo

        from sage.storage.postgres.pool import build_conn_kwargs
        from sage.storage.postgres.schema import bootstrap_schema

        conninfo = make_conninfo(**build_conn_kwargs(self._connection_params(), self._conn_environ))
        conn_class = self._connection_class or psycopg.AsyncConnection
        async with await conn_class.connect(conninfo, autocommit=True) as conn:
            await bootstrap_schema(
                conn,
                schema=vault_id,
                extensions=list(self.postgres_config.extensions),
                create_extensions=self._create_extensions,
            )

    async def open_vault_storage(
        self,
        vault_id: str,
        brain_root: Path,
        *,
        need_graph: bool,
        need_content: bool,
        storage_timer: QueryTimer | NullQueryTimer = NULL_QUERY_TIMER,
        content_timer: QueryTimer | NullQueryTimer = NULL_QUERY_TIMER,
        migrate: bool = False,
    ) -> VaultStorageHandle:
        from sage.storage.postgres.schema import validate_schema_name

        try:
            validate_schema_name(vault_id)
        except ValueError as exc:
            raise ValueError(
                f"vault id {vault_id!r} is not usable as a Postgres schema name "
                f"({exc}); the Postgres binding names each vault's schema by its "
                "vault id. Rename the vault."
            ) from exc

        from sage.storage.postgres.pool import create_pool

        await self._bootstrap(vault_id)
        pool = create_pool(
            self._connection_params(search_path=f"{vault_id},public"),
            connection_class=self._connection_class,
            environ=self._conn_environ,
        )
        await pool.open(wait=True, timeout=_POOL_OPEN_TIMEOUT_SECONDS)
        try:
            graph_store: GraphStore | None = None
            content_store: ContentStore | None = None
            if need_graph:
                from sage.storage.postgres.graph_store import PostgresGraphStore

                graph_store = PostgresGraphStore(pool, query_timer=storage_timer)
                await graph_store.initialize(migrate=migrate)
            if need_content:
                from sage.adapters.content_store_postgres import PostgresContentStore

                content_store = PostgresContentStore(pool, query_timer=content_timer)
        except BaseException:
            await pool.close()
            raise
        return VaultStorageHandle(graph_store=graph_store, content_store=content_store, pool=pool)


def build_stack_storage_provisioner(
    stack_config: SageCoreConfig, *, managed_identity: bool = False
) -> VaultStorageProvisioner:
    """Construct the stack-wide storage provisioner (CAS-ADR-042).

    Postgres is the storage port's sole binding: this always returns a
    ``PostgresVaultStorageProvisioner`` over the stack's ``postgres`` block.

    ``managed_identity`` is the cloud profile's selector: when set, the
    provisioner authenticates with a per-connection managed-identity Entra
    token instead of an env password -- the cloud endpoint has password auth
    disabled -- and it is built with ``create_extensions=False``: the Entra
    endpoint rejects an untrusted ``CREATE EXTENSION`` from any role outside
    ``azure_pg_admin`` (the command-level privilege check is not bypassed by
    ``IF NOT EXISTS`` even when the extension is already present), so the
    per-vault self-bootstrap relies on an administrator having pre-created the
    extensions rather than creating them itself.
    """
    if managed_identity:
        from sage.storage.postgres.managed_identity import (
            get_postgres_credential,
            make_token_auth_connection_class,
        )

        connection_class = make_token_auth_connection_class(get_postgres_credential())
        return PostgresVaultStorageProvisioner(
            stack_config.postgres,
            connection_class=connection_class,
            read_env_password=False,
            create_extensions=False,
        )
    return PostgresVaultStorageProvisioner(stack_config.postgres)
