"""Shared pytest fixtures for CAS test suite."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest
import yaml

from sage.mcp_init import _timing_handlers
from tests.helpers.pg_isolation import (
    IsolatedTestDB,
    create_database,
    create_extensions,
    dbname_of,
    derive_throwaway_dbname,
    drop_database,
    rewrite_dsn_dbname,
    sweep_orphan_databases,
)
from tests.helpers.schema_validation import SchemaValidator
from tests.helpers.timing_leaks import (
    alive_timing_thread_idents,
    check_and_reap_timing_leaks,
)
from tests.helpers.workers import worker_budget

# Default to stub providers on every pytest invocation. Stubs both the
# embedding provider (Avoids accidental ~270 MB nomic loads) and the
# abstraction provider (Prevents multi-GB Qwen3 MLX/Metal loads in tests
# alongside a running MCP server, which is the trigger profile documented in
# F-8). setdefault preserves explicit overrides, including per-test
# monkeypatch.delenv calls (see test_di_005). The tests that deliberately
# load the real Qwen3 model form a separate opt-in tier behind
# SAGE_TEST_REAL_MODELS=1 (tests/helpers/real_models.py).
os.environ.setdefault("SAGE_TEST_STUB_PROVIDERS", "1")

PROJECT_ROOT = Path(__file__).resolve().parent.parent
INVALID_FIXTURES_DIR = PROJECT_ROOT / "tests" / "fixtures" / "invalid"


def pytest_xdist_auto_num_workers(config: pytest.Config) -> int:
    """Size ``-n auto`` from the CPU count and the Postgres connection ceiling.

    Each worker holds its own storage pool, so the server's ``max_connections``
    binds before the cores do on a workstation; without a configured server
    the CPU count stands alone. Runs on the controller before the session
    provisioner rewrites ``SAGE_TEST_PG_DSN``, so it asks the maintenance
    database. See ``tests/helpers/workers.py`` for the derivation.
    """
    max_connections: int | None = None
    dsn = os.environ.get("SAGE_TEST_PG_DSN")
    if dsn:
        import psycopg

        try:
            with psycopg.connect(dsn, autocommit=True, connect_timeout=5) as conn:
                row = conn.execute("SHOW max_connections").fetchone()
                max_connections = int(row[0]) if row else None
        except psycopg.Error:
            max_connections = None
    return worker_budget(os.cpu_count(), max_connections)


@pytest.fixture(scope="session")
def schema_validator() -> SchemaValidator:
    """Session-scoped SchemaValidator with pre-built registry."""
    return SchemaValidator()


@pytest.fixture(scope="session")
def project_root() -> Path:
    """Path to the CAS repository root."""
    return PROJECT_ROOT


# Host name pinned into the sentinel stack config when SAGE_TEST_PG_DSN is
# unset. Deliberately unresolvable: any test that reaches storage without a
# configured test server fails fast at connect with this name in the error,
# instead of silently opening the developer's live local-socket Postgres.
SENTINEL_PG_HOST = "sage-test-pg-dsn-unset.invalid"


def _test_stack_config():
    """Build the stack config every test runs under.

    With ``SAGE_TEST_PG_DSN`` set, storage binds to that server (a throwaway;
    the suite creates and drops schemas in it). Without it, storage binds to
    ``SENTINEL_PG_HOST`` so an accidental open cannot touch live data.
    """
    from sage.config import SageCoreConfig, StackPostgresConfig

    dsn = os.environ.get("SAGE_TEST_PG_DSN")
    if dsn:
        from psycopg.conninfo import conninfo_to_dict

        parsed = conninfo_to_dict(dsn)
        postgres = StackPostgresConfig(
            host=parsed.get("host"),
            port=int(parsed.get("port") or 5432),
            database=str(parsed.get("dbname") or "postgres"),
            user=parsed.get("user"),
            extensions=["vector", "pgstattuple"],
        )
        password = parsed.get("password")
    else:
        postgres = StackPostgresConfig(host=SENTINEL_PG_HOST)
        password = None
    return SageCoreConfig(storage_backend="postgres", postgres=postgres), password


@pytest.fixture(scope="session", autouse=True)
def _provision_isolated_test_database():
    """Give this pytest process its own throwaway Postgres database.

    ``SAGE_TEST_PG_DSN`` names a *maintenance* database on a server; this fixture
    derives a per-process ``sage_test_db_<hex>`` database on that server, seeds
    its extensions, and rewrites ``SAGE_TEST_PG_DSN`` in the environment to point
    at it. Every downstream DSN reader -- the stack-config prongs, ``pg_dsn``,
    ``pg_schema``, ``_pg_hygiene_session`` -- then binds to the private database,
    so concurrent pytest processes never collide on the shared ``test_vault``
    schema. Yields an ``IsolatedTestDB`` (or ``None`` when no server is
    configured, leaving the no-Postgres path untouched).

    Autouse + session scope, plus the explicit dependency the DSN readers take on
    this fixture, guarantees the env rewrite lands before any of them read it.
    """
    maintenance_dsn = os.environ.get("SAGE_TEST_PG_DSN")
    if not maintenance_dsn:
        yield None
        return

    maintenance_db = dbname_of(maintenance_dsn)
    throwaway_dbname = derive_throwaway_dbname()
    throwaway_dsn = rewrite_dsn_dbname(maintenance_dsn, throwaway_dbname)

    # Reclaim databases crashed prior runs left behind. Cross-process-safe: only
    # idle sage_test_db_* databases are dropped, never a live sibling's.
    sweep_orphan_databases(maintenance_dsn)
    create_database(maintenance_dsn, throwaway_dbname, maintenance_db=maintenance_db)
    create_extensions(throwaway_dsn)

    # Hold a connection for the whole session so the throwaway always has a
    # backend attached. A sibling process's orphan sweep treats a database
    # with no backend as a crashed run's leftover, and nothing else connects
    # to this one until its first storage test; the sweep's age gate is the
    # second layer behind this one.
    import psycopg

    keepalive = psycopg.connect(throwaway_dsn, autocommit=True)
    keepalive_row = keepalive.execute("SELECT pg_backend_pid()").fetchone()
    keepalive_pid = int(keepalive_row[0]) if keepalive_row else None

    os.environ["SAGE_TEST_PG_DSN"] = throwaway_dsn
    try:
        yield IsolatedTestDB(
            maintenance_dsn=maintenance_dsn,
            maintenance_dbname=maintenance_db,
            throwaway_dsn=throwaway_dsn,
            throwaway_dbname=throwaway_dbname,
            keepalive_backend_pid=keepalive_pid,
        )
    finally:
        os.environ["SAGE_TEST_PG_DSN"] = maintenance_dsn
        keepalive.close()
        drop_database(maintenance_dsn, throwaway_dbname, force=True, maintenance_db=maintenance_db)


@pytest.fixture(scope="session")
def _test_stack_config_path(_provision_isolated_test_database, tmp_path_factory) -> Path:
    """Write the test stack config to a YAML file, for the env-path prong.

    Lifespan-shaped code paths (``create_app``, ``python -m sage``) do not
    read the module singleton -- they call ``load_stack_config_or_default``,
    which without an override loads the committed ``sage/config.yaml`` whose
    postgres block points at the local socket: the live database. Pointing
    ``SAGE_CONFIG_PATH`` here makes those paths resolve to the same test
    server (or sentinel) as everything else.
    """
    cfg, _password = _test_stack_config()
    pg = cfg.postgres
    doc: dict[str, Any] = {
        "storage_backend": "postgres",
        "postgres": {
            "host": pg.host,
            "port": pg.port,
            "database": pg.database,
            "extensions": list(pg.extensions),
        },
    }
    if pg.user:
        doc["postgres"]["user"] = pg.user
    path = tmp_path_factory.mktemp("stack_config") / "test_stack_config.yaml"
    path.write_text(yaml.safe_dump(doc))
    return path


@pytest.fixture(autouse=True)
def _pin_test_stack_config(monkeypatch, _provision_isolated_test_database, _test_stack_config_path):
    """Pin every stack-config resolution path to the test Postgres.

    Three prongs, all derived from ``SAGE_TEST_PG_DSN`` (or the sentinel):

    1. The ``sage.mcp_init`` module singleton -- uninjected service
       construction resolves its storage provisioner from
       ``get_stack_config()``, whose empty default points at the local
       socket, the developer's live database.
    2. ``SAGE_CONFIG_PATH`` -- lifespan-shaped paths re-load the stack
       config from disk (overwriting the singleton), and subprocesses never
       see the singleton at all.
    3. ``PGHOST`` -- the libpq-level backstop: a connection built from a
       default-constructed config (host None -> local socket) resolves the
       unroutable sentinel instead of the socket and fails fast.

    Re-applied per test (and restored on teardown) so no test can leak a
    cleared or custom stack config into the next one. Tests that assert
    stack-config loading behavior set their own config or env in-body,
    which overrides these pins.
    """
    import sage.mcp_init as mcp_init

    prior = mcp_init._stack_config
    cfg, password = _test_stack_config()
    if password:
        monkeypatch.setenv("SAGE_PG_PASSWORD", str(password))
    monkeypatch.setenv("SAGE_CONFIG_PATH", str(_test_stack_config_path))
    monkeypatch.setenv("PGHOST", SENTINEL_PG_HOST)
    mcp_init.set_stack_config(cfg)
    yield
    mcp_init.set_stack_config(prior)


@pytest.fixture(scope="session")
def _pg_hygiene_session(_provision_isolated_test_database):
    """Session connection + schema baseline for per-test vault-schema cleanup.

    ``None`` when no test server is configured (nothing can have connected).
    Depends on the isolation provisioner so it connects to this process's
    throwaway database, not the shared maintenance database.
    """
    dsn = os.environ.get("SAGE_TEST_PG_DSN")
    if not dsn:
        yield None
        return
    import psycopg

    conn = psycopg.connect(dsn, autocommit=True)
    conn.execute("SET lock_timeout = '5s'")
    baseline = {row[0] for row in conn.execute("SELECT nspname FROM pg_namespace").fetchall()}
    try:
        yield (conn, baseline)
    finally:
        conn.close()


@pytest.fixture(autouse=True)
def _drop_leaked_vault_schemas(_pg_hygiene_session):
    """Drop vault schemas a test provisioned, restoring per-test isolation.

    The Postgres binding names each vault's schema by its vault id, and many
    tests open the same ids (``test_vault`` above all) without a per-test
    suffix -- under the embedded binding a fresh tmp directory per test gave
    isolation structurally; here the schema outlives the test unless dropped.
    Schemas present at session start and the ``sage_test_*`` session schemas
    (managed by their own fixtures) are left alone.
    """
    yield
    if _pg_hygiene_session is None:
        return
    conn, baseline = _pg_hygiene_session
    rows = conn.execute(
        "SELECT nspname FROM pg_namespace "
        "WHERE nspname NOT LIKE 'pg\\_%' AND nspname NOT LIKE 'sage\\_test\\_%'"
    ).fetchall()
    for (name,) in rows:
        if name not in baseline:
            conn.execute(f'DROP SCHEMA IF EXISTS "{name}" CASCADE')


@pytest.fixture(autouse=True)
def _isolated_vault_registry():
    """Per-test isolation of sage.mcp_server._vaults.

    Production route handlers and MCP tools both resolve vaults through the
    singleton VaultRegistryService, which is bound at module import to the
    global `_vaults` dict in sage.mcp_server. Fixtures that bypass the
    FastAPI lifespan write into that dict directly (via _initialize_services
    or by setting `_vaults[id] = services`), and not every fixture cleans
    up its entries. Without isolation, a test's view of the registry is the
    union of every prior test's leftover state. This fixture clears the
    dict before each test (autouse runs first within scope) and again on
    teardown so the next test starts clean.
    """
    import sage.mcp_server as _mcp

    _mcp._vaults.clear()
    yield
    _mcp._vaults.clear()


@pytest.fixture(autouse=True)
def _redirect_vaults_root(tmp_path_factory, monkeypatch):
    """Redirect ``_VAULTS_ROOT`` away from ``~/sage_vaults/`` for every test.

    ``sage.vault_management.config_path_for_vault`` (used by both REST
    create-vault and update-config paths) resolves the on-disk
    ``vault_config.yaml`` location from a module-level ``_VAULTS_ROOT``
    constant that points at ``~/sage_vaults`` in production. Tests that
    exercise those endpoints (e.g. tests/sage/test_vault_config_api.py)
    would otherwise write YAML into the real user vault tree and leave
    orphan vault directories behind, which then show up in
    ``list_vaults`` after the server restarts.

    ``sage.services.vault_registry._VAULTS_ROOT`` is the same constant
    re-imported for default-config path construction; patch both so a
    test using ``VaultRegistryService.get_default_config`` without overriding
    storage_root/brain_root also lands in tmp space.

    ``SAGE_VAULT_ROOT`` is cleared rather than left alone. Root resolution
    honors that variable *ahead of* the patched constant, so on a machine that
    exports it the redirect below would be silently outranked and writes would
    land in the operator's real vault tree. Clearing it rather than pointing it
    at ``fake_root`` keeps the patched constant authoritative, which is what
    the tests that install their own per-test root rely on. Tests that need the
    variable set do so themselves; their ``monkeypatch.setenv`` runs after this
    fixture and wins.

    No test in this suite can go red when that ``delenv`` is removed: the
    condition it defends against is an ambient variable present at process
    start, which a test body cannot install ahead of an autouse fixture. It is
    verified out-of-band instead, and reproducibly -- export ``SAGE_VAULT_ROOT``
    to an empty scratch directory, run the vault-config write tests, and confirm
    that directory is still empty afterward. Treat the guard as load-bearing
    despite the green suite: deleting it restores the failure silently on any
    machine that exports the variable.
    """
    from sage import vault_management
    from sage.services import vault_registry

    fake_root = tmp_path_factory.mktemp("sage_vaults_isolated")
    monkeypatch.delenv("SAGE_VAULT_ROOT", raising=False)
    monkeypatch.setattr(vault_management, "_VAULTS_ROOT", fake_root)
    monkeypatch.setattr(vault_registry, "_VAULTS_ROOT", fake_root)
    yield fake_root


@pytest.fixture(autouse=True)
def _fail_on_leaked_timing_resources():
    """Fail (and reap) when a test leaks a per-vault timing handler or thread.

    Any test that builds real services with timing enabled (the default) and
    tears down without releasing them leaves the ``timing.log`` handler attached
    to the process-global timing loggers and the ``VaultTimingThread``
    running. Neither surfaces as an "unclosed file" ``ResourceWarning`` — the
    loggers keep the handler reachable, so CPython never garbage-collects it and
    ``logging.shutdown()`` closes it cleanly only at interpreter exit — so this
    guard asserts on the observable that actually moves: a net-new entry in the
    ``_timing_handlers`` registry or a net-new live ``sage-timing-flush`` thread
    introduced by the test. Lives at the root conftest so every test tree
    (``tests/app``, ``tests/sage``, and any future tree) shares one
    garbage-collection-independent check.

    The remedy at a leaking site is to build services through
    ``initialize_services_for_test`` or to call ``services.close_timing()``
    before ``graph_store.close()`` on teardown.
    """
    handlers_before = set(_timing_handlers)
    threads_before = alive_timing_thread_idents()
    yield
    check_and_reap_timing_leaks(handlers_before, threads_before)


def load_invalid_fixture(component: str, filename: str) -> Any:
    """Load an invalid fixture YAML file.

    Args:
        component: "sage" or "root_harness"
        filename: YAML filename within the component's invalid fixtures dir
    """
    path = INVALID_FIXTURES_DIR / component / filename
    with open(path) as f:
        return yaml.safe_load(f)
