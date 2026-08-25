"""Event-loop offload tests for Qwen3AbstractionProvider.

Asserts that ``generate_abstract`` runs its blocking work (model load +
inference) on a dedicated single-thread executor rather than on the
asyncio event loop. The two heavy steps -- the multi-second MLX
generation and the ~16 GB first-call model load -- must not freeze
concurrent requests on the lone HTTP/SSE event loop.

The tests do not load Qwen3. They short-circuit ``_ensure_loaded`` so the
deferred-state machinery is satisfied without touching mlx-lm, and drive
the blocking boundary through a synchronous ``_generate_fn`` stub.
"""

import asyncio
import threading
from collections.abc import Callable

import pytest

from sage.adapters.abstraction_qwen3 import Qwen3AbstractionProvider
from tests.sage.conftest import FakeGenerationResponse


class _FakeTokenizer:
    """Minimal tokenizer stub that satisfies the prompt-build path."""

    def apply_chat_template(
        self,
        messages,
        tokenize=False,
        add_generation_prompt=True,
        enable_thinking=False,
    ):
        return "PROMPT"

    def encode(self, text):
        return [0] * max(len(text), 1)

    def decode(self, tokens):
        return "x" * len(tokens)


@pytest.fixture
def provider(monkeypatch):
    """A Qwen3AbstractionProvider whose model load is stubbed.

    Memory preflight is forced above threshold so dispatch proceeds. The
    provider's executor is shut down on teardown so the dedicated worker
    thread does not leak across the suite.
    """
    monkeypatch.setattr(
        "sage.adapters.abstraction_qwen3.free_unified_memory_bytes",
        lambda: 32 * 1024**3,
    )
    monkeypatch.setattr(
        "sage.adapters.abstraction_qwen3.min_free_bytes",
        lambda: 4 * 1024**3,
    )

    p = Qwen3AbstractionProvider(model_id="stub-model")

    def fake_ensure_loaded():
        p._model = object()
        p._tokenizer = _FakeTokenizer()
        p._greedy_sampler = object()
        # _generate_fn is set by the individual test before invocation.

    monkeypatch.setattr(p, "_ensure_loaded", fake_ensure_loaded)
    yield p
    executor = getattr(p, "_executor", None)
    if executor is not None:
        executor.shutdown(wait=False)


async def _wait_until(predicate: Callable[[], bool]) -> None:
    """Poll ``predicate`` on the event loop until it is truthy.

    Each iteration yields control so that, if the loop is free, work
    progressing on another thread becomes observable here. If the loop is
    blocked, this coroutine never advances -- which is exactly the
    failure the caller's ``asyncio.wait_for`` timeout surfaces.
    """
    while not predicate():
        await asyncio.sleep(0.005)


async def test_generate_abstract_runs_inference_off_event_loop(provider):
    """While ``_generate_fn`` blocks in its worker thread, the event loop
    stays responsive: an independent coroutine runs to completion before
    generation is released.

    Regression guard for the on-loop block: if generation ran on the loop,
    the worker would freeze it for the full ``release.wait`` window and the
    ``_wait_until(started)`` probe would time out.
    """
    started = threading.Event()
    release = threading.Event()
    in_progress = threading.Event()

    def blocking_generate(*args, **kwargs):
        in_progress.set()
        started.set()
        release.wait(timeout=2.0)
        in_progress.clear()
        yield FakeGenerationResponse(text="ok")

    provider._generate_fn = blocking_generate

    gen_task = asyncio.create_task(
        provider.generate_abstract("doc text", max_tokens=10, doc_type=None)
    )
    try:
        # The worker thread enters the blocking fn only if the loop is free
        # to dispatch and we are free to observe it.
        await asyncio.wait_for(_wait_until(started.is_set), timeout=1.0)
        assert in_progress.is_set()

        # The loop processes an unrelated coroutine while the worker thread
        # is still mid-generation.
        probe = await asyncio.wait_for(asyncio.sleep(0, result="alive"), timeout=1.0)
        assert probe == "alive"
        assert in_progress.is_set(), "generation finished before the probe; no concurrency shown"
    finally:
        release.set()

    assert await asyncio.wait_for(gen_task, timeout=2.0) == "ok"


async def test_generate_abstract_uses_dedicated_single_thread(provider):
    """Inference runs on a dedicated, reused single thread named
    ``sage-abstraction*`` -- never the main/event-loop thread.

    Pins the "dedicated single-thread executor" requirement: a bare
    ``to_thread`` (default executor) would not guarantee the same thread
    across calls, and an on-loop implementation would report the main
    thread.
    """
    thread_names: list[str] = []

    def recording_generate(*args, **kwargs):
        thread_names.append(threading.current_thread().name)
        yield FakeGenerationResponse(text="ok")

    provider._generate_fn = recording_generate

    assert await provider.generate_abstract("a", max_tokens=10, doc_type=None) == "ok"
    assert await provider.generate_abstract("b", max_tokens=10, doc_type=None) == "ok"

    assert len(thread_names) == 2
    assert all(name.startswith("sage-abstraction") for name in thread_names)
    assert thread_names[0] == thread_names[1], "inference must reuse one dedicated thread"
    assert thread_names[0] != threading.main_thread().name
