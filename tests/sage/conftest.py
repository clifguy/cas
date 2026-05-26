"""SAGE-specific test fixtures.

Extends the root conftest.py fixtures with SAGE storage, services,
and stub adapters. Each test gets an isolated temp directory and
fresh SQLite database via pytest's tmp_path fixture.
"""

import asyncio
import contextlib
import threading

import pytest

import sage.adapters.abstraction_qwen3 as _abstraction_qwen3
from sage.adapters.stubs import (
    FailingAbstractionProvider,
    StubAbstractionProvider,
    StubContentStore,
    StubEmbeddingProvider,
)
from sage.config import VaultConfig
from sage.instrumentation.timing import VaultTimingThread
from sage.mcp_init import initialize_services
from sage.models.enums import SourceType
from sage.services.graph_ops import GraphOpsService
from sage.services.ingestion import IngestionService
from sage.services.lifecycle import LifecycleService
from sage.services.metadata import MetadataService
from sage.services.user_service import UserService
from sage.source_adapters.markdown_adapter import MarkdownAdapter
from sage.storage.graph_store import GraphStore
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


@contextlib.asynccontextmanager
async def initialize_services_for_test(config, **kwargs):
    """Async context manager wrapping ``initialize_services`` for tests.

    Guarantees that on exit (normal or exceptional) the per-vault
    ``VaultTimingThread`` is stopped and the graph store is closed.
    Replaces the
    ``services = await initialize_services(...); try:...; finally:
    await services.graph_store.close()`` pattern, which historically
    forgot to stop the timing thread and leaked it into subsequent
    tests, polluting their caplog windows on the timing loggers.
    """
    services = await initialize_services(config, **kwargs)
    try:
        yield services
    finally:
        if services.timing_thread is not None:
            services.timing_thread.stop(timeout=1.0)
        await services.graph_store.close()


_TIMING_FLUSH_THREAD_NAME_PREFIX = "sage-timing-flush"


def stop_leaked_timing_threads() -> None:
    """Stop any ``VaultTimingThread`` still alive in this process.

    Safety net for test-side leaks. ``VaultTimingThread`` is not a
    ``threading.Thread`` subclass — it wraps one whose default name is
    ``sage-timing-flush``. We walk ``threading.enumerate()``, match by
    name prefix, then recover the wrapper through the bound-method
    target stored on the inner thread (``Thread._target.__self__``)
    so we can call its ``stop()`` method. Other daemon threads
    (logging, asyncio, etc.) are left untouched. Wired into the
    ``_stop_leaked_vault_timing_threads`` autouse fixture below; safe
    to call directly from tests too.
    """
    for thread in list(threading.enumerate()):
        if not thread.is_alive():
            continue
        if not thread.name.startswith(_TIMING_FLUSH_THREAD_NAME_PREFIX):
            continue
        target = getattr(thread, "_target", None)
        wrapper = getattr(target, "__self__", None)
        if isinstance(wrapper, VaultTimingThread):
            # Safety net must never propagate. Tests can monkey-patch
            # VaultTimingThread.stop to raise (e.g.,
            # test_initialize_services_cleanup.py exercises the
            # cleanup-doesn't-mask-original failure mode); a propagating
            # exception here would ERROR the test on teardown. Mirrors
            # the production swallow pattern in
            # sage/instrumentation/timing.py:VaultTimingThread._run.
            try:
                wrapper.stop(timeout=1.0)
            except Exception:  # noqa: S110 — see comment above
                pass


@pytest.fixture(autouse=True)
def _stop_leaked_vault_timing_threads():
    """Safety net: kill any ``VaultTimingThread`` a test forgot to stop.

    Runs after every test in the ``tests/sage/`` subtree. Catches
    leaks from sites that have not yet adopted
    ``initialize_services_for_test`` so the failure mode (leaked
    daemon thread flushing into the next test's caplog) cannot
    recur silently.
    """
    yield
    stop_leaked_timing_threads()


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
        "source_adapters": {
            "adapters": [{"source_type": "markdown", "enabled": True}],
        },
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
async def graph_store(tmp_vault_dir):
    """Initialized graph store in a temp directory."""
    store = GraphStore(tmp_vault_dir / "brain" / "graph.db")
    await store.initialize()
    yield store
    await store.close()


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
