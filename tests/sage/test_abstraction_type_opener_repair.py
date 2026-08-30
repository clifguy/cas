"""Unit tests for the type-restating-opener repair.

CAS-ADR-020 clause (l) admits repair for the opening-clause breach once the
check's error rate has been measured and adjudicated. The repair is a
bounded excision, not a rewrite: it cuts the classifying frame out of the
opening sentence and splices the relative clause's finite verb onto the
deictic subject.

The excision is licensed only where the relativizer sits immediately after
the type phrase, so the relative clause attaches to the document itself.
That boundary is not a matter of taste. Measured against stored abstracts,
a relativizer further out in the sentence attaches to something the
document merely mentions, and excising to it produces either a broken
sentence or -- worse -- a grammatical one asserting something false. The
negative fixtures below carry those measured shapes, paraphrased.

Every fixture is text arithmetic: no inference runtime, no storage.
"""

from sage.adapters.abstraction_utils import (
    find_type_restating_opener,
    strip_type_restating_opener,
)

# The measured breach shape, and the one whose repair is unambiguous: the
# relativizer sits directly against the type phrase's trailing identifier.
_ADR_OPENER = (
    "This document serves as an Architecture Decision Record (ADR-020) that "
    "defines the revised system prompt for generating semantic abstracts."
)
_ADR_REPAIRED = "This document defines the revised system prompt for generating semantic abstracts."


class TestLicensedExcision:
    """Shapes the repair may rewrite, and exactly what it produces."""

    def test_excises_the_classifying_frame_before_an_adjacent_relativizer(self):
        """The observed breach collapses to its own relative clause.

        Asserting only that the text changed would pass on a transform
        that truncated the sentence, dropped the identifier, or lost the
        subject; the expected string is spelled out so it cannot.
        """
        assert strip_type_restating_opener(_ADR_OPENER, "adr") == _ADR_REPAIRED

    def test_matches_the_type_by_its_own_words_as_well_as_an_expansion(self):
        """A bare-token match is repaired like a spelled-out one.

        The expansion path is the shape the first breach took, so a
        transform wired only to it would leave the commonest findings
        untouched while this suite's headline test stayed green.
        """
        before = "This document is an essay that critiques a fragmentation."

        after = strip_type_restating_opener(before, "essay")

        assert after == "This document critiques a fragmentation."

    def test_which_is_licensed_like_that(self):
        before = "This document is a report which summarizes the quarter."

        after = strip_type_restating_opener(before, "report")

        assert after == "This document summarizes the quarter."

    def test_text_after_the_opening_sentence_survives_byte_for_byte(self):
        """Only the opening sentence is touched.

        The abstract is stored prose; a transform that rebuilt the whole
        text would be free to normalize whitespace, drop a trailing
        sentence, or re-wrap, none of which the repair is licensed to do.
        """
        tail = "  It also revises the retention policy. A third sentence closes it."
        result = strip_type_restating_opener(_ADR_OPENER + tail, "adr")

        assert result == _ADR_REPAIRED + tail

    def test_intervening_subject_material_is_preserved(self):
        """Everything before the classifying verb is kept.

        The cut is anchored on the verb, not on a fixed two-word subject,
        so an appositive between subject and verb rides through.
        """
        before = "This document, revised in August, is a checklist that governs a reformatting."

        assert strip_type_restating_opener(before, "checklist") == (
            "This document, revised in August, governs a reformatting."
        )

    def test_repair_is_idempotent(self):
        once = strip_type_restating_opener(_ADR_OPENER, "adr")

        assert strip_type_restating_opener(once, "adr") == once

    def test_repaired_text_carries_no_finding(self):
        """Detector and repair agree.

        The pair is what licenses the seam to store the rewritten text:
        a repair that left the frame in place would be a mutation with no
        benefit, and the residual guard downstream keys on exactly this.
        """
        for before, doc_type in (
            (_ADR_OPENER, "adr"),
            ("This document is an essay that critiques a fragmentation.", "essay"),
            ("This document is a report which summarizes the quarter.", "report"),
        ):
            after = strip_type_restating_opener(before, doc_type)

            assert find_type_restating_opener(before, doc_type) != []
            assert find_type_restating_opener(after, doc_type) == []


