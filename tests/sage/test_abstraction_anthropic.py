"""Tests for the hosted ``AnthropicAbstractionProvider``.

The provider calls the Anthropic Messages API to generate a
density-proportional semantic abstract behind the same
``AbstractionProvider`` contract as the local Qwen3 provider (CAS-ADR-030).
The Anthropic SDK is mocked throughout -- no network, no API key.

Test IDs follow ANTH-NNN.
"""

import asyncio
import logging

import httpx
import pytest
from anthropic import BadRequestError, RateLimitError

import sage.adapters.abstraction_qwen3 as _abstraction_qwen3
from sage.adapters.abstraction_anthropic import (
    _MAX_SHRINK_ROUNDS,
    AnthropicAbstractionProvider,
    _input_budget_tokens,
)
from sage.adapters.abstraction_prompt import _format_system_prompt
from sage.adapters.interfaces import (
    AbstractionInputTooLargeError,
    NonRetryableAbstractionError,
)

PROVIDER_LOGGER = "sage.adapters.abstraction_anthropic"

# Ceilings large enough that every text in the pre-existing ANTH-001..008
# cases clears the byte pre-check, so those tests exercise the same
# unguarded-in-effect path they always did.
_AMPLE_INPUT_TOKENS = 200_000

# Total count_tokens round trips the provider may spend on one document: the
# initial count, at most _MAX_SHRINK_ROUNDS proportional corrections, and the
# byte-bound fallback's confirming count.
_MAX_COUNT_CALLS = _MAX_SHRINK_ROUNDS + 2


