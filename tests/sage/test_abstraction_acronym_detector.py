"""Unit tests for the unattested-acronym-gloss detector.

CAS-ADR-020 clause (e) forbids an abstract from introducing specifics not
present in the source; the recurring breach shape is an acronym glossed
with an invented expansion. The detector is pure text arithmetic on
recorded model output, so these tests load no inference runtime.

The two anchor cases mirror real stored abstracts: an attested gloss whose
expansion the source supplies (with a curly apostrophe in the abstract),
and an invented expansion for the same acronym over a bare-acronym source.
"""

from sage.adapters.abstraction_utils import (
    AcronymGloss,
    collapse_unattested_acronym_glosses,
    find_unattested_acronym_glosses,
)
from scripts.audit_abstraction_glosses import AuditEntry, audit_entries

# ---------------------------------------------------------------------------
# find_unattested_acronym_glosses
# ---------------------------------------------------------------------------

_ATTESTED_SOURCE = (
    "CAS (Clif's Agentic System) is a personal experimental agentic "
    "ecosystem. The system indexes documents and serves them to agents."
)

_BARE_ACRONYM_SOURCE = (
    "The CAS project tracker lists active work items. CAS milestones are "
    "reviewed weekly, and each CAS ticket carries a priority."
)


class TestFindUnattestedAcronymGlosses:
    def test_attested_gloss_is_not_flagged(self):
        """A gloss whose expansion the source supplies is attested.

        Load-bearing pair with the invented-expansion test below: a
        detector that flags every parenthetical gloss fails only here.
        """
        abstract = "This document provides an overview of CAS (Clif's Agentic System)."

        assert find_unattested_acronym_glosses(abstract, _ATTESTED_SOURCE) == []

    def test_gloss_absent_from_source_is_flagged(self):
        """An expansion the source never states is an unattested claim."""
        abstract = (
            "This document describes the CAS (Configuration and Architecture "
            "Specification) system and its work tracking."
        )

        findings = find_unattested_acronym_glosses(abstract, _BARE_ACRONYM_SOURCE)

        assert findings == [
            AcronymGloss(
                acronym="CAS",
                expansion="Configuration and Architecture Specification",
            )
        ]

    def test_reversed_gloss_order_is_flagged(self):
        """The ``Expansion Words (ACR)`` adjacency is half the real surface.

        Kills a detector matching only the ``ACR (...)`` shape, which
        would pass the forward-adjacency test while missing this one.
        """
        abstract = "The Computer-Aided System (CAS) coordinates the work."

        findings = find_unattested_acronym_glosses(abstract, _BARE_ACRONYM_SOURCE)

        assert len(findings) == 1
        assert findings[0].acronym == "CAS"

    def test_inconsistent_parenthetical_is_not_a_gloss(self):
        """A parenthetical whose initials do not fit the acronym is an aside.

        Kills a detector with no initials-consistency check; the attested
        case cannot catch that, because its parenthetical genuinely is a
        consistent expansion.
        """
        abstract = "SAGE (see the architecture reference) stores the graph."
        source = "SAGE stores the graph and the content chunks."

        assert find_unattested_acronym_glosses(abstract, source) == []

    def test_bare_acronym_is_not_flagged(self):
        """An acronym with no parenthetical anywhere makes no claim."""
        abstract = "This document lists CAS work items and their priorities."

        assert find_unattested_acronym_glosses(abstract, _BARE_ACRONYM_SOURCE) == []

    def test_normalization_accepts_case_and_punctuation_variants(self):
        """Apostrophe and case variants of an attested expansion still attest.

        Kills an exact-substring attestation check: the real stored case
        pairs a curly apostrophe in the abstract against a straight one in
        the source, and an exact matcher would flag a correct abstract.
        """
        abstract = "This document provides an overview of CAS (Clif’s Agentic System)."
        source = "The clif's agentic system project, abbreviated CAS, indexes documents."

        assert find_unattested_acronym_glosses(abstract, source) == []

    def test_hyphen_gap_attests(self):
        """A hyphenated gloss attests against the source's spaced form.

        The calibrated false-positive shape: a human reads the gloss as
        attested, and a repair would delete correct content. Kills the
        strict attestation check, which treats a hyphen as a distinct
        character and flags this.
        """
        abstract = "The CAS (Computer-Aided System) coordinates the work."
        source = "The computer aided system coordinates all scheduled work."

        assert find_unattested_acronym_glosses(abstract, source) == []

    def test_hyphen_gap_attests_in_the_other_direction(self):
        """A spaced gloss attests against the source's hyphenated form.

        Kills a widening applied to the expansion side only (stripping
        hyphens from the claim before an exact source match), which
        passes the hyphenated-abstract case while flagging this one.
        """
        abstract = "The CAS (Computer Aided System) coordinates the work."
        source = "The computer-aided system coordinates all scheduled work."

        assert find_unattested_acronym_glosses(abstract, source) == []

    def test_plural_last_word_attests(self):
        """A last-word plural of an attested expansion still attests.

        The other calibrated near-miss shape. Kills the strict
        attestation check, which demands the exact grammatical number
        the source used.
        """
        abstract = "The CAS (Computer Aided Systems) coordinate the work."
        source = "Each computer aided system coordinates its own work."

        assert find_unattested_acronym_glosses(abstract, source) == []

    def test_singular_last_word_attests_against_plural_source(self):
        """A singular gloss attests against the source's plural form.

        Already true of the strict check -- a singular phrase is a
        substring prefix of its plural. Pinned so a widening rewrite
        that replaced the substring test with whole-phrase variant
        equality could not silently lose it.
        """
        abstract = "The CAS (Computer Aided System) coordinates the work."
        source = "The computer aided systems coordinate all scheduled work."

        assert find_unattested_acronym_glosses(abstract, source) == []

    def test_separator_elision_does_not_attest_across_word_boundaries(self):
        """Hyphens read as spaces, not as nothing: word boundaries survive.

        Kills a normalization that deletes separators outright instead of
        mapping hyphens to spaces: with spaces elided too, this claimed
        two-word expansion fuses to a single word the source does contain
        ("network"), and the rival attests an expansion the source never
        states as words.
        """
        abstract = "The NW (Net Work) layer routes frames."
        source = "The network layer routes frames between services."

        findings = find_unattested_acronym_glosses(abstract, source)

        assert len(findings) == 1
        assert findings[0].acronym == "NW"

    def test_interior_word_plural_gap_is_still_flagged(self):
        """A plurality gap on an interior word is a different noun phrase.

        The over-widening boundary: tolerance is confined to the final
        word, where the calibrated near-misses live. Kills a per-word
        plural toggle, which would attest this claim.
        """
        abstract = "The CSA (Computer Systems Architecture) is documented here."
        source = "The computer system architecture is documented in this file."

        findings = find_unattested_acronym_glosses(abstract, source)

        assert len(findings) == 1
        assert findings[0].acronym == "CSA"

    def test_widening_does_not_attest_an_invented_expansion(self):
        """No variant of a phrase the source never states can attest.

        Kills any fuzzy or prefix matcher smuggled in as widening: the
        source carries the bare acronym only, so every plural and hyphen
        variant of the invented expansion must stay unattested.
        """
        abstract = "The CAS (Computer-Aided Systems) coordinates the work."

        findings = find_unattested_acronym_glosses(abstract, _BARE_ACRONYM_SOURCE)

        assert len(findings) == 1
        assert findings[0].acronym == "CAS"


