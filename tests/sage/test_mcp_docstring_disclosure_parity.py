"""Disclosure parity between an MCP tool docstring and its OpenAPI operation.

Sibling gates compare *structure* across the MCP and HTTP surfaces:
``test_mcp_tool_conformance`` compares parameter shape, and
``test_openapi_conformance`` compares Pydantic field descriptions against
YAML schema descriptions. Neither reaches the prose that describes the
call itself, so a rule stated in a tool's docstring and not in its
operation description -- or the reverse -- is visible only to someone
reading both surfaces side by side. This module closes that gap.

Two complementary comparisons, both bidirectional.

**Claims** -- what the surfaces say -- are compared by coverage:

    For an enrolled pair, every claim sentence in the OpenAPI operation
    ``description`` must have a counterpart in the MCP tool docstring's
    prose body, and every claim sentence in that prose body must have a
    counterpart in the operation description.

A counterpart is a sentence whose best similarity ratio against the other
surface meets ``COVERAGE_FLOOR``. Four properties of that relation are
deliberate:

- **Not verbatim.** The two surfaces address different readers -- an
  agent choosing a tool, and a REST caller reading a contract -- and are
  written in different registers by design. Equality is the relation the
  sibling description-parity gate uses for Pydantic fields against YAML,
  and it works there only because both sides are one sentence written
  once. It is wrong here.
- **Order-insensitive.** The two surfaces order the same rules
  differently. Any positional or paragraph-aligned relation would report
  a pair that is genuinely in sync.
- **Prose body only.** A docstring's ``Args:``, ``Error modes:`` and
  worked-example blocks are excluded, because the spec carries that
  content structurally in ``parameters[*].description`` and
  ``responses[*].description`` rather than in the operation description.
  Those surfaces have their own gates.
- **A floor, not equality.** ``COVERAGE_FLOOR`` is the single tuned
  value in an otherwise-exact gate family. It is confined to one
  constant and defended where it is declared.

**Names** -- the identifiers the surfaces mention -- are compared
exactly, by ``test_enrolled_pairs_name_the_same_identifiers``. The two
checks answer different questions, and the second exists because the
first cannot: coverage measures how much of a claim's wording the other
surface shares, so dropping a single identifier from a nine-word rule
stays above any workable floor while the name check reports it. A
comparison over names alone would be no substitute either -- neither of
the two divergences this module was built for introduced a name.

**Polarity is the exception to the fraction.** A word that inverts a
claim rather than adding to it -- ``not``, ``never``, ``without`` and
their kin, listed in ``_POLARITY_WORDS`` -- is *required* rather than
counted, because one added negation in a seven-word rule clears any
workable floor and the opposite rule would otherwise read as disclosed.
It is required against the claim's **counterpart sentence** on the other
surface, not against that surface as a whole: the whole-surface form
reads stronger than it is, since the vocabulary is a set and one ``not``
anywhere satisfies every claim's polarity at once, leaving the check
live only against a surface that never negates at all.

Three things it still does not see, all by construction. **Word order**:
two claims naming the same participants in swapped roles reduce to one
bag. **Sub-floor fragments**: a negation inside a claim shorter than
``MIN_CLAIM_WORDS`` is dropped before comparison, so "do not appear
here" is never examined. **Double negation**: polarity is a set, so
removing one of two negations in a sentence leaves it unchanged. Each is
a place where a flip passes, and none is a defect awaiting a fix -- they
are the price of a bag-of-words relation, recorded so a reader does not
infer coverage the module never claimed.

One known cutoff: ``_STRUCTURAL_HEADER`` treats ``Note:`` as the end of
a narrative, so a docstring stating a rule under that heading has it
excluded from comparison. No mapped pair's narrative carries one today.

**What this gate does not check: whether a disclosure is true.** It
compares two disclosures to each other, so prose that is wrong the same
way on both surfaces passes clean. A response field named in both
narratives but absent from the response model is invisible here, as is a
field described with the wrong arity -- parity holds and only truth
fails. Prose against the response schema, and prose against the
implementing code, are different comparisons answering to a different
oracle; neither is in scope for this module, and a divergence of that
shape surviving here is the boundary working rather than the gate
failing.

Enrollment is a ratchet. A pair is either in ``ENROLLED_PAIRS``, where
the relation is enforced, or in ``UNENROLLED_PAIRS`` with a reason and
its measured divergence. ``test_unenrolled_pins_are_not_stale`` fails on
a pinned pair that has since become clean, so the pin set can only
shrink and enrollment is forced as soon as it is earned. The same
staleness rule governs ``SURFACE_ONLY_CLAIMS``. An exclusion list that
quietly grows is how a parity gate stops meaning anything; these cannot.
"""

from __future__ import annotations

import re
from typing import Final, NamedTuple

import pytest

from tests.sage.test_mcp_tool_conformance import (
    _SURFACES_BY_NAME,
    _all_registered_tools,
    _find_operation,
    _load_spec,
    _mapped_tool_pairs,
    _resolve_expected_operation_id,
    _resolve_ref,
)

# Fraction of a claim's content words the opposing surface must also
# use for the claim to count as disclosed there.
#
# Measured band, swept in 0.05 steps over the enrolled pairs: every
# enrolled pair is clean from 0.30 through 0.85, three go red at 0.90
# and all four at 0.95. So the upper edge is 0.85 and the value below
# sits one step under it. The lower edge is not visible in that sweep --
# the pairs stay clean all the way down, which is what a floor that is
# too permissive looks like -- so it is pinned by
# ``test_a_near_miss_claim_is_reported_at_the_floor_and_not_below``
# instead, with a claim measured at 0.75 coverage that must be reported
# here and covered a step lower. Move this constant and that test fails;
# that is the point of it.
COVERAGE_FLOOR: Final[float] = 0.80

