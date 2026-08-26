"""Unit tests for the fabricated-cardinal detector.

CAS-ADR-020 clause (e) forbids an abstract from introducing specifics not
present in the source. The fabricated-cardinal check covers one surface shape
of that constraint: an abstract asserting an exact count of source-derivable
units. The check runs in the recording posture the ADR requires of any finding
class without an adjudicated error-rate measurement: it reports findings and
mutates nothing.

A finding is not a verdict of guilt. A count that agrees with the derivable
value but is unattested by the source text is still reported, with fields
that let a later calibration split the sub-classes. In a contiguously
numbered transcript the heading numerals attest every value up to the turn
count, so the attestation arm is a weak signal there and the disagreement
arm is the load-bearing one.

The detector is pure text arithmetic with no inference runtime.
"""

from sage.adapters.abstraction_utils import FabricatedCardinal, find_fabricated_cardinals
from scripts.audit_abstraction_cardinals import audit_cardinal_entries
from scripts.audit_abstraction_glosses import AuditEntry


def _turn_source(numbers: list[int], prose: str = "") -> str:
    """Build a transcript-shaped source with one heading per turn number."""
    blocks = [f"### Turn {n} — Speaker\n\nSome discussion happens here.\n" for n in numbers]
    return "\n".join(blocks) + ("\n" + prose if prose else "")


class TestFindFabricatedCardinals:
    def test_exhibit_shaped_word_claim_is_flagged(self):
        """A word-form count that disagrees with the derivable value.

        The turn numbering is contiguous, so the claimed value is attested
        by a heading numeral. An attestation-only detector is silent here;
        only the disagreement arm reports this breach.
        """
        source = _turn_source(list(range(1, 65)))
        abstract = (
            "This document records a working conversation about system design, "
            "followed by a transcript of twenty-six turns."
        )

        [finding] = find_fabricated_cardinals(abstract, source)

        assert finding == FabricatedCardinal(
            surface="twenty six turns", value=26, unit="turn", derived=64, attested=True
        )

    def test_unattested_disagreeing_digit_claim_is_flagged(self):
        """A digit-form count wrong on both arms.

        A detector that compares only against the derived count and skips
        attestation bookkeeping reports the wrong ``attested`` field here.
        """
        source = _turn_source([2, 4, 6])
        abstract = "The exchange covers 7 turns of review."

        [finding] = find_fabricated_cardinals(abstract, source)

        assert finding.value == 7
        assert finding.derived == 3
        assert finding.attested is False

    def test_agreeing_but_unattested_claim_is_flagged(self):
        """A correct derived count the source never states.

        Kills a detector wired to disagreement alone. The claim equals the
        derivable value, but no token in the source attests it, and clause
        (e) licenses only what the source states; the finding's fields are
        what a calibration needs to adjudicate this sub-class.
        """
        source = _turn_source([2, 4, 6])
        abstract = "The conversation unfolds across three turns."

        [finding] = find_fabricated_cardinals(abstract, source)

        assert finding.value == 3
        assert finding.derived == 3
        assert finding.attested is False

    def test_compound_and_hundreds_word_forms_parse(self):
        """Compound and hundreds word forms yield the composed value.

        A lexicon without compound handling parses only the trailing word
        and reports value 6.
        """
        source = _turn_source(list(range(1, 65)))
        abstract = "It claims one hundred twenty-six turns of dialogue."

        [finding] = find_fabricated_cardinals(abstract, source)

        assert finding.value == 126

    def test_duplicate_claim_yields_one_finding(self):
        """Repeats of one claim collapse to a single finding.

        Per-occurrence reporting would weight an abstract that repeats
        itself more heavily in any rate measured over these records.
        """
        source = _turn_source(list(range(1, 65)))
        abstract = (
            "The transcript spans twenty-six turns. Its 26 turns alternate "
            "between design questions and drafting."
        )

        findings = find_fabricated_cardinals(abstract, source)

        assert len(findings) == 1
        assert findings[0].surface == "twenty six turns"

    def test_correct_attested_count_is_silent(self):
        """The load-bearing negative.

        A detector that flags every cardinal claim unconditionally passes
        every positive test above; only this one fails it.
        """
        source = _turn_source(list(range(1, 65)))
        abstract = "The transcript runs sixty-four turns from framing to close."

        assert find_fabricated_cardinals(abstract, source) == []

    def test_unregistered_unit_is_silent(self):
        """Counts of units the check cannot derive are out of scope.

        Kills an open-ended detector that flags any unattested number. The
        prose asserts counts of sections and months the source never
        states; neither unit is derivable, so neither is a finding -- that
        remainder of clause (e) stays with the out-of-band evaluation.
        """
        source = _turn_source([1, 2])
        abstract = (
            "This document sets out a first-mover window of six to twelve "
            "months, organizes its case into eleven numbered sections, and "
            "closes with eleven appendices."
        )

        assert find_fabricated_cardinals(abstract, source) == []

    def test_source_without_turn_structure_deactivates_the_unit(self):
        """A source with no turn headings cannot ground a turn count.

        An implementation that treats a derived count of zero as a real
        value flags every non-transcript document whose abstract mentions
        N turns.
        """
        source = (
            "## Overview\n\nThe essay turns on a single distinction and "
            "develops it in stages, and its argument turns twice more.\n"
        )
        abstract = "The review took three turns before settling the question."

        assert find_fabricated_cardinals(abstract, source) == []

    def test_verb_turns_is_not_a_claim(self):
        """The verb reading of the unit noun is rejected by agreement.

        A parser without the number-agreement gate reads 'one turns' as a
        value-1 claim of the plural unit.
        """
        source = _turn_source([1, 2, 3])
        abstract = (
            "The discussion then turns to governance. One turns to the appendix for the schedule."
        )

        assert find_fabricated_cardinals(abstract, source) == []

    def test_range_is_not_a_claim(self):
        """A range names an interval, not an exact count.

        A parser without the preceding-token guard takes the last cardinal
        of a range as an exact claim of its value.
        """
        source = _turn_source([1, 2, 3, 4, 5])
        abstract_digit = "The heart of the exchange spans 3-4 turns."
        abstract_word = "The heart of the exchange spans three to four turns."

        assert find_fabricated_cardinals(abstract_digit, source) == []
        assert find_fabricated_cardinals(abstract_word, source) == []

    def test_hedged_count_is_not_a_claim(self):
        """An approximation asserts no exact value.

        A parser without the preceding-token guard flags hedged counts
        whose rounded value differs from the derived one.
        """
        source = _turn_source(list(range(1, 65)))
        abstract_word = "The record preserves about sixty turns of discussion."
        abstract_digit = "The record preserves more than 60 turns of discussion."

        assert find_fabricated_cardinals(abstract_word, source) == []
        assert find_fabricated_cardinals(abstract_digit, source) == []

    def test_ordinal_is_not_a_claim(self):
        """A position reference asserts no count.

        A lexicon that admits ordinal words misreads 'the twenty-sixth
        turn' as a claim of twenty-six turns.
        """
        source = _turn_source(list(range(1, 65)))
        abstract = "At the discussion's twenty-sixth turn the argument reverses course."

        assert find_fabricated_cardinals(abstract, source) == []

    def test_year_token_does_not_attest_a_substring(self):
        """Attestation is token-level, never substring.

        A substring check reads the 26 inside 2026 as attesting the claim.
        """
        source = _turn_source(list(range(40, 50)), prose="The session ran during 2026.")
        abstract = "The transcript records twenty-six turns."

        [finding] = find_fabricated_cardinals(abstract, source)

        assert finding.attested is False

    def test_word_form_in_source_attests_digit_claim(self):
        """Either surface form attests either.

        A digit-only attestation pass misses the source's word form and
        wrongly reports this agreeing claim as unattested.
        """
        source = _turn_source([10, 20, 30], prose="It took exactly three turns of review.")
        abstract = "The matter settles in 3 turns."

        assert find_fabricated_cardinals(abstract, source) == []

    def test_singular_form_with_value_one(self):
        """The singular unit registers, and agreement runs both ways."""
        agreeing_source = _turn_source([1])
        disagreeing_source = _turn_source([1, 2, 3])
        abstract = "A single exchange: the whole matter takes one turn."

        assert find_fabricated_cardinals(abstract, agreeing_source) == []

        [finding] = find_fabricated_cardinals(abstract, disagreeing_source)
        assert finding.value == 1
        assert finding.derived == 3