# ---------------------------------------------------------------------------
# collapse_unattested_acronym_glosses
# ---------------------------------------------------------------------------

_QZE_BARE_SOURCE = (
    "The QZE protocol frames messages between agents. QZE sessions are "
    "stateless, and every QZE frame carries a sequence number."
)

_QZE_ATTESTED_SOURCE = (
    "The quantum zeta exchange protocol, abbreviated QZE, frames messages "
    "between agents. Every frame carries a sequence number."
)


class TestCollapseUnattestedAcronymGlosses:
    def test_forward_gloss_collapses_to_bare_acronym(self):
        """An unattested ``ACR (Expansion)`` collapses to the acronym alone.

        The expected string is pinned in full: a repair that deletes the
        whole gloss including the acronym, or leaves the parentheses
        behind, produces different bytes and fails here.
        """
        abstract = "The QZE (Quantum Zeta Exchange) protocol frames messages."

        repaired = collapse_unattested_acronym_glosses(abstract, _QZE_BARE_SOURCE)

        assert repaired == "The QZE protocol frames messages."

    def test_reversed_gloss_collapses_to_bare_acronym(self):
        """An unattested ``Expansion (ACR)`` collapses to the acronym alone.

        Kills a forward-only implementation, which passes the forward
        test while leaving this adjacency -- half the real surface --
        untouched.
        """
        abstract = "The Quantum Zeta Exchange (QZE) frames messages."

        repaired = collapse_unattested_acronym_glosses(abstract, _QZE_BARE_SOURCE)

        assert repaired == "The QZE frames messages."

    def test_attested_gloss_survives_byte_identical(self):
        """An attested gloss is returned byte-identical.

        The asymmetric-cost rule in executable form: kills an
        unconditional collapse of every consistent gloss, which mutates
        correct content the source attests.
        """
        abstract = "The QZE (Quantum Zeta Exchange) protocol frames messages."

        repaired = collapse_unattested_acronym_glosses(abstract, _QZE_ATTESTED_SOURCE)

        assert repaired == abstract

    def test_only_the_unattested_gloss_collapses_in_mixed_text(self):
        """Two claims for one acronym: only the unattested site changes.

        Kills a replacement keyed on the acronym alone, which cannot
        distinguish the attested claim from the invented one and
        collapses both.
        """
        abstract = (
            "The Quantum Zeta Exchange (QZE) frames messages. Some texts "
            "call QZE (Quick Zone Evaluator) a scheduler."
        )

        repaired = collapse_unattested_acronym_glosses(abstract, _QZE_ATTESTED_SOURCE)

        assert repaired == (
            "The Quantum Zeta Exchange (QZE) frames messages. Some texts call QZE a scheduler."
        )

    def test_every_occurrence_of_an_unattested_pair_collapses(self):
        """A gloss repeated verbatim collapses at every site.

        Kills a dedup-driven single replacement mirroring the detector's
        pair dedup: the second occurrence would survive it.
        """
        abstract = (
            "The QZE (Quantum Zeta Exchange) protocol frames messages. "
            "The QZE (Quantum Zeta Exchange) protocol is stateless."
        )

        repaired = collapse_unattested_acronym_glosses(abstract, _QZE_BARE_SOURCE)

        assert repaired == ("The QZE protocol frames messages. The QZE protocol is stateless.")

    def test_reversed_gloss_with_interior_newline_collapses_cleanly(self):
        """A reversed gloss whose expansion wraps a line collapses cleanly.

        Kills a string-search replacement built from the detector's
        space-joined expansion: the joined string does not occur in the
        original bytes, so only span-carrying detection can locate and
        remove this site.
        """
        abstract = "The Quantum Zeta\nExchange (QZE) frames messages."

        repaired = collapse_unattested_acronym_glosses(abstract, _QZE_BARE_SOURCE)

        assert repaired == "The QZE frames messages."

    def test_collapse_is_idempotent(self):
        """Collapsing already-collapsed text changes nothing.

        A collapsed site leaves a bare acronym, which makes no claim;
        kills a second pass that mangles bare acronyms.
        """
        abstract = "The QZE (Quantum Zeta Exchange) protocol frames messages."

        once = collapse_unattested_acronym_glosses(abstract, _QZE_BARE_SOURCE)
        twice = collapse_unattested_acronym_glosses(once, _QZE_BARE_SOURCE)

        assert twice == once

    def test_inconsistent_parenthetical_survives(self):
        """A parenthetical aside is not a gloss and is never collapsed.

        Kills a collapse keyed on parenthetical presence rather than
        gloss consistency, which would delete ordinary asides.
        """
        abstract = "SAGE (see the architecture reference) stores the graph."
        source = "SAGE stores the graph and the content chunks."

        repaired = collapse_unattested_acronym_glosses(abstract, source)

        assert repaired == abstract

    def test_surrounding_punctuation_is_preserved(self):
        """Punctuation flanking a collapsed gloss is untouched.

        Kills an off-by-one span that consumes the character after the
        closing parenthesis or leaves the parenthesis behind.
        """
        abstract = "One protocol, the QZE (Quantum Zeta Exchange), is stateless."

        repaired = collapse_unattested_acronym_glosses(abstract, _QZE_BARE_SOURCE)

        assert repaired == "One protocol, the QZE, is stateless."

    def test_collapsed_output_carries_no_findings(self):
        """The detector finds nothing in collapsed output.

        The find/collapse coherence property the seam and the backfill
        postcondition rest on: kills a collapse whose candidate logic
        drifted from the detector's, leaving sites the detector still
        flags.
        """
        abstract = (
            "The QZE (Quantum Zeta Exchange) protocol frames messages. "
            "The Quick Zone Evaluator (QZE) schedules them."
        )

        repaired = collapse_unattested_acronym_glosses(abstract, _QZE_BARE_SOURCE)

        assert find_unattested_acronym_glosses(repaired, _QZE_BARE_SOURCE) == []