# Sentence fragments shorter than this are dropped before comparison.
# Short fragments are list stems and clause tails rather than claims, and
# they match each other indiscriminately at any workable floor.
MIN_CLAIM_WORDS: Final[int] = 5

# Docstring section headers that end the prose body. Everything from the
# first of these onward is structural content the spec expresses in
# other nodes, which their own gates compare.
_STRUCTURAL_HEADER: Final[re.Pattern[str]] = re.compile(
    r"^[ \t]*(Args:|Arguments:|Error modes:|Returns:|Raises:|Note:|Notes:|"
    r"Worked example|Example[s]?:|Example$)",
    re.MULTILINE,
)

_BACKTICKED: Final[re.Pattern[str]] = re.compile(r"`{1,2}([^`]*)`{1,2}")
_SENTENCE_SPLIT: Final[re.Pattern[str]] = re.compile(r"(?<=[.;])\s+")
_IDENTIFIER: Final[re.Pattern[str]] = re.compile(r"\b[a-z][a-z0-9]*(?:_[a-z0-9]+)+\b")


def _prose_body(text: object) -> str:
    """Return the narrative part of a docstring or description.

    Truncates at the first structural section header. A description that
    carries no such header is returned whole, which is the ordinary case
    on the spec side.
    """
    if not isinstance(text, str):
        return ""
    match = _STRUCTURAL_HEADER.search(text)
    return text[: match.start()] if match else text


def _normalize(sentence: str) -> str:
    """Reduce a sentence to its comparable form.

    Collapses whitespace the way the sibling description-parity gate
    does -- which is what makes a YAML folded scalar comparable to an
    indented Python docstring -- then additionally strips the markup the
    two surfaces spell differently. The spec writes inline code in
    single markdown backticks and the docstrings write it in RST double
    backticks, so a token-preserving comparison has to remove both;
    emphasis, case and punctuation go for the same reason.
    """
    text = _BACKTICKED.sub(r"\1", sentence).replace("*", "")
    text = re.sub(r"\s+", " ", text).strip().lower()
    return re.sub(r"[^a-z0-9 ]", "", text).strip()


def _claim_sentences(text: object, prose_only: bool = True) -> list[str]:
    """Normalized claim sentences of a docstring or operation description."""
    source = _prose_body(text) if prose_only else (text if isinstance(text, str) else "")
    body = re.sub(r"\s+", " ", source).strip()
    if not body:
        return []
    sentences = (_normalize(part) for part in _SENTENCE_SPLIT.split(body))
    return [s for s in sentences if len(s.split()) >= MIN_CLAIM_WORDS]


# Words carried by almost every sentence on both surfaces. They say
# nothing about which claim a sentence makes, and leaving them in would
# let any two sentences cover one another.
#
# Words that carry a claim's meaning are deliberately NOT here, even
# where they look like function words. Three groups came out: negations
# and their kin (``not``, ``no``, ``never``, ``without``, ``unless``,
# ``nor``, ``neither``); scope words (``only``, ``all``, ``any``,
# ``every``, ``before``, ``after``, ``until``, ``ever``, ``still``,
# ``but``); and the deontic modals (``must``, ``can``, ``could``). They
# are the entire content of a claim's polarity or scope, so stripping
# them let a surface state the opposite rule and stay covered -- a rule
# and its negation reduced to the same bag -- and they are rare enough
# per sentence not to cause the indiscriminate matching this list exists
# to prevent.
#
# The epistemic modals (``may``, ``should``, ``would``) and the
# distributives that read as grammar rather than scope (``each``,
# ``both``, ``some``) stay: they qualify a claim without inverting or
# bounding it, and no enrolled pair's coverage turns on them.
_FUNCTION_WORDS: Final[frozenset[str]] = frozenset(
    """a an the and or so for yet of to in on at by from with
    is are was were be been being it its this that these those there here as
    if when where which who whom whose what how than then also
    each both either some one two do does did done has
    have had having may might shall should will would you your
    they them their we our us he she him her his hers rather into onto over
    under out up down off about against between through during
    above below same other another such own more most less least very just
    even always because while upon per via""".split()
)


# Words by which a surface names itself. The two surfaces necessarily
# refer to the same call by different nouns -- one is a tool in a
# catalog, the other an operation in a contract -- and that difference
# is not a difference in what they disclose.
_BOUNDARY_WORDS: Final[frozenset[str]] = frozenset(
    {"tool", "tools", "operation", "operations", "endpoint", "endpoints", "call", "calls"}
)


def _content_words(sentence: str) -> frozenset[str]:
    """The words of a claim that carry its meaning."""
    return frozenset(
        w for w in sentence.split() if w not in _FUNCTION_WORDS and w not in _BOUNDARY_WORDS
    )


# Words that invert a claim rather than adding to it. A fraction cannot
# police these: adding one ``not`` to a seven-word rule still clears any
# workable floor, so the opposite rule reads as disclosed. They are
# therefore *required* rather than counted -- a claim whose polarity word
# the other surface never uses is uncovered whatever else it shares.
_POLARITY_WORDS: Final[frozenset[str]] = frozenset(
    {"not", "no", "never", "without", "unless", "none", "cannot", "nor", "neither"}
)


def _coverage(claim: str, surface_words: frozenset[str]) -> float:
    """Fraction of a claim's content words the other surface also uses.

    Measured against the opposing surface as a whole rather than
    sentence by sentence. Two properties follow, and both are required
    by ground truth: a claim stays covered when the other surface makes
    it in a different position, and it stays covered when the other
    surface makes it at a different length. Elaboration is the ordinary
    difference between these two registers -- the tool boundary
    routinely expands a claim the contract states once -- and a relation
    that reported it would report most of the surface.
    """
    words = _content_words(claim)
    if not words:
        return 1.0
    return len(words & surface_words) / len(words)


