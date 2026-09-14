"""A passage's structure relative to its document (CAS-ADR-049 Decision 3).

The decision separates two roles one field was serving. The *address* is the
heading path as the source produced it -- what enumeration returns and what a
section read accepts. The *indexed structure* is that path relative to the
document: the same path with a root element removed when, and only when, that
element equals the document title, because the title is document-level and the
document surface carries it.

These are the rule alone, with no store in the picture. The rule is deliberately
conservative, and the tests below pin that conservatism rather than merely
exercising the happy path: the two errors available here are not symmetric. A
root left unstripped costs one document today's behaviour. A root stripped that
was not the title permanently removes real structural text from weight A, and
Decision 3 explicitly preserves that weight for headings within the document.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

import sage
from sage.adapters.interfaces import HEADING_PATH_SEPARATOR
from sage.services.passage_structure import indexed_structure

# ---------------------------------------------------------------------------
# The rule
# ---------------------------------------------------------------------------


def test_a_root_equal_to_the_title_is_removed():
    """The case the decision is about: a source whose title is its top heading."""
    assert indexed_structure("Alpha > Beta > Gamma", "Alpha") == "Beta > Gamma"


def test_a_root_unequal_to_the_title_is_kept_whole():
    """The shape a word processor or a spreadsheet produces.

    Anti-coincidental-pass: this is the control on the test above. A function
    that unconditionally returned everything after the first separator would
    pass that one and fail this one -- the same path is used, and only the
    title differs.
    """
    assert indexed_structure("Alpha > Beta > Gamma", "Different") == "Alpha > Beta > Gamma"


def test_a_path_that_is_exactly_the_title_becomes_empty():
    """The top-level heading's own passage.

    An empty result is legitimate and common -- 1,169 rows in the cas vault --
    which is why the stored column is nullable with no default and ``NULL``,
    not ``''``, is what "not yet derived" means.

    The title is not thereby erased from that passage: the chunker prepends the
    ATX heading line to the content, so the title still carries at weight D.
    Decision 3 demotes it; it does not remove it.
    """
    assert indexed_structure("Alpha", "Alpha") == ""


def test_only_the_root_segment_is_considered():
    """A title recurring deeper in the path is a heading, and keeps its weight.

    Anti-coincidental-pass: this is the only test in the module that fails a
    split-and-filter implementation -- ``join(s for s in segments if s !=
    title)`` -- which passes every other assertion here and returns ``"Beta"``,
    silently discarding a real heading. Verified by mutation.
    """
    assert indexed_structure("Alpha > Beta > Alpha", "Alpha") == "Beta > Alpha"


def test_a_root_that_merely_starts_with_the_title_is_kept():
    """Containment is not identity.

    Anti-coincidental-pass: kills a ``startswith`` implementation, which would
    strip ``Introduction`` because the title is ``Intro``.
    """
    assert indexed_structure("Introduction > A", "Intro") == "Introduction > A"


def test_a_case_variant_root_is_not_stripped():
    """Exact equality, pinned as a decision rather than left as an accident.

    Two headings differing in case are two different headings; the address is
    what the source produced. Measured on the cas vault, *zero* passages match
    their title case-insensitively but not exactly, so folding case would add
    false-positive surface and reach nothing. Loosening this later should be a
    deliberate edit against a red test.
    """
    assert indexed_structure("alpha > Beta", "Alpha") == "alpha > Beta"


def test_surrounding_whitespace_is_ignored():
    """The one widening the rule permits.

    The markdown adapter already strips heading text, so a leading or trailing
    space difference is an artifact of the pipeline rather than authorial
    intent, and it cannot change what the text means to the index. Pinned so it
    is not later removed as over-normalization.
    """
    assert indexed_structure("  Alpha   > Beta", " Alpha ") == "Beta"


# ---------------------------------------------------------------------------
# Degenerate inputs
# ---------------------------------------------------------------------------


def test_an_empty_path_yields_an_empty_structure():
    """The no-headings fallback, which the chunker emits with an empty path."""
    assert indexed_structure("", "Alpha") == ""


@pytest.mark.parametrize("title", [None, "", "   "])
def test_an_absent_title_never_strips(title):
    """No title means no root to recognise, so the path is returned whole.

    Anti-coincidental-pass: an implementation comparing ``root == (title or "")``
    strips an empty root off an empty path and appears to work. Demanding the
    *path back whole* under an empty title is what that implementation fails.
    """
    assert indexed_structure("Alpha > Beta", title) == "Alpha > Beta"


def test_a_separator_inside_the_remainder_is_left_alone():
    """Only the first separator is a boundary; the rest of the path is opaque.

    The adapters join without escaping, so a heading whose own text contains the
    separator is already ambiguous at every consumer that splits on it. This
    rule inherits that ambiguity and does not deepen it.
    """
    assert indexed_structure("Alpha > Beta > Gamma", "Alpha") == "Beta > Gamma"


def test_a_root_with_no_separator_and_no_match_is_kept():
    """A single-segment path that is not the title -- a sheet name, a slide."""
    assert indexed_structure("Sheet1", "Q3 Review") == "Sheet1"


# ---------------------------------------------------------------------------
# The separator is shared, not restated
# ---------------------------------------------------------------------------


def test_the_rule_splits_on_the_separator_the_adapters_join_with():
    """One constant, so the rule cannot drift from the paths it reads.

    Stated as behaviour rather than as a source scan: the rule must recognise a
    root delimited by ``HEADING_PATH_SEPARATOR`` itself, so a change to the
    constant that the rule did not follow shows up here.
    """
    path = f"Alpha{HEADING_PATH_SEPARATOR}Beta"
    assert indexed_structure(path, "Alpha") == "Beta"


def test_both_writers_call_the_one_rule():
    """Neither ingest nor the migration derives the structure itself.

    CAS-ADR-049's consequence list requires the two writers to apply one rule,
    "or a re-ingested document and a migrated one carry different structure for
    the same source". ``test_indexed_structure_agreement`` shows they agree on a
    fixture; this is what says there is only one rule to agree on, so they
    cannot drift on a source nobody wrote a test for.

    An identity comparison rather than a source scan, so it holds however either
    module spells its import.
    """
    from sage.services import ingestion, maintenance, passage_structure

    assert ingestion.indexed_structure is passage_structure.indexed_structure
    assert maintenance.indexed_structure is passage_structure.indexed_structure


def test_nothing_restates_the_separator():
    """The delimiter is defined once, so a path cannot be split on another.

    Scoped to the whole ``sage`` package rather than to the producers alone. A
    path is joined in one place and split in another -- the adapters build it,
    while heading-candidate ranking, the child-heading prefix query and its
    double all take it apart -- and the two halves cannot be allowed to answer
    differently. Watching only the producing half leaves a drift on the
    consuming half unobserved, which is what a narrower earlier version of this
    scan did.

    The scan reads string *constants* rather than source lines, and looks for
    the delimiter at an edge of one. A line-oriented scan for the quoted form
    ``" > "`` misses the two spellings that matter most: the LIKE pattern
    ``" > %"``, where the character after the delimiter is not a quote, and an
    f-string join ``f"{a} > {b}"``. The first of those is the child-heading
    prefix query -- one of the three sites this scan was widened to cover, and
    the one that decides what a section read returns -- so the earlier predicate
    could not hold the site it was written for.

    The edge test is what separates a spelling from prose. A restatement puts
    the delimiter at the start or the end of its literal (``" > "``, ``" > %"``,
    and the constant an f-string join leaves between two placeholders are all
    exactly that); prose mentioning a path puts it in the middle
    (``"Section 3 > Definitions"``), which is why three field descriptions in
    the models are not flagged and need no allowlist to stay unflagged.

    The edge test has one collision it cannot resolve. The delimiter is also a
    comparison operator with a space either side, so ``f"{column} > {value}"``
    leaves the same constant in the same position as a join, and no predicate
    over the literal separates them. Every contextual signal -- the module, SQL
    text nearby, the names in the placeholders -- fails on the child-heading
    prefix query, which is a heading path built inside SQL in the SQL binding,
    so exempting comparisons that way would exempt the site the scan exists
    for. The predicate therefore stays whole, and the failure message carries
    the remedy for both readings instead of advice that is wrong for one.

    Anti-coincidental-pass: the assertion is bracketed by a positive control
    that the tree was actually read. A glob that matched nothing would otherwise
    report a clean scan. The module that *defines* the constant is the one
    exemption, since the definition is necessarily a literal.
    """
    root = Path(sage.__file__).parent
    modules = sorted(root.rglob("*.py"))
    assert len(modules) >= 50, "the sage package was not read; the scan proves nothing"

    definition = root / "adapters" / "interfaces.py"
    restated: dict[str, list[tuple[int, str]]] = {}
    for path in modules:
        if path == definition:
            continue
        source = path.read_text()
        lines = _restated_separator_lines(source)
        if lines:
            text = source.splitlines()
            restated[str(path.relative_to(root))] = [(n, text[n - 1].strip()) for n in lines]

    assert not restated, _restatement_message(restated)


# The spelling the failure message offers for a comparison operator. Closing up
# either space takes the operator off the literal's edge.
_SANCTIONED_COMPARISON = 'f"{column}>{value}"'


def _restated_separator_lines(source: str) -> list[int]:
    """The lines on which a string literal starts or ends with the delimiter."""
    return sorted(
        {
            node.lineno
            for node in ast.walk(ast.parse(source))
            if isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and (
                node.value.startswith(HEADING_PATH_SEPARATOR)
                or node.value.endswith(HEADING_PATH_SEPARATOR)
            )
        }
    )


def _restatement_message(restated: dict[str, list[tuple[int, str]]]) -> str:
    """The failure text, naming the fix for each reading a flagged literal can have."""
    hits = "\n".join(
        f"  {module}:{line}: {text}" for module, found in restated.items() for line, text in found
    )
    return (
        f"these literals start or end with the heading-path delimiter {HEADING_PATH_SEPARATOR!r}:\n"
        f"{hits}\n"
        "If a line spells the delimiter, import HEADING_PATH_SEPARATOR from "
        "sage.adapters.interfaces instead.\n"
        "If it is a comparison operator (a SQL predicate, say), importing the delimiter "
        "would break it; close up a space beside the operator instead, as in "
        f"{_SANCTIONED_COMPARISON}."
    )


@pytest.mark.parametrize(
    "source",
    [
        'f"{a} > {b}"',
        'prefix + " > %"',
        '" > ".join(parts)',
        'path.split(" > ")',
        'f"{root} > Notes"',
        '"Notes > " + child',
    ],
    ids=["fstring-join", "like-pattern", "join", "split", "leading-edge", "trailing-edge"],
)
def test_the_scan_flags_each_restating_spelling(source):
    """Every spelling the scan exists to catch is caught.

    Anti-coincidental-pass: the f-string join is the case a narrowing aimed at
    comparisons would lose, since the two leave the same constant between two
    placeholders. A scan that exempted placeholder-flanked constants, or that
    reported nothing at all, fails here. The two edge cases each hold the
    delimiter at one end only, so a scan that tested a single edge fails one of
    them; every other spelling here satisfies both edges at once.
    """
    assert _restated_separator_lines(source) == [1]


def test_the_scan_leaves_prose_alone():
    """A delimiter in the middle of a literal is prose about a path, not a spelling."""
    assert _restated_separator_lines('x = "Section 3 > Definitions"') == []


def test_a_comparison_is_flagged_like_a_join():
    """A spaced comparison operator in an f-string is flagged, by design.

    Pinned rather than tolerated: the scan cannot tell this shape from a join,
    so the remedy lives in the failure message. Narrowing the predicate to pass
    it should be a deliberate edit against this test.
    """
    assert _restated_separator_lines('f"{column} > {value}"') == [1]


def test_the_message_names_both_readings():
    """The advice on failure is right whichever reading the flagged code has.

    Anti-coincidental-pass: a message that drops either reading, the recommended
    spelling, or the flagged line fails here. What this does not reach is the
    scan ceasing to use the message: nothing in the suite trips the scan on
    purpose, so that is held by the scan's own assertion reading
    ``_restatement_message`` rather than by a test.
    """
    message = _restatement_message({"adapters/x.py": [(3, 'f"{column} > {value}"')]})
    assert "import HEADING_PATH_SEPARATOR from sage.adapters.interfaces" in message
    assert "comparison operator" in message
    assert _SANCTIONED_COMPARISON in message
    assert 'adapters/x.py:3: f"{column} > {value}"' in message


@pytest.mark.parametrize(
    "source",
    [_SANCTIONED_COMPARISON, 'f"{c}> {v}"', 'f"{c} >{v}"', 'f"{c}> %s"'],
)
def test_the_sanctioned_comparison_spelling_passes_the_scan(source):
    """The way past the message recommends really does get past the scan.

    Anti-coincidental-pass: a scan that flagged any ``>``, or that compared a
    stripped literal, would still flag every spelling above and would flag
    these too, leaving the message's advice as wrong as the advice it replaced.
    """
    assert _restated_separator_lines(source) == []
