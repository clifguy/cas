"""guardrail tests for Qwen3AbstractionProvider.

Asserts the two MLX/Metal OOM guardrails on
``Qwen3AbstractionProvider.generate_abstract``:

  1. **Preflight memory check** raises a structured
     ``UnifiedMemoryExhaustedError`` (instead of letting MLX abort the
     process) when free unified memory is below threshold, and
     proceeds normally when above threshold.
  2. **Single-flight serialization** via the module-level
     ``_generation_lock`` prevents two concurrent ingest pipelines
     from competing for MLX simultaneously.

The tests do not load Qwen3. They short-circuit ``_ensure_loaded`` so
the deferred-state machinery is satisfied without touching mlx-lm.
"""

import asyncio

import pytest

from sage.adapters.abstraction_qwen3 import Qwen3AbstractionProvider
from sage.utils.unified_memory import UnifiedMemoryExhaustedError


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

    Tests inject behavior via monkeypatch on free_unified_memory_bytes,
    min_free_bytes, and ``provider._generate_fn``.
    """
    p = Qwen3AbstractionProvider(model_id="stub-model")

    def fake_ensure_loaded():
        p._model = object()
        p._tokenizer = _FakeTokenizer()
        p._greedy_sampler = object()
        # _generate_fn is set by the individual test before invocation

    monkeypatch.setattr(p, "_ensure_loaded", fake_ensure_loaded)
    yield p
    if p._executor is not None:
        p._executor.shutdown(wait=False)


async def test_t0029_preflight_below_threshold_raises_structured_error(provider, monkeypatch):
    """When free unified memory is below threshold, generate_abstract
    raises UnifiedMemoryExhaustedError with structured detail in place
    of letting MLX abort the process. Satisfies acceptance.
    """
    monkeypatch.setattr(
        "sage.adapters.abstraction_qwen3.free_unified_memory_bytes",
        lambda: 1 * 1024**3,
    )
    monkeypatch.setattr(
        "sage.adapters.abstraction_qwen3.min_free_bytes",
        lambda: 4 * 1024**3,
    )

    def fail_if_called(*args, **kwargs):
        pytest.fail("_generate_fn invoked despite preflight failure")

    provider._generate_fn = fail_if_called

    with pytest.raises(UnifiedMemoryExhaustedError) as exc_info:
        await provider.generate_abstract("doc text", max_tokens=200, doc_type="ticket")

    err = exc_info.value
    assert err.code == "unified_memory_exhausted"
    assert err.status_code == 503
    assert err.detail == {
        "free_bytes": 1 * 1024**3,
        "min_free_bytes": 4 * 1024**3,
        "model_id": "stub-model",
    }


async def test_t0029_preflight_above_threshold_proceeds(provider, monkeypatch):
    """When free unified memory is above threshold, generate_abstract
    proceeds to invoke _generate_fn. Without this test, the
    failure-mode test above could pass trivially against an
    unconditional raise.
    """
    monkeypatch.setattr(
        "sage.adapters.abstraction_qwen3.free_unified_memory_bytes",
        lambda: 32 * 1024**3,
    )
    monkeypatch.setattr(
        "sage.adapters.abstraction_qwen3.min_free_bytes",
        lambda: 4 * 1024**3,
    )

    calls = []

    def fake_generate(*args, **kwargs):
        calls.append(kwargs)
        return "GENERATED ABSTRACT"

    provider._generate_fn = fake_generate

    result = await provider.generate_abstract("doc text", max_tokens=200, doc_type="ticket")

    assert result == "GENERATED ABSTRACT"
    assert len(calls) == 1


async def test_t0029_lock_serializes_concurrent_calls(provider, monkeypatch):
    """The module-level ``_generation_lock`` is acquired by
    generate_abstract. Holding the lock externally must block a
    concurrent generate_abstract call until the lock is released.
    """
    monkeypatch.setattr(
        "sage.adapters.abstraction_qwen3.free_unified_memory_bytes",
        lambda: 32 * 1024**3,
    )
    monkeypatch.setattr(
        "sage.adapters.abstraction_qwen3.min_free_bytes",
        lambda: 4 * 1024**3,
    )

    provider._generate_fn = lambda *a, **k: "ok"

    from sage.adapters.abstraction_qwen3 import _generation_lock

    async with _generation_lock:
        gen_task = asyncio.create_task(
            provider.generate_abstract("doc", max_tokens=10, doc_type=None)
        )
        # Yield to the event loop so the task gets a chance to run
        # and would complete if the lock were not honored.
        await asyncio.sleep(0.05)
        assert not gen_task.done(), "generate_abstract returned without waiting on _generation_lock"

    # Lock released — the contender should now finish.
    result = await asyncio.wait_for(gen_task, timeout=1.0)
    assert result == "ok"
