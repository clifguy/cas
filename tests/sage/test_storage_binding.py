"""Tests for the durable-storage binding of the local profile (CAS-ADR-042).

The binding is a provisioner per backend: the embedded provisioner reproduces
the SQLite/LanceDB construction the service initializer previously hardcoded,
and the Postgres provisioner builds a per-vault search_path-bound connection
pool over the stack config's postgres block. `build_stack_storage_provisioner`
is the dispatch between them: the `SAGE_TEST_STORAGE_BACKEND` environment
override first, then the stack config's `storage_backend` key.

Test IDs follow STO-NNN (STOrage binding).
"""

import uuid

import pytest

from sage.config import SageCoreConfig
from sage.storage_binding import (
    STORAGE_BACKEND_ENV_VAR,
    EmbeddedVaultStorageProvisioner,
    PostgresVaultStorageProvisioner,
    build_stack_storage_provisioner,
)


def test_sto_001_config_key_selects_the_provisioner(monkeypatch):
    """With the env override unset, `storage_backend: embedded` dispatches to
    the embedded provisioner and `storage_backend: postgres` to the Postgres
    provisioner carrying the stack's postgres connection block.

    Happy path for the selector key. The Postgres provisioner must carry the
    config's postgres block (not a default-constructed one), or a non-default
    host/database in sage/config.yaml would be silently ignored.
    """
    monkeypatch.delenv(STORAGE_BACKEND_ENV_VAR, raising=False)

    embedded = build_stack_storage_provisioner(
        SageCoreConfig.model_validate({"storage_backend": "embedded"})
    )
    assert isinstance(embedded, EmbeddedVaultStorageProvisioner)

    cfg = SageCoreConfig.model_validate(
        {"storage_backend": "postgres", "postgres": {"database": "sage_sto_001"}}
    )
    postgres = build_stack_storage_provisioner(cfg)
    assert isinstance(postgres, PostgresVaultStorageProvisioner)
    assert postgres.postgres_config.database == "sage_sto_001"


def test_sto_002_env_override_wins_over_config(monkeypatch):
    """`SAGE_TEST_STORAGE_BACKEND` overrides the config key in both
    directions, mirroring the SAGE_TEST_STUB_PROVIDERS dispatch contract.

    Anti-coincidental-pass: this is the property the whole test suite's
    insulation depends on (tests/conftest.py pins the env to `embedded`
    while the committed config selects `postgres`). A dispatch that read the
    config before the env would fail both directions here while STO-001
    still passed.
    """
    monkeypatch.setenv(STORAGE_BACKEND_ENV_VAR, "postgres")
    via_env_pg = build_stack_storage_provisioner(
        SageCoreConfig.model_validate({"storage_backend": "embedded"})
    )
    assert isinstance(via_env_pg, PostgresVaultStorageProvisioner)

    monkeypatch.setenv(STORAGE_BACKEND_ENV_VAR, "embedded")
    via_env_embedded = build_stack_storage_provisioner(
        SageCoreConfig.model_validate({"storage_backend": "postgres"})
    )
    assert isinstance(via_env_embedded, EmbeddedVaultStorageProvisioner)

    monkeypatch.setenv(STORAGE_BACKEND_ENV_VAR, "lancedb")
    with pytest.raises(ValueError, match="lancedb"):
        build_stack_storage_provisioner(SageCoreConfig())


async def test_sto_003_embedded_provisioner_builds_todays_stores(tmp_vault_dir):
    """The embedded provisioner honors the need flags and builds the same
    SQLite/LanceDB pair the service initializer previously hardcoded: an
    initialized `SqliteGraphStore` at `brain_root/graph.db` and a
    `LanceDBContentStore` over `brain_root`.

    The need flags matter for injection precedence: a caller that injects
    only a content stub must not pay a LanceDB construction (and vice
    versa), so a flag set False must leave that slot None.
    """
    from sage.adapters.content_store_lancedb import LanceDBContentStore
    from sage.storage.graph_store import SqliteGraphStore

    brain_root = tmp_vault_dir / "brain"
    provisioner = EmbeddedVaultStorageProvisioner()

    graph_only = await provisioner.open_vault_storage(
        "test_vault", brain_root, need_graph=True, need_content=False, migrate=True
    )
    try:
        assert isinstance(graph_only.graph_store, SqliteGraphStore)
        assert graph_only.content_store is None
        # initialize() ran: the SQLite database file exists and serves reads.
        assert (brain_root / "graph.db").exists()
        assert await graph_only.graph_store.list_all_documents() == []
    finally:
        await graph_only.graph_store.close()
        await graph_only.close()

    content_only = await provisioner.open_vault_storage(
        "test_vault", brain_root, need_graph=False, need_content=True, migrate=True
    )
    assert content_only.graph_store is None
    assert isinstance(content_only.content_store, LanceDBContentStore)
    await content_only.close()  # embedded handle owns no pool; close is a no-op
    await content_only.close()  # and is idempotent


