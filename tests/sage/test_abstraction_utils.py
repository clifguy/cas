"""Tests for abstraction utility functions: density-proportional token
budgeting and sentence-boundary trimming.

compute_max_tokens: scales the LLM generation budget proportionally to
document length, bounded by a configurable hard cap.

trim_to_sentence_boundary: trims LLM output back to the last complete
sentence to prevent mid-sentence truncation.
"""

import pytest

from sage.adapters.abstraction_utils import compute_max_tokens, trim_to_sentence_boundary
from sage.config import StackAbstractionConfig
from sage.config import VaultAbstractionConfig as AbstractionConfig

# ---------------------------------------------------------------------------
# compute_max_tokens
# ---------------------------------------------------------------------------


class TestComputeMaxTokens:
    """Tests for density-proportional token budget computation."""

    def test_short_document_gets_near_base(self):
        """A short document (100 words) should get close to base tokens."""
        config = AbstractionConfig()  # defaults
        result = compute_max_tokens(100, config)
        # 150 + 100 * 0.02 = 152
        assert result == 152

    def test_medium_document_scales_linearly(self):
        """A medium document (5000 words) should scale proportionally."""
        config = AbstractionConfig()
        result = compute_max_tokens(5000, config)
        # 150 + 5000 * 0.02 = 250
        assert result == 250

    def test_large_document_hits_cap(self):
        """A large document (60000 words) should hit the hard cap."""
        config = AbstractionConfig()
        result = compute_max_tokens(60000, config)
        # 150 + 60000 * 0.02 = 1350, under cap of 1500
        assert result == 1350

    def test_very_large_document_capped(self):
        """A very large document should not exceed max_abstract_tokens."""
        config = AbstractionConfig()
        result = compute_max_tokens(200000, config)
        # 150 + 200000 * 0.02 = 4150, capped to 1500
        assert result == config.max_abstract_tokens

    def test_zero_word_count_returns_base(self):
        """Zero word count should return base tokens."""
        config = AbstractionConfig()
        result = compute_max_tokens(0, config)
        assert result == config.base_abstract_tokens

    def test_negative_word_count_returns_base(self):
        """Negative word count (defensive) should return base tokens."""
        config = AbstractionConfig()
        result = compute_max_tokens(-50, config)
        assert result == config.base_abstract_tokens

    def test_custom_config_values(self):
        """Custom config parameters should be respected."""
        config = AbstractionConfig(
            base_abstract_tokens=200,
            tokens_per_word=0.05,
            max_abstract_tokens=800,
        )
        result = compute_max_tokens(10000, config)
        # 200 + 10000 * 0.05 = 700, under cap of 800
        assert result == 700

    def test_custom_config_cap_applied(self):
        """Custom cap should apply when computed value exceeds it."""
        config = AbstractionConfig(
            base_abstract_tokens=200,
            tokens_per_word=0.05,
            max_abstract_tokens=400,
        )
        result = compute_max_tokens(10000, config)
        # 200 + 10000 * 0.05 = 700, capped to 400
        assert result == 400

    def test_result_is_integer(self):
        """Result should always be an integer (token counts are discrete)."""
        config = AbstractionConfig()
        result = compute_max_tokens(333, config)
        assert isinstance(result, int)

    def test_boundary_at_exactly_cap(self):
        """Word count that produces exactly the cap value."""
        config = AbstractionConfig(
            base_abstract_tokens=100,
            tokens_per_word=0.01,
            max_abstract_tokens=200,
        )
        # 100 + 10000 * 0.01 = 200, exactly the cap
        result = compute_max_tokens(10000, config)
        assert result == 200


# ---------------------------------------------------------------------------
# trim_to_sentence_boundary
# ---------------------------------------------------------------------------


