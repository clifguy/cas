"""Unit tests for the local provider's opener-constraint binding.

The pure automaton lives in ``abstraction_utils`` and is covered beside
the detector it mirrors. This module covers the half that has to know
about a tokenizer: the vocabulary index that turns a forbidden surface
into token ids, and the decoding-time processor that applies it.

Both are driven through injected primitives -- decode, index, mask -- so
the whole file runs without MLX, Metal, or model weights. The mask itself
is the provider's one-line MLX call and is exercised only in a live
generation, like every other line that touches the loaded model.
"""

import pytest

from sage.adapters import abstraction_qwen3
from sage.adapters.abstraction_qwen3 import (
    Qwen3AbstractionProvider,
    _build_surface_index,
    _OpenerConstraint,
    _suppress_logits,
)
from tests.sage.conftest import FakeGenerationResponse, stub_stream_generate


class _FakeTokenizer:
    """A vocabulary of literal surfaces, indexed by position."""

    def __init__(self, surfaces: list[str]) -> None:
        self._surfaces = surfaces
        self.vocab_size = len(surfaces)

    def batch_decode(self, sequences):
        return [self._surfaces[ids[0]] for ids in sequences]


class _Recorder:
    """Captures what the processor asked of each injected primitive."""

    def __init__(self, text_by_ids=None):
        self.decoded: list[list[int]] = []
        self.masked: list[tuple[int, ...]] = []
        self._text_by_ids = text_by_ids or {}

    def decode(self, ids):
        self.decoded.append(list(ids))
        return self._text_by_ids.get(tuple(ids), "")

    def mask(self, logits, ids):
        self.masked.append(tuple(ids))
        return "MASKED"


def _constraint(recorder, doc_type="ticket", surfaces=None):
    return _OpenerConstraint(
        doc_type,
        decode=recorder.decode,
        surfaces=surfaces if surfaces is not None else {" ticket": (7,)},
        mask=recorder.mask,
    )


class TestSurfaceIndex:
    """What a forbidden surface resolves to in a real vocabulary."""

    def test_separates_word_initial_from_continuation_forms(self):
        """The leading space is load-bearing, so it survives indexing.

        " ticket" opens a word and "ticket" continues one. Collapsing the
        two would mask a continuation token at a word boundary, changing
        generation at a position the detector never inspects.
        """
        index = _build_surface_index(_FakeTokenizer([" ticket", "ticket"]))

        assert index[" ticket"] == (0,)
        assert index["ticket"] == (1,)

    def test_ignores_a_token_that_merely_shares_a_prefix(self):
        """Prefix blocking would forbid every word starting with the type.

        "ticketing" is a different word; masking it would make the
        constraint wider than the detector at exactly the position where
        the difference is invisible to the clause (f) counter.
        """
        index = _build_surface_index(_FakeTokenizer([" ticket", " ticketing"]))

        assert index[" ticket"] == (0,)

    def test_admits_a_trailing_punctuation_variant(self):
        """The detector strips edge punctuation, so the index must too.

        " ticket," compares bare in the opener, so a constraint that let
        it through would be defeated by a comma.
        """
        index = _build_surface_index(_FakeTokenizer([" ticket,", " (ticket)"]))

        assert index[" ticket"] == (0, 1)

    def test_folds_case(self):
        index = _build_surface_index(_FakeTokenizer([" Ticket", " TICKET"]))

        assert index[" ticket"] == (0, 1)

    def test_skips_a_surface_that_normalizes_to_nothing(self):
        """Punctuation-only and whitespace-only tokens index to no word."""
        index = _build_surface_index(_FakeTokenizer(["   ", " ,", ""]))

        assert index == {}


class TestProcessorState:
    """How the processor locates the generated suffix, and when it stops."""

    def test_watermarks_the_prompt_prefix_on_the_first_call(self):
        """The first call precedes the first generated token.

        MLX invokes a processor only from the sampling step, never from
        the chunked prefill loop, so whatever tokens it carries on call
        one are prompt. Decoding them would feed the automaton the tail of
        the prompt and open the window on the prompt's own wording.
        """
        recorder = _Recorder()
        constraint = _constraint(recorder)

        constraint([11, 12, 13, 14, 15], "LOGITS")

        assert recorder.decoded == [[]]

    def test_decodes_only_the_generated_suffix(self):
        recorder = _Recorder()
        constraint = _constraint(recorder)

        constraint([11, 12, 13], "LOGITS")
        constraint([11, 12, 13, 90, 91], "LOGITS")

        assert recorder.decoded == [[], [90, 91]]

    def test_short_circuits_permanently_once_the_window_closes(self):
        """Past the opening sentence the processor stops working entirely.

        Asserting on the return value would pass for an implementation
        that recomputed on every token forever and simply found nothing to
        mask. The claim being pinned is the cost one: the mechanism is
        live for the opening clause and is a no-op for the rest of a
        generation, which is what makes a per-token decode affordable.
        """
        recorder = _Recorder(text_by_ids={(90,): "This document records a policy."})
        constraint = _constraint(recorder)

        constraint([11], "LOGITS")
        constraint([11, 90], "LOGITS")
        constraint([11, 90, 91], "LOGITS")
        constraint([11, 90, 91, 92], "LOGITS")

        assert recorder.decoded == [[], [90]]
        assert recorder.masked == []