# ---------------------------------------------------------------------------
# audit_entries (corpus audit core)
# ---------------------------------------------------------------------------


class TestAuditEntries:
    def test_corpus_audit_counts_only_unattested_findings(self):
        """The audit reports the unattested document and nothing else.

        Anti-coincidental-pass: the attested and bare entries share the
        detector's negative paths, so an audit that reports every document
        it visited -- or every document with any parenthetical -- fails.
        """
        entries = [
            AuditEntry(
                doc_id="doc-attested",
                lifecycle_status="active",
                abstract="An overview of CAS (Clif's Agentic System).",
                source_text=_ATTESTED_SOURCE,
            ),
            AuditEntry(
                doc_id="doc-unattested",
                lifecycle_status="active",
                abstract="Describes the CAS (Configuration and Architecture Specification).",
                source_text=_BARE_ACRONYM_SOURCE,
            ),
            AuditEntry(
                doc_id="doc-bare",
                lifecycle_status="archived",
                abstract="This document lists CAS work items.",
                source_text=_BARE_ACRONYM_SOURCE,
            ),
        ]

        findings = audit_entries(entries)

        assert [finding.doc_id for finding in findings] == ["doc-unattested"]
        assert findings[0].glosses == (
            AcronymGloss(
                acronym="CAS",
                expansion="Configuration and Architecture Specification",
            ),
        )

    def test_audit_stops_reporting_the_widened_near_miss(self):
        """A last-word-plural near-miss of an attested phrase is clean.

        Kills a second attestation code path pinned to strict matching:
        if the audit re-implemented attestation instead of calling the
        detector, widening the detector would leave the audit still
        reporting the near-miss class it exists to exclude.
        """
        entries = [
            AuditEntry(
                doc_id="doc-near-miss",
                lifecycle_status="completed",
                abstract="An overview of CAS (Clif's Agentic Systems).",
                source_text=_ATTESTED_SOURCE,
            ),
        ]

        assert audit_entries(entries) == []
