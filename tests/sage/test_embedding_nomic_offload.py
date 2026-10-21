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


@pytest.mark.parametrize("first", ["encode", "count"])
async def test_sizing_and_inference_share_tokenizer_exclusion(provider, first):
    """The fast tokenizer's mutable truncation state cannot be borrowed twice.

    Hold either real provider operation inside the model/tokenizer boundary,
    then launch the other on its production worker shape. The second must wait
    without blocking the event loop; both operations must subsequently finish.
    """
    entered = threading.Event()
    release = threading.Event()
    second_started = threading.Event()
    calls = []

    class Model:
        def use(self, operation):
            calls.append(operation)
            if operation == first:
                entered.set()
                release.wait(timeout=2.0)

        def tokenizer(self, text):
            self.use("count")
            return {"input_ids": [1, 2, 3]}

        def encode(self, texts, **kwargs):
            self.use("encode")
            return np.zeros((len(texts), EXPECTED_DIMENSIONS), dtype=np.float32)

    provider._model = Model()

    original_sync = provider._embed_sync

    def observed_sync(texts):
        if first == "count":
            second_started.set()
        return original_sync(texts)

    provider._embed_sync = observed_sync

    async def run(operation, *, second=False):
        if operation == "encode":
            return await provider.embed(["inference"])

        def count():
            if second:
                second_started.set()
            return provider.count_tokens("sizing")

        return await asyncio.to_thread(count)

    first_task = asyncio.create_task(run(first))
    second_task = None
    try:
        await asyncio.wait_for(_wait_until(entered.is_set), timeout=1.0)
        second_task = asyncio.create_task(
            run("count" if first == "encode" else "encode", second=True)
        )
        await asyncio.wait_for(_wait_until(second_started.is_set), timeout=1.0)
        await asyncio.sleep(0.05)
        assert not second_task.done(), "sizing and inference borrowed the tokenizer concurrently"
        assert calls == [first]
        assert not first_task.done(), "the first operation must still be gated"
    finally:
        release.set()
        outcomes = await asyncio.gather(
            first_task, *([second_task] if second_task else []), return_exceptions=True
        )
    assert not any(isinstance(outcome, BaseException) for outcome in outcomes), outcomes
    assert calls == [first, "count" if first == "encode" else "encode"]


async def test_bulk_yields_to_interactive_and_preserves_content(provider):
    started = threading.Event()
    release = threading.Event()
    calls = []

    class Model:
        def encode(self, texts, **kwargs):
            calls.append(list(texts))
            if len(calls) == 1:
                started.set()
                release.wait(2)
            return np.array([[float(text.split(":")[1])] for text in texts])

    provider._model = Model()
    bulk = asyncio.create_task(provider.embed([f"bulk:{i}" for i in range(12)]))
    await asyncio.wait_for(_wait_until(started.is_set), 1)
    query = asyncio.create_task(provider.embed(["query:99"]))
    await asyncio.sleep(0)
    release.set()
    assert await asyncio.wait_for(query, 1) == [[99.0]]
    assert await bulk == [[float(i)] for i in range(12)]
    flattened = [text for batch in calls for text in batch]
    assert flattened.index("query:99") < flattened.index("bulk:11")
    assert all(len(batch) <= 1 for batch in calls)


async def test_cancelled_bulk_drops_remaining_work_and_recovers(provider):
    started = threading.Event()
    release = threading.Event()
    calls = []

    class Model:
        def encode(self, texts, **kwargs):
            calls.extend(texts)
            if texts[0] == "bulk":
                started.set()
                release.wait(2)
            if texts[0] == "fail":
                raise ValueError("inference failed")
            return np.zeros((len(texts), 1))

    provider._model = Model()
    bulk = asyncio.create_task(provider.embed(["bulk"] * 12))
    await asyncio.wait_for(_wait_until(started.is_set), 1)
    bulk.cancel()
    with pytest.raises(asyncio.CancelledError):
        await bulk
    query = asyncio.create_task(provider.embed(["query"]))
    release.set()
    assert await asyncio.wait_for(query, 1) == [[0.0]]
    assert calls.count("bulk") == 1
    with pytest.raises(ValueError, match="inference failed"):
        await provider.embed(["fail"])
    assert await provider.embed(["recovered"]) == [[0.0]]