# A claim must share at least this fraction of its content words with a
# sentence for that sentence to be its counterpart. Below it the two are
# talking about different things, and comparing their polarity would be
# comparing unrelated rules.
_COUNTERPART_FLOOR: Final[float] = 0.50


def _counterpart(claim: str, others: list[str]) -> str | None:
    """The sentence on the other surface that best matches this claim."""
    words = _content_words(claim)
    if not words:
        return None
    best, best_score = None, 0.0
    for other in others:
        other_words = _content_words(other)
        if not other_words:
            continue
        score = len(words & other_words) / len(words)
        if score > best_score:
            best, best_score = other, score
    return best if best_score >= _COUNTERPART_FLOOR else None


def _polarity_disagrees(claim: str, others: list[str], surface_words: frozenset[str]) -> bool:
    """Whether the other surface contradicts this claim's polarity.

    Checked against the claim's **counterpart sentence**, not against the
    opposing surface as a whole. Whole-surface was the first attempt and
    is far weaker than it reads: the vocabulary is a set, so one ``not``
    anywhere on the other side satisfies every claim's polarity
    requirement at once. A rule and its opposite then pass whenever the
    other surface negates something else -- which every enrolled
    contract does -- and the check fires only on a surface that never
    negates at all.

    A claim with no counterpart above ``_COUNTERPART_FLOOR`` falls back
    to the whole-surface test rather than being exempted: no mate means
    the claim is undisclosed on the coverage fraction anyway, and
    exempting it would be the one direction that can only lose signal.
    """
    polarity = _content_words(claim) & _POLARITY_WORDS
    if not polarity:
        return False
    mate = _counterpart(claim, others)
    if mate is None:
        return bool(polarity - surface_words)
    return bool(polarity - _content_words(mate))


def _identifiers(text: object) -> set[str]:
    """Snake_case names a surface's narrative uses.

    Matched by shape rather than by markup: the contract writes inline
    code in single markdown backticks and the docstrings in RST double
    ones, and neither marks every identifier it mentions.
    """
    return set(_IDENTIFIER.findall(_prose_body(text)))


def _surface_words(claims: list[str]) -> frozenset[str]:
    """Every content word a surface uses across all its claims."""
    return frozenset().union(*(_content_words(c) for c in claims)) if claims else frozenset()


def _uncovered(claims: list[str], others: list[str], floor: float = COVERAGE_FLOOR) -> list[str]:
    """Claims the other surface does not disclose at ``floor``."""
    vocabulary = _surface_words(others)
    return [
        c
        for c in claims
        if _polarity_disagrees(c, others, vocabulary) or _coverage(c, vocabulary) < floor
    ]


def disclosure_divergence(
    docstring: object,
    description: object,
    exempt: frozenset[str] = frozenset(),
    floor: float = COVERAGE_FLOOR,
    description_whole: object = None,
) -> tuple[list[str], list[str]]:
    """Compare two disclosure surfaces.

    Returns ``(spec_only, docstring_only)`` -- the claim sentences the
    operation description makes and the docstring does not, and the
    reverse. Both lists are empty when the surfaces disclose the same
    claims. Sentences in ``exempt`` are dropped from both results.

    Claims are extracted from the narrative surfaces, but coverage is
    measured against the whole of the opposing one. The asymmetry is
    deliberate, and answers two different questions. Which claims does
    this surface's prose make? Only its prose can say. Does the other
    surface disclose that claim *anywhere* a caller reads? Its argument
    and error blocks count too: a tool that states a rule under
    ``Args:`` has disclosed it, even though the contract states the same
    rule in its operation description. Without the widening, every such
    pair reports a divergence that is one of placement rather than
    disclosure -- two enrolled pairs do exactly that, verified.

    What each side widens to is stated rather than assumed, because the
    two are close but not mirrors. The docstring side widens to the
    whole docstring, ``Args:`` and ``Error modes:`` included. The
    contract side widens to ``parameters[*].description``,
    ``responses[*].description`` and the ``requestBody``'s own
    ``description`` (supplied as ``description_whole``), and no further:
    component-schema property prose is a much larger pool answering to
    its own gate.

    The residue is deliberate. For a tool whose arguments travel in a
    request body, an ``Args:`` entry describes a body *field*, whose
    contract mirror is schema-property prose that this pool excludes --
    so the docstring side can disclose something the contract side
    cannot be credited for. ``chain`` is the live instance: its
    "the result is symmetric" claim scores 0.455 against the docstring's
    prose and 1.000 against the whole docstring, disclosed only under an
    ``Args:`` entry. That is legitimate disclosure; the asymmetry it
    creates is the cost of leaving schema prose to the gate that owns
    it.

    This is the single entry point every test goes through, so a probe
    that plants a divergence in either input traverses the whole
    extraction and comparison path rather than a fragment of it.
    """
    doc_claims = _claim_sentences(docstring)
    spec_claims = _claim_sentences(description)
    doc_vocabulary = _claim_sentences(docstring, prose_only=False)
    spec_vocabulary = (
        _claim_sentences(description_whole, prose_only=False)
        if description_whole is not None
        else spec_claims
    )
    spec_only = [c for c in _uncovered(spec_claims, doc_vocabulary, floor) if c not in exempt]
    doc_only = [c for c in _uncovered(doc_claims, spec_vocabulary, floor) if c not in exempt]
    return spec_only, doc_only


def _described(spec: dict, node: object) -> str:
    """The description on one spec node, following a local ``$ref``.

    Resolution goes through the sibling gate's ``_resolve_ref``, which
    raises on a reference the spec does not define. The hand-rolled walk
    this replaced failed open -- a renamed component resolved to an empty
    node and silently shrank the vocabulary, so a docstring claim read as
    undisclosed for a cause no failure message ever named.
    """
    if not isinstance(node, dict):
        return ""
    if "$ref" in node:
        node = _resolve_ref(spec, str(node["$ref"]))
    description = node.get("description") if isinstance(node, dict) else None
    return description if isinstance(description, str) else ""


