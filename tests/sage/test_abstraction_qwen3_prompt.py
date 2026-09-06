"""Prompt-content tests for Qwen3AbstractionProvider.

Asserts two prompt-layer directives:

  1. **Acronym non-expansion.** The system prompt instructs the model
     to leave unknown acronyms unexpanded — surfaced by the
     blind review (Qwen3-30B invented "Context-Aware System" for
     "CAS"; Qwen3-8B invented "Component Architecture Specification";
     correct per CAS-ADR-007 is "Clif's Agentic System").
  2. **Metadata restraint (opening phrases).** The system prompt
     forbids opening with phrases that restate the document's
     ``doc_type`` (e.g., "This document is an architecture decision
     record...").

The tests run through ``generate_abstract`` so the directives are
verified along the full call path. They do not load MLX: ``_ensure_loaded``
is stubbed and ``_generate_fn`` is replaced by a capture function. A
fake tokenizer surfaces the system-message content through
``apply_chat_template`` so the captured ``prompt`` kwarg carries the
directive text.

These assertions guard the directives against silent removal from the
prompt; they say nothing about whether the model obeys them. Outcome
enforcement for the acronym directive is the deterministic post-generation
check in ``sage.adapters.abstraction_utils`` (CAS-ADR-020 clause (e)),
which inspects the generated abstract itself.
"""

import pytest

from sage.adapters.abstraction_qwen3 import (
    Qwen3AbstractionProvider,
    _format_system_prompt,
)
from tests.sage.conftest import FakeGenerationResponse, stub_stream_generate

ACRONYM_DIRECTIVE = (
    "Do not expand acronyms or initialisms unless the expansion "
    "appears verbatim in the source text."
)
METADATA_RESTRAINT_DIRECTIVE = (
    "Do not open your description with a phrase that restates the document's type"
)
SPECIFICS_RESTRAINT_DIRECTIVE = (
    "Do not introduce specifics (numbers, names, dates, quotes, examples) "
    "that are not present in the source text."
)
REASONING_TRIGGERS = (
    "<think>",
    "</think>",
    "think step by step",
    "Let me think",
    "Let's think",
    "reasoning:",
)


class _ContentSurfacingTokenizer:
    """Tokenizer fake whose ``apply_chat_template`` surfaces the
    system-message content so assertions on the captured ``prompt``
    kwarg can verify directive presence end-to-end.

    Also records every ``apply_chat_template`` invocation so a test
    can inspect the kwargs (specifically ``enable_thinking``).
    """

    def __init__(self):
        self.calls = []

    def apply_chat_template(
        self,
        messages,
        tokenize=False,
        add_generation_prompt=True,
        enable_thinking=False,
    ):
        self.calls.append(
            {
                "messages": messages,
                "tokenize": tokenize,
                "add_generation_prompt": add_generation_prompt,
                "enable_thinking": enable_thinking,
            }
        )
        parts = []
        for msg in messages:
            parts.append(f"<|{msg['role']}|>\n{msg['content']}")
        return "\n".join(parts)

    def encode(self, text):
        return [0] * max(len(text), 1)

    def decode(self, tokens):
        return "x" * len(tokens)


@pytest.fixture
def provider(monkeypatch):
    """A ``Qwen3AbstractionProvider`` with model load stubbed and the
    content-surfacing tokenizer wired in.

    The preflight memory check is monkeypatched to always pass; tests
    inject ``provider._generate_fn`` as a capture function.
    """
    p = Qwen3AbstractionProvider(model_id="stub-model")
    tokenizer = _ContentSurfacingTokenizer()

    def fake_ensure_loaded():
        p._model = object()
        p._tokenizer = tokenizer
        p._greedy_sampler = object()
        # _generate_fn is set by the individual test before invocation

    monkeypatch.setattr(p, "_ensure_loaded", fake_ensure_loaded)
    monkeypatch.setattr(
        "sage.adapters.abstraction_qwen3.free_unified_memory_bytes",
        lambda: 32 * 1024**3,
    )
    monkeypatch.setattr(
        "sage.adapters.abstraction_qwen3.min_free_bytes",
        lambda: 4 * 1024**3,
    )
    # Stash the tokenizer on the provider for tests that need to read it
    p._test_tokenizer = tokenizer
    return p


