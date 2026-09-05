"""SAGE-specific test fixtures.

Extends the root conftest.py fixtures with SAGE storage, services,
and stub adapters. Each test gets an isolated temp directory via
pytest's tmp_path fixture and a clean slate in the test Postgres
(per-test truncation in the session schema; leaked vault schemas are
dropped by the root conftest's hygiene fixture).
"""

import asyncio
import contextlib
import dataclasses
import os
from collections.abc import AsyncIterator, Callable
from pathlib import Path

import pytest
from fastapi import FastAPI

import sage.adapters.abstraction_qwen3 as _abstraction_qwen3
from sage.adapters.stubs import (
    FailingAbstractionProvider,
    StubAbstractionProvider,
    StubContentStore,
    StubEmbeddingProvider,
)
from sage.config import VaultConfig
from sage.mcp_init import initialize_services
from sage.models.enums import SourceType
from sage.services.graph_ops import GraphOpsService
from sage.services.ingestion import IngestionService
from sage.services.lifecycle import LifecycleService
from sage.services.metadata import MetadataService
from sage.services.user_service import UserService
from sage.source_adapters.markdown_adapter import MarkdownAdapter
from sage.storage.locks import DocumentLockManager


@pytest.fixture(autouse=True)
def _reset_generation_lock():
    """Give each test a fresh ``_generation_lock``.

    The module-level ``asyncio.Lock`` in ``sage.adapters.abstraction_qwen3``
    binds internal state to whatever event loop first awaits it.
    pytest-asyncio runs each test in a fresh loop, so without this
    reset the lock can fail later tests with
    ``RuntimeError: <Lock object> is bound to a different event loop``
    when an earlier test exercised it. The lock has no semantic state
    worth preserving between tests.
    """
    _abstraction_qwen3._generation_lock = asyncio.Lock()
    yield
    _abstraction_qwen3._generation_lock = asyncio.Lock()


@pytest.fixture
async def app_with_one_vault(minimal_config: VaultConfig) -> AsyncIterator[FastAPI]:
    """A SAGE app with ``minimal_config``'s vault initialized and registered.

    Yields the FastAPI app whose MCP mounts (``app.state.mcp_mounts``) share
    the app-populated vault registry. ``app.state.vault_registry`` is the
    process-global MCP registry, so teardown touches only the vaults this
    fixture added: each is closed through ``close_storage()`` (the contract
    ``initialize_services_for_test`` sets) and popped, and anything another
    test left registered is neither closed under it nor evicted.
    """
    from sage import mcp_server
    from sage.app import _initialize_services, create_app

    app = create_app(config=minimal_config)
    registry = mcp_server._vaults  # what app.state.vault_registry is bound to
    before = set(registry)
    await _initialize_services(
        app,
        minimal_config,
        content_store=StubContentStore(),
        embedding_provider=StubEmbeddingProvider(),
        abstraction_provider=StubAbstractionProvider(),
    )
    try:
        yield app
    finally:
        for vault_id in set(registry) - before:
            services = registry.pop(vault_id)
            services.close_timing()
            await services.close_storage()


@pytest.fixture
def tool_payload() -> Callable[[object], dict]:
    """Decoder for the JSON body FastMCP wraps a tool's dict return into.

    ``call_tool`` returns ``[TextContent(text=<json>)]`` for a dict-returning
    tool; tests that need to inspect the payload rather than substring-match
    its ``str()`` (an error envelope also names the ids it failed on) decode
    it through this.
    """
    import json

    def decode(result: object) -> dict:
        return json.loads(result[0].text)  # type: ignore[index]

    return decode


@contextlib.asynccontextmanager
async def initialize_services_for_test(config, **kwargs):
    """Async context manager wrapping ``initialize_services`` for tests.

    Guarantees that on exit (normal or exceptional) the per-vault
    ``VaultTimingThread`` is stopped, its ``timing.log`` file handle is
    released, and the graph store is closed (the first two via
    ``close_timing``). Replaces the
    ``services = await initialize_services(...); try:...; finally:
    await services.graph_store.close()`` pattern, which historically
    forgot to stop the timing thread and leaked it (and its log handle)
    into subsequent tests, polluting their caplog windows on the timing
    loggers.
    """
    services = await initialize_services(config, **kwargs)
    try:
        yield services
    finally:
        services.close_timing()
        await services.close_storage()


