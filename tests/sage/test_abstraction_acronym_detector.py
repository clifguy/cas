"""Unit tests for the unattested-acronym-gloss detector.

CAS-ADR-020 clause (e) forbids an abstract from introducing specifics not
present in the source; the recurring breach shape is an acronym glossed
with an invented expansion. The detector is pure text arithmetic on
recorded model output, so these tests load no inference runtime.

The two anchor cases mirror real stored abstracts: an attested gloss whose
expansion the source supplies (with a curly apostrophe in the abstract),
and an invented expansion for the same acronym over a bare-acronym source.
"""

from sage.adapters.abstraction_utils import AcronymGloss, find_unattested_acronym_glosses
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