class TestTrimToSentenceBoundary:
    """Tests for sentence-boundary trimming of LLM output."""

    def test_complete_sentence_unchanged(self):
        """Text ending at a sentence boundary should be returned as-is."""
        text = "This is a complete sentence."
        assert trim_to_sentence_boundary(text) == text

    def test_trailing_whitespace_preserved(self):
        """Trailing whitespace after a sentence end is acceptable."""
        text = "First sentence. Second sentence.  "
        result = trim_to_sentence_boundary(text)
        assert result == "First sentence. Second sentence."

    def test_truncated_mid_sentence(self):
        """Text truncated mid-sentence should trim to last complete sentence."""
        text = "First sentence. Second sentence. Third sent"
        result = trim_to_sentence_boundary(text)
        assert result == "First sentence. Second sentence."

    def test_exclamation_mark_boundary(self):
        """Exclamation marks should be recognized as sentence boundaries."""
        text = "What a result! The data shows partial"
        result = trim_to_sentence_boundary(text)
        assert result == "What a result!"

    def test_question_mark_boundary(self):
        """Question marks should be recognized as sentence boundaries."""
        text = "Why does this matter? Because the underlying"
        result = trim_to_sentence_boundary(text)
        assert result == "Why does this matter?"

    def test_no_sentence_boundary_returns_original(self):
        """Text with no sentence boundaries should be returned as-is."""
        text = "a long fragment with no sentence-ending punctuation"
        assert trim_to_sentence_boundary(text) == text

    def test_empty_string(self):
        """Empty string should return empty string."""
        assert trim_to_sentence_boundary("") == ""

    def test_whitespace_only(self):
        """Whitespace-only string should return empty string."""
        assert trim_to_sentence_boundary("   ") == ""

    def test_multiple_sentences_trims_to_last_complete(self):
        """Only the trailing incomplete sentence should be removed."""
        text = "One. Two. Three. Four. Five is incomp"
        result = trim_to_sentence_boundary(text)
        assert result == "One. Two. Three. Four."

    def test_period_in_abbreviation_not_false_boundary(self):
        """An abbreviation mid-sentence survives the trim.

        This case is satisfied by trimming to the last boundary of any
        kind, because a real boundary follows the abbreviation. It is
        therefore NOT evidence that abbreviations are handled -- see
        ``test_trailing_abbreviation_is_not_a_boundary`` for the case
        that is.
        """
        text = "Dr. Smith analyzed the data. The results were inconcl"
        result = trim_to_sentence_boundary(text)
        assert result == "Dr. Smith analyzed the data."

    def test_trailing_abbreviation_is_not_a_boundary(self):
        """An abbreviation period at the cut point is not a sentence end.

        The load-bearing partner of the test above: here the abbreviation
        is the LAST period in the text, so an implementation that treats
        every period as a boundary returns the fragment unchanged and
        fails only this assertion. Mirrors a real truncated abstract that
        ended "...distinguishing category errors vs."
        """
        text = "The distinction holds. It separates category errors vs."
        result = trim_to_sentence_boundary(text)
        assert result == "The distinction holds."

    def test_trailing_list_number_is_not_a_boundary(self):
        """A bare list-item number at line start is not a sentence end.

        Mirrors a real truncated abstract whose final characters were a
        dangling enumerator: "6. **Suffering and Ill-Health** ... 7."
        """
        text = "The note revises several sections.\n6. Suffering and Ill-Health was revised.\n7."
        result = trim_to_sentence_boundary(text)
        assert result == (
            "The note revises several sections.\n6. Suffering and Ill-Health was revised."
        )

    def test_sentence_ending_in_a_number_still_terminates(self):
        """A number ending an ordinary sentence remains a boundary.

        Guards the list-number rule against over-trimming: the digits here
        are mid-line, not a line-leading enumerator. An implementation that
        rejects any period after digits fails this.
        """
        text = "The native window is 262144. The prefill rate is lower at that"
        result = trim_to_sentence_boundary(text)
        assert result == "The native window is 262144."

    def test_etc_still_terminates(self):
        """``etc.`` is deliberately outside the non-terminal set.

        Unlike ``e.g.`` or ``vs.``, it routinely ends a sentence, so
        treating it as non-terminal would discard a complete final
        sentence in the common case.
        """
        text = "It covers ingestion, retrieval, etc."
        assert trim_to_sentence_boundary(text) == text

    def test_sentence_ending_with_closing_paren(self):
        """Sentence ending with punctuation inside parens should be handled."""
        text = "See the results (Figure 1). The next phase inv"
        result = trim_to_sentence_boundary(text)
        assert result == "See the results (Figure 1)."

    def test_sentence_ending_with_closing_quote(self):
        """Sentence ending with punctuation before a closing quote."""
        text = 'He said "hello." Then he left the'
        result = trim_to_sentence_boundary(text)
        assert result == 'He said "hello."'

    def test_single_complete_sentence(self):
        """A single complete sentence should be returned unchanged."""
        text = "The algorithm converges in O(n log n) time."
        assert trim_to_sentence_boundary(text) == text

    def test_newlines_between_sentences(self):
        """Newlines should not prevent sentence boundary detection."""
        text = "First paragraph ends here.\n\nSecond paragraph starts and is trunc"
        result = trim_to_sentence_boundary(text)
        assert result == "First paragraph ends here."


# ---------------------------------------------------------------------------
# StackAbstractionConfig.provider (dispatch pattern, re-anchored at
# stack scope by CAS-ADR-030)
# ---------------------------------------------------------------------------


class TestStackAbstractionConfigProvider:
    """Tests for the ``provider`` dispatch key on StackAbstractionConfig."""

    def test_cfg_001_default_provider_is_local_mlx(self):
        """A StackAbstractionConfig built with no kwargs has
        provider="local-mlx".

        The default mirrors the JSON schema's `default: "local-mlx"` at the
        stack scope so a sage/config.yaml that omits the field still
        constructs a usable provider.
        """
        config = StackAbstractionConfig()
        assert isinstance(config.provider, str)
        assert config.provider == "local-mlx"

    def test_cfg_002_unknown_provider_rejected(self):
        """Pydantic Literal rejects keys outside the supported set.

        Verifies the field type is actually Literal (not bare str); a typo
        that left the field as ``str`` would silently accept any value.
        """
        import pydantic

        with pytest.raises(pydantic.ValidationError):
            StackAbstractionConfig(provider="ollama")