def stack_postgres_config_from_dsn(dsn: str, monkeypatch, extensions=("vector",)):
    """Translate a test-harness DSN into a `StackPostgresConfig`.

    The live storage binding composes its connection from the stack config
    (password from the environment, never from config), so tests that drive
    the binding against the `SAGE_TEST_PG_DSN` server need this translation.
    Any password embedded in the DSN is exported via monkeypatch so the
    pool's env-only password rule still holds.
    """
    from psycopg.conninfo import conninfo_to_dict

    from sage.config import StackPostgresConfig

    parsed = conninfo_to_dict(dsn)
    if parsed.get("password"):
        monkeypatch.setenv("SAGE_PG_PASSWORD", str(parsed["password"]))
    return StackPostgresConfig(
        host=parsed.get("host"),
        port=int(parsed.get("port") or 5432),
        database=str(parsed.get("dbname") or "postgres"),
        user=parsed.get("user"),
        extensions=list(extensions),
    )


@pytest.fixture
def tmp_vault_dir(tmp_path):
    """Create a temporary vault directory structure."""
    brain_dir = tmp_path / "brain"
    brain_dir.mkdir()
    sources_dir = tmp_path / "sources"
    sources_dir.mkdir()
    return tmp_path


@pytest.fixture
def minimal_vault_config_dict(tmp_vault_dir):
    """Minimal vault config dict for testing (base states only)."""
    return {
        "vault": {
            "id": "test_vault",
            "name": "Test Vault",
            "owner": "testuser",
            "storage_root": str(tmp_vault_dir / "sources"),
            "brain_root": str(tmp_vault_dir / "brain"),
            "visibility": "personal",
        },
        "document_types": {
            "doc_types": [
                {"value": "note", "label": "Note"},
                {"value": "memo", "label": "Memo"},
            ],
        },
        "lifecycle": {
            "base_states_required": True,
            "states": [
                {"value": "active", "label": "Active"},
                {"value": "completed", "label": "Completed"},
                {"value": "archived", "label": "Archived", "is_terminal": True},
            ],
            "transitions": [
                {"from_state": "(new)", "action": "ingest", "to_state": "active"},
                {
                    "from_state": "active",
                    "action": "supersede",
                    "to_state": "archived",
                    "creates_edge": "supersedes",
                },
                {"from_state": "active", "action": "complete", "to_state": "completed"},
                {"from_state": "active", "action": "archive", "to_state": "archived"},
                {"from_state": "completed", "action": "archive", "to_state": "archived"},
                {"from_state": "archived", "action": "reactivate", "to_state": "active"},
            ],
        },
        # Zero the abstraction-queue retry backoff so tests that exercise a
        # failing provider exhaust their attempts instantly instead of sleeping
        # through the production exponential backoff. Other abstraction defaults
        # (enabled, token budget, max_attempts) are left at their model defaults.
        "abstraction": {"retry_backoff_base_seconds": 0.0},
        "metadata_extraction": {},
        "edge_inference": {
            "tier_assignments": [
                {
                    "edge_type": "supersedes",
                    "tier": 1,
                    "inference_rules": [{"method": "version_chain"}],
                },
            ],
        },
    }


@pytest.fixture
def minimal_config(minimal_vault_config_dict):
    """Parsed VaultConfig from minimal dict."""
    return VaultConfig.model_validate(minimal_vault_config_dict)


@pytest.fixture
def extended_vault_config_dict(minimal_vault_config_dict):
    """Minimal config extended with a domain-specific lifecycle state and action.

    Exercises the engine's handling of custom lifecycle extensions: adds
    a `filed` state (non-terminal) and a `file` action from `active` to
    `filed`, on top of the base states/transitions in
    `minimal_vault_config_dict`. Used by tests that verify domain-specific
    states/actions are surfaced by lifecycle and graph-ops services.
    """
    import copy

    config = copy.deepcopy(minimal_vault_config_dict)
    config["lifecycle"]["states"].append({"value": "filed", "label": "Filed"})
    config["lifecycle"]["transitions"].append(
        {"from_state": "active", "action": "file", "to_state": "filed"}
    )
    return config


@pytest.fixture
def extended_config(extended_vault_config_dict):
    """Parsed VaultConfig from the extended dict."""
    return VaultConfig.model_validate(extended_vault_config_dict)


