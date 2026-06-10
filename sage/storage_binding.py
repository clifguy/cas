"""Durable-storage binding for the local deployment profile (CAS-ADR-042).

A deployment profile co-binds the adapter ports as one switch; this module
carries the storage half of the local profile's bundle. The binding object is
a *provisioner*: it opens a vault's graph and content stores together, because
the two stores co-vary as one binding — the Postgres pair shares a per-vault
connection pool, and the embedded SQLite/LanceDB pair is the one coherent
fallback the stack config's ``storage_backend`` key can select instead.

:func:`build_stack_storage_provisioner` is the dispatch between the two
backends. Mirroring the abstraction provider's dispatch contract, a test
environment override (``SAGE_TEST_STORAGE_BACKEND``) is consulted before the
config key, so the test suite pins service construction to the embedded
stores while the committed configuration selects Postgres.

This module sits in the wiring layer (alongside the service initializer): it
imports the storage adapters and the stack config, and nothing below
``sage.storage`` / ``sage.adapters`` imports it back.
"""

from __future__ import annotations

import os
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sage.adapters.interfaces import ContentStore, GraphStore
from sage.config import SageCoreConfig, StackPostgresConfig
from sage.instrumentation.timing import NULL_QUERY_TIMER, NullQueryTimer, QueryTimer

# Environment override for the storage-backend dispatch, consulted before the
# stack config's ``storage_backend`` key. The test suite sets it to
# ``embedded`` process-wide so the hundreds of tests that construct services
# without injecting stores never require a provisioned Postgres; tests that
# exercise the Postgres binding clear it per-test.
STORAGE_BACKEND_ENV_VAR = "SAGE_TEST_STORAGE_BACKEND"

_VALID_BACKENDS = ("postgres", "embedded")

# A vault whose Postgres server is reachable opens its pool well inside this
# bound; an unreachable server fails the vault's load loudly at startup
# instead of hanging it.
_POOL_OPEN_TIMEOUT_SECONDS = 10.0


@dataclass
class VaultStorageHandle:
    """One vault's opened durable-storage pair plus the resource backing it.

    ``graph_store`` / ``content_store`` are ``None`` for the slots the caller
    did not request (it injects its own). :meth:`close` releases the backing
    resource the handle owns — the per-vault connection pool on the Postgres
    binding, nothing on the embedded binding (its stores close through their
    own ``close``). Idempotent; closing the stores themselves remains the
    service teardown's job so injected stores keep their existing lifecycle.
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


class EmbeddedVaultStorageProvisioner(VaultStorageProvisioner):
    """The embedded fallback binding: SQLite graph + LanceDB content.

    Reproduces the construction the service initializer carried before the
    storage seam existed: an initialized ``SqliteGraphStore`` at
    ``brain_root/graph.db`` and a ``LanceDBContentStore`` over ``brain_root``.
    The handle owns no backing resource beyond the stores themselves.
    """

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
        from sage.adapters.content_store_lancedb import LanceDBContentStore
        from sage.storage.graph_store import SqliteGraphStore

        graph_store: GraphStore | None = None
        content_store: ContentStore | None = None
        if need_graph:
            graph_store = SqliteGraphStore(brain_root / "graph.db", query_timer=storage_timer)
            await graph_store.initialize(migrate=migrate)
        if need_content:
            content_store = LanceDBContentStore(
                brain_root,
                migrate=migrate,
                query_timer=content_timer,
            )
        return VaultStorageHandle(graph_store=graph_store, content_store=content_store)


class PostgresVaultStorageProvisioner(VaultStorageProvisioner):
    """The Postgres binding: both stores over one per-vault connection pool.

    A vault's rows live in a Postgres schema named by its vault id, bound via
    the pool's ``search_path``. Opening a vault validates the id as a schema
    identifier, idempotently bootstraps the schema over a plain connection
    (provisioning must precede the pool: the pool's configure hook registers
    the pgvector type, which exists only after ``CREATE EXTENSION vector``),
    then opens the pool and constructs both stores over it. The returned
    handle owns the pool; the database itself must already exist (see
    docs/process/postgres-local-runtime.md).
    """

    def __init__(self, postgres_config: StackPostgresConfig) -> None:
        self.postgres_config = postgres_config

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

        conninfo = make_conninfo(**build_conn_kwargs(self._connection_params()))
        async with await psycopg.AsyncConnection.connect(conninfo, autocommit=True) as conn:
            await bootstrap_schema(
                conn, schema=vault_id, extensions=list(self.postgres_config.extensions)
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
                "vault id. Rename the vault or select the embedded fallback "
                "(storage_backend: embedded)."
            ) from exc

        from sage.storage.postgres.pool import create_pool

        await self._bootstrap(vault_id)
        pool = create_pool(self._connection_params(search_path=f"{vault_id},public"))
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


def build_stack_storage_provisioner(stack_config: SageCoreConfig) -> VaultStorageProvisioner:
    """Construct the stack-wide storage provisioner (CAS-ADR-042).

    Dispatch contract:
      1. ``SAGE_TEST_STORAGE_BACKEND`` set -> that backend (env override,
         topmost so the test suite can pin the embedded stores process-wide
         while the committed config selects Postgres)
      2. ``stack.storage_backend == "embedded"`` -> embedded fallback pair
      3. ``stack.storage_backend == "postgres"`` -> Postgres adapters over
         the stack's ``postgres`` connection block

    An unrecognized env value fails loud: a typo'd override silently falling
    through to the configured backend would point a test run at live data.
    """
    backend = os.environ.get(STORAGE_BACKEND_ENV_VAR) or stack_config.storage_backend
    if backend not in _VALID_BACKENDS:
        raise ValueError(
            f"Unknown storage backend {backend!r} (from {STORAGE_BACKEND_ENV_VAR}); "
            f"expected one of {_VALID_BACKENDS}."
        )
    if backend == "embedded":
        return EmbeddedVaultStorageProvisioner()
    return PostgresVaultStorageProvisioner(stack_config.postgres)