def _surfaces_for(surface_name: str, tool_name: str) -> tuple[str, str, str]:
    """The live surfaces of one mapped pair.

    Returns ``(docstring, spec_narrative, spec_whole)``. The docstring is
    read from the built server's ``Tool`` model rather than from the
    function, because that is the text a client actually receives in its
    catalog.

    ``spec_narrative`` is the operation's summary and description -- the
    prose a caller reads as the account of the call. ``spec_whole`` adds
    exactly the two surfaces a docstring's own structural blocks
    correspond to: ``parameters[*].description`` for its ``Args:`` block
    and ``responses[*].description`` for its ``Error modes:``. It stops
    there deliberately. Following ``$ref`` down into component-schema
    property prose would widen the pool far past that correspondence and
    weaken the tool-says-more direction, which is the direction most
    pairs diverge in; schema property descriptions have their own gate.
    """
    tool = _all_registered_tools()[tool_name]
    surface = _SURFACES_BY_NAME[surface_name]
    spec = _load_spec(surface.spec_path)
    op_id = _resolve_expected_operation_id(surface, tool_name)
    operation = _find_operation(spec, op_id)
    assert operation is not None, (
        f"{surface_name}.{tool_name} resolves to operationId {op_id!r}, which "
        "the spec does not define. The tool-to-operation mapping gate should "
        "have caught this first."
    )
    # ``summary`` joins ``description`` because a docstring opens with its
    # own summary line, and the spec carries that sentence in the
    # neighbouring field rather than in the description.
    narrative = f"{operation.get('summary') or ''}\n\n{operation.get('description') or ''}"
    structural = [_described(spec, node) for node in (operation.get("parameters") or [])]
    structural += [_described(spec, node) for node in (operation.get("responses") or {}).values()]
    if operation.get("requestBody"):
        structural.append(_described(spec, operation["requestBody"]))
    return (
        tool.description or "",
        narrative,
        narrative + "\n\n" + "\n\n".join(part for part in structural if part),
    )


def pair_divergence(
    surface_name: str, tool_name: str, exempt: frozenset[str] = frozenset()
) -> tuple[list[str], list[str]]:
    """Disclosure divergence for one live mapped pair."""
    docstring, narrative, whole = _surfaces_for(surface_name, tool_name)
    return disclosure_divergence(docstring, narrative, exempt=exempt, description_whole=whole)


# ---------------------------------------------------------------------------
# Enrollment
# ---------------------------------------------------------------------------

# Pairs held to the relation. Each was reconciled to reach this list;
# none arrived clean.
ENROLLED_PAIRS: Final[frozenset[tuple[str, str]]] = frozenset(
    {
        ("sage_core", "chain"),
        ("sage_core", "list_headings"),
        ("sage_core", "list_staging_edges"),
        ("sage_core", "verify_preconditions"),
    }
)


# Pairs not yet held to the relation, each with the divergence measured
# when it was pinned. The two surfaces were written as companions rather
# than copies -- the tool docstrings were the model for the operation
# descriptions and stayed free to say more -- so most pairs carry real
# divergence that predates any gate. Draining this list is a
# reconciliation campaign, not a precondition for gating the pairs that
# are ready.
#
# Nothing may be added here to silence a failure. An entry records
# divergence that already existed, and ``test_unenrolled_pins_are_not_stale``
# rejects it the moment the pair comes clean -- so the list can only
# shrink, and enrollment is forced rather than volunteered.
class Pin(NamedTuple):
    """The divergence a pinned pair carried when it was pinned.

    Counts rather than prose. The strings these replaced read as
    measurements but were asserted by nothing, so a pinned pair could
    drift further -- including in the exact direction this module
    exists to close, a contract gaining a rule its tool never gets --
    and stay green.

    Asserted as an **equality**, not a ceiling. A ceiling holds against
    the pin rather than against the pair: reconcile five of a pair's ten
    spec-only claims and nothing forces the pin down, so a later spec
    edit adding five rules the tool never gets restores the old count
    and stays green, hidden behind slack the earlier reconciliation
    created. Equality spends a little friction -- reconciling a pinned
    pair reddens until its pin is lowered in the same change -- to buy a
    ratchet that moves in one direction only.
    """

    spec_only: int
    doc_only: int


UNENROLLED_PAIRS: Final[dict[tuple[str, str], Pin]] = {
    ("sage_core", "create_edges"): Pin(6, 16),
    ("sage_core", "create_vault"): Pin(4, 17),
    ("sage_core", "delete_edge"): Pin(6, 3),
    ("sage_core", "get_document"): Pin(10, 2),
    ("sage_core", "get_filename_metadata"): Pin(7, 4),
    ("sage_core", "get_vault_config"): Pin(6, 3),
    ("sage_core", "get_vault_stats"): Pin(6, 1),
    ("sage_core", "ingest_document"): Pin(13, 16),
    ("sage_core", "list_pending_metadata"): Pin(7, 3),
    ("sage_core", "list_vaults"): Pin(4, 5),
    ("sage_core", "migrate_vault"): Pin(8, 10),
    ("sage_core", "optimize_vault_content_store"): Pin(2, 2),
    ("sage_core", "read_projection"): Pin(2, 10),
    ("sage_core", "read_section"): Pin(2, 1),
    ("sage_core", "recompute_abstract"): Pin(3, 21),
    ("sage_core", "recompute_deferred_vault_abstracts"): Pin(6, 8),
    ("sage_core", "recompute_views"): Pin(4, 10),
    ("sage_core", "restore_vault_source_file"): Pin(0, 10),
    ("sage_core", "search"): Pin(3, 12),
    ("sage_core", "traverse"): Pin(5, 1),
    ("sage_core", "update_lifecycles"): Pin(9, 7),
    ("sage_core", "update_metadata"): Pin(9, 18),
    ("sage_core", "update_vault_config"): Pin(7, 8),
    ("sage_core", "verify_hashes"): Pin(4, 5),
    ("sage_core", "verify_vault_drift"): Pin(5, 7),
    ("sage_core", "verify_vault_source_files"): Pin(3, 7),
    ("cas_app", "bulk_ingest_document"): Pin(9, 37),
    ("cas_app", "list_directory"): Pin(6, 9),
}

