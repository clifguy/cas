"""Tests for the hosted ``AnthropicAbstractionProvider``.

The provider calls the Anthropic Messages API to generate a
density-proportional semantic abstract behind the same
``AbstractionProvider`` contract as the local Qwen3 provider (CAS-ADR-030).
The Anthropic SDK is mocked throughout -- no network, no API key.

Test IDs follow ANTH-NNN.
"""

import asyncio

import pytest

import sage.adapters.abstraction_qwen3 as _abstraction_qwen3
from sage.adapters.abstraction_anthropic import AnthropicAbstractionProvider
from sage.adapters.abstraction_prompt import _format_system_prompt


class _FakeTextBlock:
    def __init__(self, text: str) -> None:
        self.type = "text"
        self.text = text


class _FakeResponse:
    def __init__(self, blocks: list) -> None:
        self.content = blocks


class _Recorder:
    """Captures constructor and ``messages.create`` calls of the fake client."""

    def __init__(self, response_text: str) -> None:
        self.response_text = response_text
        self.ctor_kwargs: list[dict] = []
        self.create_calls: list[dict] = []


def _install_fake_anthropic(monkeypatch, response_text: str = "abstract") -> _Recorder:
    """Patch ``anthropic.AsyncAnthropic`` with a recording fake.

    The provider does ``from anthropic import AsyncAnthropic`` lazily inside
    its client constructor, so patching the attribute on the ``anthropic``
    module is picked up at call time.
    """
    recorder = _Recorder(response_text)

    class _FakeMessages:
        async def create(self, **kwargs):
            recorder.create_calls.append(kwargs)
            return _FakeResponse([_FakeTextBlock(recorder.response_text)])

    class _FakeAsyncAnthropic:
        def __init__(self, **kwargs):
            recorder.ctor_kwargs.append(kwargs)
            self.messages = _FakeMessages()

    monkeypatch.setattr("anthropic.AsyncAnthropic", _FakeAsyncAnthropic)
    return recorder


@pytest.mark.asyncio
async def test_anth_001_generate_abstract_calls_messages_create_and_returns_text(monkeypatch):
    """generate_abstract sends a single Messages API request with the right
    model, max_tokens, system prompt, and user content, and returns the
    (stripped) text of the response.

    Anti-coincidental-pass: the fake returns a unique string and the test
    asserts that exact string flows back AND that create() saw the configured
    model/max_tokens/system/messages -- a provider that ignored its args or
    returned a canned string would fail.
    """
    recorder = _install_fake_anthropic(monkeypatch, response_text="A density-proportional card.")
    provider = AnthropicAbstractionProvider(model_id="claude-haiku-4-5")

    result = await provider.generate_abstract("Some document text.", max_tokens=200, doc_type="adr")

    assert result == "A density-proportional card."
    assert len(recorder.create_calls) == 1
    call = recorder.create_calls[0]
    assert call["model"] == "claude-haiku-4-5"
    assert call["max_tokens"] == 200
    assert call["system"] == _format_system_prompt("adr")
    assert call["messages"] == [{"role": "user", "content": "Some document text."}]


@pytest.mark.asyncio
async def test_anth_002_max_tokens_and_doc_type_threaded(monkeypatch):
    """The density budget (max_tokens) and doc_type both reach the API: a
    different max_tokens is passed through verbatim and a None doc_type
    renders the no-clause system prompt.
    """
    recorder = _install_fake_anthropic(monkeypatch, response_text="card")
    provider = AnthropicAbstractionProvider(model_id="claude-haiku-4-5")

    await provider.generate_abstract("text", max_tokens=512, doc_type=None)

    call = recorder.create_calls[0]
    assert call["max_tokens"] == 512
    assert call["system"] == _format_system_prompt(None)


@pytest.mark.asyncio
async def test_anth_003_empty_text_raises_runtimeerror_without_api_call(monkeypatch):
    """Empty/whitespace input raises RuntimeError and never reaches the API
    (edge guard short-circuits before any request)."""
    recorder = _install_fake_anthropic(monkeypatch)
    provider = AnthropicAbstractionProvider(model_id="claude-haiku-4-5")

    with pytest.raises(RuntimeError):
        await provider.generate_abstract("   ", max_tokens=100, doc_type=None)

    assert recorder.create_calls == []


