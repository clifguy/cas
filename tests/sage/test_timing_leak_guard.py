"""Positive-control tests for the per-vault timing-leak guard.

The guard (an autouse fixture in the root ``tests/conftest.py``) and its
reaping primitives live in ``tests/helpers/timing_leaks.py``. A guard that
silently observes the wrong registry, or whose autouse fixture failed to
install, would pass every test vacuously — the same blindness the
``ResourceWarning``-based metric had against a strongly-referenced
``timing.log`` handler. These tests deliberately leak each observable and
assert the detection logic reports it, so the guard cannot rot into a no-op
unnoticed. Each test reaps what it leaks before returning, so it does not trip
the guard at its own teardown.
"""

from __future__ import annotations

import logging

import pytest

from sage.instrumentation import QueryTimer, TimingConfig
from sage.instrumentation.timing import VaultTimingThread
from sage.mcp_init import _TIMING_LOGGER_NAMES, _install_timing_handler, _timing_handlers
from tests.helpers.timing_leaks import (
    alive_timing_thread_idents,
    check_and_reap_timing_leaks,
    force_release_timing_handler,
    leaked_timing_handlers,
    stop_leaked_timing_threads,
)


def test_leaked_timing_handlers_detected_then_reaped(tmp_path):
    """A net-new ``_timing_handlers`` entry is observed by the diff, then reaped.

    Trap (anti-coincidental): if ``leaked_timing_handlers`` diffed the wrong
    object or always returned empty, the ``assert leaked`` below fails — the
    guard would otherwise wave a real handler leak through.
    """
    before = set(_timing_handlers)
    log_path = tmp_path / "timing.log"

    _install_timing_handler(log_path)
    try:
        leaked = leaked_timing_handlers(before)
        assert leaked, "guard failed to observe a net-new _timing_handlers entry"
        assert str(log_path) in leaked
    finally:
        for key in leaked_timing_handlers(before):
            force_release_timing_handler(key)

    assert leaked_timing_handlers(before) == set(), "reaping left a residual registry entry"


def test_leaked_timing_threads_detected_then_reaped():
    """A net-new live ``sage-timing-flush`` thread is observed, then reaped.

    Trap (anti-coincidental): a vacuous ident diff (always empty) fails the
    ``assert new_idents`` below; a broken reaper fails the post-stop re-diff.
    This is the observable the removed ``tests/sage`` reaper fixture would have
    masked had it been left in place (it finalizes before the guard).
    """
    before = alive_timing_thread_idents()
    timer = QueryTimer(
        logger_name="sage.storage.timing",
        config=TimingConfig(emit_threshold_ms=10_000.0, summary_interval_seconds=0.05),
        layer="storage",
    )
    leaked = VaultTimingThread(timers=[timer], interval_seconds=0.05)
    leaked.start()
    try:
        new_idents = alive_timing_thread_idents() - before
        assert new_idents, "guard failed to observe a net-new sage-timing-flush thread"

        stop_leaked_timing_threads()
        assert alive_timing_thread_idents() - before == set(), "reaping left a live thread"
    finally:
        leaked.stop(timeout=1.0)


def test_force_release_timing_handler_detaches_and_closes(tmp_path):
    """``force_release_timing_handler`` removes the entry, detaches, and closes.

    Trap (anti-coincidental): popping the registry but not detaching/closing
    leaves the handler attached to the process-global loggers (it would keep
    receiving records and the file handle would stay open). The
    ``not in .handlers`` and ``stream closed`` assertions catch that.
    """
    log_path = tmp_path / "timing.log"
    handler = _install_timing_handler(log_path)
    assert str(log_path) in _timing_handlers
    for name in _TIMING_LOGGER_NAMES:
        assert handler in logging.getLogger(name).handlers

    force_release_timing_handler(str(log_path))

    assert str(log_path) not in _timing_handlers
    for name in _TIMING_LOGGER_NAMES:
        assert handler not in logging.getLogger(name).handlers
    assert handler.stream is None or handler.stream.closed


def test_check_and_reap_timing_leaks_fails_on_leak(tmp_path):
    """The guard's check fails on a leak — and reaps it even as it fails.

    Trap (anti-coincidental): a check that returned silently on a real leak is
    the failure mode this whole guard exists to prevent. ``pytest.raises``
    pins the failing behaviour; the post-call assertion pins that the offender
    is reaped so it cannot cascade into the next test.
    """
    handlers_before = set(_timing_handlers)
    threads_before = alive_timing_thread_idents()

    _install_timing_handler(tmp_path / "timing.log")
    try:
        with pytest.raises(pytest.fail.Exception, match="leaked per-vault timing resources"):
            check_and_reap_timing_leaks(handlers_before, threads_before)
        # The guard reaps even as it fails — no residue for the next test.
        assert leaked_timing_handlers(handlers_before) == set()
    finally:
        for key in leaked_timing_handlers(handlers_before):
            force_release_timing_handler(key)
