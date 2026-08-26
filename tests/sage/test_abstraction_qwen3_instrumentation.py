"""Latency-record and truncation-efficiency tests for the local MLX provider.

Two concerns that share a fixture:

  * the structured latency record the provider emits per generation, and
  * the truncation helper's cost, which the record's own token counts depend
    on -- the cheap pre-check deliberately skips measuring the input, so the
    record derives the count from the prompt length the model reports.

Every test drives the real ``_generate_sync`` with the model load stubbed;
none loads MLX weights.
"""

import json
import logging

import pytest

from sage.adapters.abstraction_prompt import wrap_source_document
from sage.adapters.abstraction_qwen3 import Qwen3AbstractionProvider
from tests.sage.conftest import FakeGenerationResponse, stub_stream_generate

TIMING_LOGGER = "sage.abstraction.timing"
ADAPTER_LOGGER = "sage.adapters.abstraction_qwen3"


class _CountingTokenizer:
    """One token per character, counting the calls made against it."""

    def __init__(self) -> None:
        self.encode_calls: list[str] = []
        self.template_calls: list[str | None] = []

    def apply_chat_template(
        self,
        messages,
        tokenize=False,
        add_generation_prompt=True,
        enable_thinking=False,
    ):
        self.template_calls.append(messages[1]["content"])
        return "\n".join(f"<|{m['role']}|>\n{m['content']}" for m in messages)

    def encode(self, text):
        self.encode_calls.append(text)
        return [0] * max(len(text), 1)

    def decode(self, tokens):
        return "x" * len(tokens)


class _EncodeRefusingTokenizer(_CountingTokenizer):
    """Raises if asked to encode one specific document.

    Makes "the full encode was skipped" observable rather than inferred: a
    pre-check that ran and was then ignored still reaches the encode, and
    trips this. Keyed on the document itself rather than on a length
    threshold, because the constant template overhead is measured through the
    same method and is longer than a short document.
    """

    def __init__(self, forbidden: str) -> None:
        super().__init__()
        self._forbidden = forbidden

    def encode(self, text):
        if text == self._forbidden:
            raise AssertionError("the full document was encoded despite the pre-check")
        return super().encode(text)


def _make_provider(monkeypatch, tokenizer, context_window=32768):
    """A provider with the model load stubbed and the preflight open."""
    provider = Qwen3AbstractionProvider(model_id="stub-model", context_window=context_window)

    def fake_ensure_loaded():
        provider._model = object()
        provider._tokenizer = tokenizer
        provider._greedy_sampler = object()

    monkeypatch.setattr(provider, "_ensure_loaded", fake_ensure_loaded)
    monkeypatch.setattr(
        "sage.adapters.abstraction_qwen3.free_unified_memory_bytes",
        lambda: 32 * 1024**3,
    )
    monkeypatch.setattr(
        "sage.adapters.abstraction_qwen3.min_free_bytes",
        lambda: 4 * 1024**3,
    )
    # The load is deferred to first use, so `_tokenizer` is still None when a
    # test wants to introspect it. Stash it where tests can read it directly.
    provider._test_tokenizer = tokenizer
    return provider


@pytest.fixture
def provider(monkeypatch):
    p = _make_provider(monkeypatch, _CountingTokenizer())
    yield p
    if p._executor is not None:
        p._executor.shutdown(wait=False)


def _records(caplog):
    """Decode every structured record emitted on the timing logger."""
    return [json.loads(r.getMessage()) for r in caplog.records if r.name == TIMING_LOGGER]


# ── The latency record ──────────────────────────────────────────────


async def test_local_record_emitted_with_all_fields(provider, caplog):
    """One record per generation, carrying the full local breakdown."""
    provider._generate_fn = stub_stream_generate("GENERATED")

    with caplog.at_level(logging.INFO, logger=TIMING_LOGGER):
        await provider.generate_abstract("some document text", 200, "adr")

    records = _records(caplog)
    assert len(records) == 1
    record = records[0]
    assert record["label"] == "abstract.mlx"
    for field in (
        "document_chars",
        "input_tokens",
        "retained_tokens",
        "prompt_tokens",
        "prefill_ms",
        "prefill_tps",
        "decode_ms",
        "generated_tokens",
        "decode_tps",
    ):
        assert field in record, f"record is missing {field}"
        assert record[field] is not None, f"{field} was emitted as null"

    # Both phases now publish a count and a rate; a reader seeing the old
    # names would be reading a record this provider no longer emits.
    for retired in ("input_chars", "tokens_per_second"):
        assert retired not in record, f"record still carries the retired {retired}"


