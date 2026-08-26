"""Utility functions for semantic abstract generation.

compute_max_tokens: density-proportional token budget based on document
word count and AbstractionConfig parameters.

trim_to_sentence_boundary: post-generation trimming to prevent
mid-sentence truncation when the LLM hits the token ceiling.

find_unattested_acronym_glosses: deterministic post-generation check
that acronym expansions claimed by an abstract are attested in the
source text (CAS-ADR-020 clause (e)).

collapse_unattested_acronym_glosses: the clause (e) repair -- replaces
each unattested gloss with its bare acronym (CAS-ADR-020 clause (h)).

find_structure_echo: deterministic post-generation check that an abstract
is continuous prose rather than reproduced document structure (CAS-ADR-020
clause (k)).

find_fabricated_cardinals: deterministic post-generation check that exact
counts an abstract asserts for source-derivable units agree with the source
(CAS-ADR-020 clause (e)); recording posture, no repair.
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

    NFKC-normalizes, unifies apostrophes and dashes, treats hyphens as
    spaces, casefolds, and collapses whitespace runs to single spaces.

    The hyphen-to-space step lives here rather than in
    ``_APOSTROPHE_DASH_MAP`` so the initials path, which shares that map,
    is provably untouched. Because the same transform applies to both the
    claimed expansion and the source, it is symmetric, and it can only
    grow the attested set: every pre-existing match survives the
    character map, so the set of flagged glosses can only shrink.
    """
    folded = (
        unicodedata.normalize("NFKC", text)
        .translate(_APOSTROPHE_DASH_MAP)
        .replace("-", " ")
        .casefold()
    )
    return " ".join(folded.split())


def _attestation_variants(expansion: str) -> tuple[str, ...]:
    """Normalized forms under which a claimed expansion counts as attested.

    The base normalization is always present, plus plural/singular
    toggles of the final word only. The measured false-positive class
    for the clause (e) check was glosses separated from an attested
    expansion by nothing but a final-word plural or a hyphen; tolerance
    is confined to that class. Interior-word toggles are deliberately
    excluded -- a plurality change mid-phrase names a different noun
    phrase, not a typographic variant. The length guards keep degenerate
    strips of short words from manufacturing matches.
    """
    base = _normalize(expansion)
    words = base.split()
    if not words:
        return (base,)
    last = words[-1]
    alternates = [last + "s", last + "es"]
    if last.endswith("es") and len(last) >= 5:
        alternates.append(last[:-2])
    if last.endswith("s") and not last.endswith("ss") and len(last) >= 4:
        alternates.append(last[:-1])
    variants = [base]
    for alternate in alternates:
        variant = " ".join([*words[:-1], alternate])
        if variant not in variants:
            variants.append(variant)
    return tuple(variants)


@dataclass(frozen=True)
class _GlossSite:
    """One occurrence of a claimed gloss, with its replaceable span.

    ``start``/``end`` bound the region a repair replaces: the full
    ``ACR (Expansion)`` match for the forward adjacency, or the expansion
    window plus the parenthetical for the reversed one. ``gloss.acronym``
    holds the acronym as it appeared in the text, so a repair that
    substitutes it preserves internal periods.
    """

    gloss: AcronymGloss
    start: int
    end: int


def _trailing_expansion(preceding: str, acronym: str) -> tuple[str, int] | None:
    """Find the shortest trailing word window whose initials fit the acronym.

    Grows the window backwards one word at a time so an attached leading
    article ("the Computer-Aided System") never inflates the claimed
    expansion.

    Returns:
        The candidate expansion with its words space-joined, and the
        offset of the window's first word in ``preceding``; None when no
        window within the lookback bound is consistent with the acronym.
    """
    target = _acronym_letters(acronym).casefold()
    tokens = list(re.finditer(r"\S+", preceding))
    window: list[str] = []
    for token in reversed(tokens[-_MAX_EXPANSION_WORDS:]):
        window.insert(0, token.group())
        candidate = " ".join(window)
        if _initials(candidate) == target:
            return candidate, token.start()
    return None


