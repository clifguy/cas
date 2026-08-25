"""Context-window resolution tests for the local MLX abstraction provider.

Covers the three-way relationship between the configured window, the loaded
model's native window, and the window the truncation helper actually spends:

  * configured under native  -> configured stands, silently
  * configured over native   -> reported at load, clamped to native
  * native unknown           -> configured stands, silently

The suite drives the real ``_ensure_loaded`` against a fake ``mlx_lm``
injected into ``sys.modules`` rather than stubbing the method out, because
the guard being tested lives inside it. The fake is deliberately minimal:
it carries only the three symbols the loader imports.
"""

import logging
import sys
import types

import pytest

from sage.adapters.abstraction_qwen3 import (
    DEFAULT_CONTEXT_WINDOW,
    Qwen3AbstractionProvider,
    _reset_qwen3_singleton,
    get_qwen3_abstraction_provider,
)


class _FakeTokenizer:
    """Chat-template tokenizer with one token per character."""

    def apply_chat_template(
        self,
        messages,
        tokenize=False,
        add_generation_prompt=True,
        enable_thinking=False,
    ):
        return "\n".join(f"<|{m['role']}|>\n{m['content']}" for m in messages)

    def encode(self, text):
        return [0] * max(len(text), 1)

    def decode(self, tokens):
        return "x" * len(tokens)


class _FakeResponse:
    """Minimal stand-in for ``mlx_lm.generate.GenerationResponse``."""

    def __init__(self, text: str) -> None:
        self.text = text
        self.prompt_tokens = 10
        self.prompt_tps = 100.0
        self.generation_tokens = 5
        self.generation_tps = 50.0


def _install_fake_mlx(monkeypatch, native_window: int | None):
    """Inject a fake ``mlx_lm`` whose loaded model advertises *native_window*.

    ``native_window=None`` produces a model with no ``args`` attribute at all,
    standing in for a model family that does not advertise its prompt length.
    """

    class _FakeModel:
        pass

    model = _FakeModel()
    if native_window is not None:
        model.args = types.SimpleNamespace(max_position_embeddings=native_window)

    def fake_load(model_id):
        return model, _FakeTokenizer()

    def fake_stream_generate(model, tokenizer, prompt, **kwargs):
        yield _FakeResponse("STUB ABSTRACT")

    mlx_lm = types.ModuleType("mlx_lm")
    mlx_lm.load = fake_load
    mlx_lm.stream_generate = fake_stream_generate
    sample_utils = types.ModuleType("mlx_lm.sample_utils")
    sample_utils.make_sampler = lambda temp=0.0: object()
    mlx_lm.sample_utils = sample_utils

    monkeypatch.setitem(sys.modules, "mlx_lm", mlx_lm)
    monkeypatch.setitem(sys.modules, "mlx_lm.sample_utils", sample_utils)
    return model


def test_native_window_resolved_from_model_args(monkeypatch):
    """A model advertising ``args.max_position_embeddings`` publishes it."""
    _install_fake_mlx(monkeypatch, native_window=40960)

    provider = Qwen3AbstractionProvider(model_id="stub-model", context_window=16384)
    provider._ensure_loaded()

    assert provider._native_context_window == 40960


def test_native_window_absent_leaves_none(monkeypatch):
    """A model family that advertises no prompt length loads without error.

    The guard is best-effort: an unknown native window must not become a
    startup failure for a model that simply does not publish the attribute.
    """
    _install_fake_mlx(monkeypatch, native_window=None)

    provider = Qwen3AbstractionProvider(model_id="stub-model", context_window=131072)
    provider._ensure_loaded()

    assert provider._native_context_window is None


def test_configured_over_native_warns_and_clamps(monkeypatch, caplog):
    """A configured window larger than the model's native window is reported
    at load and clamped to the native value.

    Both numbers appear in the warning so the reader can act on it without
    consulting the config, and the effective window drops to what the weights
    can actually attend over -- the alternative being output that degrades
    with no signal at all.
    """
    _install_fake_mlx(monkeypatch, native_window=40960)

    provider = Qwen3AbstractionProvider(model_id="stub-model", context_window=131072)
    with caplog.at_level(logging.WARNING, logger="sage.adapters.abstraction_qwen3"):
        provider._ensure_loaded()

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    message = warnings[0].getMessage()
    assert "131072" in message
    assert "40960" in message
    assert provider._effective_context_window() == 40960


def test_configured_under_native_is_honored_silently(monkeypatch, caplog):
    """A configured window below the native window stands, with no warning.

    Anti-coincidental-pass: this is the negative control for the clamp test.
    A guard that warned unconditionally, or that always returned the native
    window, would satisfy the clamp assertions and fail here.
    """
    _install_fake_mlx(monkeypatch, native_window=40960)

    provider = Qwen3AbstractionProvider(model_id="stub-model", context_window=16384)
    with caplog.at_level(logging.WARNING, logger="sage.adapters.abstraction_qwen3"):
        provider._ensure_loaded()

    assert [r for r in caplog.records if r.levelno == logging.WARNING] == []
    assert provider._effective_context_window() == 16384


def test_unknown_native_window_honors_configured(monkeypatch, caplog):
    """With no native window to compare against, the configured value stands."""
    _install_fake_mlx(monkeypatch, native_window=None)

    provider = Qwen3AbstractionProvider(model_id="stub-model", context_window=131072)
    with caplog.at_level(logging.WARNING, logger="sage.adapters.abstraction_qwen3"):
        provider._ensure_loaded()

    assert [r for r in caplog.records if r.levelno == logging.WARNING] == []
    assert provider._effective_context_window() == 131072


def test_truncation_uses_effective_window(monkeypatch):
    """The clamped window reaches the code that spends it.

    Anti-coincidental-pass: every other test in this file would pass with
    ``_native_context_window`` recorded and never read -- a bookkeeping
    attribute. This one asserts the truncation helper's available-token
    arithmetic is computed from the clamped value, by checking that a
    document sized between the native and the configured window is
    truncated rather than passed through whole.
    """
    _install_fake_mlx(monkeypatch, native_window=40960)

    provider = Qwen3AbstractionProvider(model_id="stub-model", context_window=131072)
    provider._ensure_loaded()

    max_tokens = 500
    overhead = len(provider._tokenizer.encode(provider._build_prompt("", None)))
    available = 40960 - max_tokens - overhead

    # One character per token: comfortably inside 131072, past the native 40960.
    text = "y" * (available + 1000)
    outcome = provider._truncate_for_context(text, max_tokens, None)

    assert len(provider._tokenizer.encode(outcome.text)) == available
    assert outcome.input_tokens == available + 1000


def test_factory_resolves_none_to_module_default():
    """An unset context window resolves to the module default at the factory.

    None is the "unconfigured" sentinel the startup dispatch forwards when the
    config omits the field; the provider -- not the caller -- owns the
    fallback, so an unset config reproduces the pre-configuration behavior.
    """
    _reset_qwen3_singleton()
    try:
        provider = get_qwen3_abstraction_provider(model_id="stub-model", context_window=None)
        assert provider._context_window == DEFAULT_CONTEXT_WINDOW
    finally:
        _reset_qwen3_singleton()


@pytest.mark.parametrize("configured", [16384, 65536])
def test_configured_window_survives_construction(configured):
    """An explicit window is stored verbatim, not silently normalized."""
    provider = Qwen3AbstractionProvider(model_id="stub-model", context_window=configured)
    assert provider._context_window == configured