async def test_acronym_non_expansion_directive_present_in_prompt(provider):
    """Fix 1 acceptance: the prompt sent to ``_generate_fn`` carries
    the acronym non-expansion directive. Guards against the
    failure mode where the model hallucinates an expansion for an
    acronym (e.g., "CAS") whose expansion is not in the source text.
    """
    captured = {}

    def capture_generate(*args, **kwargs):
        captured["prompt"] = kwargs.get("prompt")
        yield FakeGenerationResponse(text="GENERATED ABSTRACT")

    provider._generate_fn = capture_generate

    await provider.generate_abstract(
        text="The CAS portfolio document describes the system.",
        max_tokens=200,
        doc_type="adr",
    )

    assert captured["prompt"] is not None, "_generate_fn was not invoked"
    assert ACRONYM_DIRECTIVE in captured["prompt"], (
        "Acronym non-expansion directive missing from the prompt sent to the model. "
        f"Expected substring: {ACRONYM_DIRECTIVE!r}"
    )


async def test_metadata_restraint_directive_present_in_prompt(provider):
    """Fix 2 acceptance: the prompt sent to ``_generate_fn`` carries
    the metadata-restraint directive against opening phrases that
    restate the document's ``doc_type``. Independent from Test 1 so a
    future revert of one directive without the other produces a
    precise failure signal.
    """
    captured = {}

    def capture_generate(*args, **kwargs):
        captured["prompt"] = kwargs.get("prompt")
        yield FakeGenerationResponse(text="GENERATED ABSTRACT")

    provider._generate_fn = capture_generate

    await provider.generate_abstract(
        text="Some document content.",
        max_tokens=200,
        doc_type="adr",
    )

    assert captured["prompt"] is not None, "_generate_fn was not invoked"
    assert METADATA_RESTRAINT_DIRECTIVE in captured["prompt"], (
        "Metadata-restraint directive missing from the prompt sent to the model. "
        f"Expected substring: {METADATA_RESTRAINT_DIRECTIVE!r}"
    )


async def test_specifics_restraint_directive_present_in_prompt(provider):
    """The prompt sent to ``_generate_fn`` carries the CAS-ADR-020 clause (e)
    anti-fabrication directive against introducing specifics the source does
    not state. A removal guard only: the directive's presence in the prompt
    is not evidence the model obeys it -- clause (e) enforcement rests on
    the deterministic post-generation checks, and the ADR records that a
    prompt-construction assertion cannot observe a breach. Independent from
    the other directive tests so a revert of this sentence alone produces a
    precise failure signal.
    """
    captured = {}

    def capture_generate(*args, **kwargs):
        captured["prompt"] = kwargs.get("prompt")
        yield FakeGenerationResponse(text="GENERATED ABSTRACT")

    provider._generate_fn = capture_generate

    await provider.generate_abstract(
        text="Some document content.",
        max_tokens=200,
        doc_type="adr",
    )

    assert captured["prompt"] is not None, "_generate_fn was not invoked"
    assert SPECIFICS_RESTRAINT_DIRECTIVE in captured["prompt"], (
        "Specifics-restraint directive missing from the prompt sent to the model. "
        f"Expected substring: {SPECIFICS_RESTRAINT_DIRECTIVE!r}"
    )


async def test_directives_compatible_with_enable_thinking_false(provider):
    """Ticket acceptance #2: the directives are
    ``enable_thinking=False``-compatible.

    Two assertions compose into the guard:

      * **Wiring:** ``apply_chat_template`` is invoked with
        ``enable_thinking=False`` (current behavior, must remain).
      * **Content:** the rendered system prompt contains both new
        directives (anchor against coincidental pass) AND contains
        none of the known reasoning-trigger phrases that could
        prompt chain-of-thought emission even with thinking
        disabled.
    """
    provider._generate_fn = stub_stream_generate("ok")

    await provider.generate_abstract(
        text="Some document content.",
        max_tokens=200,
        doc_type="adr",
    )

    # Wiring assertion: every apply_chat_template call must carry
    # enable_thinking=False. We check all calls (probe + real) for
    # defense against a future regression that only fixes some paths.
    tokenizer = provider._test_tokenizer
    assert tokenizer.calls, "apply_chat_template was not invoked"
    for call in tokenizer.calls:
        assert call["enable_thinking"] is False, (
            f"apply_chat_template invoked with enable_thinking={call['enable_thinking']!r}; "
            "must be False for Qwen3 reasoning-disabled mode."
        )

    # Content assertion (anchor): new directives present.
    rendered = _format_system_prompt("adr")
    assert ACRONYM_DIRECTIVE in rendered, (
        "Acronym directive missing from rendered system prompt; "
        "trigger-absent check below would pass coincidentally."
    )
    assert METADATA_RESTRAINT_DIRECTIVE in rendered, (
        "Metadata-restraint directive missing from rendered system prompt; "
        "trigger-absent check below would pass coincidentally."
    )

    # Content assertion (guard): no reasoning triggers.
    for trigger in REASONING_TRIGGERS:
        assert trigger not in rendered, (
            f"System prompt contains reasoning trigger {trigger!r}; "
            "this can defeat enable_thinking=False and cause chain-of-thought leakage."
        )