class TestUnlicensedShapes:
    """Shapes the repair must decline, returning the input untouched.

    Each carries a measured stored-abstract shape. The rewrite each one
    would receive under a looser rule is named in its docstring, because
    the reason to decline is the damage, not the difficulty.
    """

    def test_a_relativizer_further_out_is_declined(self):
        """A relative clause attached to something the document mentions.

        Excising to this relativizer yields "This document are documents
        representing observed failures" -- broken, and false about a
        clause that described the records the document governs.
        """
        before = (
            "This document serves as an active steering document for the vault, establishing "
            "conventions for querying failure records, which are records representing "
            "observed failures."
        )

        assert strip_type_restating_opener(before, "steering_document") == before

    def test_a_relativizer_inside_the_content_is_declined(self):
        """The silent-corruption case, and the reason the rule is strict.

        Excising to this relativizer yields "This document creates
        reference edges from documents to artifacts" -- grammatical, and
        false: the inference rule creates them, not the document. A false
        repair of this shape is indistinguishable from a faithful
        abstract on every subsequent retrieval.
        """
        before = (
            "This document describes a ticket to enhance the ingestion pipeline by adding "
            "a new edge-inference rule that creates reference edges from documents to "
            "artifacts mentioned inline."
        )

        assert strip_type_restating_opener(before, "ticket") == before

    def test_a_participial_modifier_is_declined(self):
        """The dominant stored shape, and not an excision at all.

        Reaching a finite verb here means generating one from
        "governing", which is composition rather than excision.
        """
        before = "This document is a master glossary governing terminology across the portfolio."

        assert strip_type_restating_opener(before, "glossary") == before

    def test_a_prepositional_modifier_is_declined(self):
        before = "This document is a detailed work plan for the development of a release."

        assert strip_type_restating_opener(before, "work_plan") == before

    def test_a_bare_complement_is_declined(self):
        before = "This document is a chat transcript between a user and an assistant."

        assert strip_type_restating_opener(before, "chat_transcript") == before

    def test_a_relativizer_with_no_following_content_is_declined(self):
        before = "This document is an essay that."

        assert strip_type_restating_opener(before, "essay") == before

    def test_an_unflagged_abstract_is_returned_unchanged(self):
        """No finding, no rewrite.

        The repair must not fire on the prompt's own sanctioned style,
        where the type word heads the subject rather than a predicate
        complement.
        """
        before = "The guideline prescribes a retention policy for archived chunks."

        assert strip_type_restating_opener(before, "steering_document") == before

    def test_a_none_doc_type_is_returned_unchanged(self):
        assert strip_type_restating_opener(_ADR_OPENER, None) == _ADR_OPENER

    def test_an_empty_abstract_is_returned_unchanged(self):
        assert strip_type_restating_opener("", "adr") == ""


class TestVerbAnchoring:
    """The cut is anchored by token, never by substring search.

    A transform that located its verb with ``str.find`` would cut inside
    the first word that happens to contain it -- "This" carries "is" --
    silently truncating the subject on every abstract whose classifying
    verb is "is". That is the commonest verb in the measured corpus, so
    the bug would corrupt the majority of repairs while the expansion
    fixtures above stayed green.
    """

    def test_the_subject_survives_a_verb_that_is_a_substring_of_it(self):
        before = "This document is a checklist that governs a reformatting."

        assert strip_type_restating_opener(before, "checklist") == (
            "This document governs a reformatting."
        )

    def test_a_type_word_inside_the_subject_does_not_anchor_the_cut(self):
        """A later sentence's frame is out of scope.

        Only the opening sentence is inspected, so a classifying frame in
        the second sentence must not be cut -- and must not shift where
        the first sentence is cut either.
        """
        before = (
            "This document is an essay that critiques a fragmentation. "
            "This document is an essay about a second topic."
        )

        assert strip_type_restating_opener(before, "essay") == (
            "This document critiques a fragmentation. "
            "This document is an essay about a second topic."
        )