class TestProcessorMasking:
    """What the processor does once the automaton names a surface."""

    def test_masks_the_ids_the_index_maps_the_forbidden_surface_to(self):
        recorder = _Recorder(text_by_ids={(90,): "This document is a"})
        constraint = _constraint(recorder, surfaces={" ticket": (7, 8)})

        constraint([11], "LOGITS")
        result = constraint([11, 90], "LOGITS")

        assert recorder.masked == [(7, 8)]
        assert result == "MASKED"

    def test_returns_the_logits_untouched_when_nothing_is_forbidden(self):
        recorder = _Recorder(text_by_ids={(90,): "This document"})
        constraint = _constraint(recorder)

        constraint([11], "LOGITS")
        result = constraint([11, 90], "LOGITS")

        assert recorder.masked == []
        assert result == "LOGITS"

    def test_a_forbidden_surface_absent_from_the_vocabulary_masks_nothing(self):
        """No tokenizer renders every surface; a miss is not an error."""
        recorder = _Recorder(text_by_ids={(90,): "This document is a"})
        constraint = _constraint(recorder, surfaces={})

        constraint([11], "LOGITS")
        result = constraint([11, 90], "LOGITS")

        assert recorder.masked == []
        assert result == "LOGITS"

    def test_a_none_doc_type_masks_nothing(self):
        recorder = _Recorder(text_by_ids={(90,): "This document is a"})
        constraint = _OpenerConstraint(
            None, decode=recorder.decode, surfaces={" ticket": (7,)}, mask=recorder.mask
        )

        constraint([11], "LOGITS")
        constraint([11, 90], "LOGITS")

        assert recorder.masked == []

    def test_masked_ids_are_ordered_and_deduplicated(self):
        """Two candidate surfaces may resolve to an overlapping id set."""
        recorder = _Recorder(text_by_ids={(90,): "This document is a failure"})
        constraint = _constraint(
            recorder,
            doc_type="failure_record",
            surfaces={" record": (7, 5), " failure": (5,)},
        )

        constraint([11], "LOGITS")
        constraint([11, 90], "LOGITS")

        assert recorder.masked == [(5, 7)]


class _IndexableTokenizer:
    """A tokenizer the surface index and the prompt builder can both use."""

    vocab_size = 3

    def __init__(self) -> None:
        self.batch_decode_calls = 0

    def apply_chat_template(self, messages, **kwargs):
        return "\n".join(f"<|{m['role']}|>\n{m['content']}" for m in messages)

    def encode(self, text):
        return [0] * max(len(text), 1)

    def decode(self, tokens):
        return "x" * len(tokens)

    def batch_decode(self, sequences):
        self.batch_decode_calls += 1
        return [" ticket", "ticket", " other"][: len(sequences)]


def _loaded_provider(monkeypatch, *, opener_constraint, tokenizer=None):
    """A provider with the model load stubbed and the preflight open."""
    provider = Qwen3AbstractionProvider(
        model_id="stub-model", context_window=32768, opener_constraint=opener_constraint
    )
    tokenizer = tokenizer or _IndexableTokenizer()

    def fake_ensure_loaded():
        provider._model = object()
        provider._tokenizer = tokenizer
        provider._greedy_sampler = object()

    monkeypatch.setattr(provider, "_ensure_loaded", fake_ensure_loaded)
    monkeypatch.setattr(
        abstraction_qwen3, "free_unified_memory_bytes", lambda: 64 * 1024**3, raising=False
    )
    return provider, tokenizer