# Claims one surface may make alone, by pair. A contract states some
# rules against a transport the other boundary does not have, and a tool
# states some against a framework the contract does not see; neither is
# a disclosure gap. Entries are normalized claim text, and
# ``test_surface_only_claims_are_not_stale`` rejects one as soon as the
# other surface makes the claim too.
TERMINATION: Final[str] = (
    "the stream's termination contract, which the MCP tool has no transport to "
    "state: its contract is report-and-return, so there is no committed "
    "response for a mid-run failure to end, and no summary event whose absence "
    "could signal one"
)

SURFACE_ONLY_CLAIMS: Final[dict[tuple[str, str], dict[str, str]]] = {
    ("cas_app", "bulk_ingest_document"): {
        "a failure of the batch itself which can only surface after the 200 is "
        "committed ends the stream it emits no further events  there is no error "
        "event variant  and closes the connection without a summary": TERMINATION,
        "a stream that ends without one did not complete and the cause is logged "
        "serverside rather than sent": TERMINATION,
        "clients should treat the summary event not the end of the stream as the "
        "completion signal": TERMINATION,
    },
    ("sage_core", "recompute_deferred_vault_abstracts"): {
        "a failure of the run itself which can only surface after the 200 is "
        "committed ends the stream it emits no further events  there is no error "
        "event variant  and closes the connection without a summary": TERMINATION,
        "a stream that ends without one did not complete and the cause is logged "
        "serverside rather than sent": TERMINATION,
        "clients should treat the summary event not the end of the stream as the "
        "completion signal": TERMINATION,
    },
}


# Identifiers an enrolled pair may name on one surface only. Keyed by
# pair; the value is the justification, so an entry cannot be added
# without one.
SURFACE_ONLY_IDENTIFIERS: Final[dict[tuple[str, str], dict[str, str]]] = {
    ("sage_core", "verify_preconditions"): {
        "function_id": (
            "the HTTP path parameter's name, which the contract's narrative "
            "must explain because the name is misleading; the tool names the "
            "same argument in its Args block instead"
        ),
    },
}


def _exempt_for(surface_name: str, tool_name: str) -> frozenset[str]:
    """Claims pinned as legitimate on one surface only for this pair."""
    return frozenset(SURFACE_ONLY_CLAIMS.get((surface_name, tool_name), {}))


# ---------------------------------------------------------------------------
# Live-surface tests
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("surface_name", "tool_name"),
    sorted(ENROLLED_PAIRS),
    ids=[f"{s}-{t}" for s, t in sorted(ENROLLED_PAIRS)],
)
def test_enrolled_pairs_disclose_the_same_claims(surface_name: str, tool_name: str):
    """An enrolled pair states the same claims at both boundaries."""
    spec_only, doc_only = pair_divergence(
        surface_name, tool_name, exempt=_exempt_for(surface_name, tool_name)
    )
    assert not spec_only, (
        f"{tool_name}: the operation description states claim(s) the tool "
        f"docstring does not: {spec_only}. Add them to the docstring, or -- if "
        "the claim is about a transport the MCP boundary does not have -- pin "
        "it in SURFACE_ONLY_CLAIMS with a reason."
    )
    assert not doc_only, (
        f"{tool_name}: the tool docstring states claim(s) the operation "
        f"description does not: {doc_only}. Add them to the description, or "
        "pin them in SURFACE_ONLY_CLAIMS with a reason."
    )


@pytest.mark.parametrize(
    ("surface_name", "tool_name"),
    sorted(ENROLLED_PAIRS),
    ids=[f"{s}-{t}" for s, t in sorted(ENROLLED_PAIRS)],
)
def test_enrolled_pairs_name_the_same_identifiers(surface_name: str, tool_name: str):
    """An enrolled pair's narratives name the same identifiers.

    The claim comparison above measures how much of a claim's wording
    the other surface shares, so it tolerates a claim that survives
    almost intact -- dropping one identifier from a nine-word rule stays
    above the floor. That is the wrong tolerance for a *name*: a status
    value, error code or inference rule named on one surface and not the
    other is a disclosure gap however similar the surrounding prose. So
    names are compared exactly, and by shape rather than by markup,
    because the two surfaces do not agree on how to mark an identifier
    up.

    Scoped to the narratives. Argument and error blocks name identifiers
    the other surface carries in its parameter and response nodes, and
    comparing those would report the placement difference rather than a
    disclosure one.
    """
    docstring, narrative, _whole = _surfaces_for(surface_name, tool_name)
    exempt = set(SURFACE_ONLY_IDENTIFIERS.get((surface_name, tool_name), {}))
    doc_names = _identifiers(docstring)
    spec_names = _identifiers(narrative)

    spec_only = sorted(spec_names - doc_names - exempt)
    doc_only = sorted(doc_names - spec_names - exempt)
    assert not spec_only, (
        f"{tool_name}: the operation description names {spec_only}, which the "
        "tool docstring's narrative does not. Name them there too, or pin them "
        "in SURFACE_ONLY_IDENTIFIERS with a reason."
    )
    assert not doc_only, (
        f"{tool_name}: the tool docstring names {doc_only}, which the operation "
        "description does not."
    )


