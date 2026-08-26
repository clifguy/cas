"""Utility functions for semantic abstract generation.

compute_max_tokens: density-proportional token budget based on document
word count and AbstractionConfig parameters.

trim_to_sentence_boundary: post-generation trimming to prevent
mid-sentence truncation when the LLM hits the token ceiling.

find_unattested_acronym_glosses: deterministic post-generation check
that acronym expansions claimed by an abstract are attested in the
source text (CAS-ADR-020 clause (e)).

find_structure_echo: deterministic post-generation check that an abstract
is continuous prose rather than reproduced document structure (CAS-ADR-020
clause (k)).
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

# Abbreviations whose period is part of the token rather than a sentence
# end. Membership is deliberately narrow: an entry here costs a real
# sentence ending whenever the abbreviation legitimately closes one, so
# the set holds only forms that essentially never do. "etc." is excluded
# for exactly that reason -- it routinely ends a sentence, and rejecting
# it would discard the final sentence of any text that closes with it.
_NON_TERMINAL_ABBREVIATIONS = frozenset(
    {
        "e.g.",
        "i.e.",
        "cf.",
        "vs.",
        "al.",
        "viz.",
        "approx.",
        "esp.",
        "dr.",
        "mr.",
        "mrs.",
        "ms.",
        "st.",
        "fig.",
        "no.",
    }
)

# The token immediately preceding a candidate boundary: the run of
# non-whitespace characters ending at the punctuation.
_TRAILING_TOKEN = re.compile(r"\S+$")

# A line-leading enumerator ("6.", "7.") -- a list marker, not a sentence.
_ENUMERATOR = re.compile(r"^\d{1,3}\.$")


def _is_non_terminal(text: str, match: re.Match[str]) -> bool:
    """Whether a candidate boundary is a token period rather than a stop.

    Two shapes qualify, both observed truncating real abstracts at the
    token ceiling: an abbreviation whose period belongs to the word, and
    a bare enumerator opening a list item. Only a period can be
    non-terminal -- "!" and "?" carry no such ambiguity.

    The enumerator test requires line-leading position, so digits that
    merely end a sentence ("the native window is 262144.") stay
    terminal; a rule keyed to digits alone would truncate every sentence
    ending in a number.
    """
    if text[match.start()] != ".":
        return False

    token_match = _TRAILING_TOKEN.search(text, 0, match.start() + 1)
    if token_match is None:
        return False
    token = token_match.group()

    if token.casefold() in _NON_TERMINAL_ABBREVIATIONS:
        return True

    if _ENUMERATOR.match(token):
        line_start = text.rfind("\n", 0, token_match.start()) + 1
        return not text[line_start : token_match.start()].strip()

    return False


def trim_to_sentence_boundary(text: str) -> str:
    """Trim text to the last complete sentence boundary.

    Finds the last occurrence of sentence-ending punctuation (.!?)
    optionally followed by closing quotes/parens, then followed by
    whitespace or end-of-string. Truncates everything after that point.
    Candidates whose period belongs to a token rather than to a sentence
    -- an abbreviation, or a list item's enumerator -- are passed over,
    so a text cut off mid-phrase at one of those is trimmed back to the
    preceding real sentence instead of being accepted as complete.

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
    matches = [m for m in _SENTENCE_END.finditer(stripped) if not _is_non_terminal(stripped, m)]
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


# ---------------------------------------------------------------------------
# Structural-markup detection (CAS-ADR-020 clause (k))
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class StructureEcho:
    """One line of an abstract carrying document structure rather than prose."""

    kind: str
    line: str


# A markdown ATX heading. One is decisive: an abstract is prose, and prose has
# no headings, so this needs no run threshold.
_HEADING_LINE = re.compile(r"^\s{0,3}#{1,6}\s+\S")

# A line-leading list item, bulleted or enumerated.
_LIST_LINE = re.compile(r"^\s{0,3}(?:[-*+]|\d{1,3}[.)])\s+\S")

# A line consisting only of a bolded span, optionally followed by a colon --
# the shape a model produces when it reaches for subheads without using
# heading syntax.
_SUBHEAD_LINE = re.compile(r"^\s{0,3}\*\*[^*\n]+\*\*:?\s*$")

# List and subhead lines are reported only in runs. A single line-leading
# dash is an aside or a wrapped clause; a single bold span is emphasis. Two
# consecutive are a structure the model is laying out. Headings are exempt
# from the threshold for the reason given at _HEADING_LINE.
_RUN_THRESHOLD = 2


def find_structure_echo(abstract: str) -> list[StructureEcho]:
    """Find lines in an abstract that reproduce document structure.

    CAS-ADR-020 clause (j) requires the abstract to be continuous prose;
    clause (k) makes this check the enforcement, in a non-mutating posture.

    The check needs no source text. A claimed acronym expansion is a breach
    only relative to what the source says, so its check must read both; markup
    in an abstract is a breach on its face, because the abstract's required
    form is prose whatever the source contains.

    This is a proxy and clause (k) records it as one. It detects the shape the
    measured breaches took -- a model producing the structured document a
    source's trailing directive asked for -- and not the breach itself. An
    abstract that carries out a source's instruction in flowing prose is a
    breach of clause (i) and passes this check.

    Args:
        abstract: The generated semantic abstract to inspect.

    Returns:
        Findings in line order. Empty for prose.
    """
    findings: list[StructureEcho] = []
    lines = abstract.splitlines()

    pending: dict[str, list[str]] = {"list": [], "subhead": []}

    def _flush(kind: str) -> None:
        """Close an open run, reporting it as one finding if it qualifies.

        A run is one structural feature, so it yields one record naming the
        line it began at rather than one per line -- an eleven-item outline is
        a single breach, and reporting it eleven times would weight it eleven
        times in any rate measured over these records.
        """
        run = pending[kind]
        if len(run) >= _RUN_THRESHOLD:
            findings.append(StructureEcho(kind=kind, line=run[0]))
        pending[kind] = []

    for raw in lines:
        line = raw.rstrip()
        if _HEADING_LINE.match(line):
            _flush("list")
            _flush("subhead")
            findings.append(StructureEcho(kind="heading", line=line))
            continue

        # A subhead is also a plausible list line under some markup, so it is
        # classified first; the two runs are tracked separately so an
        # alternating sequence does not silently reset both.
        if _SUBHEAD_LINE.match(line):
            _flush("list")
            pending["subhead"].append(line)
            continue

        if _LIST_LINE.match(line):
            _flush("subhead")
            pending["list"].append(line)
            continue

        if line.strip():
            _flush("list")
            _flush("subhead")

    _flush("list")
    _flush("subhead")
    return findings
