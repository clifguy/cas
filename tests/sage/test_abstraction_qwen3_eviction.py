"""Eviction-primitive tests for Qwen3AbstractionProvider.

These cover the prevention half of the F8 GPU OOM pattern: a deliberate,
caller-controllable eviction primitive that releases the resident ~16 GB
Qwen3 footprint on demand, plus an idle-policy helper that callers can
use to drive eviction. preflight check and asyncio lock remain
in place and are exercised by ``test_abstraction_qwen3_guardrail.py``;
this file covers the new surface only:

  * ``unload()`` — clear resident model state (idempotent).
  * ``evict_if_idle(s)`` — unload iff idle longer than threshold AND loaded.
  * ``_last_used_at`` — monotonic timestamp updated by ``generate_abstract``.

The tests do not load Qwen3. They short-circuit ``_ensure_loaded`` so the
deferred-state machinery is satisfied without touching mlx-lm.
"""

import asyncio

import pytest

from sage.adapters.abstraction_qwen3 import Qwen3AbstractionProvider
from tests.sage.conftest import stub_stream_generate


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
    """Provider whose model load is stubbed and whose preflight is open.

    Tests that need a fresh (unloaded) provider can call ``provider`` and
    leave ``_model`` as ``None``. Tests that need a loaded provider can
    call the helper closure ``load_now`` returned alongside.
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
        p._generate_fn = stub_stream_generate("GENERATED ABSTRACT")

    monkeypatch.setattr(p, "_ensure_loaded", fake_ensure_loaded)
    yield p
    if p._executor is not None:
        p._executor.shutdown(wait=False)


def _load(provider) -> None:
    """Force the provider into the loaded state via the stubbed ensure."""
    provider._ensure_loaded()


# ───────────────────────────────────────────────────────────────────────
# A, B: unload behavior
# ───────────────────────────────────────────────────────────────────────


async def test_unload_clears_resident_state_and_returns_true(provider):
    """When the model is loaded, ``unload()`` drops every deferred-state
    field back to ``None`` and reports ``True``. This is the eviction
    primitive's contract: callers know the unload happened and the
    footprint is gone.
    """
    _load(provider)
    assert provider._model is not None  # precondition

    result = await provider.unload()

    assert result is True
    assert provider._model is None
    assert provider._tokenizer is None
    assert provider._generate_fn is None
    assert provider._greedy_sampler is None


async def test_unload_when_not_loaded_is_noop_returns_false(provider):
    """A fresh provider's ``unload()`` must be a safe no-op that returns
    ``False``. Without this guarantee, callers wiring periodic eviction
    would risk spurious errors on cold systems.
    """
    assert provider._model is None  # precondition

    result = await provider.unload()

    assert result is False
    assert provider._model is None
    assert provider._tokenizer is None


# ───────────────────────────────────────────────────────────────────────
# C, D, E, F: evict_if_idle policy
# ───────────────────────────────────────────────────────────────────────


async def test_evict_if_idle_evicts_after_threshold(provider, monkeypatch):
    """When the model has been idle longer than the threshold, the
    policy method evicts and reports ``True``. Happy path of the
    caller-driven eviction policy.
    """
    _load(provider)
    provider._last_used_at = 1000.0
    monkeypatch.setattr(
        "sage.adapters.abstraction_qwen3.time.monotonic",
        lambda: 1060.0,
    )

    result = await provider.evict_if_idle(idle_threshold_seconds=30.0)

    assert result is True
    assert provider._model is None


async def test_evict_if_idle_does_not_evict_within_threshold(provider, monkeypatch):
    """When the model has been used recently, the policy method must
    not evict — premature eviction is the worst failure mode of this
    pattern because it forces the ~16 GB reload on the next call. Tests
    C and the unload tests would all pass against an unconditional
    eviction if this case were not asserted.
    """
    _load(provider)
    provider._last_used_at = 1000.0
    monkeypatch.setattr(
        "sage.adapters.abstraction_qwen3.time.monotonic",
        lambda: 1005.0,
    )

    result = await provider.evict_if_idle(idle_threshold_seconds=30.0)

    assert result is False
    assert provider._model is not None


async def test_evict_if_idle_does_not_evict_if_never_used(provider):
    """A loaded provider that has not yet served a ``generate_abstract``
    call has ``_last_used_at is None``. The policy method must treat
    this as "not idle" rather than "infinitely idle" — a model that
    just finished loading should not be immediately reaped.
    """
    _load(provider)
    assert provider._last_used_at is None  # precondition

    result = await provider.evict_if_idle(idle_threshold_seconds=0.0)

    assert result is False
    assert provider._model is not None


async def test_evict_if_idle_does_not_evict_if_not_loaded(provider):
    """A fresh provider with no resident model has nothing to evict.
    The policy method must report ``False`` rather than raising, so a
    periodic caller does not need to track load state itself.
    """
    assert provider._model is None  # precondition

    result = await provider.evict_if_idle(idle_threshold_seconds=0.0)

    assert result is False


# ───────────────────────────────────────────────────────────────────────
# G: idle tracking supplier
# ───────────────────────────────────────────────────────────────────────


async def test_generate_abstract_updates_last_used_at(provider, monkeypatch):
    """``generate_abstract`` must publish a monotonic timestamp at the
    end of a successful call. Without this supplier, the
    ``evict_if_idle`` policy has nothing to read; tests C/D would be
    asserting on a feature whose input is never wired.
    """
    monkeypatch.setattr(
        "sage.adapters.abstraction_qwen3.time.monotonic",
        lambda: 12345.0,
    )
    assert provider._last_used_at is None  # precondition

    await provider.generate_abstract("doc text", max_tokens=10, doc_type=None)

    assert provider._last_used_at == 12345.0


# ───────────────────────────────────────────────────────────────────────
# H: concurrency safety
# ───────────────────────────────────────────────────────────────────────


async def test_unload_acquires_generation_lock(provider):
    """``unload()`` must serialize against ``generate_abstract`` via the
    same module-level ``_generation_lock``. If unload could fire while
    a generation is in flight, the MLX model would be cleared from
    under the active call.
    """
    # Resolve the lock at call time — the conftest may have swapped
    # the module attribute for event-loop isolation.
    from sage.adapters.abstraction_qwen3 import _generation_lock

    _load(provider)

    async with _generation_lock:
        unload_task = asyncio.create_task(provider.unload())
        # Yield so the task gets a chance to run; without the lock guard
        # it would complete here and the assertion would fail.
        await asyncio.sleep(0.05)
        assert not unload_task.done(), "unload() returned without waiting on _generation_lock"

    # Lock released — the contender should now finish.
    result = await asyncio.wait_for(unload_task, timeout=1.0)
    assert result is True
    assert provider._model is None


# ───────────────────────────────────────────────────────────────────────
# I: round-trip with the lazy-load path
# ───────────────────────────────────────────────────────────────────────


async def test_unload_then_generate_triggers_lazy_reload(provider, monkeypatch):
    """After ``unload()``, the next ``generate_abstract`` must re-fire
    the lazy-load path. This guards the round-trip: a future change
    that left the provider unable to recover after eviction would
    break the chosen pattern silently.
    """
    _load(provider)
    await provider.unload()
    assert provider._model is None  # eviction landed

    load_count = {"n": 0}

    def tracking_ensure_loaded():
        load_count["n"] += 1
        provider._model = object()
        provider._tokenizer = _FakeTokenizer()
        provider._greedy_sampler = object()
        provider._generate_fn = stub_stream_generate("RELOADED")

    monkeypatch.setattr(provider, "_ensure_loaded", tracking_ensure_loaded)

    result = await provider.generate_abstract("doc text", max_tokens=10, doc_type=None)

    assert load_count["n"] == 1
    assert result == "RELOADED"
    assert provider._model is not None
