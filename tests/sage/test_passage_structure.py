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


def test_no_source_adapter_restates_the_separator():
    """The delimiter is defined once, so a path cannot be built with another.

    Scoped to the whole ``sage/source_adapters`` tree rather than to the four
    modules that join paths today, so an adapter added later is covered without
    an allowlist edit -- a new format that spelled the delimiter itself would
    produce paths this rule cannot split, and the failure would surface as a
    document whose title silently stopped being stripped.

    Anti-coincidental-pass: the assertion is bracketed by a positive control
    that the tree was actually read. A glob that matched nothing would
    otherwise report a clean scan.
    """
    adapters = sorted((Path(sage.__file__).parent / "source_adapters").glob("*.py"))
    assert len(adapters) >= 5, "the source-adapter tree was not read; the scan proves nothing"

    offenders = {
        path.name: [
            number
            for number, line in enumerate(path.read_text().splitlines(), start=1)
            if f'"{HEADING_PATH_SEPARATOR}"' in line or f"'{HEADING_PATH_SEPARATOR}'" in line
        ]
        for path in adapters
    }
    restated = {name: lines for name, lines in offenders.items() if lines}
    assert not restated, (
        f"source adapters spell the heading-path delimiter themselves: {restated}; "
        "import HEADING_PATH_SEPARATOR from sage.adapters.interfaces instead"
    )
