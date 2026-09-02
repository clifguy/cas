"""Opt-in gate, machine-wide lock, and release helper for real-model tests.

A handful of tests load real model weights -- the Qwen3 abstraction model via
MLX and the nomic embedding model via torch. They are minutes of wall-clock
and most of the machine's memory, and two concurrent runs exhaust unified
memory outright. This module keeps them in a separate tier:

* ``requires_real_models`` skips them unless ``SAGE_TEST_REAL_MODELS=1``,
  the same opt-in shape as the Docker smoke tests behind ``SAGE_TEST_DOCKER``.
* ``real_model_lock`` serializes model loads across every pytest process on
  the machine (``flock`` on a file in the temp directory), so a second run
  waits instead of failing on memory.
* ``loaded_provider`` builds a provider under the lock and unloads it on exit,
  so a class- or module-scoped fixture releases the weights the moment its
  tests finish rather than holding them for the rest of the run.
"""

from __future__ import annotations

import asyncio
import fcntl
import logging
import os
import tempfile
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Protocol, TypeVar

import pytest

logger = logging.getLogger(__name__)

REAL_MODELS_ENV = "SAGE_TEST_REAL_MODELS"
REAL_MODELS_SKIP_REASON = (
    f"real-model tests are opt-in: set {REAL_MODELS_ENV}=1 "
    "(loads the Qwen3 MLX model and the nomic embedding model)"
)
REAL_MODEL_LOCK_PATH = Path(tempfile.gettempdir()) / "sage-test-real-models.lock"


def real_models_enabled(environ: Mapping[str, str] | None = None) -> bool:
    """True only when the opt-in variable is the literal ``1``."""
    env = os.environ if environ is None else environ
    return env.get(REAL_MODELS_ENV) == "1"


requires_real_models = pytest.mark.skipif(not real_models_enabled(), reason=REAL_MODELS_SKIP_REASON)


class _Unloadable(Protocol):
    async def unload(self) -> bool: ...


P = TypeVar("P", bound=_Unloadable)


@contextmanager
def real_model_lock() -> Iterator[None]:
    """Hold the machine-wide real-model lock for the duration of the block.

    Tries a non-blocking acquire first so a wait is logged once rather than
    looking like a hang; then blocks until the other holder releases.
    """
    fd = os.open(REAL_MODEL_LOCK_PATH, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            logger.warning(
                "waiting for another real-model test run to release %s",
                REAL_MODEL_LOCK_PATH,
            )
            fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def release_provider(provider: _Unloadable) -> bool:
    """Unload a provider's model from synchronous fixture teardown.

    ``unload`` is a coroutine; a fresh event loop is fine here because no
    generation is in flight when a fixture tears down.
    """
    return asyncio.run(provider.unload())


@contextmanager
def loaded_provider(factory: Callable[[], P]) -> Iterator[P]:
    """Build a provider under the lock and release it on exit, error or not."""
    with real_model_lock():
        provider = factory()
        try:
            yield provider
        finally:
            release_provider(provider)
