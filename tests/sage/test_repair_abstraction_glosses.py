"""Unit tests for the stored-abstract gloss-repair planner.

The repair script's pure core plans the CAS-ADR-020 clause (e) backfill:
which stored abstracts change, and to what. Storage access and writes
live in the script's ``run``; the planner is exercisable against a
synthetic catalog exactly like the audit core it mirrors.
"""

from sage.adapters.abstraction_utils import (
    AcronymGloss,
    collapse_unattested_acronym_glosses,
    find_unattested_acronym_glosses,
)
from scripts.audit_abstraction_glosses import AuditEntry
from scripts.repair_abstraction_glosses import plan_repairs

_ATTESTED_SOURCE = (
    "CAS (Clif's Agentic System) is a personal experimental agentic "
    "ecosystem. The system indexes documents and serves them to agents."
)

_BARE_ACRONYM_SOURCE = (
    "The CAS project tracker lists active work items. CAS milestones are "
    "reviewed weekly, and each CAS ticket carries a priority."
)


def _entry(doc_id: str, lifecycle: str, abstract: str, source: str) -> AuditEntry:
    return AuditEntry(
        doc_id=doc_id, lifecycle_status=lifecycle, abstract=abstract, source_text=source
    )


class TestPlanRepairs:
    def test_plan_covers_only_documents_with_unattested_glosses(self):
        """Only the document carrying an unattested gloss is planned.

        Kills a planner that plans every visited document: rewriting
        attested abstracts is the asymmetric-cost breach at corpus
        scale, and the attested and bare entries would surface it.
        """
        entries = [
            _entry(
                "doc-attested",
                "active",
                "An overview of CAS (Clif's Agentic System).",
                _ATTESTED_SOURCE,
            ),
            _entry(
                "doc-unattested",
                "active",
                "Describes the CAS (Configuration and Architecture Specification).",
                _BARE_ACRONYM_SOURCE,
            ),
            _entry(
                "doc-bare",
                "archived",
                "This document lists CAS work items.",
                _BARE_ACRONYM_SOURCE,
            ),
        ]

        plans = plan_repairs(entries)

        assert [plan.doc_id for plan in plans] == ["doc-unattested"]
        assert plans[0].glosses == (
            AcronymGloss(
                acronym="CAS",
                expansion="Configuration and Architecture Specification",
            ),
        )
        assert plans[0].abstract_before == (
            "Describes the CAS (Configuration and Architecture Specification)."
        )

    def test_planned_after_text_is_the_collapse_and_is_clean(self):
        """The planned text is the in-place collapse, with no residual findings.

        Kills a planner that regenerates, trims, or blanks the abstract
        instead of collapsing in place; the residual assertion is the
        find/collapse coherence postcondition the apply path refuses to
        write without.
        """
        before = "Describes the CAS (Configuration and Architecture Specification)."
        entries = [_entry("doc-unattested", "completed", before, _BARE_ACRONYM_SOURCE)]

        plans = plan_repairs(entries)

        assert len(plans) == 1
        plan = plans[0]
        assert plan.abstract_after == collapse_unattested_acronym_glosses(
            before, _BARE_ACRONYM_SOURCE
        )
        assert plan.abstract_after == "Describes the CAS."
        assert plan.residual == ()
        assert find_unattested_acronym_glosses(plan.abstract_after, _BARE_ACRONYM_SOURCE) == []

    def test_residual_reports_findings_when_collapse_is_ineffective(self, monkeypatch):
        """A collapse that leaves findings behind surfaces in the residual.

        Kills a planner that hardcodes an empty residual instead of
        re-running the detector on the planned text: the apply path's
        refusal to write a non-empty-residual plan is only a guard if the
        residual is actually computed. Simulated by forcing the collapse
        to return its input unchanged.
        """
        import scripts.repair_abstraction_glosses as repair_module

        monkeypatch.setattr(
            repair_module,
            "collapse_unattested_acronym_glosses",
            lambda abstract, source_text: abstract,
        )
        before = "Describes the CAS (Configuration and Architecture Specification)."
        entries = [_entry("doc-drifted", "active", before, _BARE_ACRONYM_SOURCE)]

        plans = plan_repairs(entries)

        assert len(plans) == 1
        assert plans[0].residual == plans[0].glosses
        assert plans[0].residual != ()

    def test_plan_respects_the_lifecycle_filter(self):
        """Only entries in the requested lifecycle classes are planned.

        Kills a filter flag parsed but never applied -- which under an
        apply run would silently widen the write set beyond the classes
        the operator named.
        """
        gloss_abstract = "Describes the CAS (Configuration and Architecture Specification)."
        entries = [
            _entry("doc-active", "active", gloss_abstract, _BARE_ACRONYM_SOURCE),
            _entry("doc-completed", "completed", gloss_abstract, _BARE_ACRONYM_SOURCE),
            _entry("doc-archived", "archived", gloss_abstract, _BARE_ACRONYM_SOURCE),
        ]

        plans = plan_repairs(entries, lifecycles=frozenset({"active"}))

        assert [plan.doc_id for plan in plans] == ["doc-active"]