@pytest.mark.usefixtures("pg_dsn")
async def test_sto_004_postgres_provisioner_bootstraps_and_owns_the_pool(
    pg_dsn, tmp_vault_dir, monkeypatch
):
    """The Postgres provisioner validates the vault id as a schema name,
    bootstraps the per-vault schema idempotently, opens a pool bound to it,
    builds both stores over that pool, and `handle.close()` closes the pool
    idempotently.

    Bootstrap idempotence is proven by opening the same vault twice: a
    non-idempotent bootstrap fails the second open. The write/read
    round-trip (`ensure_tier3_unique_index` + existence probe) is the
    positive control proving the bootstrap actually created the tables —
    without it, a provisioner that skipped bootstrap entirely could still
    pass the isinstance assertions when the schema happened to pre-exist.
    """
    psycopg = pytest.importorskip("psycopg")

    from sage.adapters.content_store_postgres import PostgresContentStore
    from sage.storage.postgres.graph_store import PostgresGraphStore
    from tests.sage.conftest import stack_postgres_config_from_dsn

    pg_config = stack_postgres_config_from_dsn(pg_dsn, monkeypatch)
    vault_id = f"sage_test_{uuid.uuid4().hex[:10]}"
    provisioner = PostgresVaultStorageProvisioner(pg_config)
    brain_root = tmp_vault_dir / "brain"

    try:
        handle = await provisioner.open_vault_storage(
            vault_id, brain_root, need_graph=True, need_content=True, migrate=True
        )
        try:
            assert isinstance(handle.graph_store, PostgresGraphStore)
            assert isinstance(handle.content_store, PostgresContentStore)
            # Round-trip through the bootstrapped schema: a DDL write and a
            # catalog read prove the tables exist in the per-vault schema.
            await handle.graph_store.ensure_tier3_unique_index("note", "ticket_id")
            assert await handle.graph_store.tier3_unique_index_exists("note", "ticket_id")
            assert await handle.graph_store.list_all_documents() == []
        finally:
            await handle.graph_store.close()
            await handle.close()
            await handle.close()  # idempotent

        # Second open against the same vault id: bootstrap must be idempotent.
        again = await provisioner.open_vault_storage(
            vault_id, brain_root, need_graph=True, need_content=False, migrate=True
        )
        await again.graph_store.close()
        await again.close()
    finally:
        async with await psycopg.AsyncConnection.connect(pg_dsn, autocommit=True) as conn:
            await conn.execute(f'DROP SCHEMA IF EXISTS "{vault_id}" CASCADE')


async def test_sto_005_invalid_schema_vault_id_fails_loud_before_connecting(tmp_vault_dir):
    """A vault id that is not a safe Postgres schema identifier raises
    `ValueError` naming the offending id, before any connection attempt.

    The connection parameters point at a TEST-NET address that no test
    environment routes, so if the provisioner connected before validating,
    this test would hang or fail with a connection error instead of the
    immediate ValueError — that is the anti-coincidental-pass control.
    """
    from sage.config import StackPostgresConfig

    provisioner = PostgresVaultStorageProvisioner(
        StackPostgresConfig(host="203.0.113.1", database="unreachable")
    )

    for bad_vault_id in ("test-vault", "2026vault", "Test_Vault"):
        with pytest.raises(ValueError, match=bad_vault_id):
            await provisioner.open_vault_storage(
                bad_vault_id,
                tmp_vault_dir / "brain",
                need_graph=True,
                need_content=True,
            )


