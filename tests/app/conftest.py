"""tests/app-specific fixtures and leak guards.

Extends the root ``tests/conftest.py``. The app integration tests build real
per-vault services with timing enabled, so a fixture that tears down without
releasing the per-vault ``timing.log`` handler leaks both the
``RotatingFileHandler`` (which stays attached to the three process-global timing
loggers) and its ``VaultTimingThread`` (a live daemon) into the rest of the run.
"""

import logging
import threading

import pytest

from sage.mcp_init import _TIMING_LOGGER_NAMES, _timing_handlers
from tests.sage.conftest import (
    _TIMING_FLUSH_THREAD_NAME_PREFIX,
    stop_leaked_timing_threads,
)


def _alive_timing_thread_idents() -> set[int]:
    """Idents of the live ``sage-timing-flush`` daemon threads right now."""
    return {
        thread.ident
        for thread in threading.enumerate()
        if thread.is_alive() and thread.name.startswith(_TIMING_FLUSH_THREAD_NAME_PREFIX)
    }


def _force_release_timing_handler(key: str) -> None:
    """Detach a leaked per-path timing handler from the loggers and close it."""
    ref = _timing_handlers.pop(key, None)
    if ref is None:
        return
    for name in _TIMING_LOGGER_NAMES:
        logging.getLogger(name).removeHandler(ref.handler)
    ref.handler.close()


@pytest.fixture(autouse=True)
def _fail_on_leaked_timing_resources():
    """Fail (and reap) when a test leaks a per-vault timing handler or thread.

    A teardown that closes only the graph store leaves the ``timing.log``
    handler attached to the process-global timing loggers and the
    ``VaultTimingThread`` running. Neither surfaces as an "unclosed file"
    ``ResourceWarning`` — the loggers keep the handler reachable, so CPython
    never garbage-collects it and ``logging.shutdown()`` closes it cleanly only
    at interpreter exit — so this guard asserts on the observable that actually
    moves: a net-new entry in the ``_timing_handlers`` registry or a net-new
    live ``sage-timing-flush`` thread introduced by the test. Detected leaks are
    reaped before failing so a single offender cannot cascade into unrelated
    tests downstream.

    The remedy at a leaking site is to build services through
    ``initialize_services_for_test`` or to call ``services.close_timing()``
    before ``graph_store.close()`` on teardown.
    """
    handlers_before = set(_timing_handlers)
    threads_before = _alive_timing_thread_idents()
    yield
    leaked_handlers = set(_timing_handlers) - handlers_before
    leaked_threads = _alive_timing_thread_idents() - threads_before
    # Reap regardless of outcome so the leak cannot pollute later tests.
    for key in leaked_handlers:
        _force_release_timing_handler(key)
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
