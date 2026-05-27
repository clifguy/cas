"""Transactional cleanup for ``initialize_services`` (AC2, Risk note).

When ``initialize_services`` raises mid-construction, any partially-allocated
resources (timing thread, graph store connections, content store) must be
released before the exception propagates. The original exception is the one
that propagates; cleanup-time exceptions are logged, not re-raised.

These three tests exercise the transactional contract directly against
``initialize_services``. The end-to-end integration via ``reload_vault``
is covered in ``tests/sage/test_mcp_server.py`` (the N1/N2 reload-failure
tests for).
"""

from __future__ import annotations

import asyncio
import threading

import pytest

from sage.adapters.stubs import (
    StubAbstractionProvider,
    StubContentStore,
    StubEmbeddingProvider,
)
from sage.api.errors import SAGEError
from sage.config import VaultConfig
from sage.mcp_init import initialize_services


def _count_timing_threads() -> int:
    """Count live threads matching the per-vault timing flusher's name."""
    return sum(1 for t in threading.enumerate() if t.name.startswith("sage-timing-flush"))


@pytest.fixture
def vault_config_with_timing(minimal_vault_config_dict):
    """VaultConfig with timing enabled, so _build_vault_timers spins up a thread.

    ``TimingConfig.enabled`` defaults to True, so the minimal config already
    builds a thread; this fixture is explicit about the contract for clarity.
    """
    cfg = dict(minimal_vault_config_dict)
    cfg["timing"] = {"enabled": True, "summary_interval_seconds": 60.0}
    return VaultConfig.model_validate(cfg)


async def test_initialize_services_cleans_up_timing_thread_on_failure(
    vault_config_with_timing, monkeypatch
):
    """N5: failed initialize_services must stop the timing thread.

    ``_build_vault_timers`` calls ``flusher.start()`` before returning. If a
    downstream constructor raises, the thread keeps running unless the
    transactional cleanup wrapper stops it.

    Trap (anti-coincidental): without try/except cleanup, the test sees an
    extra live ``sage-timing-flush`` thread post-failure. The thread-count
    delta is the trap.
    """
    pre_count = _count_timing_threads()

    # Patch UserService.bootstrap_owner — it runs late in initialize_services,
    # AFTER _build_vault_timers has started the flusher thread.
    from sage.services.user_service import UserService

    async def raising_bootstrap(self):
        raise SAGEError(
            code="schema_migration_required",
            message="N5 failure injection",
            status_code=409,
        )

    monkeypatch.setattr(UserService, "bootstrap_owner", raising_bootstrap)

    # Raw initialize_services: this test exercises the failure path
    # (pytest.raises below), so initialize_services_for_test is the wrong
    # shape — its async-context-manager exit never runs when the wrapped
    # initialize_services call raises.
    with pytest.raises(SAGEError, match="N5 failure injection"):
        await initialize_services(
            vault_config_with_timing,
            content_store=StubContentStore(),
            embedding_provider=StubEmbeddingProvider(),
            abstraction_provider=StubAbstractionProvider(),
        )

    # Cleanup is best-effort and joins with a 1.0s timeout; give it a moment.
    await asyncio.sleep(0.2)
    post_count = _count_timing_threads()
    assert post_count <= pre_count, (
        f"Timing thread leaked on failed initialize_services: pre={pre_count}, post={post_count}"
    )


