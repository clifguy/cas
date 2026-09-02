"""Opt-in gate, machine-wide lock, and release helper for real-model tests.

A handful of tests load the real Qwen3 abstraction model through MLX. They are
minutes of wall-clock and most of the machine's memory, and two concurrent
loads exhaust unified memory outright. This module keeps them in a separate
tier:

* ``requires_real_models`` skips them unless ``SAGE_TEST_REAL_MODELS=1``,
  the same opt-in shape as the Docker smoke tests behind ``SAGE_TEST_DOCKER``.
* ``real_model_lock`` serializes model loads across every pytest process on
  the machine (``flock`` on a file in the temp directory), so a second run
  waits instead of failing on memory. Re-entry from the same process -- two
  loaded providers alive at once -- is refused outright rather than left to
  block on the flock forever.
* ``loaded_provider`` builds a provider under the lock and unloads it on exit,
  so a class-scoped fixture releases the weights the moment its tests finish
  rather than holding them for the rest of the run.

The lock path is a parameter so tests of this module can use a private file
instead of contending with a real-model run elsewhere on the machine.
"""

from __future__ import annotations

import asyncio
import fcntl
import logging
import os
import tempfile
import threading
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Protocol, TypeVar

import pytest

logger = logging.getLogger(__name__)

REAL_MODELS_ENV = "SAGE_TEST_REAL_MODELS"
REAL_MODELS_SKIP_REASON = (
    f"real-model tests are opt-in: set {REAL_MODELS_ENV}=1 (loads the Qwen3 MLX model)"
)
REAL_MODEL_LOCK_PATH = Path(tempfile.gettempdir()) / "sage-test-real-models.lock"

# Process-local guard in front of the flock. flock contends across file
# descriptions, so a second acquire from the same process would block
# forever; the guard turns that into an immediate error instead.
_IN_PROCESS_GUARD = threading.Lock()


def real_models_enabled(environ: Mapping[str, str] | None = None) -> bool:
    """True only when the opt-in variable is the literal ``1``."""
    env = os.environ if environ is None else environ
    return env.get(REAL_MODELS_ENV) == "1"


requires_real_models = pytest.mark.skipif(not real_models_enabled(), reason=REAL_MODELS_SKIP_REASON)


class _Unloadable(Protocol):
    async def unload(self) -> bool: ...


P = TypeVar("P", bound=_Unloadable)


@contextmanager
def real_model_lock(path: Path | None = None) -> Iterator[None]:
    """Hold the machine-wide real-model lock for the duration of the block.

    Tries a non-blocking acquire first so a wait is logged once rather than
    looking like a hang; then blocks until the other holder releases. Raises
    ``RuntimeError`` if this process already holds the lock.
    """
    lock_path = REAL_MODEL_LOCK_PATH if path is None else path
    if not _IN_PROCESS_GUARD.acquire(blocking=False):
        raise RuntimeError(
            "real_model_lock re-entered in-process: two real-model fixtures are "
            "alive at once, which would block on the flock forever; keep one "
            "loaded provider per process"
        )
    try:
        fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o644)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                logger.warning("waiting for another real-model test run to release %s", lock_path)
                fcntl.flock(fd, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)
    finally:
        _IN_PROCESS_GUARD.release()


def release_provider(provider: _Unloadable) -> bool:
    """Unload a provider's model from synchronous fixture teardown.

    ``unload`` is a coroutine; a fresh event loop is fine here because no
    generation is in flight when a fixture tears down.
    """
    return asyncio.run(provider.unload())


@contextmanager
def loaded_provider(factory: Callable[[], P], *, lock_path: Path | None = None) -> Iterator[P]:
    """Build a provider under the lock and release it on exit, error or not."""
    with real_model_lock(lock_path):
        provider = factory()
        try:
            yield provider
        finally:
            release_provider(provider)
