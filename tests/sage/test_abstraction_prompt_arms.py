"""Unit tests for the attribution measurement's prompt constructions.

CAS-ADR-020 clause (h) names the failure these tests are written against:
asserting that a directive appears in a constructed prompt targets an
input as a stand-in for an outcome, and is green whether or not the model
ever sees it. So the binding tests below assert on the message the
provider actually assembles and sends, and the registry tests assert that
two arms *differ* -- a selector wired to nothing leaves them identical.
"""

from scripts.abstraction_prompt_arms import (
    DEFAULT_CONSTRUCTION,
    PROMPT_CONSTRUCTIONS,
    bind_construction,
)

_ARTIFACT_NOUN = "relevance-triage card"


class _CapturingTokenizer:
    """Surfaces the assembled messages instead of rendering a template.

    The provider's prompt assembly hands its messages to the tokenizer's
    chat template, which is where they would otherwise become an opaque
    string. Capturing them there is what lets a test read the system
    content the provider sends rather than the content it was configured
    with.
    """

    def __init__(self):
        self.messages = None

    def apply_chat_template(self, messages, **kwargs):
        self.messages = messages
        return "\n".join(m["content"] for m in messages)


class _FakeProvider:
    """Stands in for the loaded local provider's prompt-assembly surface."""

    def __init__(self):
        self._tokenizer = _CapturingTokenizer()
        self._overhead_tokens = {"adr": 1234}

    def _build_prompt(self, text, doc_type):  # pragma: no cover - replaced by binding
        raise AssertionError("the production assembly should have been rebound")


class TestRegistry:
    def test_the_current_arm_renders_what_production_sends(self):
        """The default arm delegates rather than restating the prompt.

        A frozen copy here would drift the moment the production prompt
        changed, and the measurement would then compare two historical
        constructions while reporting one of them as current.
        """
        from sage.adapters.abstraction_prompt import _format_system_prompt

        for doc_type in ("adr", None):
            rendered = PROMPT_CONSTRUCTIONS["current"].render_system_prompt(doc_type)

            assert rendered == _format_system_prompt(doc_type)

    def test_the_arms_render_different_system_prompts(self):
        """Three distinct constructions, pairwise.

        A registry whose entries collapsed to one value would let every
        arm of an attribution run report the same measurement while
        appearing to vary the thing under test.
        """
        rendered = {
            name: construction.render_system_prompt("adr")
            for name, construction in PROMPT_CONSTRUCTIONS.items()
        }

        assert len(set(rendered.values())) == len(PROMPT_CONSTRUCTIONS)

    def test_only_the_historical_arms_carry_the_artifact_noun(self):
        """The noun whose removal the regression is attributed to.

        This is the axis the middle arm exists to isolate, so its
        presence is asserted per arm rather than inferred from the arms
        differing.
        """
        assert _ARTIFACT_NOUN not in PROMPT_CONSTRUCTIONS["current"].render_system_prompt("adr")
        assert _ARTIFACT_NOUN in PROMPT_CONSTRUCTIONS["pre-revision"].render_system_prompt("adr")
        assert _ARTIFACT_NOUN in PROMPT_CONSTRUCTIONS["framing-noun-restored"].render_system_prompt(
            "adr"
        )

    def test_the_middle_arm_moves_only_the_framing_sentence(self):
        """It restores the noun while holding delimitation constant.

        If it also dropped the delimitation it would be the pre-revision
        arm under another name, and the two-axis apportionment the
        measurement exists for would collapse to one.
        """
        middle = PROMPT_CONSTRUCTIONS["framing-noun-restored"]
        current = PROMPT_CONSTRUCTIONS["current"]

        assert middle.delimit_source is True
        assert middle.render_system_prompt("adr") != current.render_system_prompt("adr")
        # The delimitation paragraph is what the pre-revision arm lacks.
        assert "<source_document>" in middle.render_system_prompt("adr")

    def test_only_the_pre_revision_arm_leaves_the_source_undelimited(self):
        text = "Body text."

        assert PROMPT_CONSTRUCTIONS["pre-revision"].render_source(text) == text
        assert PROMPT_CONSTRUCTIONS["current"].render_source(text) != text

    def test_the_default_arm_is_the_production_construction(self):
        assert DEFAULT_CONSTRUCTION == "current"
        assert DEFAULT_CONSTRUCTION in PROMPT_CONSTRUCTIONS


class TestBindConstruction:
    def test_the_bound_construction_reaches_the_assembled_messages(self):
        """The selected arm is what the provider sends, not what it stores.

        Asserting on the registry alone would pass on a binding wired to
        nothing; this reads the system content off the tokenizer the
        provider handed its messages to.
        """
        provider = _FakeProvider()
        bind_construction(provider, PROMPT_CONSTRUCTIONS["pre-revision"])

        provider._build_prompt("Body text.", "adr")

        system, user = provider._tokenizer.messages
        assert system["role"] == "system"
        assert _ARTIFACT_NOUN in system["content"]
        assert user["content"] == "Body text."

    def test_rebinding_switches_arms_on_one_provider(self):
        """Two arms in one process, one set of loaded weights.

        The local provider is a process-wide singleton, so an
        implementation that needed a fresh instance per arm would either
        reload the model or be refused; this pins that it does not.

        Both turns are asserted, not just the system one. The arms differ
        along two axes and a binding that read only the system prompt --
        passing the source through raw whatever the construction says --
        satisfies every system-message assertion in this module while
        measuring an arm that exists nowhere: pre-revision wording with
        current delimitation.
        """
        provider = _FakeProvider()

        bind_construction(provider, PROMPT_CONSTRUCTIONS["pre-revision"])
        provider._build_prompt("Body text.", "adr")
        first_system, first_user = provider._tokenizer.messages

        bind_construction(provider, PROMPT_CONSTRUCTIONS["current"])
        provider._build_prompt("Body text.", "adr")
        second_system, second_user = provider._tokenizer.messages

        assert _ARTIFACT_NOUN in first_system["content"]
        assert _ARTIFACT_NOUN not in second_system["content"]
        assert first_user["content"] == "Body text."
        assert second_user["content"] != "Body text."
        assert "<source_document>" in second_user["content"]

    def test_binding_clears_the_template_overhead_cache(self):
        """Overhead is a property of the construction, not just the doc_type.

        The cache is keyed by doc_type alone. Carried across a rebind, the
        second arm would size its input budget against the first arm's
        prompt -- and the arms differ in length by both a framing
        sentence and the delimitation paragraph -- so it would truncate a
        different set of documents than its own numbers claim.
        """
        provider = _FakeProvider()
        assert provider._overhead_tokens

        bind_construction(provider, PROMPT_CONSTRUCTIONS["pre-revision"])

        assert provider._overhead_tokens == {}
