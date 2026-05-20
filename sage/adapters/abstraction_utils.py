"""Utility functions for semantic abstract generation.

compute_max_tokens: density-proportional token budget based on document
word count and AbstractionConfig parameters.

trim_to_sentence_boundary: post-generation trimming to prevent
mid-sentence truncation when the LLM hits the token ceiling.
"""

import re

from sage.config import VaultAbstractionConfig


def compute_max_tokens(word_count: int, config: VaultAbstractionConfig) -> int:
    """Compute a density-proportional max_tokens budget.

    Formula: min(base + word_count * tokens_per_word, hard_cap)

    Args:
        word_count: Number of words in the document text.
        config: Abstraction configuration with scaling parameters.

    Returns:
        Integer token budget, at least base_abstract_tokens and at most
        max_abstract_tokens.
    """
    effective_count = max(word_count, 0)
    raw = config.base_abstract_tokens + effective_count * config.tokens_per_word
    return min(int(raw), config.max_abstract_tokens)


# Sentence-ending punctuation followed by optional closing punctuation
# (quotes, parens, brackets), then whitespace or end-of-string.
_SENTENCE_END = re.compile(
    r'[.!?]["\'\)\]\u201d]*'  # terminal punctuation + optional closers
    r"(?=\s|$)"  # followed by whitespace or end-of-string
)


def trim_to_sentence_boundary(text: str) -> str:
    """Trim text to the last complete sentence boundary.

    Finds the last occurrence of sentence-ending punctuation (.!?)
    optionally followed by closing quotes/parens, then followed by
    whitespace or end-of-string. Truncates everything after that point.

    If no sentence boundary is found, returns the text stripped of
    leading/trailing whitespace. This prevents data loss when the LLM
    produces a single long sentence.

    Args:
        text: Raw LLM output, potentially truncated mid-sentence.

    Returns:
        Text trimmed to the last complete sentence, or the stripped
        original if no boundary exists.
    """
    stripped = text.strip()
    if not stripped:
        return ""

    # Find all sentence boundaries
    matches = list(_SENTENCE_END.finditer(stripped))
    if not matches:
        return stripped

    last_match = matches[-1]
    # Check if the last boundary IS at the end of the text
    if last_match.end() >= len(stripped):
        return stripped

    # Trim to end of last complete sentence
    return stripped[: last_match.end()]