@pytest.fixture
async def postgres_graph_store(pg_dsn, pg_schema):
    """A PostgresGraphStore over a per-test-truncated session schema.

    ``pg_dsn`` / ``pg_schema`` are signature dependencies (not getfixturevalue),
    so pytest resolves the session Postgres provisioning *before* this async
    body enters the event loop. ``pg_dsn`` importorskips psycopg and skips
    without ``SAGE_TEST_PG_DSN``, so this fixture is never built on a machine
    without a configured server.
    """
    from sage.storage.postgres.graph_store import PostgresGraphStore
    from sage.storage.postgres.pool import pool_from_conninfo

    pool = pool_from_conninfo(pg_dsn, search_path=f"{pg_schema},public")
    await pool.open()
    try:
        async with pool.connection() as conn:
            await conn.execute(f"TRUNCATE {', '.join(_PG_TABLES)} CASCADE")  # noqa: S608
            # The session schema persists across tests: tables truncate but
            # per-vault tier3 unique indexes a prior test created do not. Drop
            # them so each test starts from the bare schema (the SQLite store
            # gets a fresh db file per test and has no such leak).
            cur = await conn.execute(
                "SELECT indexname FROM pg_indexes "
                "WHERE schemaname = current_schema() "
                "AND indexname LIKE 'idx_tier3_unique_%'"
            )
            for (idxname,) in await cur.fetchall():
                await conn.execute(f'DROP INDEX IF EXISTS "{idxname}"')  # noqa: S608
        store = PostgresGraphStore(pool)
        await store.initialize(migrate=True)
        yield store
        await store.close()
    finally:
        await pool.close()


@pytest.fixture
def graph_store(request):
    """Initialized graph store over the test Postgres (CAS-ADR-042).

    A sync dispatcher that delegates to the async ``postgres_graph_store``
    fixture, which skips (via ``pg_dsn``) when ``SAGE_TEST_PG_DSN`` is unset.
    The storage port has a single binding; behavioral tests written against
    this fixture exercise the port contract, not Postgres specifics.
    """
    return request.getfixturevalue("postgres_graph_store")


@pytest.fixture
def lock_manager():
    return DocumentLockManager()


@pytest.fixture
def stub_content_store():
    return StubContentStore()


@pytest.fixture
def stub_embedding_provider():
    return StubEmbeddingProvider()


@pytest.fixture
def stub_abstraction_provider():
    return StubAbstractionProvider()


@pytest.fixture
def failing_abstraction_provider():
    return FailingAbstractionProvider()


@pytest.fixture
def user_service(graph_store, minimal_config):
    return UserService(graph_store, minimal_config)


@pytest.fixture
def lifecycle_service(graph_store, lock_manager, minimal_config, stub_content_store):
    return LifecycleService(graph_store, lock_manager, minimal_config, stub_content_store)


@pytest.fixture
def extended_lifecycle_service(graph_store, lock_manager, extended_config, stub_content_store):
    return LifecycleService(graph_store, lock_manager, extended_config, stub_content_store)


@pytest.fixture
def metadata_service(graph_store, lock_manager, minimal_config, stub_content_store):
    return MetadataService(graph_store, lock_manager, minimal_config, stub_content_store)


@pytest.fixture
def graph_ops_service(graph_store, minimal_config):
    return GraphOpsService(graph_store, minimal_config)


@pytest.fixture
def extended_graph_ops_service(graph_store, extended_config):
    return GraphOpsService(graph_store, extended_config)


@pytest.fixture
def ingestion_service(
    graph_store,
    lock_manager,
    stub_content_store,
    stub_embedding_provider,
    stub_abstraction_provider,
    minimal_config,
    lifecycle_service,
):
    return IngestionService(
        graph_store=graph_store,
        lock_manager=lock_manager,
        content_store=stub_content_store,
        embedding_provider=stub_embedding_provider,
        abstraction_provider=stub_abstraction_provider,
        config=minimal_config,
        source_adapters={SourceType.MARKDOWN: MarkdownAdapter()},
        lifecycle_service=lifecycle_service,
    )


