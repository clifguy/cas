"""Utility functions for semantic abstract generation.

compute_max_tokens: density-proportional token budget based on document
word count and AbstractionConfig parameters.

trim_to_sentence_boundary: post-generation trimming to prevent
mid-sentence truncation when the LLM hits the token ceiling.

find_unattested_acronym_glosses: deterministic post-generation check
that acronym expansions claimed by an abstract are attested in the
source text (CAS-ADR-020 clause (e)).
"""

import re
import unicodedata
from dataclasses import dataclass

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


@dataclass(frozen=True)
class AcronymGloss:
    """An acronym paired with the expansion a text claims for it."""

    acronym: str
    expansion: str


# Lowercase function words that contribute no initial when an expansion is
# checked against its acronym ("Clif's Agentic System" -> CAS, "Runtime for
# Orchestration, Operations, and Testing" -> ROOT).
_FUNCTION_WORDS = frozenset({"a", "an", "and", "of", "the", "for", "to", "in", "on", "with"})

# An acronym token: a leading capital, then capitals, digits, ampersands, or
# internal periods (U.S.A.). Length bounds apply to the period-stripped form
# and are enforced separately.
_ACRONYM = r"[A-Z][A-Z0-9&.]{1,12}"

# Forward adjacency: ACR (Expansion Words).
_FORWARD_GLOSS = re.compile(rf"\b({_ACRONYM})\s*\(([^()]+)\)")

# Reversed adjacency: a parenthetical holding only an acronym; the candidate
# expansion is read from the words preceding it.
_REVERSED_GLOSS = re.compile(rf"\(({_ACRONYM})\)")

# The reversed adjacency reads at most this many words back from the
# parenthetical when looking for a consistent expansion.
_MAX_EXPANSION_WORDS = 12

# Typographic apostrophes and dashes unified to their ASCII forms, so a
# model's curly punctuation matches a source's straight punctuation.
_APOSTROPHE_DASH_MAP = str.maketrans(
    {
        "‘": "'",
        "’": "'",
        "‛": "'",
        "ʼ": "'",
        "‐": "-",
        "‑": "-",
        "‒": "-",
        "–": "-",
        "—": "-",
        "−": "-",
    }
)


def _acronym_letters(token: str) -> str:
    """Reduce an acronym token to its alphanumeric characters."""
    return re.sub(r"[^A-Za-z0-9]", "", token)


def _initials(expansion: str) -> str:
    """Casefolded initials of an expansion's initial-bearing words.

    Words are split on whitespace and hyphens; lowercase function words
    are dropped before initials are taken.
    """
    unified = expansion.translate(_APOSTROPHE_DASH_MAP)
    parts = [part.strip("'\".,;:()") for part in re.split(r"[\s\-]+", unified)]
    kept = [
        part
        for part in parts
        if part and not (part.islower() and part.casefold() in _FUNCTION_WORDS)
    ]
    return "".join(part[0] for part in kept).casefold()


def _normalize(text: str) -> str:
    """Normalize text for attestation comparison.

    NFKC-normalizes, unifies apostrophes and dashes, casefolds, and
    collapses whitespace runs to single spaces.
    """
    folded = unicodedata.normalize("NFKC", text).translate(_APOSTROPHE_DASH_MAP).casefold()
    return " ".join(folded.split())


def _trailing_expansion(preceding_words: list[str], acronym: str) -> str | None:
    """Find the shortest trailing word window whose initials fit the acronym.

    Grows the window backwards one word at a time so an attached leading
    article ("the Computer-Aided System") never inflates the claimed
    expansion.

    Returns:
        The candidate expansion as it appeared, or None when no window
        within the lookback bound is consistent with the acronym.
    """
    target = _acronym_letters(acronym).casefold()
    window: list[str] = []
    for word in reversed(preceding_words[-_MAX_EXPANSION_WORDS:]):
        window.insert(0, word)
        candidate = " ".join(window)
        if _initials(candidate) == target:
            return candidate
    return None


def _gloss_candidates(abstract: str) -> list[AcronymGloss]:
    """Collect acronym-expansion pairs claimed by the abstract.

    Covers both adjacency shapes: ``ACR (Expansion Words)`` and
    ``Expansion Words (ACR)``. Candidates are not yet consistency- or
    attestation-checked.
    """
    candidates: list[AcronymGloss] = []
    for match in _FORWARD_GLOSS.finditer(abstract):
        candidates.append(AcronymGloss(acronym=match.group(1), expansion=match.group(2).strip()))
    for match in _REVERSED_GLOSS.finditer(abstract):
        acronym = match.group(1)
        preceding_words = abstract[: match.start()].split()
        expansion = _trailing_expansion(preceding_words, acronym)
        if expansion is not None:
            candidates.append(AcronymGloss(acronym=acronym, expansion=expansion))
    return candidates


def find_unattested_acronym_glosses(abstract: str, source_text: str) -> list[AcronymGloss]:
    """Find acronym glosses in an abstract that the source does not attest.

    A gloss is an acronym paired with a claimed expansion, in either
    adjacency: ``ACR (Expansion Words)`` or ``Expansion Words (ACR)``. A
    parenthetical whose initials are inconsistent with the acronym is an
    ordinary aside, not an expansion claim, and is never flagged. A
    consistent claim is attested when the expansion occurs in the source
    text under normalization (NFKC, casefold, apostrophe and dash
    unification, whitespace collapse).

    This is the deterministic post-generation check CAS-ADR-020 clause (e)
    enforcement rests on: it inspects recorded model output rather than
    prompt construction, so it can fail while the constraint is breached.
    The caller decides what to do with a finding; the function mutates
    nothing.

    Args:
        abstract: The generated semantic abstract to inspect.
        source_text: The full source text the abstract was generated from.

    Returns:
        Unattested glosses in order of first appearance, deduplicated.
        Empty when every claimed expansion is attested or no claim exists.
    """
    normalized_source = _normalize(source_text)
    findings: list[AcronymGloss] = []
    seen: set[AcronymGloss] = set()
    for candidate in _gloss_candidates(abstract):
        letters = _acronym_letters(candidate.acronym)
        if not 2 <= len(letters) <= 10:
            continue
        if _initials(candidate.expansion) != letters.casefold():
            continue
        if _normalize(candidate.expansion) in normalized_source:
            continue
        if candidate not in seen:
            seen.add(candidate)
            findings.append(candidate)
    return findings
