"""Shared per-vault timing-resource leak detection for the test suite.

The SAGE timing loggers (the ``sage.*.timing`` family) are
process-global, but the ``RotatingFileHandler`` behind them and the
``VaultTimingThread`` that flushes summaries are per-vault. A test that builds
real services with timing enabled (the default) and tears down without
releasing them leaks both into the rest of the run: the handler stays attached
to the process-global loggers, so CPython never garbage-collects it and it
emits no "unclosed file" ``ResourceWarning`` — it is closed cleanly only by
``logging.shutdown()`` at interpreter exit — while the daemon flush thread keeps
running. This module carries the garbage-collection-independent observables
(the ``sage.mcp_init._timing_handlers`` registry and the live
``sage-timing-flush`` threads) that the root-conftest autouse guard diffs around
every test, plus the reaping primitives it uses so one offender cannot cascade
into unrelated tests downstream.

The remedy at a leaking site is to build services through
``initialize_services_for_test`` or to call ``services.close_timing()`` before
``graph_store.close()`` on teardown.
"""

from __future__ import annotations

import threading

import pytest

from sage.instrumentation.timing import VaultTimingThread
from sage.mcp_init import _release_timing_handler, _timing_handlers

#: Default name of the inner daemon thread a ``VaultTimingThread`` wraps.
TIMING_FLUSH_THREAD_NAME_PREFIX = "sage-timing-flush"


def alive_timing_thread_idents() -> set[int]:
    """Idents of the live ``sage-timing-flush`` daemon threads right now."""
    return {
        thread.ident
        for thread in threading.enumerate()
        if thread.is_alive() and thread.name.startswith(TIMING_FLUSH_THREAD_NAME_PREFIX)
    }


def leaked_timing_handlers(handlers_before: set[str]) -> set[str]:
    """Registry keys added to ``_timing_handlers`` since the snapshot was taken."""
    return set(_timing_handlers) - handlers_before


def force_release_timing_handler(key: str) -> None:
    """Detach a leaked per-path timing handler from the loggers and close it.

    Drops the entry's reference count to one and hands off to the production
    releaser rather than detaching by hand, so the reaper cannot diverge from
    it. Detaching in this module would skip the propagation restore the
    releaser performs on the process's last handler, silently leaving the
    timing loggers non-propagating for every test that follows.
    """
    ref = _timing_handlers.get(key)
    if ref is None:
        return
    ref.refcount = 1
    _release_timing_handler(ref.handler)


def stop_leaked_timing_threads() -> None:
    """Stop any ``VaultTimingThread`` still alive in this process.

    Reaping primitive for test-side leaks. ``VaultTimingThread`` is not a
    ``threading.Thread`` subclass — it wraps one whose default name is
    ``sage-timing-flush``. We walk ``threading.enumerate()``, match by name
    prefix, then recover the wrapper through the bound-method target stored on
    the inner thread (``Thread._target.__self__``) so we can call its
    ``stop()`` method. Other daemon threads (logging, asyncio, etc.) are left
    untouched. Safe to call directly from tests; also used by the root-conftest
    guard to reap a detected thread leak.
    """
    for thread in list(threading.enumerate()):
        if not thread.is_alive():
            continue
        if not thread.name.startswith(TIMING_FLUSH_THREAD_NAME_PREFIX):
            continue
        target = getattr(thread, "_target", None)
        wrapper = getattr(target, "__self__", None)
        if isinstance(wrapper, VaultTimingThread):
            # Reaping must never propagate. Tests can monkey-patch
            # VaultTimingThread.stop to raise (e.g.,
            # test_initialize_services_cleanup.py exercises the
            # cleanup-doesn't-mask-original failure mode); a propagating
            # exception here would ERROR the test on teardown. Mirrors the
            # production swallow pattern in
            # sage/instrumentation/timing.py:VaultTimingThread._run.
            try:
                wrapper.stop(timeout=1.0)
            except Exception:  # noqa: S110 — see comment above
                pass


def check_and_reap_timing_leaks(handlers_before: set[str], threads_before: set[int]) -> None:
    """Reap any leaked per-vault timing handler/thread, then fail if any were found.

    Diffs the ``_timing_handlers`` registry and the live ``sage-timing-flush``
    threads against the pre-test snapshots. A teardown that closes only the
    graph store leaves the ``timing.log`` handler attached to the process-global
    timing loggers and the ``VaultTimingThread`` running; neither surfaces as an
    "unclosed file" ``ResourceWarning``, so this asserts on the observable that
    actually moves. Detected leaks are reaped before failing so a single
    offender cannot cascade into unrelated tests downstream.
    """
    leaked_handlers = leaked_timing_handlers(handlers_before)
    leaked_threads = alive_timing_thread_idents() - threads_before
    # Reap regardless of outcome so the leak cannot pollute later tests.
    for key in leaked_handlers:
        force_release_timing_handler(key)
    if leaked_threads:
        stop_leaked_timing_threads()
    if leaked_handlers or leaked_threads:
        pytest.fail(
            "test leaked per-vault timing resources without releasing them: "
            f"{len(leaked_handlers)} timing.log handler(s), "
            f"{len(leaked_threads)} VaultTimingThread(s). Tear services down via "
            "initialize_services_for_test or call services.close_timing() before "
            "graph_store.close().",
            pytrace=False,
        )