def test_surface_only_identifier_pins_are_not_stale():
    """An identifier pin cannot outlive the asymmetry it records."""
    for (surface_name, tool_name), pinned in SURFACE_ONLY_IDENTIFIERS.items():
        docstring, narrative, _whole = _surfaces_for(surface_name, tool_name)
        # A pin records an asymmetry, so it is live only while the name
        # sits on exactly one surface. Intersecting with the shared set
        # caught a name that had appeared on both and missed one that had
        # vanished from both -- which outlives the asymmetry just as much.
        asymmetric = _identifiers(docstring) ^ _identifiers(narrative)
        stale = sorted(set(pinned) - asymmetric)
        assert not stale, (
            f"{tool_name}: identifier(s) {stale} no longer sit on exactly one "
            "surface -- they are either named on both now, or gone from both. "
            "Either way the pin outlived the asymmetry it records; remove it."
        )


def test_every_mapped_pair_is_enrolled_or_pinned():
    """No pair escapes examination.

    A tool that maps to an operation is either held to the relation or
    carries a written reason why not. Without this a newly added tool
    would join the surface ungated and silently.
    """
    mapped = {(s, t) for s, t in _mapped_tool_pairs()}
    accounted = ENROLLED_PAIRS | set(UNENROLLED_PAIRS)
    assert mapped - accounted == set(), (
        f"pair(s) neither enrolled nor pinned: {sorted(mapped - accounted)}"
    )
    assert accounted - mapped == set(), (
        f"pair(s) named here that the surface no longer maps: {sorted(accounted - mapped)}"
    )


@pytest.mark.parametrize(
    ("surface_name", "tool_name"),
    sorted(UNENROLLED_PAIRS),
    ids=[f"{s}-{t}" for s, t in sorted(UNENROLLED_PAIRS)],
)
def test_unenrolled_pins_are_not_stale(surface_name: str, tool_name: str):
    """The ratchet: a pin cannot outlive the divergence it records."""
    spec_only, doc_only = pair_divergence(
        surface_name, tool_name, exempt=_exempt_for(surface_name, tool_name)
    )
    pin = UNENROLLED_PAIRS[(surface_name, tool_name)]

    assert spec_only or doc_only, (
        f"{tool_name} is pinned in UNENROLLED_PAIRS but its two surfaces now "
        "disclose the same claims. Delete the pin and add the pair to "
        "ENROLLED_PAIRS -- that is the only direction this list moves."
    )
    assert len(spec_only) == pin.spec_only, (
        f"{tool_name}: the contract states {len(spec_only)} claim(s) the tool "
        f"docstring does not, against {pin.spec_only} when it was pinned. "
        + (
            "Widening is the direction this module exists to close: reconcile "
            f"the pair, or fix whatever added the claim. New: {spec_only}"
            if len(spec_only) > pin.spec_only
            else "Fewer is progress -- lower the pin to the new count in this "
            "same change, so the next widening is measured against what the "
            "pair actually discloses rather than against its old slack."
        )
    )
    assert len(doc_only) == pin.doc_only, (
        f"{tool_name}: the tool docstring states {len(doc_only)} claim(s) the "
        f"contract does not, against {pin.doc_only} when it was pinned. "
        + (
            f"New: {doc_only}"
            if len(doc_only) > pin.doc_only
            else "Fewer is progress -- lower the pin to the new count in this same change."
        )
    )


def test_surface_only_claims_are_not_stale():
    """A one-surface pin cannot outlive the asymmetry it records."""
    for (surface_name, tool_name), claims in SURFACE_ONLY_CLAIMS.items():
        spec_only, doc_only = pair_divergence(surface_name, tool_name)
        still_divergent = set(spec_only) | set(doc_only)
        stale = set(claims) - still_divergent
        assert not stale, (
            f"{tool_name}: claim(s) pinned in SURFACE_ONLY_CLAIMS are now made "
            f"on both surfaces: {sorted(stale)}. Remove the pin."
        )


def test_enrolled_pairs_have_prose_on_both_surfaces():
    """Anti-vacuity: an enrolled pair must have something to compare.

    A pair whose docstring or description extracted to nothing would
    pass the enrolled-pair test for the worst reason -- an empty claim
    set is covered by anything.
    """
    for surface_name, tool_name in sorted(ENROLLED_PAIRS):
        docstring, narrative, _whole = _surfaces_for(surface_name, tool_name)
        assert _claim_sentences(docstring), f"{tool_name}: docstring yielded no claims"
        assert _claim_sentences(narrative), f"{tool_name}: description yielded no claims"


def test_enrollment_is_not_empty():
    """The gate cannot be switched off by emptying its enrolled set."""
    assert ENROLLED_PAIRS, "ENROLLED_PAIRS is empty; nothing is being gated"


# ---------------------------------------------------------------------------
# Regression fixtures
#
# The two rule paragraphs below are verbatim from the changes where the
# two surfaces of the source-file repair operation fell out of step --
# once in each direction. They sit on a shared preamble held identical
# on purpose, so each assertion attributes what the relation reports to
# the rule under test rather than to the ordinary register difference
# between a tool docstring and a contract. That difference is exercised
# against the live surfaces by the enrolled-pair test instead.
# ---------------------------------------------------------------------------

_SHARED_PREAMBLE: Final[str] = """
The repair counterpart of ``verify_vault_source_files``. That audit
reports a retained copy that changed outside SAGE but cannot fix one,
and re-ingesting cannot stand in: retention sees only that the offered
bytes differ from what sits at its target -- indistinguishable from a
name collision -- so it homes the document at a second path and leaves
the damaged copy in place. This writes to the path the document record
already names, so the document does not move.

Writes nothing when the retained copy already hashes to its recorded
digest, returning ``status: already_intact``.
"""