async def test_continuing_callers_cannot_starve_bulk(provider):
    calls = []

    class Model:
        def encode(self, texts, **kwargs):
            calls.extend(texts)
            return np.zeros((len(texts), 1))

    provider._model = Model()

    async def traffic():
        for _ in range(20):
            await provider.embed(["query"])

    await asyncio.wait_for(
        asyncio.gather(provider.embed(["bulk"] * 20), *(traffic() for _ in range(4))), 3
    )
    assert calls.count("bulk") == 20
    assert calls.count("query") == 80
    bulk_positions = [i for i, text in enumerate(calls) if text == "bulk"]
    assert max(b - a for a, b in zip(bulk_positions, bulk_positions[1:])) <= 5
    assert calls.index("query") < bulk_positions[-1]


async def test_cancel_queued_and_waiting_work_releases_bounded_admission(provider):
    started = threading.Event()
    release = threading.Event()
    calls = []

    class Model:
        def encode(self, texts, **kwargs):
            calls.extend(texts)
            if texts == ["active"]:
                started.set()
                release.wait(2)
            return np.zeros((len(texts), 1))

    provider._model = Model()
    active = asyncio.create_task(provider.embed(["active"]))
    await asyncio.wait_for(_wait_until(started.is_set), 1)
    queued = asyncio.create_task(provider.embed(["cancel-queued"]))
    waiting = asyncio.create_task(provider.embed(["cancel-waiting"]))
    await asyncio.sleep(0.02)
    assert provider._executor._work_queue.qsize() == 1
    assert not queued.done() and not waiting.done()
    queued.cancel()
    waiting.cancel()
    await asyncio.gather(queued, waiting, return_exceptions=True)
    release.set()
    await asyncio.wait_for(active, 1)
    assert await asyncio.wait_for(provider.embed(["recovered"]), 1) == [[0.0]]
    assert calls == ["active", "recovered"]


async def test_repeated_cancel_does_not_accumulate_executor_work(provider):
    started = threading.Event()
    release = threading.Event()
    calls = []

    class Model:
        def encode(self, texts, **kwargs):
            calls.extend(texts)
            if texts == ["active"]:
                started.set()
                release.wait(2)
            return np.zeros((len(texts), 1))

    provider._model = Model()
    active = asyncio.create_task(provider.embed(["active"]))
    await asyncio.wait_for(_wait_until(started.is_set), 1)
    for _ in range(30):
        task = asyncio.create_task(provider.embed(["abandoned"] * 100))
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        assert provider._executor._work_queue.qsize() <= 1
    release.set()
    await asyncio.wait_for(active, 1)
    assert await asyncio.wait_for(provider.embed(["live"]), 1) == [[0.0]]
    assert calls == ["active", "live"]


def test_idle_provider_reused_after_event_loop_shutdown(provider):
    class Model:
        def encode(self, texts, **kwargs):
            return np.zeros((len(texts), 1))

    provider._model = Model()

    async def contend():
        return await asyncio.gather(*(provider.embed(["input"]) for _ in range(8)))

    assert len(asyncio.run(contend())) == 8
    assert len(asyncio.run(contend())) == 8


def test_closed_loop_abandoned_inference_does_not_poison_or_overadmit(provider):
    started = threading.Event()
    release = threading.Event()
    calls = []

    class Model:
        def encode(self, texts, **kwargs):
            calls.extend(texts)
            if texts == ["old-failure"]:
                started.set()
                release.wait(2)
                raise ValueError("old abandoned failure")
            return np.zeros((len(texts), 1))

    provider._model = Model()

    async def abandon():
        task = asyncio.create_task(provider.embed(["old-failure"]))
        await asyncio.wait_for(_wait_until(started.is_set), 1)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    asyncio.run(abandon())

    async def resume():
        tasks = [asyncio.create_task(provider.embed(["new"])) for _ in range(8)]
        await asyncio.sleep(0.02)
        assert provider._executor._work_queue.qsize() <= 1
        release.set()
        return await asyncio.wait_for(asyncio.gather(*tasks), 1)

    try:
        assert len(asyncio.run(resume())) == 8
    finally:
        release.set()
    assert calls == ["old-failure"] + ["new"] * 8