@pytest.fixture
def ingestion_service_no_abstraction(
    graph_store,
    lock_manager,
    stub_content_store,
    stub_embedding_provider,
    stub_abstraction_provider,
    minimal_vault_config_dict,
):
    """Ingestion service with abstraction disabled (BH-025)."""
    config_dict = minimal_vault_config_dict.copy()
    config_dict["abstraction"] = {"enabled": False}
    config = VaultConfig.model_validate(config_dict)
    return IngestionService(
        graph_store=graph_store,
        lock_manager=lock_manager,
        content_store=stub_content_store,
        embedding_provider=stub_embedding_provider,
        abstraction_provider=stub_abstraction_provider,
        config=config,
        source_adapters={SourceType.MARKDOWN: MarkdownAdapter()},
    )


@pytest.fixture
def ingestion_service_failing_llm(
    graph_store,
    lock_manager,
    stub_content_store,
    stub_embedding_provider,
    failing_abstraction_provider,
    minimal_config,
):
    """Ingestion service with failing LLM (BH-024)."""
    return IngestionService(
        graph_store=graph_store,
        lock_manager=lock_manager,
        content_store=stub_content_store,
        embedding_provider=stub_embedding_provider,
        abstraction_provider=failing_abstraction_provider,
        config=minimal_config,
        source_adapters={SourceType.MARKDOWN: MarkdownAdapter()},
    )


# ---------------------------------------------------------------------------
# Postgres storage harness (CAS-ADR-042)
#
# Storage tests run against a real Postgres named by SAGE_TEST_PG_DSN. When that
# is unset (or psycopg is absent) the tests skip -- local runs without a server
# and the default CI path stay green; CI's storage job sets the DSN to a pgvector
# service container. The harness provisions a disposable, uniquely-named schema
# per session (never the live working database: assert_disposable_target refuses
# 'public' and any non-'sage_test_' schema, and the harness drops only that
# schema, never a database), runs the canonical bootstrap into it, and hands out
# a pool bound to that schema. Per-test isolation is a truncation at pool setup.
# ---------------------------------------------------------------------------

_PG_TABLES = ("documents", "edges", "staging_edges", "users", "document_tags", "chunks")


@pytest.fixture(scope="session")
def pg_dsn(_provision_isolated_test_database):
    """Session Postgres DSN, or skip when no test server is configured.

    Depends on the isolation provisioner (root conftest) so the DSN it returns is
    this process's throwaway database, not the shared maintenance database.
    """
    pytest.importorskip("psycopg")
    dsn = os.environ.get("SAGE_TEST_PG_DSN")
    if not dsn:
        pytest.skip("set SAGE_TEST_PG_DSN to a throwaway Postgres to run the storage tests")
    return dsn


@pytest.fixture(scope="session")
def pg_schema(pg_dsn):
    """Provision a disposable schema for the session; drop it at the end.

    A sync fixture whose async work runs under ``asyncio.run`` so a
    session-scoped resource does not collide with pytest-asyncio's per-test
    event loop. ``assert_disposable_target`` guarantees the schema is a
    ``sage_test_*`` throwaway, never the live working database.
    """
    import psycopg

    from sage.storage.postgres.schema import assert_disposable_target, bootstrap_schema

    schema = assert_disposable_target("sage_test_" + os.urandom(4).hex())

    async def _setup() -> None:
        async with await psycopg.AsyncConnection.connect(pg_dsn, autocommit=True) as conn:
            await bootstrap_schema(conn, schema=schema, extensions=["vector", "pgstattuple"])

    async def _teardown() -> None:
        async with await psycopg.AsyncConnection.connect(pg_dsn, autocommit=True) as conn:
            await conn.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')  # noqa: S608

    asyncio.run(_setup())
    try:
        yield schema
    finally:
        asyncio.run(_teardown())


@pytest.fixture
async def pg_pool(pg_dsn, pg_schema):
    """An opened async pool bound to the session's disposable schema.

    Truncates all tables at setup so each test starts from a clean slate, then
    closes the pool on teardown.
    """
    from sage.storage.postgres.pool import pool_from_conninfo

    pool = pool_from_conninfo(pg_dsn, search_path=f"{pg_schema},public")
    await pool.open()
    try:
        async with pool.connection() as conn:
            await conn.execute(f"TRUNCATE {', '.join(_PG_TABLES)} CASCADE")  # noqa: S608
        yield pool
    finally:
        await pool.close()