# A rule the tool docstring states and the operation description does
# not: the write is refused for a linked path, whatever its bytes hash
# to.
_LINK_RULE: Final[str] = """
A recorded path that is a *link* is never reported that way, however
the bytes behind it hash: it is not the copy the record names, and the
write is refused (``vault_source_path_refused``) rather than landing
wherever the link points. Remove the link and re-run.
"""

# The mirror: a rule the operation description states and the tool
# docstring does not, so the tool says less than the contract about the
# same call.
#
# It introduces no new quoted vocabulary -- it reaches for "the same
# refusal" rather than naming a code -- which is why a comparison over
# enumerated tokens stays green straight through this divergence and a
# comparison over claims does not.
_CONTAINMENT_RULE: Final[str] = """
Nor is a recorded path that resolves *outside* the vault's source
tree, for the same reason and with the same refusal: the store
will not write there, so the repair cannot land where the record
names. Re-point the path or reconfigure the vault, then re-run.
"""

_SENTINEL_CLAIM: Final[str] = (
    "The caller must first negotiate a quorum lease with the regional "
    "coordinator before any byte is delivered."
)


def test_prose_body_excludes_structural_blocks():
    """The prose body stops at the first structural section header.

    Claim extraction rests on this split. If it failed open, an
    ``Args:`` block would be read as narrative and compared against a
    contract that carries its arguments in ``parameters`` instead.
    """
    docstring = (
        "Do the thing to the record.\n\n"
        "The thing is idempotent across retries.\n\n"
        "Error modes:\n"
        "- ``not_found`` (404): no such record anywhere.\n\n"
        "Args:\n"
        "    record_id: Target record to operate on.\n"
    )
    body = _prose_body(docstring)
    assert "The thing is idempotent across retries." in body
    assert "not_found" not in body
    assert "record_id" not in body


def test_claim_sentences_normalize_both_backtick_spellings():
    """RST and markdown inline code reduce to the same claim.

    The two surfaces spell inline code differently. Without this, every
    sentence naming a field would read as two different claims and each
    pair would diverge for a reason that is purely typographic.
    """
    rst = "A path that is a ``link`` is refused outright by the store."
    markdown = "A path that is a `link` is refused outright by the store."
    assert _claim_sentences(rst) == _claim_sentences(markdown)
    assert _claim_sentences(rst), "the sentence must survive normalization"


def test_identifiers_are_found_by_shape_not_markup():
    """The identifier extractor finds names however they are marked up.

    Both identifier comparisons are set differences, so an extractor
    that found nothing would satisfy them by comparing two empty sets
    and pinning an empty intersection -- passing against the very drift
    they exist to catch. This is the assertion that cannot.
    """
    assert _identifiers("The ``filename_code_match`` rule applies here.") == {"filename_code_match"}
    assert _identifiers("The `filename_code_match` rule applies here.") == {"filename_code_match"}
    assert _identifiers("A rule with no snake case names at all.") == set()
    # Structural blocks are out of scope for the narrative comparison.
    assert _identifiers("Narrative here.\n\nArgs:\n    vault_id: Target.\n") == set()


def test_short_fragments_are_not_claims():
    """Fragments below the claim floor are dropped."""
    assert _claim_sentences("Deletes it.") == []


def test_reports_a_rule_the_docstring_states_and_the_spec_omits():
    """A rule disclosed only at the tool boundary is reported.

    Negative and positive control on one pair: the relation must report
    the link rule while the description omits it, and must fall silent
    once the description carries it. Without the second half, a relation
    that reported everything would pass.
    """
    docstring = _SHARED_PREAMBLE + _LINK_RULE

    _spec_only, doc_only = disclosure_divergence(docstring, _SHARED_PREAMBLE)
    assert doc_only, "the link rule is absent from the description and must be reported"
    assert any("link" in claim for claim in doc_only)

    _spec_only, doc_only = disclosure_divergence(docstring, _SHARED_PREAMBLE + _LINK_RULE)
    assert not doc_only, f"the relation must fall silent once both surfaces agree; got {doc_only}"


def test_reports_a_rule_the_spec_states_and_the_docstring_omits():
    """A rule disclosed only at the HTTP boundary is reported.

    The mirror of the case above. The assertion anchors on the claim,
    never on a quoted token -- this divergence introduces none.
    """
    docstring = _SHARED_PREAMBLE + _LINK_RULE
    description = _SHARED_PREAMBLE + _LINK_RULE + _CONTAINMENT_RULE

    spec_only, _doc_only = disclosure_divergence(docstring, description)
    assert spec_only, "the containment rule is absent from the docstring"
    assert any("outside the vaults source tree" in claim for claim in spec_only)

    spec_only, _doc_only = disclosure_divergence(docstring + _CONTAINMENT_RULE, description)
    assert not spec_only, f"the relation must fall silent once both surfaces agree; got {spec_only}"


def test_a_token_comparison_would_not_have_caught_the_containment_drift():
    """Why the comparable unit is the claim and not the vocabulary.

    Pins the reasoning that selected this relation, so a later
    simplification to set equality over quoted tokens fails here rather
    than shipping a gate that is green through the exact divergence it
    was built for.
    """
    quoted = _BACKTICKED
    before = set(quoted.findall(_SHARED_PREAMBLE + _LINK_RULE))
    after = set(quoted.findall(_SHARED_PREAMBLE + _LINK_RULE + _CONTAINMENT_RULE))
    assert before, (
        "the fixture must quote some vocabulary, or the equality below holds "
        "for the wrong reason and this control is inert"
    )
    assert before == after, (
        "the containment rule introduces no new quoted token, so a token-set "
        "comparison cannot see it"
    )


