"""Unit tests for the structural-markup detector.

CAS-ADR-020 requires an abstract to be continuous prose and enforces that with
a deterministic post-generation check. The check is a proxy and is documented
as one: it detects the shape the measured breaches took -- a model producing
the structured document a source's trailing directive asked for -- not the
breach itself. A directive carried out in prose passes it.

The detector needs no source text: an abstract carries no markdown structure
legitimately, whatever its source looks like. That makes these tests pure
text arithmetic with no inference runtime.
"""

from sage.adapters.abstraction_utils import StructureEcho, find_structure_echo

_CLEAN_PROSE = (
    "This document proposes a center for AI and human flourishing, arguing "
    "that the cultural conversation is in a formative phase. It sets out a "
    "first-mover window of six to twelve months, describes an operational "
    "model built for rapid response, and organizes its case into eleven "
    "numbered sections with appendices. It does not price the engagement."
)


class TestFindStructureEcho:
    def test_clean_prose_is_not_flagged(self):
        """The load-bearing negative.

        This prose mentions numbered sections and enumerates in-sentence, so a
        detector keyed on digits, periods, or the word 'section' fires here.
        Every positive test below passes for such a detector; only this one
        fails it.
        """
        assert find_structure_echo(_CLEAN_PROSE) == []

    def test_atx_heading_is_flagged(self):
        """A single heading is enough -- an abstract never has one."""
        abstract = "# Part 2: Sections 7-11\n\nThe center integrates with existing products."

        findings = find_structure_echo(abstract)

        assert [f.kind for f in findings] == ["heading"]
        assert findings[0].line == "# Part 2: Sections 7-11"

    def test_deeper_heading_levels_are_flagged(self):
        assert find_structure_echo("### 7.1 Enhancement\n\nBody text follows.")

    def test_numbered_list_run_is_flagged(self):
        abstract = (
            "The note revises several sections.\n"
            "6. Suffering and Ill-Health\n"
            "7. Embodiment and Limit\n"
        )

        assert [f.kind for f in find_structure_echo(abstract)] == ["list"]

    def test_bullet_run_is_flagged(self):
        abstract = "It covers:\n- onboarding materials\n- product documentation\n"

        assert [f.kind for f in find_structure_echo(abstract)] == ["list"]

    def test_bold_subhead_run_is_flagged(self):
        abstract = "**Project Overview**\n\n**Core Analytical Frameworks**\n"

        assert [f.kind for f in find_structure_echo(abstract)] == ["subhead"]

    def test_a_single_list_line_is_not_flagged(self):
        """One line-leading dash is a dash; two are a list.

        Guards the run threshold. A detector thresholded at one flags an
        abstract whose sentence happens to wrap onto a line beginning with a
        hyphenated word or an aside.
        """
        assert find_structure_echo("The text argues one point.\n- and adds an aside\n") == []

    def test_a_single_bold_span_is_not_flagged(self):
        """Emphasis inside prose is not a subhead."""
        abstract = "The document names **Covenantal Ontology** as its framework and applies it."

        assert find_structure_echo(abstract) == []

    def test_inline_enumeration_is_not_flagged(self):
        """Enumerating within a sentence is prose, not an outline."""
        abstract = "It covers three areas: 1. ingestion, 2. retrieval, and 3. abstraction."

        assert find_structure_echo(abstract) == []

    def test_findings_are_deduplicated_and_ordered(self):
        abstract = "# One\n\n## Two\n\n- a\n- b\n"

        findings = find_structure_echo(abstract)

        assert [f.kind for f in findings] == ["heading", "heading", "list"]

    def test_empty_abstract_is_not_flagged(self):
        assert find_structure_echo("") == []
        assert find_structure_echo("   \n  ") == []

    def test_finding_is_hashable_and_carries_the_offending_line(self):
        """The record names what tripped it, so a breach is adjudicable."""
        [finding] = find_structure_echo("## Integration\n\nBody.")

        assert isinstance(finding, StructureEcho)
        assert finding.kind == "heading"
        assert finding.line == "## Integration"
        assert {finding}