async def test_local_record_metrics_derive_from_generation_response(provider, caplog):
    """Durations, counts, and rates come from the model's own phase statistics.

    Anti-coincidental-pass: the stubbed rates imply a 2-second prefill and an
    8-second decode, which a wall clock measuring a sub-millisecond stub
    cannot produce. All four stubbed figures are mutually distinct, so a field
    wired to the wrong attribute is visible rather than plausible.

    This test cannot tell a sourced prefill rate from one recomputed out of
    the published count and duration -- that recomputation is algebraically
    identical here. The degenerate case below is what separates them.
    """
    provider._generate_fn = stub_stream_generate(
        "GENERATED",
        prompt_tokens=1000,
        prompt_tps=500.0,
        generation_tokens=200,
        generation_tps=25.0,
    )

    with caplog.at_level(logging.INFO, logger=TIMING_LOGGER):
        await provider.generate_abstract("some document text", 200, "adr")

    record = _records(caplog)[0]
    assert record["prompt_tokens"] == 1000
    assert record["prefill_ms"] == 2000.0
    assert record["prefill_tps"] == 500.0
    assert record["decode_ms"] == 8000.0
    assert record["generated_tokens"] == 200
    assert record["decode_tps"] == 25.0
    assert "tokens_per_second" not in record


async def test_local_record_prefill_rate_is_read_not_recomputed(provider, caplog):
    """A prefill rate survives a prompt count that yields no prefill duration.

    Anti-coincidental-pass, and the load-bearing case for the whole prefill
    pair. On any ordinary generation, reading the rate off the model and
    recomputing it from the published count and duration give the same number,
    because the duration was itself derived from those two values -- so no
    assertion on a normal record can tell the two implementations apart.

    A zero prompt count breaks the tie: the duration helper reports an absent
    duration rather than a zero one, so a recomputing implementation has
    nothing to divide and must emit null, while one reading the model's own
    figure still reports the rate the model gave.
    """
    provider._generate_fn = stub_stream_generate(
        "GENERATED",
        prompt_tokens=0,
        prompt_tps=500.0,
        generation_tokens=200,
        generation_tps=25.0,
    )

    with caplog.at_level(logging.INFO, logger=TIMING_LOGGER):
        await provider.generate_abstract("some document text", 200, "adr")

    record = _records(caplog)[0]
    assert record["prompt_tokens"] == 0
    assert record["prefill_ms"] is None
    assert record["prefill_tps"] == 500.0


async def test_local_record_correlates_with_truncation_notice(monkeypatch, caplog):
    """The record's token counts are the truncation notice's two numbers.

    The provider interface carries no document id, so the two log lines are
    joined on the counts they share. If they disagreed, a reader could not
    tell which notice belongs to which record.
    """
    tokenizer = _CountingTokenizer()
    provider = _make_provider(monkeypatch, tokenizer, context_window=4096)
    provider._ensure_loaded()

    overhead = len(tokenizer.encode(provider._build_prompt("", "adr")))
    available = 4096 - 200 - overhead
    provider._generate_fn = stub_stream_generate("GENERATED", prompt_tokens=available + overhead)

    long_text = "z" * 20000
    with caplog.at_level(logging.INFO):
        await provider.generate_abstract(long_text, 200, "adr")

    notices = [
        r.getMessage()
        for r in caplog.records
        if r.name == ADAPTER_LOGGER and "Truncating input" in r.getMessage()
    ]
    assert len(notices) == 1

    record = _records(caplog)[0]
    assert f"from {record['input_tokens']} to {record['retained_tokens']} tokens" in (notices[0])

    # The published prompt count is the retained document plus the constant
    # template, so the template's cost falls out of a single record instead of
    # requiring a run that happened to truncate.
    assert record["prompt_tokens"] - record["retained_tokens"] == overhead
    provider._executor.shutdown(wait=False)


async def test_retained_equals_input_when_not_truncated(provider, caplog):
    """A document that fits reports equal input and retained counts.

    The pre-check skips measuring the input for such a document, so the count
    is derived from the prompt length the model reports. That derivation is
    exact rather than an estimate precisely because nothing was dropped.
    """
    text = "short document"
    provider._ensure_loaded()
    overhead = len(provider._tokenizer.encode(provider._build_prompt("", "adr")))
    provider._generate_fn = stub_stream_generate("GENERATED", prompt_tokens=overhead + len(text))

    with caplog.at_level(logging.INFO):
        await provider.generate_abstract(text, 200, "adr")

    notices = [r for r in caplog.records if "Truncating input" in r.getMessage()]
    assert notices == []

    record = _records(caplog)[0]
    assert record["retained_tokens"] == len(text)
    assert record["input_tokens"] == record["retained_tokens"]


async def test_instrumentation_does_not_alter_prompt_or_output(provider):
    """Two identical calls produce identical prompts and identical output.

    The runnable stand-in for the real-weights determinism test: greedy
    decoding is deterministic only if nothing upstream of the sampler varies
    between calls, and measurement must not be that something.
    """
    prompts: list[str] = []

    def capture(*args, **kwargs):
        prompts.append(kwargs["prompt"])
        yield FakeGenerationResponse(text="GENERATED ABSTRACT")

    provider._generate_fn = capture

    first = await provider.generate_abstract("identical input", 200, "adr")
    second = await provider.generate_abstract("identical input", 200, "adr")

    assert first == second
    assert len(prompts) == 2
    assert prompts[0] == prompts[1]