# Flips whose only content-word difference is the negation itself, so the
# coverage fraction still clears the floor and the polarity rule is the
# only thing that can report them. The pair with an unrelated negation on
# both surfaces is the shape the first implementation missed: it required
# a claim's polarity against the *whole* opposing surface, where one
# ``not`` anywhere satisfied every claim at once.
_POLARITY_FLIPS: Final[tuple[tuple[str, str, str], ...]] = (
    (
        "A retained copy is not rewritten when its digest already matches the record.",
        "A retained copy is always rewritten when its digest already matches the record.",
        "plain negation",
    ),
    (
        "Body content is not read by this call at any point.",
        "Body content is read by this call at any point.",
        "negation carrying the whole rule",
    ),
    (
        "A retained copy is not rewritten when its digest already matches the record.\n\n"
        "Nothing is written when the vault is not registered for retention.",
        "A retained copy is always rewritten when its digest already matches the record.\n\n"
        "Nothing is written when the vault is not registered for retention.",
        "negation present elsewhere on both surfaces",
    ),
)


@pytest.mark.parametrize(
    ("stated", "flipped", "shape"),
    _POLARITY_FLIPS,
    ids=[shape.replace(" ", "-") for _s, _f, shape in _POLARITY_FLIPS],
)
def test_a_rule_flipped_to_its_opposite_is_reported(stated: str, flipped: str, shape: str):
    """A surface stating the opposite rule is reported."""
    spec_only, doc_only = disclosure_divergence(stated, flipped)
    assert spec_only or doc_only, f"a rule and its opposite read as the same disclosure ({shape})"


@pytest.mark.parametrize(
    ("stated", "flipped", "shape"),
    _POLARITY_FLIPS,
    ids=[shape.replace(" ", "-") for _s, _f, shape in _POLARITY_FLIPS],
)
def test_each_polarity_flip_is_reported_by_the_polarity_rule_alone(
    stated: str, flipped: str, shape: str, monkeypatch: pytest.MonkeyPatch
):
    """Each flip above is caught by the polarity rule and nothing else.

    The control this replaces asserted only that the flips were reported,
    and two of its three fixtures were reported by the coverage fraction
    instead -- their negations became ordinary content words when the
    polarity list stopped treating them as function words, and the
    fraction fell below the floor on its own. So the control passed
    identically against the rule it was written to constrain and against
    no rule at all, and could not have told the whole-surface
    implementation from the per-counterpart one either.

    Disabling the rule and requiring silence is what makes each fixture
    a test of the rule rather than of the fraction.
    """
    monkeypatch.setattr(
        "tests.sage.test_mcp_docstring_disclosure_parity._POLARITY_WORDS", frozenset()
    )
    spec_only, doc_only = disclosure_divergence(stated, flipped)
    assert not spec_only and not doc_only, (
        f"the {shape!r} fixture is reported with the polarity rule disabled, so it "
        f"tests the coverage fraction rather than polarity: {spec_only or doc_only}"
    )


def test_polarity_is_checked_against_the_counterpart_not_the_whole_surface():
    """The rule survives an unrelated negation on the opposing surface.

    Pins the difference between the two implementations directly, so a
    reversion to whole-surface polarity fails here rather than silently
    weakening every enrolled pair.
    """
    stated, flipped, _shape = _POLARITY_FLIPS[2]
    claim = [c for c in _claim_sentences(stated) if "rewritten" in c][0]
    others = _claim_sentences(flipped)

    assert _polarity_disagrees(claim, others, _surface_words(others)), (
        "the flip must be reported even though the other surface negates elsewhere"
    )
    assert not (_content_words(claim) & _POLARITY_WORDS) - _surface_words(others), (
        "precondition: whole-surface polarity is satisfied here, which is exactly "
        "why the earlier implementation stayed silent"
    )


def test_a_near_miss_claim_is_reported_at_the_floor_and_not_below():
    """The floor is pinned from below by a claim measured against it.

    Every other control here is alien vocabulary or a polarity flip, and
    both are reported at any floor -- so nothing else would notice
    ``COVERAGE_FLOOR`` drifting to a value at which the gate is inert.
    This claim shares all but three of its content words with the
    preamble (0.75 coverage, measured), so it must be reported at
    ``COVERAGE_FLOOR`` and must not be one step below it. The assertions
    derive both floors from the constant, so moving the constant moves
    the test with it.
    """
    near_miss = (
        "The repair counterpart reports a retained copy that changed outside "
        "SAGE using a scheduled quarterly audit."
    )
    reported, _ = disclosure_divergence(
        _SHARED_PREAMBLE, _SHARED_PREAMBLE + near_miss, floor=COVERAGE_FLOOR
    )
    assert any("quarterly" in claim for claim in reported), (
        f"a claim at 0.75 coverage must be reported at floor {COVERAGE_FLOOR}"
    )

    covered, _ = disclosure_divergence(
        _SHARED_PREAMBLE, _SHARED_PREAMBLE + near_miss, floor=COVERAGE_FLOOR - 0.10
    )
    assert not any("quarterly" in claim for claim in covered), (
        f"the same claim must be covered at floor {COVERAGE_FLOOR - 0.10}; if it "
        "is still reported the floor is not what pins this"
    )


def test_an_injected_claim_is_reported():
    """Anti-vacuity control on the relation itself.

    The controls above assert the relation is quiet on surfaces that
    agree; they cannot tell agreement apart from a comparison that never
    runs. Planting an unrelated claim in one input proves the path is
    live at the configured floor.
    """
    agreed = _SHARED_PREAMBLE + _LINK_RULE
    spec_only, doc_only = disclosure_divergence(agreed, agreed)
    assert not spec_only and not doc_only, "identical surfaces must not diverge"

    spec_only, _doc_only = disclosure_divergence(agreed, agreed + _SENTINEL_CLAIM)
    assert any("quorum lease" in claim for claim in spec_only), (
        "an injected claim must be reported; the comparison is not running"
    )
