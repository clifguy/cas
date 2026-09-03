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
stays above any workable floor. A probe that deleted one inference rule
from a docstring passed the claim check and failed the name check,
which is the division of labour intended. A comparison over names alone
would be no substitute either -- neither of the two divergences this
module was built for introduced a name.

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
from typing import Final

import pytest

from tests.sage.test_mcp_tool_conformance import (
    _SURFACES_BY_NAME,
    _all_registered_tools,
    _find_operation,
    _load_spec,
    _mapped_tool_pairs,
    _resolve_expected_operation_id,
)

# Fraction of a claim's content words the opposing surface must also
# use for the claim to count as disclosed there. Calibrated against
# every mapped pair; see the module docstring.
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
_FUNCTION_WORDS: Final[frozenset[str]] = frozenset(
    """a an the and or but nor so for yet of to in on at by from with without
    is are was were be been being it its this that these those there here as
    if when where which who whom whose what how than then only also not no
    any each every both either neither all some one two do does did done has
    have had having can could may might must shall should will would you your
    they them their we our us he she him her his hers rather into onto over
    under out up down off about against between through during before after
    above below same other another such own more most less least very just
    still even ever never always because while until unless upon per via""".split()
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
    return [c for c in claims if _coverage(c, vocabulary) < floor]


def disclosure_divergence(
    docstring: object,
    description: object,
    exempt: frozenset[str] = frozenset(),
    floor: float = COVERAGE_FLOOR,
    docstring_whole: object = None,
    description_whole: object = None,
) -> tuple[list[str], list[str]]:
    """Compare two disclosure surfaces.

    Returns ``(spec_only, docstring_only)`` -- the claim sentences the
    operation description makes and the docstring does not, and the
    reverse. Both lists are empty when the surfaces disclose the same
    claims. Sentences in ``exempt`` are dropped from both results.

    Claims are extracted from the narrative surfaces, but coverage is
    measured against the whole of the opposing one -- pass
    ``docstring_whole`` / ``description_whole`` to widen it. The
    asymmetry is deliberate, and answers two different questions. Which
    claims does this surface's prose make? Only its prose can say. Does
    the other surface disclose that claim *anywhere* a caller reads? Its
    argument and error blocks count too: a tool that states a rule under
    ``Args:`` has disclosed it, even though the contract states the same
    rule in its operation description. Without the widening, every such
    pair reports a divergence that is one of placement rather than
    disclosure.

    This is the single entry point every test goes through, so a probe
    that plants a divergence in either input traverses the whole
    extraction and comparison path rather than a fragment of it.
    """
    doc_claims = _claim_sentences(docstring)
    spec_claims = _claim_sentences(description)
    doc_vocabulary = (
        _claim_sentences(docstring_whole, prose_only=False)
        if docstring_whole is not None
        else doc_claims
    )
    spec_vocabulary = (
        _claim_sentences(description_whole, prose_only=False)
        if description_whole is not None
        else spec_claims
    )
    spec_only = [c for c in _uncovered(spec_claims, doc_vocabulary, floor) if c not in exempt]
    doc_only = [c for c in _uncovered(doc_claims, spec_vocabulary, floor) if c not in exempt]
    return spec_only, doc_only


def _surfaces_for(surface_name: str, tool_name: str) -> tuple[str, str, str]:
    """The live surfaces of one mapped pair.

    Returns ``(docstring, spec_narrative, spec_whole)``. The docstring is
    read from the built server's ``Tool`` model rather than from the
    function, because that is the text a client actually receives in its
    catalog.

    ``spec_narrative`` is the operation's summary and description -- the
    prose a caller reads as the account of the call. ``spec_whole`` adds
    the parameter and response descriptions, so that a rule the contract
    states against one argument or one status still counts as disclosed
    when the docstring states it in its narrative.
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
    narrative = f"{operation.get('summary') or ''}\n\n{operation.get('description') or ''}"
    extra = [
        _describe_node(spec, node)
        for node in list(operation.get("parameters") or [])
        + list((operation.get("responses") or {}).values())
        + ([operation["requestBody"]] if operation.get("requestBody") else [])
    ]
    return tool.description or "", narrative, narrative + "\n\n" + "\n\n".join(extra)


def _describe_node(spec: dict, node: object, depth: int = 0) -> str:
    """Every description string reachable from a spec node."""
    if depth > 6 or not isinstance(node, (dict, list)):
        return ""
    if isinstance(node, list):
        return " ".join(_describe_node(spec, n, depth + 1) for n in node)
    if "$ref" in node:
        target: object = spec
        for segment in str(node["$ref"]).lstrip("#/").split("/"):
            target = target.get(segment, {}) if isinstance(target, dict) else {}
        return _describe_node(spec, target, depth + 1)
    parts = [str(node["description"])] if isinstance(node.get("description"), str) else []
    parts += [_describe_node(spec, v, depth + 1) for k, v in node.items() if k != "description"]
    return " ".join(p for p in parts if p)


def pair_divergence(
    surface_name: str, tool_name: str, exempt: frozenset[str] = frozenset()
) -> tuple[list[str], list[str]]:
    """Disclosure divergence for one live mapped pair."""
    docstring, narrative, whole = _surfaces_for(surface_name, tool_name)
    return disclosure_divergence(
        docstring,
        narrative,
        exempt=exempt,
        docstring_whole=docstring,
        description_whole=whole,
    )


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
UNENROLLED_PAIRS: Final[dict[tuple[str, str], str]] = {
    ("sage_core", "create_edges"): (
        "6 claim(s) stated only in the contract and 15 only in the tool docstring."
    ),
    ("sage_core", "create_vault"): (
        "4 claim(s) stated only in the contract and 15 only in the tool docstring."
    ),
    ("sage_core", "delete_edge"): (
        "6 claim(s) stated only in the contract and 3 only in the tool docstring."
    ),
    ("sage_core", "get_document"): (
        "10 claim(s) stated only in the contract and 2 only in the tool docstring."
    ),
    ("sage_core", "get_filename_metadata"): (
        "7 claim(s) stated only in the contract and 4 only in the tool docstring."
    ),
    ("sage_core", "get_vault_config"): (
        "6 claim(s) stated only in the contract and 3 only in the tool docstring."
    ),
    ("sage_core", "get_vault_stats"): (
        "6 claim(s) stated only in the contract and 1 only in the tool docstring."
    ),
    ("sage_core", "ingest_document"): (
        "12 claim(s) stated only in the contract and 13 only in the tool docstring."
    ),
    ("sage_core", "list_pending_metadata"): (
        "7 claim(s) stated only in the contract and 3 only in the tool docstring."
    ),
    ("sage_core", "list_vaults"): (
        "4 claim(s) stated only in the contract and 5 only in the tool docstring."
    ),
    ("sage_core", "migrate_vault"): (
        "8 claim(s) stated only in the contract and 10 only in the tool docstring."
    ),
    ("sage_core", "optimize_vault_content_store"): (
        "2 claim(s) stated only in the contract and 2 only in the tool docstring."
    ),
    ("sage_core", "read_projection"): (
        "2 claim(s) stated only in the contract and 10 only in the tool docstring."
    ),
    ("sage_core", "read_section"): (
        "2 claim(s) stated only in the contract and 1 only in the tool docstring."
    ),
    ("sage_core", "recompute_abstract"): (
        "4 claim(s) stated only in the contract and 20 only in the tool docstring."
    ),
    ("sage_core", "recompute_deferred_vault_abstracts"): (
        "6 claim(s) stated only in the contract and 8 only in the tool docstring."
    ),
    ("sage_core", "recompute_views"): (
        "4 claim(s) stated only in the contract and 10 only in the tool docstring."
    ),
    ("sage_core", "restore_vault_source_file"): (
        "0 claim(s) stated only in the contract and 6 only in the tool docstring."
    ),
    ("sage_core", "search"): (
        "3 claim(s) stated only in the contract and 8 only in the tool docstring."
    ),
    ("sage_core", "traverse"): (
        "5 claim(s) stated only in the contract and 1 only in the tool docstring."
    ),
    ("sage_core", "update_lifecycles"): (
        "9 claim(s) stated only in the contract and 6 only in the tool docstring."
    ),
    ("sage_core", "update_metadata"): (
        "9 claim(s) stated only in the contract and 16 only in the tool docstring."
    ),
    ("sage_core", "update_vault_config"): (
        "7 claim(s) stated only in the contract and 9 only in the tool docstring."
    ),
    ("sage_core", "verify_hashes"): (
        "3 claim(s) stated only in the contract and 4 only in the tool docstring."
    ),
    ("sage_core", "verify_vault_drift"): (
        "4 claim(s) stated only in the contract and 6 only in the tool docstring."
    ),
    ("sage_core", "verify_vault_source_files"): (
        "3 claim(s) stated only in the contract and 6 only in the tool docstring."
    ),
    ("cas_app", "bulk_ingest_document"): (
        "9 claim(s) stated only in the contract and 36 only in the tool docstring."
    ),
    ("cas_app", "list_directory"): (
        "6 claim(s) stated only in the contract and 9 only in the tool docstring."
    ),
}

# Claims one surface may make alone, by pair. A contract states some
# rules against a transport the other boundary does not have, and a tool
# states some against a framework the contract does not see; neither is
# a disclosure gap. Entries are normalized claim text, and
# ``test_surface_only_claims_are_not_stale`` rejects one as soon as the
# other surface makes the claim too.
SURFACE_ONLY_CLAIMS: Final[dict[tuple[str, str], frozenset[str]]] = {}


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
    return SURFACE_ONLY_CLAIMS.get((surface_name, tool_name), frozenset())


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
        shared = _identifiers(docstring) & _identifiers(narrative)
        stale = sorted(set(pinned) & shared)
        assert not stale, (
            f"{tool_name}: identifier(s) {stale} are now named on both surfaces. Remove the pin."
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
    assert spec_only or doc_only, (
        f"{tool_name} is pinned in UNENROLLED_PAIRS but its two surfaces now "
        "disclose the same claims. Delete the pin and add the pair to "
        "ENROLLED_PAIRS -- that is the only direction this list moves."
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

# Stated first in the tool docstring; the operation description caught
# up in the same change only because the author noticed.
_LINK_RULE: Final[str] = """
A recorded path that is a *link* is never reported that way, however
the bytes behind it hash: it is not the copy the record names, and the
write is refused (``vault_source_path_refused``) rather than landing
wherever the link points. Remove the link and re-run.
"""

# Stated first in the operation description and not in the docstring,
# leaving the tool saying less than the contract about the same call.
# Caught by a person reading both, and fixed separately.
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

    The mirror of the case above, and the one that actually shipped.
    The assertion anchors on the claim, never on a quoted token -- that
    divergence introduced none.
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
    quoted = re.compile(r"`{1,2}([^`]*)`{1,2}")
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