# ---------------------------------------------------------------------------
# create_extensions: the cloud managed-identity profile skips CREATE EXTENSION
#
# A recording connection class captures the DDL the provisioner's bootstrap runs
# without a live server: it reuses the provisioner's own connection_class
# injection seam (the same seam the cloud binding uses to inject token auth), so
# the assertions exercise the real `_bootstrap -> bootstrap_schema ->
# schema_statements -> execute` chain.
# ---------------------------------------------------------------------------


class _RecordingBootstrapConn:
    """Records the statements bootstrap_schema executes; no live Postgres.

    Doubles as the connect() result context manager and the transaction()
    context manager, the two `async with` blocks bootstrap_schema opens.
    """

    def __init__(self) -> None:
        self.statements: list[str] = []

    async def execute(self, statement: str) -> None:
        self.statements.append(statement)

    def transaction(self) -> "_RecordingBootstrapConn":
        return self

    async def __aenter__(self) -> "_RecordingBootstrapConn":
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False


def _recording_conn_class() -> type:
    """A psycopg-shaped connection class whose connect() yields a fresh recorder.

    Mirrors `await conn_class.connect(conninfo, autocommit=True)`; the opened
    recorders are exposed on the class so a test can read the executed DDL.
    """
    opened: list[_RecordingBootstrapConn] = []

    class _ConnClass:
        connections = opened

        @classmethod
        async def connect(
            cls, conninfo: str, *, autocommit: bool = False
        ) -> _RecordingBootstrapConn:
            conn = _RecordingBootstrapConn()
            opened.append(conn)
            return conn

    return _ConnClass


@pytest.mark.parametrize("create_extensions", [True, False])
async def test_sto_006_bootstrap_emits_create_extension_only_when_enabled(create_extensions):
    """The provisioner emits CREATE EXTENSION exactly when `create_extensions`
    is set, and always creates its schema and tables.

    This is the keystone for the cloud managed-identity fix: with the flag off
    (how the cloud binding constructs the provisioner), the unprivileged
    workload's self-bootstrap issues no CREATE EXTENSION — Azure rejects an
    untrusted CREATE EXTENSION from a non-azure_pg_admin role even with IF NOT
    EXISTS — and relies on the admin-pre-created extensions instead.

    Anti-coincidental-pass: the `create_extensions=True` case is the positive
    control proving the recorder and the bootstrap path actually run (a CREATE
    EXTENSION is recorded); a `_bootstrap` that hardcoded the flag rather than
    forwarding `self._create_extensions` would fail one of the two cases. The
    schema/table asserts prove the off case still bootstraps everything else.
    """
    pytest.importorskip("psycopg")
    from sage.config import StackPostgresConfig

    conn_class = _recording_conn_class()
    provisioner = PostgresVaultStorageProvisioner(
        StackPostgresConfig(host="db.example", user="svc"),
        connection_class=conn_class,
        read_env_password=False,
        create_extensions=create_extensions,
    )

    await provisioner._bootstrap("sage_test_v")

    assert len(conn_class.connections) == 1
    executed = conn_class.connections[0].statements
    assert any('CREATE SCHEMA IF NOT EXISTS "sage_test_v"' in s for s in executed)
    assert any("CREATE TABLE IF NOT EXISTS documents" in s for s in executed)
    assert any("CREATE EXTENSION" in s for s in executed) is create_extensions


def test_sto_007_local_postgres_binding_creates_extensions(monkeypatch):
    """The local Postgres binding (no managed identity) leaves extension
    creation on: the connecting role can create extensions and no admin
    bootstrap runs ahead of it.

    Anti-coincidental-pass: paired with the cloud half (CLD-005a, which asserts
    the managed-identity binding turns the flag off), a binding that set the
    flag the same way on both paths would fail one of the two.
    """
    monkeypatch.delenv(STORAGE_BACKEND_ENV_VAR, raising=False)

    prov = build_stack_storage_provisioner(
        SageCoreConfig.model_validate({"storage_backend": "postgres"})
    )
    assert isinstance(prov, PostgresVaultStorageProvisioner)
    assert prov._create_extensions is True