async def test_generated_text_is_concatenation_of_stream_segments(provider):
    """Every streamed segment lands in the returned text, in order.

    Anti-coincidental-pass: a seam that returned only the final segment, or
    that dropped the first, passes any single-segment stub. Three distinct
    segments make both failures visible.
    """
    provider._generate_fn = stub_stream_generate("Hel", "lo ", "world")

    result = await provider.generate_abstract("some document text", 200, "adr")

    assert result == "Hello world"


# ── Truncation-path efficiency ──────────────────────────────────────


async def test_short_document_skips_full_encode(monkeypatch, caplog):
    """A document that cannot overflow the window is never encoded.

    The tokenizer refuses to encode anything longer than the chat template,
    so reaching the full encode raises. A pre-check that is computed and then
    discarded fails here while passing every behavioral test.
    """
    text = "a short document that comfortably fits"
    tokenizer = _EncodeRefusingTokenizer(forbidden=text)
    provider = _make_provider(monkeypatch, tokenizer)
    provider._generate_fn = stub_stream_generate("GENERATED")

    result = await provider.generate_abstract(text, 200, "adr")

    assert result == "GENERATED"
    assert text not in tokenizer.encode_calls
    provider._executor.shutdown(wait=False)


async def test_long_document_still_truncates(monkeypatch, caplog):
    """The negative control: an oversized document is measured and trimmed."""
    tokenizer = _CountingTokenizer()
    provider = _make_provider(monkeypatch, tokenizer, context_window=4096)
    provider._generate_fn = stub_stream_generate("GENERATED")
    provider._ensure_loaded()

    overhead = len(tokenizer.encode(provider._build_prompt("", "adr")))
    available = 4096 - 200 - overhead
    long_text = "z" * (available + 500)

    outcome = provider._truncate_for_context(long_text, 200, "adr")

    assert outcome.input_tokens == available + 500
    assert len(tokenizer.encode(outcome.text)) == available


async def test_bytes_precheck_is_conservative_on_multibyte(monkeypatch):
    """A text short in characters but long in bytes takes the encode path.

    Anti-coincidental-pass: the unsound `len(text) <= available` variant of
    the pre-check looks correct on ASCII and silently waves this document
    through unmeasured, under-truncating it. Every token consumes at least
    one UTF-8 byte, so only the byte length can prove a document safe.
    """
    tokenizer = _CountingTokenizer()
    provider = _make_provider(monkeypatch, tokenizer, context_window=4096)
    provider._ensure_loaded()

    overhead = len(tokenizer.encode(provider._build_prompt("", "adr")))
    available = 4096 - 200 - overhead

    # Three bytes per character: under `available` in characters, over it in
    # bytes, so the cheap bound cannot prove it safe.
    text = "漢" * (available - 100)
    assert len(text) < available < len(text.encode("utf-8"))

    tokenizer.encode_calls.clear()
    outcome = provider._truncate_for_context(text, 200, "adr")

    assert text in tokenizer.encode_calls, "pre-check wrongly cleared a multibyte text"
    assert outcome.input_tokens == len(text)


async def test_template_overhead_encoded_once_per_doc_type(provider):
    """The constant chat-template overhead is measured once, then reused."""
    provider._generate_fn = stub_stream_generate("GENERATED")
    tokenizer = provider._test_tokenizer

    await provider.generate_abstract("first document", 200, "adr")
    await provider.generate_abstract("second document", 200, "adr")

    empty_user_templates = [c for c in tokenizer.template_calls if c == wrap_source_document("")]
    assert len(empty_user_templates) == 1


async def test_template_overhead_recomputed_for_new_doc_type(provider):
    """A different doc_type gets its own measurement.

    Anti-coincidental-pass: the system prompt varies by doc_type, so a single
    cached scalar passes the same-type test above and then mis-sizes the
    window for every other type.
    """
    provider._generate_fn = stub_stream_generate("GENERATED")
    tokenizer = provider._test_tokenizer

    await provider.generate_abstract("first document", 200, "adr")
    await provider.generate_abstract("second document", 200, "ticket")

    empty_user_templates = [c for c in tokenizer.template_calls if c == wrap_source_document("")]
    assert len(empty_user_templates) == 2


async def test_overhead_cache_cleared_on_unload(provider):
    """Unload drops the cache, so a reload re-measures against its tokenizer.

    A cache that never invalidates passes every same-session test and then
    silently mis-sizes the prompt after a reload installs a tokenizer whose
    template encodes differently.
    """
    provider._generate_fn = stub_stream_generate("GENERATED")
    tokenizer = provider._test_tokenizer

    await provider.generate_abstract("first document", 200, "adr")
    assert provider._overhead_tokens != {}

    await provider.unload()
    assert provider._overhead_tokens == {}

    provider._generate_fn = stub_stream_generate("GENERATED")
    await provider.generate_abstract("second document", 200, "adr")

    empty_user_templates = [c for c in tokenizer.template_calls if c == wrap_source_document("")]
    assert len(empty_user_templates) == 2
