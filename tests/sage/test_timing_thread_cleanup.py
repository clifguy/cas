"""Tests for the test-side timing-thread cleanup machinery.

The helpers under test:

- ``initialize_services_for_test`` (async context manager, in
  ``tests/sage/conftest.py``) — wraps ``initialize_services`` so that
  callers can no longer forget to stop the ``VaultTimingThread`` and close
  the graph store on exit.
- ``stop_leaked_timing_threads`` (function, in
  ``tests/helpers/timing_leaks.py``) — sweeps ``threading.enumerate()`` for
  any ``VaultTimingThread`` instance still alive in the current process and
  stops it. The root-conftest timing-leak guard calls it to reap a detected
  thread leak.

These tests are the implementation contract for both symbols.
"""

from __future__ import annotations

import threading

import pytest

from sage.instrumentation.timing import VaultTimingThread

# VaultTimingThread is NOT a threading.Thread subclass — it wraps one
# whose default name is "sage-timing-flush". We identify live timing
# threads by the inner-thread name and read.is_alive() off the inner
# thread (the wrapper does not expose that method).
_TIMING_FLUSH_NAME_PREFIX = "sage-timing-flush"


def _alive_timing_thread_count() -> int:
    return sum(
        1
        for t in threading.enumerate()
        if t.is_alive() and t.name.startswith(_TIMING_FLUSH_NAME_PREFIX)
    )


def _wrapper_alive(tt: VaultTimingThread) -> bool:
    # Private attribute access is contained to this helper.
    return tt._thread.is_alive()


# ── Spec 1 ──────────────────────────────────────────────────────────────
# Helper stops the timing thread on normal exit.


async def test_helper_stops_timing_thread_on_normal_exit(minimal_config):
    from tests.sage.conftest import initialize_services_for_test

    count_before = _alive_timing_thread_count()

    async with initialize_services_for_test(minimal_config) as services:
        count_during = _alive_timing_thread_count()
        assert services.timing_thread is not None
        assert _wrapper_alive(services.timing_thread)

    count_after = _alive_timing_thread_count()

    # Anti-coincidental-pass guard: the helper must have actually started
    # a thread (otherwise a no-op helper that never called
    # initialize_services would silently pass).
    assert count_during == count_before + 1
    # The thread must be stopped by the time the context exits.
    assert count_after == count_before
    assert not _wrapper_alive(services.timing_thread)


# ── Spec 2 ──────────────────────────────────────────────────────────────
# Helper stops the timing thread even when the body raises.


async def test_helper_stops_timing_thread_when_body_raises(minimal_config):
    from tests.sage.conftest import initialize_services_for_test

    count_before = _alive_timing_thread_count()
    captured_services = None

    with pytest.raises(RuntimeError, match="deliberate"):
        async with initialize_services_for_test(minimal_config) as services:
            captured_services = services
            raise RuntimeError("deliberate")

    count_after = _alive_timing_thread_count()

    assert count_after == count_before
    assert captured_services is not None
    assert not _wrapper_alive(captured_services.timing_thread)


# ── Spec 3 ──────────────────────────────────────────────────────────────
# Helper closes the graph_store on exit.


async def test_helper_closes_graph_store_on_exit(minimal_config):
    from tests.sage.conftest import initialize_services_for_test

    close_calls = 0

    async with initialize_services_for_test(minimal_config) as services:
        # Replace close() with a counting delegate that survives context
        # exit. unittest.mock.patch.object reverts on inner-block exit,
        # which would happen BEFORE the helper's __aexit__ — defeating
        # the assertion. A manual rebind keeps the spy alive.
        original_close = services.graph_store.close

        async def _counting_close():
            nonlocal close_calls
            close_calls += 1
            await original_close()

        services.graph_store.close = _counting_close

    # After the helper exits, close() should have been called exactly once.
    assert close_calls == 1


# ── Spec 4 ──────────────────────────────────────────────────────────────
# stop_leaked_timing_threads() stops only live VaultTimingThread instances.


def test_stop_leaked_timing_threads_targets_only_vault_timing_threads():
    from sage.instrumentation import QueryTimer, TimingConfig
    from tests.helpers.timing_leaks import stop_leaked_timing_threads

    # Start an unrelated long-sleeping daemon thread; it must survive.
    sentinel_done = threading.Event()

    def _sentinel() -> None:
        sentinel_done.wait(timeout=5.0)

    unrelated = threading.Thread(target=_sentinel, daemon=True, name="t-0208-sentinel")
    unrelated.start()

    # Start a VaultTimingThread by hand (no initialize_services involved
    # — this isolates the test from the conftest helper's behaviour).
    timer = QueryTimer(
        logger_name="sage.storage.timing",
        config=TimingConfig(
            emit_threshold_ms=10_000.0,
            summary_interval_seconds=0.05,
        ),
        layer="storage",
    )
    leaked = VaultTimingThread(timers=[timer], interval_seconds=0.05)
    leaked.start()

    try:
        assert _wrapper_alive(leaked)
        assert unrelated.is_alive()

        stop_leaked_timing_threads()

        assert not _wrapper_alive(leaked)
        # The unrelated daemon must NOT have been stopped — name-prefix
        # narrowing is the contract.
        assert unrelated.is_alive()
    finally:
        sentinel_done.set()
        unrelated.join(timeout=2.0)


# ── Spec 5 ──────────────────────────────────────────────────────────────
# stop_leaked_timing_threads() is a no-op when nothing to stop.


def test_stop_leaked_timing_threads_no_op_when_nothing_alive():
    from tests.helpers.timing_leaks import stop_leaked_timing_threads

    # Precondition: no VaultTimingThread instances alive. If this fails,
    # something earlier leaked — the autouse safety net itself is broken.
    assert _alive_timing_thread_count() == 0, (
        "expected no live VaultTimingThread instances at test entry; a "
        "previous test leaked one and the autouse safety net did not "
        "catch it"
    )

    threads_before = set(threading.enumerate())
    stop_leaked_timing_threads()
    threads_after = set(threading.enumerate())

    # Nothing started, nothing stopped.
    assert threads_after == threads_before


# ── Spec 7 ──────────────────────────────────────────────────────────────
# stop_leaked_timing_threads() swallows exceptions raised by a
# corrupted/monkey-patched stop(). The safety net must never ERROR the
# very tests it exists to protect — test_initialize_services_cleanup.py
# exercises a deliberately-broken stop() to verify cleanup-doesn't-mask
# semantics; the safety net must coexist with that.


def test_stop_leaked_timing_threads_swallows_stop_exceptions(monkeypatch):
    from sage.instrumentation import QueryTimer, TimingConfig
    from tests.helpers.timing_leaks import stop_leaked_timing_threads

    # Start a real VaultTimingThread.
    timer = QueryTimer(
        logger_name="sage.storage.timing",
        config=TimingConfig(
            emit_threshold_ms=10_000.0,
            summary_interval_seconds=0.05,
        ),
        layer="storage",
    )
    leaked = VaultTimingThread(timers=[timer], interval_seconds=0.05)
    leaked.start()

    # Monkey-patch stop() to raise — mimics the N7 cleanup test.
    def broken_stop(self, timeout: float = 2.0) -> None:
        raise RuntimeError("intentional: stop is broken")

    monkeypatch.setattr(VaultTimingThread, "stop", broken_stop)

    try:
        # MUST NOT propagate. If it does, the safety net is unfit for
        # purpose.
        stop_leaked_timing_threads()
    finally:
        # Restore stop() and actually stop the thread for hygiene.
        monkeypatch.undo()
        leaked.stop(timeout=1.0)