async def test_initialize_services_cleans_up_graph_store_on_failure(
    minimal_vault_config_dict, monkeypatch
):
    """N6: failed initialize_services must close the graph store.

    ``GraphStore.__init__`` constructs an executor and connection pool;
    ``.initialize()`` opens connections to apply schema. If a downstream
    constructor raises, the executor and connections leak.

    Trap (anti-coincidental): without try/except cleanup, ``_executor`` is
    not None and ``_all_connections`` is non-empty after the failure. The
    assertions on the graph store's internal state are the trap.

    We capture the graph_store reference by patching the GraphStore class
    so we can inspect it after the failure.
    """
    config = VaultConfig.model_validate(minimal_vault_config_dict)

    from sage.services.user_service import UserService
    from sage.storage import graph_store as _gs_module

    captured: dict = {}
    original_graph_store_cls = _gs_module.GraphStore

    class CapturingGraphStore(original_graph_store_cls):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            captured["graph_store"] = self

    # Patch the GraphStore class at the import site inside mcp_init.
    import sage.mcp_init as _mcp_init

    monkeypatch.setattr(_mcp_init, "GraphStore", CapturingGraphStore)

    # Patch bootstrap_owner to raise AFTER GraphStore is constructed and
    # initialized.
    async def raising_bootstrap(self):
        raise SAGEError(
            code="schema_migration_required",
            message="N6 failure injection",
            status_code=409,
        )

    monkeypatch.setattr(UserService, "bootstrap_owner", raising_bootstrap)

    # Raw initialize_services: this test exercises the failure path
    # (pytest.raises below), so initialize_services_for_test is the wrong
    # shape — its async-context-manager exit never runs when the wrapped
    # initialize_services call raises.
    with pytest.raises(SAGEError, match="N6 failure injection"):
        await initialize_services(
            config,
            content_store=StubContentStore(),
            embedding_provider=StubEmbeddingProvider(),
            abstraction_provider=StubAbstractionProvider(),
        )

    # The graph store must have been constructed
    assert "graph_store" in captured, "GraphStore was never constructed; test fixture is broken"
    graph_store = captured["graph_store"]

    # Cleanup must have closed the graph store
    assert graph_store._executor is None, (
        "GraphStore._executor not closed on failure (connection pool leaked)"
    )
    assert graph_store._all_connections == [], (
        f"GraphStore._all_connections has {len(graph_store._all_connections)} "
        "leaked connections on failure"
    )
    # Behavioural co-assertion per TEST-SAGE-BH-137: the CAS-ADR-036 dispatch
    # barrier must engage on the error-path teardown, not just the bookkeeping
    # fields. A silent-degrade close would let post-failure callers transparently
    # use the wreckage; the barrier raises instead.
    with pytest.raises(RuntimeError, match="closed"):
        await graph_store.list_all_documents()


async def test_initialize_services_cleanup_does_not_mask_original_exception(
    vault_config_with_timing, monkeypatch
):
    """N7 (Risk note): cleanup-time exceptions must not mask the original.

    The Risk note in explicitly requires "cleanup must release any
    partially-allocated resources without re-raising". Cleanup-time errors
    are logged and swallowed; the original exception is what propagates.

    Trap (anti-coincidental): without best-effort cleanup that swallows
    secondary exceptions, the cleanup-time exception masks the original.
    The ``match=`` argument on pytest.raises is the trap — it asserts the
    PROPAGATING exception carries the original message.
    """
    from sage.instrumentation.timing import VaultTimingThread
    from sage.services.user_service import UserService

    # Inject the original failure
    async def raising_bootstrap(self):
        raise SAGEError(
            code="schema_migration_required",
            message="N7 ORIGINAL exception that must propagate",
            status_code=409,
        )

    monkeypatch.setattr(UserService, "bootstrap_owner", raising_bootstrap)

    # Also break VaultTimingThread.stop so cleanup-time itself raises
    def broken_stop(self, timeout=2.0):
        raise RuntimeError("N7 CLEANUP exception that must NOT propagate")

    monkeypatch.setattr(VaultTimingThread, "stop", broken_stop)

    # The original exception (SAGEError with "N7 ORIGINAL") must propagate.
    # The cleanup exception (RuntimeError with "N7 CLEANUP") must NOT.
    # Raw initialize_services: this test exercises the failure path
    # (pytest.raises below), so initialize_services_for_test is the wrong
    # shape — its async-context-manager exit never runs when the wrapped
    # initialize_services call raises.
    with pytest.raises(SAGEError, match="N7 ORIGINAL exception"):
        await initialize_services(
            vault_config_with_timing,
            content_store=StubContentStore(),
            embedding_provider=StubEmbeddingProvider(),
            abstraction_provider=StubAbstractionProvider(),
        )