@dataclasses.dataclass
class VaultSourceBackendHandle:
    """Handle a binding-parameterized test uses to inspect the active backend.

    ``retained_bytes`` answers "what bytes does the vault's source store hold
    at this vault-relative path?" against whichever binding the fixture
    selected, so one assertion body serves both legs.
    """

    backend: str
    fake_client: object | None

    def retained_bytes(self, storage_root: Path, rel: str) -> bytes:
        if self.fake_client is not None:
            return self.fake_client.sources[rel]
        return (Path(storage_root) / rel).read_bytes()

    def write_retained_bytes(self, storage_root: Path, rel: str, data: bytes) -> None:
        """Overwrite a retained copy *out of band*, as something other than SAGE.

        The setup half of ``retained_bytes``, for tests whose precondition is a
        store SAGE did not author -- the drift the integrity audit exists to
        surface and the restore exists to repair. Deliberately bypasses the
        document-store leg's upload path so no rewrite-at-rest stamping is
        applied: an external writer leaves exactly the bytes it wrote, which is
        what makes the resulting mismatch a real one rather than a stamping
        artifact.
        """
        if self.fake_client is not None:
            self.fake_client.sources[rel] = data
        else:
            (Path(storage_root) / rel).write_bytes(data)


@pytest.fixture(params=["filesystem", "document_store"])
def vault_source_backend(request, monkeypatch):
    """Pin the stack's vault-source binding for the test, one leg per backend.

    Both legs exercise the real dispatch in ``build_stack_vault_source_store``
    via the env override. The document-store leg fakes only the Graph
    transport: one shared ``FakeGraphClient`` is returned by the (call-time
    resolved) client factory, so state persists across the fresh store
    constructions the stack resolver performs per service call while every
    line of ``DocumentStoreVaultSourceStore`` still runs.
    """
    backend = request.param
    monkeypatch.setenv("SAGE_TEST_VAULT_SOURCE_BACKEND", backend)
    if backend == "filesystem":
        return VaultSourceBackendHandle(backend=backend, fake_client=None)

    from tests.helpers.fake_graph_client import FakeGraphClient

    fake = FakeGraphClient()
    monkeypatch.setattr(
        "sage.vault_source_document_store.build_sharepoint_graph_client",
        lambda *args, **kwargs: fake,
    )
    return VaultSourceBackendHandle(backend=backend, fake_client=fake)


# ── Local abstraction-provider inference stubs ──────────────────────
#
# The local MLX provider drives inference through a streaming generator and
# reads its latency figures off the final response, so a stub standing in for
# that seam yields response objects rather than returning a string.


@dataclasses.dataclass
class FakeGenerationResponse:
    """Stand-in for the local provider's per-segment generation response.

    Carries the segment text plus the phase counts and rates the provider's
    latency record is derived from. Defaults describe a plausible generation
    so a test that cares only about the text can ignore them.
    """

    text: str
    prompt_tokens: int = 1000
    prompt_tps: float = 500.0
    generation_tokens: int = 200
    generation_tps: float = 25.0


def stub_stream_generate(*segments: str, **stats):
    """Build a stub for ``Qwen3AbstractionProvider._generate_fn``.

    Yields one response per segment; the caller concatenates them exactly as
    the real streaming entry point's one-shot wrapper does. Keyword arguments
    override the phase statistics carried on every response.
    """
    if not segments:
        segments = ("STUB ABSTRACT",)

    def _stream(*args, **kwargs):
        for segment in segments:
            yield FakeGenerationResponse(text=segment, **stats)

    return _stream


@pytest.fixture
def refusing_source_store(monkeypatch):
    """A document-store vault-source binding whose Graph client can be told to refuse.

    Pins the stack's binding through the same env override the shared
    ``vault_source_backend`` fixture uses, so the real
    ``DocumentStoreVaultSourceStore`` and the real dispatch both run and only
    the Graph transport is faked. Returned so a test can install a refusal on
    the operation it is about.
    """
    from tests.helpers.fake_graph_client import FakeGraphClient

    monkeypatch.setenv("SAGE_TEST_VAULT_SOURCE_BACKEND", "document_store")
    fake = FakeGraphClient()
    monkeypatch.setattr(
        "sage.vault_source_document_store.build_sharepoint_graph_client",
        lambda *args, **kwargs: fake,
    )
    return fake