def _even_counter(text: str) -> int:
    """Uniform ~4 bytes per token, the density of ordinary prose."""
    return max(1, len(text.encode("utf-8")) // 4)


def _uneven_counter(text: str) -> int:
    """Dense head, sparse tail.

    A proportional estimate taken against the whole-document average keeps the
    head and so overshoots, which is what forces the shrink loop to iterate
    rather than converging on its first guess.
    """
    split = len(text) // 4
    head, tail = text[:split], text[split:]
    return max(1, len(head.encode("utf-8")) + len(tail.encode("utf-8")) // 8)


class _FakeTextBlock:
    def __init__(self, text: str) -> None:
        self.type = "text"
        self.text = text


class _FakeResponse:
    def __init__(self, blocks: list) -> None:
        self.content = blocks


class _FakeModelInfo:
    def __init__(self, max_input_tokens: int | None) -> None:
        self.max_input_tokens = max_input_tokens


class _FakeTokenCount:
    def __init__(self, input_tokens: int) -> None:
        self.input_tokens = input_tokens


class _Recorder:
    """Captures constructor, ``models.retrieve``, ``messages.count_tokens``,
    and ``messages.create`` calls of the fake client."""

    def __init__(self, response_text: str, token_counter) -> None:
        self.response_text = response_text
        self.token_counter = token_counter
        self.ctor_kwargs: list[dict] = []
        self.create_calls: list[dict] = []
        self.retrieve_calls: list[str] = []
        self.count_calls: list[dict] = []

    def sent_text(self) -> str:
        """The document text of the single generation request."""
        return self.create_calls[0]["messages"][0]["content"]


def _install_fake_anthropic(
    monkeypatch,
    response_text: str = "abstract",
    *,
    max_input_tokens: int | None = _AMPLE_INPUT_TOKENS,
    retrieve_error: Exception | None = None,
    create_error: Exception | None = None,
    token_counter=_even_counter,
) -> _Recorder:
    """Patch ``anthropic.AsyncAnthropic`` with a recording fake.

    The provider does ``from anthropic import AsyncAnthropic`` lazily inside
    its client constructor, so patching the attribute on the ``anthropic``
    module is picked up at call time.

    ``token_counter`` models the tokenizer: the fake counts whatever the
    provider actually submits (system prompt plus message content), so a test
    can assert what the provider sent without restating the provider's own
    arithmetic.
    """
    recorder = _Recorder(response_text, token_counter)

    class _FakeMessages:
        async def create(self, **kwargs):
            recorder.create_calls.append(kwargs)
            if create_error is not None:
                raise create_error
            return _FakeResponse([_FakeTextBlock(recorder.response_text)])

        async def count_tokens(self, **kwargs):
            recorder.count_calls.append(kwargs)
            payload = (kwargs.get("system") or "") + "".join(
                message["content"] for message in kwargs["messages"]
            )
            return _FakeTokenCount(recorder.token_counter(payload))

    class _FakeModels:
        async def retrieve(self, model_id: str):
            recorder.retrieve_calls.append(model_id)
            if retrieve_error is not None:
                raise retrieve_error
            return _FakeModelInfo(max_input_tokens)

    class _FakeAsyncAnthropic:
        def __init__(self, **kwargs):
            recorder.ctor_kwargs.append(kwargs)
            self.messages = _FakeMessages()
            self.models = _FakeModels()

    monkeypatch.setattr("anthropic.AsyncAnthropic", _FakeAsyncAnthropic)
    return recorder


def _sdk_error(cls, message: str, status: int) -> Exception:
    request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    return cls(message, response=httpx.Response(status, request=request), body=None)


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


@pytest.mark.asyncio
async def test_anth_009_input_limit_discovered_from_model_registry(monkeypatch):
    """The input ceiling is read from the model registry for the configured
    model, not assumed.

    Anti-coincidental-pass: a provider carrying a hardcoded ceiling would never
    consult the registry and would ship this text unchecked. Asserting both
    that the lookup happened for the provider's own model id AND that the small
    reported ceiling changed the outcome (a count was taken, which the ample
    default ceiling never provokes) kills the constant.
    """
    recorder = _install_fake_anthropic(monkeypatch, max_input_tokens=4000)
    provider = AnthropicAbstractionProvider(model_id="claude-haiku-4-5")

    await provider.generate_abstract("x" * 3000, max_tokens=100, doc_type=None)

    assert recorder.retrieve_calls == ["claude-haiku-4-5"]
    assert len(recorder.count_calls) == 1


@pytest.mark.asyncio
async def test_anth_010_discovered_input_limit_cached_across_calls(monkeypatch):
    """The ceiling is discovered once and reused, not re-queried per abstract.

    Anti-coincidental-pass: a per-call lookup satisfies ANTH-009 identically --
    it too records a retrieve for the right model. Only the call count
    separates the two, and a lookup on every document would add a round trip to
    every ingestion.
    """
    recorder = _install_fake_anthropic(monkeypatch, max_input_tokens=4000)
    provider = AnthropicAbstractionProvider(model_id="claude-haiku-4-5")

    await provider.generate_abstract("x" * 3000, max_tokens=100, doc_type=None)
    await provider.generate_abstract("y" * 3000, max_tokens=100, doc_type=None)

    assert len(recorder.retrieve_calls) == 1


@pytest.mark.asyncio
async def test_anth_011_text_within_budget_is_sent_verbatim_without_counting(monkeypatch):
    """An ordinary document is submitted unchanged and costs no counting round
    trip: the byte pre-check settles it.

    Anti-coincidental-pass: this is the control for the whole guard. An
    implementation that counts unconditionally, or that truncates
    unconditionally, passes every over-length case below and fails only here.
    """
    recorder = _install_fake_anthropic(monkeypatch)
    provider = AnthropicAbstractionProvider(model_id="claude-haiku-4-5")

    text = "A short document about nothing in particular."
    await provider.generate_abstract(text, max_tokens=100, doc_type=None)

    assert recorder.count_calls == []
    assert recorder.sent_text() == text


@pytest.mark.asyncio
async def test_anth_012_byte_pre_check_boundary_is_inclusive(monkeypatch):
    """The pre-check admits text up to the budget measured in UTF-8 bytes, and
    counts as soon as it exceeds it by one.

    Anti-coincidental-pass: the two halves bracket the boundary from both
    sides, so a pre-check keyed on character count, or one off by a byte in
    either direction, fails one half while passing the other.
    """
    limit = 8000
    budget = _input_budget_tokens(limit, 100, _format_system_prompt(None))

    at_boundary = _install_fake_anthropic(monkeypatch, max_input_tokens=limit)
    provider = AnthropicAbstractionProvider(model_id="claude-haiku-4-5")
    await provider.generate_abstract("x" * budget, max_tokens=100, doc_type=None)
    assert at_boundary.count_calls == []

    over_boundary = _install_fake_anthropic(monkeypatch, max_input_tokens=limit)
    provider = AnthropicAbstractionProvider(model_id="claude-haiku-4-5")
    await provider.generate_abstract("x" * (budget + 1), max_tokens=100, doc_type=None)
    assert len(over_boundary.count_calls) == 1


@pytest.mark.asyncio
async def test_anth_013_text_over_byte_check_but_under_token_budget_untruncated(monkeypatch):
    """Text whose byte length exceeds the budget but whose token count does not
    is counted and then submitted whole.

    Anti-coincidental-pass: the load-bearing case. An implementation that
    truncates on the byte pre-check alone would silently discard most of every
    large-but-fitting document -- roughly three quarters of it at ordinary
    prose density -- and still return a plausible abstract, passing ANTH-011,
    ANTH-012, ANTH-014 and ANTH-015 unchanged. Only asserting that the text
    arrives whole after a count catches it.
    """
    limit = 8000
    budget = _input_budget_tokens(limit, 100, _format_system_prompt(None))
    text = "x" * (budget + 700)

    recorder = _install_fake_anthropic(monkeypatch, max_input_tokens=limit)
    provider = AnthropicAbstractionProvider(model_id="claude-haiku-4-5")

    await provider.generate_abstract(text, max_tokens=100, doc_type=None)

    assert len(recorder.count_calls) == 1
    counted = recorder.count_calls[0]
    assert counted["model"] == "claude-haiku-4-5"
    assert counted["system"] == _format_system_prompt(None)
    assert counted["messages"] == [{"role": "user", "content": text}]
    assert recorder.sent_text() == text


@pytest.mark.asyncio
async def test_anth_014_over_budget_text_is_reduced_to_fit_and_still_sent(monkeypatch):
    """Text over the token budget is reduced to a leading portion and the
    generation call still goes through.

    Anti-coincidental-pass: asserting the submitted text is a strict *prefix*
    -- not merely shorter -- kills an implementation that re-encodes or
    re-summarizes the input, and asserting that generation still happened kills
    one that fails the document instead of degrading it.
    """
    limit = 8000
    budget = _input_budget_tokens(limit, 100, _format_system_prompt(None))
    text = "x" * 40_000

    recorder = _install_fake_anthropic(monkeypatch, max_input_tokens=limit)
    provider = AnthropicAbstractionProvider(model_id="claude-haiku-4-5")

    result = await provider.generate_abstract(text, max_tokens=100, doc_type=None)

    sent = recorder.sent_text()
    assert result == "abstract"
    assert len(recorder.create_calls) == 1
    assert len(sent) < len(text)
    assert text.startswith(sent)
    assert _even_counter(_format_system_prompt(None) + sent) <= budget


@pytest.mark.asyncio
async def test_anth_015_reduction_is_recorded_and_silent_when_absent(monkeypatch, caplog):
    """A reduction emits one record naming what went in, what was kept, and the
    budget it was fitted to; a document that needed no reduction emits nothing.

    Anti-coincidental-pass: the negative half is what makes the positive half
    mean something. A record emitted unconditionally would satisfy any
    assertion about its contents while reporting truncation for documents that
    were never truncated, which is the reading operators would act on.
    """
    limit = 8000
    system = _format_system_prompt(None)
    budget = _input_budget_tokens(limit, 100, system)
    text = "x" * 40_000
    original_tokens = _even_counter(system + text)

    recorder = _install_fake_anthropic(monkeypatch, max_input_tokens=limit)
    provider = AnthropicAbstractionProvider(model_id="claude-haiku-4-5")

    with caplog.at_level(logging.INFO, logger=PROVIDER_LOGGER):
        await provider.generate_abstract(text, max_tokens=100, doc_type=None)

    records = [r for r in caplog.records if r.name == PROVIDER_LOGGER]
    assert len(records) == 1
    message = records[0].getMessage()
    retained_tokens = _even_counter(system + recorder.sent_text())
    assert str(original_tokens) in message
    assert str(retained_tokens) in message
    assert str(budget) in message

    # Negative control: the same provider on a document that fits says nothing.
    caplog.clear()
    with caplog.at_level(logging.INFO, logger=PROVIDER_LOGGER):
        await provider.generate_abstract("A short document.", max_tokens=100, doc_type=None)
    assert [r for r in caplog.records if r.name == PROVIDER_LOGGER] == []


@pytest.mark.asyncio
async def test_anth_016_shrink_converges_under_uneven_density_and_is_bounded(monkeypatch):
    """Against a document whose token density is uneven, the reduction still
    lands within the budget and spends a bounded number of round trips.

    Anti-coincidental-pass: a single proportional estimate with no verifying
    re-count passes ANTH-014, where uniform density makes the first guess
    correct, and overshoots here. An unbounded correction loop would satisfy
    the budget assertion while spending an unbounded number of API calls on one
    document. Neither failure is visible without both assertions together.
    """
    limit = 8000
    system = _format_system_prompt(None)
    budget = _input_budget_tokens(limit, 100, system)
    text = "x" * 60_000

    recorder = _install_fake_anthropic(
        monkeypatch, max_input_tokens=limit, token_counter=_uneven_counter
    )
    provider = AnthropicAbstractionProvider(model_id="claude-haiku-4-5")

    await provider.generate_abstract(text, max_tokens=100, doc_type=None)

    assert _uneven_counter(system + recorder.sent_text()) <= budget
    assert 1 < len(recorder.count_calls) <= _MAX_COUNT_CALLS


@pytest.mark.parametrize(
    "kwargs",
    [
        pytest.param({"retrieve_error": RuntimeError("registry unreachable")}, id="lookup-fails"),
        pytest.param({"max_input_tokens": None}, id="no-ceiling-reported"),
    ],
)
@pytest.mark.asyncio
async def test_anth_017_undiscoverable_limit_degrades_to_unchecked_call(
    monkeypatch, caplog, kwargs
):
    """When no ceiling can be established the abstract is still produced, from
    the full text, and the condition is reported once rather than per document.

    Anti-coincidental-pass: letting the lookup failure propagate would convert
    a working path into a hard failure for *every* document, not just
    over-large ones -- asserting the returned abstract is what catches that.
    Warning once across two calls kills a re-warn that would flood the log at
    one line per ingestion.
    """
    recorder = _install_fake_anthropic(monkeypatch, **kwargs)
    provider = AnthropicAbstractionProvider(model_id="claude-haiku-4-5")

    with caplog.at_level(logging.WARNING, logger=PROVIDER_LOGGER):
        first = await provider.generate_abstract("x" * 3000, max_tokens=100, doc_type=None)
        second = await provider.generate_abstract("y" * 3000, max_tokens=100, doc_type=None)

    assert first == "abstract"
    assert second == "abstract"
    assert recorder.count_calls == []
    assert recorder.create_calls[0]["messages"][0]["content"] == "x" * 3000
    warnings = [r for r in caplog.records if r.name == PROVIDER_LOGGER]
    assert len(warnings) == 1


@pytest.mark.asyncio
async def test_anth_018_over_length_rejection_raises_non_retryable(monkeypatch):
    """An over-length rejection from the API surfaces as the non-retryable
    abstraction type, carrying the model it was rejected for."""
    error = _sdk_error(BadRequestError, "prompt is too long: 431204 tokens > 199000 maximum", 400)
    _install_fake_anthropic(monkeypatch, create_error=error)
    provider = AnthropicAbstractionProvider(model_id="claude-haiku-4-5")

    with pytest.raises(AbstractionInputTooLargeError) as excinfo:
        await provider.generate_abstract("Some text.", max_tokens=100, doc_type=None)

    assert isinstance(excinfo.value, NonRetryableAbstractionError)
    assert excinfo.value.model_id == "claude-haiku-4-5"
    assert excinfo.value.__cause__ is error


@pytest.mark.parametrize(
    "error",
    [
        pytest.param(
            _sdk_error(BadRequestError, "model: unknown model 'claude-nope'", 400),
            id="unrelated-bad-request",
        ),
        pytest.param(_sdk_error(RateLimitError, "rate limited", 429), id="rate-limit"),
    ],
)
@pytest.mark.asyncio
async def test_anth_018b_other_api_errors_keep_their_retry_budget(monkeypatch, error):
    """API errors that are not over-length propagate unchanged, so the retry
    budget above the port still covers them.

    Anti-coincidental-pass: the highest-value assertion in the guard.
    Classifying every bad request -- or every exception -- as non-retryable
    passes the over-length case above while silently stopping retries for
    throttling and for misconfigurations a later attempt would clear.
    """
    _install_fake_anthropic(monkeypatch, create_error=error)
    provider = AnthropicAbstractionProvider(model_id="claude-haiku-4-5")

    with pytest.raises(type(error)) as excinfo:
        await provider.generate_abstract("Some text.", max_tokens=100, doc_type=None)

    assert not isinstance(excinfo.value, NonRetryableAbstractionError)
