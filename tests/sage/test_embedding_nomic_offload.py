"""Event-loop offload tests for NomicEmbeddingProvider.

Asserts that ``embed`` runs the blocking ``model.encode`` on a dedicated
single-thread executor rather than on the asyncio event loop. Releasing
the GIL inside the C extension does not help the lone event loop: the
loop runs on the calling thread, so a direct synchronous encode freezes
every concurrent request until it returns.

The tests do not load nomic-embed-text. They build the provider via
``object.__new__`` to bypass the eager model load and inject a fake model
whose ``encode`` drives the blocking boundary.
"""

import asyncio
import threading
from collections.abc import Callable

import numpy as np
import pytest

from sage.adapters.embedding_nomic import EXPECTED_DIMENSIONS, NomicEmbeddingProvider


@pytest.fixture
def provider():
    """A NomicEmbeddingProvider with its eager model load bypassed.

    Each test assigns ``provider._model``. The executor is shut down on
    teardown so the dedicated worker thread does not leak.
    """
    p = object.__new__(NomicEmbeddingProvider)
    p._model_name = "stub-model"
    p._dimensions = EXPECTED_DIMENSIONS
    p._executor = None
    yield p
    if p._executor is not None:
        p._executor.shutdown(wait=False)


async def _wait_until(predicate: Callable[[], bool]) -> None:
    while not predicate():
        await asyncio.sleep(0.005)


async def test_embed_runs_encode_off_event_loop(provider):
    """While ``model.encode`` blocks in its worker thread, the event loop
    stays responsive: an independent coroutine runs to completion before
    encoding is released.

    Regression guard for the on-loop block: if encode ran on the loop, the
    worker would freeze it for the full ``release.wait`` window and the
    ``_wait_until(started)`` probe would time out.
    """
    started = threading.Event()
    release = threading.Event()
    in_progress = threading.Event()

    class _FakeModel:
        def encode(self, texts, **kwargs):
            in_progress.set()
            started.set()
            release.wait(timeout=2.0)
            in_progress.clear()
            return np.zeros((len(texts), EXPECTED_DIMENSIONS), dtype=np.float32)

    provider._model = _FakeModel()

    embed_task = asyncio.create_task(provider.embed(["a", "b"]))
    try:
        await asyncio.wait_for(_wait_until(started.is_set), timeout=1.0)
        assert in_progress.is_set()

        probe = await asyncio.wait_for(asyncio.sleep(0, result="alive"), timeout=1.0)
        assert probe == "alive"
        assert in_progress.is_set(), "encode finished before the probe; no concurrency shown"
    finally:
        release.set()

    result = await asyncio.wait_for(embed_task, timeout=2.0)
    assert len(result) == 2
    assert len(result[0]) == EXPECTED_DIMENSIONS


async def test_embed_uses_dedicated_single_thread(provider):
    """Encoding runs on a dedicated, reused single thread named
    ``sage-embedding*`` -- never the main/event-loop thread.

    Pins the "dedicated single-thread executor" requirement: a bare
    ``to_thread`` would not guarantee the same thread across calls, and an
    on-loop implementation would report the main thread.
    """
    thread_names: list[str] = []

    class _FakeModel:
        def encode(self, texts, **kwargs):
            thread_names.append(threading.current_thread().name)
            return np.zeros((len(texts), EXPECTED_DIMENSIONS), dtype=np.float32)

    provider._model = _FakeModel()

    await provider.embed(["a"])
    await provider.embed(["b"])

    assert len(thread_names) == 2
    assert all(name.startswith("sage-embedding") for name in thread_names)
    assert thread_names[0] == thread_names[1], "encoding must reuse one dedicated thread"
    assert thread_names[0] != threading.main_thread().name


async def test_embed_empty_input_creates_no_thread(provider):
    """Empty input returns immediately without spinning up the executor
    (AD-006), so an empty batch never costs a worker thread.
    """
    provider._model = object()  # must never be touched

    assert await provider.embed([]) == []
    assert provider._executor is None
