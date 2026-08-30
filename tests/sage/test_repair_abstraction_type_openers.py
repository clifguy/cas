"""Unit tests for the stored-abstract type-restating-opener repair planner.

The planner is the pure core of the corpus repair pass: it decides which
stored abstracts carry a licensed excision and what each would become,
without touching storage. Storage access and the writes live in the
script's ``run``, so these tests drive a synthetic catalog.

The residual field is the load-bearing one. Unlike the clause (e)
collapse, whose repair is defined for every finding it reports, this
repair is licensed for one shape only -- so a plan whose rewritten text
still carries a finding is the normal case for the majority of the corpus,
not an anomaly, and the apply path must decline it rather than write it.
"""

from scripts.audit_abstraction_glosses import AuditEntry
from scripts.repair_abstraction_type_openers import plan_repairs

_REPAIRABLE = (
    "This document serves as an accepted Architecture Decision Record "
    "(ADR-029) that revises the retention policy."
)
_REPAIRED = "This document revises the retention policy."
#: Flagged, but the modifier is participial -- no finite verb to splice.
_UNREPAIRABLE = (
    "This document serves as an accepted Architecture Decision Record "
    "governing the retention policy."
)
_CLEAN = "This document revises the retention policy for archived chunks."


def _entry(doc_id, abstract, *, doc_type="adr", lifecycle="active"):
    return AuditEntry(
        doc_id=doc_id,
        lifecycle_status=lifecycle,
        abstract=abstract,
        source_text="Body text.",
        doc_type=doc_type,
    )


class TestPlanRepairs:
    def test_only_flagged_entries_are_planned(self):
        """Clean and type-less entries produce no plan.

        A planner that emitted a plan per entry would write the whole
        corpus back unchanged, which the apply path cannot tell from a
        real repair.
        """
        entries = [
            _entry("doc-flagged", _REPAIRABLE),
            _entry("doc-clean", _CLEAN),
            _entry("doc-typeless", _REPAIRABLE, doc_type=None),
        ]

        plans = plan_repairs(entries)

        assert [plan.doc_id for plan in plans] == ["doc-flagged"]

    def test_a_licensed_plan_carries_the_excised_text_and_no_residual(self):
        """The planned text is spelled out, not merely asserted to differ.

        ``residual`` empty is what the apply path checks before writing,
        so the two are asserted together: a plan that changed the text
        without clearing the finding must not read as writable.
        """
        [plan] = plan_repairs([_entry("doc-a", _REPAIRABLE)])

        assert plan.abstract_before == _REPAIRABLE
        assert plan.abstract_after == _REPAIRED
        assert plan.residual == ()
        assert plan.lifecycle_status == "active"

    def test_an_unrepairable_entry_plans_a_nonempty_residual(self):
        """The dominant stored shape is planned but not writable.

        This is the test that keeps the suite honest: without it, a
        planner that silently dropped every unrepairable finding would
        pass everything above while reporting a corpus far cleaner than
        it is. The finding is real, the text is unchanged, and the
        residual is what stops the write.
        """
        [plan] = plan_repairs([_entry("doc-b", _UNREPAIRABLE)])

        assert plan.abstract_after == _UNREPAIRABLE
        assert plan.residual != ()
        assert plan.residual[0].doc_type == "adr"

    def test_lifecycle_filter_selects_classes(self):
        entries = [
            _entry("doc-active", _REPAIRABLE, lifecycle="active"),
            _entry("doc-completed", _REPAIRABLE, lifecycle="completed"),
            _entry("doc-archived", _REPAIRABLE, lifecycle="archived"),
        ]

        plans = plan_repairs(entries, lifecycles=frozenset({"active", "archived"}))

        assert [plan.doc_id for plan in plans] == ["doc-active", "doc-archived"]

    def test_no_filter_covers_every_class(self):
        """The default is every lifecycle class.

        A discovering agent's search surfaces archived documents
        alongside active ones, so the abstract is load-bearing in each.
        """
        entries = [
            _entry("doc-active", _REPAIRABLE, lifecycle="active"),
            _entry("doc-completed", _REPAIRABLE, lifecycle="completed"),
            _entry("doc-archived", _REPAIRABLE, lifecycle="archived"),
        ]

        assert len(plan_repairs(entries)) == 3


class TestParseArgs:
    def test_default_lifecycle_covers_all_three_classes(self):
        """The CLI default matches the planner's no-filter behaviour.

        A default that silently narrowed to ``active`` would leave the
        majority of the flagged corpus untouched while every planner test
        above stayed green.
        """
        from scripts.repair_abstraction_type_openers import _parse_args

        args = _parse_args([])
        parsed = frozenset(part.strip() for part in args.lifecycle.split(",") if part.strip())

        assert parsed == frozenset({"active", "completed", "archived"})
        assert args.apply is False