class TestProviderWiring:
    """How the constraint reaches -- and does not reach -- the sampling loop."""

    def test_passes_a_logits_processor_when_the_constraint_is_enabled(self, monkeypatch):
        provider, _ = _loaded_provider(monkeypatch, opener_constraint=True)
        captured = {}

        def capture(*args, **kwargs):
            captured.update(kwargs)
            yield FakeGenerationResponse(text="GENERATED")

        provider._generate_fn = capture

        provider._generate_sync("a document", 200, "ticket")

        assert len(captured["logits_processors"]) == 1
        assert isinstance(captured["logits_processors"][0], _OpenerConstraint)

    def test_passes_no_logits_processor_when_the_constraint_is_disabled(self, monkeypatch):
        provider, _ = _loaded_provider(monkeypatch, opener_constraint=False)
        captured = {}

        def capture(*args, **kwargs):
            captured.update(kwargs)
            yield FakeGenerationResponse(text="GENERATED")

        provider._generate_fn = capture

        provider._generate_sync("a document", 200, "ticket")

        assert "logits_processors" not in captured

    def test_passes_no_logits_processor_without_a_doc_type(self, monkeypatch):
        """A document with no type has no phrase to restate."""
        provider, _ = _loaded_provider(monkeypatch, opener_constraint=True)
        captured = {}

        def capture(*args, **kwargs):
            captured.update(kwargs)
            yield FakeGenerationResponse(text="GENERATED")

        provider._generate_fn = capture

        provider._generate_sync("a document", 200, None)

        assert "logits_processors" not in captured

    def test_the_constraint_leaves_the_prompt_byte_identical(self, monkeypatch):
        """The mechanism is decoding-time, not another directive.

        CAS-ADR-020 records that the only prompt wording measured to help
        was removed for cause, so a constraint that worked by editing the
        prompt would be the approach already ruled out. This is the test
        that says it does not.
        """
        prompts = []

        def capture(*args, **kwargs):
            prompts.append(kwargs["prompt"])
            yield FakeGenerationResponse(text="GENERATED")

        for enabled in (False, True):
            provider, _ = _loaded_provider(monkeypatch, opener_constraint=enabled)
            provider._generate_fn = capture
            provider._generate_sync("identical input", 200, "ticket")

        assert prompts[0] == prompts[1]

    def test_two_constrained_calls_produce_identical_prompts_and_output(self, monkeypatch):
        """Greedy decoding stays deterministic under the constraint.

        The constraint carries per-generation state -- a prompt watermark
        and a closed flag -- so a provider that built one processor and
        reused it would decode the wrong span on the second call and mask
        different tokens. Identical inputs must still give identical
        output, which is the property the abstraction path is tested on.
        """
        provider, _ = _loaded_provider(monkeypatch, opener_constraint=True)
        prompts = []
        processors = []

        def capture(*args, **kwargs):
            prompts.append(kwargs["prompt"])
            processors.append(kwargs["logits_processors"][0])
            yield FakeGenerationResponse(text="GENERATED ABSTRACT")

        provider._generate_fn = capture

        first = provider._generate_sync("identical input", 200, "ticket")
        second = provider._generate_sync("identical input", 200, "ticket")

        assert first == second
        assert prompts[0] == prompts[1]
        assert processors[0] is not processors[1]

    def test_the_surface_index_is_built_once_and_reused(self, monkeypatch):
        """A vocabulary pass per generation would be pure overhead."""
        provider, tokenizer = _loaded_provider(monkeypatch, opener_constraint=True)
        provider._generate_fn = stub_stream_generate("GENERATED")

        provider._generate_sync("a document", 200, "ticket")
        provider._generate_sync("another document", 200, "ticket")

        assert tokenizer.batch_decode_calls == 1

    def test_the_index_is_not_built_when_the_constraint_is_disabled(self, monkeypatch):
        provider, tokenizer = _loaded_provider(monkeypatch, opener_constraint=False)
        provider._generate_fn = stub_stream_generate("GENERATED")

        provider._generate_sync("a document", 200, "ticket")

        assert tokenizer.batch_decode_calls == 0

    async def test_unload_clears_the_surface_index(self, monkeypatch):
        """The ids are meaningless against a different vocabulary.

        A reload may install a different tokenizer, and a retained index
        would then mask arbitrary tokens rather than fail -- the same
        reason the template-overhead cache is cleared here.
        """
        provider, _ = _loaded_provider(monkeypatch, opener_constraint=True)
        provider._generate_fn = stub_stream_generate("GENERATED")
        provider._generate_sync("a document", 200, "ticket")
        assert provider._surface_index is not None

        await provider.unload()

        assert provider._surface_index is None


class TestLogitSuppression:
    """The one line that touches the accelerator runtime.

    Everything above drives the constraint through injected primitives, so
    the mask itself -- the only place a wrong axis, a wrong dtype, or a
    no-op would live -- is exercised nowhere. Asserting that a processor
    reaches the sampling loop says the mechanism is wired, not that it
    changes what the sampler selects; this pair closes that gap. Skipped
    where MLX is not installed, which is every non-Apple-Silicon runner.
    """

    def test_drives_the_named_ids_out_of_contention(self):
        """The argmax moves off a suppressed id, and nothing else shifts.

        Asserting only that the suppressed entries dropped would pass for a
        mask that flattened the whole row; asserting only that the argmax
        moved would pass for one that suppressed everything. The surviving
        untouched value is what separates them.
        """
        mx = pytest.importorskip("mlx.core")

        logits = mx.array([[1.0, 5.0, 2.0, 9.0]])
        assert int(mx.argmax(logits, axis=-1).item()) == 3

        masked = _suppress_logits(logits, (3, 1))

        assert int(mx.argmax(masked, axis=-1).item()) == 2
        assert masked[0, 0].item() == 1.0
        assert masked[0, 2].item() == 2.0

    def test_leaves_the_input_row_shape_intact(self):
        """The sampler indexes the returned row; a reshape would break it."""
        mx = pytest.importorskip("mlx.core")

        logits = mx.array([[1.0, 5.0, 2.0, 9.0]])

        assert _suppress_logits(logits, (0,)).shape == logits.shape