class TestAuditCardinalEntries:
    """The corpus audit's pure core, exercised against a synthetic catalog.

    Storage access lives in the script's ``run``; the core reuses the same
    catalog shape as the gloss audit, so the two instruments read one
    reconstruction of each document's source.
    """

    def test_only_documents_with_findings_report(self):
        """Flagged, clean, and inapplicable entries are told apart.

        A core that reports every audited document, or that treats a
        source without derivable structure as a zero count, fails on the
        clean and inapplicable entries respectively.
        """
        flagged = AuditEntry(
            doc_id="doc-flagged",
            lifecycle_status="active",
            abstract="The exchange spans twenty-six turns of discussion.",
            source_text=_turn_source([1, 2, 3]),
        )
        clean = AuditEntry(
            doc_id="doc-clean",
            lifecycle_status="active",
            abstract="The exchange spans three turns of discussion.",
            source_text=_turn_source([1, 2, 3]),
        )
        inapplicable = AuditEntry(
            doc_id="doc-essay",
            lifecycle_status="completed",
            abstract="The review took three turns before settling the question.",
            source_text="## Overview\n\nAn essay that turns on a single distinction.\n",
        )

        findings = audit_cardinal_entries([flagged, clean, inapplicable])

        assert [f.doc_id for f in findings] == ["doc-flagged"]
        assert findings[0].lifecycle_status == "active"
        [claim] = findings[0].claims
        assert claim.value == 26
        assert claim.derived == 3