@pytest.mark.asyncio
async def test_anth_004_empty_model_output_raises_runtimeerror(monkeypatch):
    """An empty/whitespace model response raises RuntimeError naming the
    empty output rather than returning a blank abstract."""
    _install_fake_anthropic(monkeypatch, response_text="   ")
    provider = AnthropicAbstractionProvider(model_id="claude-haiku-4-5")

    with pytest.raises(RuntimeError):
        await provider.generate_abstract("Some text.", max_tokens=100, doc_type=None)


@pytest.mark.asyncio
async def test_anth_005_api_key_not_passed_explicitly(monkeypatch):
    """The provider constructs ``AsyncAnthropic()`` with no ``api_key=`` kwarg;
    the SDK resolves ``ANTHROPIC_API_KEY`` from the environment. Pins
    'API key from env, never hand-plumbed/committed'.
    """
    recorder = _install_fake_anthropic(monkeypatch)
    provider = AnthropicAbstractionProvider(model_id="claude-haiku-4-5")

    await provider.generate_abstract("text", max_tokens=100, doc_type=None)

    assert len(recorder.ctor_kwargs) == 1
    assert "api_key" not in recorder.ctor_kwargs[0]


@pytest.mark.asyncio
async def test_anth_008_explicit_api_key_passed_to_client(monkeypatch):
    """An explicit api_key is handed to ``AsyncAnthropic(api_key=...)`` -- the
    cloud profile's path, where the key is fetched from the managed secret store
    and never placed in the environment.

    Anti-coincidental-pass: the recorder captures the constructor kwargs; a
    provider that ignored the explicit key (falling back to the env) would not
    show ``api_key`` in the ctor kwargs. Complements ANTH-005, which pins the
    no-key path to the env (no ``api_key`` kwarg).
    """
    recorder = _install_fake_anthropic(monkeypatch)
    provider = AnthropicAbstractionProvider(model_id="claude-haiku-4-5", api_key="sk-explicit")

    await provider.generate_abstract("text", max_tokens=100, doc_type=None)

    assert len(recorder.ctor_kwargs) == 1
    assert recorder.ctor_kwargs[0].get("api_key") == "sk-explicit"


@pytest.mark.asyncio
async def test_anth_006_client_constructed_lazily_and_reused(monkeypatch):
    """Constructing the provider does not create an SDK client; the client is
    created on the first generate_abstract and the same instance is reused on
    the second call (one constructor invocation total).
    """
    recorder = _install_fake_anthropic(monkeypatch)
    provider = AnthropicAbstractionProvider(model_id="claude-haiku-4-5")

    # No client created merely by constructing the provider.
    assert recorder.ctor_kwargs == []

    await provider.generate_abstract("text one", max_tokens=100, doc_type=None)
    await provider.generate_abstract("text two", max_tokens=100, doc_type=None)

    assert len(recorder.ctor_kwargs) == 1  # constructed once, reused


@pytest.mark.asyncio
async def test_anth_007_not_serialized_behind_mlx_generation_lock(monkeypatch):
    """The hosted provider is NOT serialized behind the MLX
    ``_generation_lock`` (the F-8 unified-memory guard is scoped to local
    providers).

    Anti-coincidental-pass: the test HOLDS ``_generation_lock`` for the whole
    duration of the hosted call. If the hosted provider erroneously acquired
    the same lock, it would block forever and ``asyncio.wait_for`` would raise
    TimeoutError. Completing within the timeout proves the hosted call does
    not contend on the local-provider lock.
    """
    _install_fake_anthropic(monkeypatch, response_text="card")
    provider = AnthropicAbstractionProvider(model_id="claude-haiku-4-5")

    async with _abstraction_qwen3._generation_lock:
        result = await asyncio.wait_for(
            provider.generate_abstract("text", max_tokens=100, doc_type=None),
            timeout=2.0,
        )

    assert result == "card"