def _gloss_candidates(abstract: str) -> list[_GlossSite]:
    """Collect gloss sites claimed by the abstract.

    Covers both adjacency shapes: ``ACR (Expansion Words)`` and
    ``Expansion Words (ACR)``. Sites are not yet consistency- or
    attestation-checked. Forward sites precede reversed sites, each in
    text order.
    """
    sites: list[_GlossSite] = []
    for match in _FORWARD_GLOSS.finditer(abstract):
        sites.append(
            _GlossSite(
                gloss=AcronymGloss(acronym=match.group(1), expansion=match.group(2).strip()),
                start=match.start(),
                end=match.end(),
            )
        )
    for match in _REVERSED_GLOSS.finditer(abstract):
        acronym = match.group(1)
        found = _trailing_expansion(abstract[: match.start()], acronym)
        if found is not None:
            expansion, window_start = found
            sites.append(
                _GlossSite(
                    gloss=AcronymGloss(acronym=acronym, expansion=expansion),
                    start=window_start,
                    end=match.end(),
                )
            )
    return sites


def _unattested_sites(abstract: str, source_text: str) -> list[_GlossSite]:
    """Gloss sites whose claimed expansion the source does not attest.

    The shared core of detection and repair: both read the same sites
    through the same filters, so what one reports the other collapses,
    by construction rather than by parallel implementations.
    """
    normalized_source = _normalize(source_text)
    sites: list[_GlossSite] = []
    for site in _gloss_candidates(abstract):
        letters = _acronym_letters(site.gloss.acronym)
        if not 2 <= len(letters) <= 10:
            continue
        if _initials(site.gloss.expansion) != letters.casefold():
            continue
        if any(
            variant in normalized_source for variant in _attestation_variants(site.gloss.expansion)
        ):
            continue
        sites.append(site)
    return sites


def find_unattested_acronym_glosses(abstract: str, source_text: str) -> list[AcronymGloss]:
    """Find acronym glosses in an abstract that the source does not attest.

    A gloss is an acronym paired with a claimed expansion, in either
    adjacency: ``ACR (Expansion Words)`` or ``Expansion Words (ACR)``. A
    parenthetical whose initials are inconsistent with the acronym is an
    ordinary aside, not an expansion claim, and is never flagged. A
    consistent claim is attested when the expansion occurs in the source
    text under normalization (NFKC, casefold, apostrophe and dash
    unification, hyphens read as spaces, whitespace collapse), with
    plural/singular tolerance on the expansion's final word.

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
    findings: list[AcronymGloss] = []
    seen: set[AcronymGloss] = set()
    for site in _unattested_sites(abstract, source_text):
        if site.gloss not in seen:
            seen.add(site.gloss)
            findings.append(site.gloss)
    return findings


def collapse_unattested_acronym_glosses(abstract: str, source_text: str) -> str:
    """Collapse each unattested acronym gloss to its bare acronym.

    The repair posture CAS-ADR-020 clause (h) admits once the check's
    error rate is measured: an unattested claim is removed, the acronym
    the source actually uses stays. Every unattested site collapses,
    including repeats of the same pair; attested glosses and ordinary
    parenthetical asides are returned byte-identical. Idempotent, since
    a bare acronym makes no claim.

    No whitespace or punctuation cleanup is needed: every site starts
    and ends on a non-space character and the replacement is a non-empty
    token, so surrounding spacing and punctuation are untouched and no
    double space can arise.

    Args:
        abstract: The generated semantic abstract to repair.
        source_text: The full source text the abstract was generated from.

    Returns:
        The abstract with each unattested gloss replaced by its bare
        acronym; the input unchanged when there is nothing to repair.
    """
    sites = sorted(
        _unattested_sites(abstract, source_text), key=lambda site: (site.start, site.end)
    )
    pieces: list[str] = []
    cursor = 0
    for site in sites:
        if site.start < cursor:
            # Overlapping site: the earlier site already consumed this
            # region, and first-wins keeps the rebuild well-defined.
            continue
        pieces.append(abstract[cursor : site.start])
        pieces.append(site.gloss.acronym)
        cursor = site.end
    pieces.append(abstract[cursor:])
    return "".join(pieces)


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


# ---------------------------------------------------------------------------
# Fabricated-cardinal detection (CAS-ADR-020 clause (e))
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FabricatedCardinal:
    """An exact count an abstract asserts for a source-derivable unit.

    ``surface`` is the normalized text of the claim ("twenty six turns");
    ``value`` is the asserted count, ``unit`` the canonical singular unit
    name, ``derived`` the count mechanically derived from the source, and
    ``attested`` whether the asserted value occurs anywhere in the source
    as a standalone number in either digit or word form.
    """

    surface: str
    value: int
    unit: str
    derived: int
    attested: bool


# The hand-rolled number-word lexicon, scoped to 1-999. Deliberately
# excluded: approximations ("a hundred", "a dozen", "several"), which
# assert no exact value; "zero"/"no", which are negation prose; ordinal
# words, which name a position rather than a count; and thousand-and-up
# compounds, out of scope until measurement shows a need.
_UNITS_WORD_VALUES = {
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
}
_TEEN_VALUES = {
    "ten": 10,
    "eleven": 11,
    "twelve": 12,
    "thirteen": 13,
    "fourteen": 14,
    "fifteen": 15,
    "sixteen": 16,
    "seventeen": 17,
    "eighteen": 18,
    "nineteen": 19,
}
_TENS_VALUES = {
    "twenty": 20,
    "thirty": 30,
    "forty": 40,
    "fifty": 50,
    "sixty": 60,
    "seventy": 70,
    "eighty": 80,
    "ninety": 90,
}

# Tokens that may appear inside a number-word run. "and" is admitted as the
# connective of "one hundred and six" but carries no value of its own.
_NUMBER_RUN_WORDS = (
    frozenset(_UNITS_WORD_VALUES)
    | frozenset(_TEEN_VALUES)
    | frozenset(_TENS_VALUES)
    | {"hundred", "and"}
)

# A token asserting an exact digit count. Full-match only: "2026" is one
# number, never an attestation of 26; leading zeros and separators make no
# exact claim.
_EXACT_DIGITS = re.compile(r"[1-9]\d{0,3}")

# Words that mark the cardinal they precede as inexact -- a bound, an
# approximation, or one end of a range or disjunction. One gate covers all
# three shapes because they share the grammar "cue CARDINAL unit".
_NON_EXACT_CUES = frozenset(
    {
        "to",
        "or",
        "than",
        "least",
        "most",
        "about",
        "approximately",
        "roughly",
        "around",
        "nearly",
        "over",
        "under",
        "almost",
        "some",
    }
)

# Punctuation stripped from token edges after normalization, so "turns."
# and "(26)" compare as bare tokens.
_EDGE_PUNCTUATION = "'\"().,;:!?[]"


@dataclass(frozen=True)
class _DerivableUnit:
    """A unit whose count is mechanically derivable from source text.

    ``marker`` matches one occurrence of the unit's structural signature in
    the raw (un-normalized) source; the derived count is the number of
    matches. A source with zero matches does not exhibit the structure, so
    the unit is treated as unregistered for that document -- prose that
    happens to use the unit noun in another sense can then never be
    flagged.
    """

    canonical: str
    singular: str
    plural: str
    marker: re.Pattern[str]


# A conversation-transcript turn heading: an ATX heading whose text starts
# with "Turn <number>", any suffix allowed ("### Turn 12 -- Speaker").
_TURN_HEADING = re.compile(r"^\s{0,3}#{1,6}\s+turn\s+\d+\b", re.IGNORECASE | re.MULTILINE)

# The unit registry. Growing it is a data change, not a code change.
_DERIVABLE_UNITS = (
    _DerivableUnit(canonical="turn", singular="turn", plural="turns", marker=_TURN_HEADING),
)

_UNIT_SURFACE_FORMS = {
    form: unit for unit in _DERIVABLE_UNITS for form in (unit.singular, unit.plural)
}


def _cardinal_tokens(text: str) -> list[str]:
    """Tokenize normalized text for cardinal-claim scanning."""
    stripped = (token.strip(_EDGE_PUNCTUATION) for token in text.split())
    return [token for token in stripped if token]


def _parse_number_words(words: list[str]) -> int | None:
    """Parse a whole number-word run, or None when it is not one number.

    Grammar: ``[units "hundred" ["and"]] [tens [units] | teens | units]``,
    range 1-999. The run must parse in full -- "six twenty" is two numbers,
    not one, and returns None so the caller can fall back to per-token
    values.
    """
    if not words:
        return None
    index = 0
    value = 0
    if len(words) >= 2 and words[0] in _UNITS_WORD_VALUES and words[1] == "hundred":
        value = _UNITS_WORD_VALUES[words[0]] * 100
        index = 2
        if index < len(words) and words[index] == "and":
            index += 1
        if index == len(words):
            return value
    remainder = words[index:]
    if "hundred" in remainder or "and" in remainder:
        return None
    if remainder[0] in _TENS_VALUES:
        value += _TENS_VALUES[remainder[0]]
        if len(remainder) == 1:
            return value
        if len(remainder) == 2 and remainder[1] in _UNITS_WORD_VALUES:
            return value + _UNITS_WORD_VALUES[remainder[1]]
        return None
    if len(remainder) == 1 and remainder[0] in _TEEN_VALUES:
        return value + _TEEN_VALUES[remainder[0]]
    if len(remainder) == 1 and remainder[0] in _UNITS_WORD_VALUES:
        return value + _UNITS_WORD_VALUES[remainder[0]]
    return None


def _word_claim_before(tokens: list[str], unit_index: int) -> tuple[int, int] | None:
    """Parse the number-word claim ending just before a unit token.

    Takes the maximal run of number-run words immediately preceding the
    unit and parses its longest valid suffix, so a stray number word ahead
    of a well-formed compound ("3 4" from a normalized range, or prose
    debris) does not silently swallow the claim -- the unparsed prefix is
    left for the preceding-token guard to judge.

    Returns:
        The parsed value and the claim's starting token index, or None
        when no suffix of the run parses.
    """
    run_start = unit_index
    while run_start > 0 and tokens[run_start - 1] in _NUMBER_RUN_WORDS:
        run_start -= 1
    for begin in range(run_start, unit_index):
        value = _parse_number_words(tokens[begin:unit_index])
        if value is not None:
            return value, begin
    return None


def _attested_values(source_text: str) -> set[int]:
    """Every exact number the source states, in digit or word form.

    Token-level over the normalized source: a full-match digit token
    contributes its value ("2026" contributes 2026, never 26); a maximal
    number-word run contributes its whole-run parse when it parses, and
    the values of its individually parseable words when it does not
    ("sixty four" attests 64; a malformed "six twenty" attests 6 and 20).
    """
    tokens = _cardinal_tokens(_normalize(source_text))
    values: set[int] = set()
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if _EXACT_DIGITS.fullmatch(token):
            values.add(int(token))
            index += 1
            continue
        if token in _NUMBER_RUN_WORDS:
            run_end = index
            while run_end < len(tokens) and tokens[run_end] in _NUMBER_RUN_WORDS:
                run_end += 1
            run = tokens[index:run_end]
            whole = _parse_number_words(run)
            if whole is not None:
                values.add(whole)
            else:
                for word in run:
                    for lexicon in (_UNITS_WORD_VALUES, _TEEN_VALUES, _TENS_VALUES):
                        if word in lexicon:
                            values.add(lexicon[word])
            index = run_end
            continue
        index += 1
    return values


def _cardinal_claims(tokens: list[str]) -> list[tuple[int, _DerivableUnit, str]]:
    """Exact cardinal-count claims in a normalized token stream.

    A claim is a cardinal (digit token or number-word run) immediately
    followed by a registered unit's surface form, surviving two gates:
    number agreement between the value and the unit form (killing the verb
    reading of "one turns to..."), and a preceding-token guard that rejects
    ranges, disjunctions, and hedges ("3 4", "three to four", "about
    sixty") by refusing any claim preceded by another cardinal or an
    inexactness cue.
    """
    claims: list[tuple[int, _DerivableUnit, str]] = []
    for unit_index, token in enumerate(tokens):
        unit = _UNIT_SURFACE_FORMS.get(token)
        if unit is None or unit_index == 0:
            continue
        previous = tokens[unit_index - 1]
        if _EXACT_DIGITS.fullmatch(previous):
            value, begin = int(previous), unit_index - 1
        else:
            found = _word_claim_before(tokens, unit_index)
            if found is None:
                continue
            value, begin = found
        if (value == 1) != (token == unit.singular):
            continue
        if begin > 0:
            before = tokens[begin - 1]
            if (
                before in _NUMBER_RUN_WORDS
                or before in _NON_EXACT_CUES
                or _EXACT_DIGITS.fullmatch(before)
            ):
                continue
        claims.append((value, unit, " ".join(tokens[begin : unit_index + 1])))
    return claims


def find_fabricated_cardinals(abstract: str, source_text: str) -> list[FabricatedCardinal]:
    """Find exact counts of source-derivable units the source does not license.

    A claim is flagged when its value disagrees with the count derived from
    the source's structure, or when the source never states the value as a
    standalone number in digit or word form. Only units in the derivable
    registry participate, and only for sources that exhibit the unit's
    structure; counts of anything else are the open-ended remainder of
    CAS-ADR-020 clause (e) and stay with the out-of-band behavioral
    evaluation.

    This check runs in the recording posture CAS-ADR-020 requires of a
    finding class with no adjudicated error-rate measurement: the caller
    records findings and stores the abstract unmodified. A finding is
    calibration data, not a verdict -- in particular, ``attested`` is a
    weak signal for exactly the documents this check targets, because a
    contiguously numbered transcript's heading numerals attest every value
    up to the turn count; the disagreement arm carries the check.

    Args:
        abstract: The generated semantic abstract to inspect.
        source_text: The full source text the abstract was generated from.

    Returns:
        Findings in order of first appearance, deduplicated on
        (value, unit). Empty when every claim agrees and is attested, when
        no claim exists, or when the source exhibits no derivable
        structure.
    """
    derived_counts = {
        unit.canonical: count
        for unit in _DERIVABLE_UNITS
        if (count := len(unit.marker.findall(source_text))) >= 1
    }
    if not derived_counts:
        return []
    attested: set[int] | None = None
    findings: list[FabricatedCardinal] = []
    seen: set[tuple[int, str]] = set()
    for value, unit, surface in _cardinal_claims(_cardinal_tokens(_normalize(abstract))):
        derived = derived_counts.get(unit.canonical)
        if derived is None:
            continue
        if attested is None:
            attested = _attested_values(source_text)
        is_attested = value in attested
        if value == derived and is_attested:
            continue
        key = (value, unit.canonical)
        if key in seen:
            continue
        seen.add(key)
        findings.append(
            FabricatedCardinal(
                surface=surface,
                value=value,
                unit=unit.canonical,
                derived=derived,
                attested=is_attested,
            )
        )
    return findings
