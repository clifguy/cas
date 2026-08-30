"""Unit tests for the generation-side type-restating-opener constraint.

CAS-ADR-020 clause (f) forbids an abstract from opening by restating the
document's own type. The post-generation excision repairs that breach only
where a relativizer stands directly against the type phrase; every other
measured shape would need a finite verb composed rather than cut, so it is
recorded and left as the model wrote it. Closing those means constraining
generation instead of editing output.

This module covers the constraint's pure half: given the text generated so
far, which next surfaces would complete the classifying frame. It is
deliberately the same automaton the detector runs, read off the same
registries, so the two cannot drift apart -- the extent test at the bottom
of this file is the instrument that says so.

A returned surface is a literal next-token surface, leading whitespace
included, because that is how a byte-level tokenizer renders a word-initial
token. "This document is a" forbids " ticket"; a text already ending in
whitespace forbids "ticket".

Every fixture is text arithmetic: no inference runtime, no tokenizer, no
storage.
"""

import pytest

from sage.adapters import abstraction_utils
from sage.adapters.abstraction_utils import (
    find_type_restating_opener,
    forbidden_opener_continuations,
)


class TestBlockedSurfaces:
    """Shapes where a next token would complete the classifying frame."""

    def test_blocks_the_type_word_at_a_licensed_complement_position(self):
        """The commonest measured shape, one token before it lands.

        The surface carries its leading space: the model has emitted
        "...is a" and the next token it wants is " ticket".
        """
        assert forbidden_opener_continuations("This document is a", "ticket") == frozenset(
            {" ticket"}
        )

    def test_drops_the_leading_space_when_the_text_already_ends_in_one(self):
        """The separator is a property of the text, not a constant.

        A constraint that always prepended a space would forbid a surface
        no tokenizer would ever produce here, and the frame would land.
        """
        assert forbidden_opener_continuations("This document is a ", "ticket") == frozenset(
            {"ticket"}
        )

    def test_blocks_a_partial_type_word_by_its_remaining_suffix(self):
        """Subword pieces are walked, not guessed at.

        A tokenizer that splits the type word means the word-initial
        block never fires; the frame completes on the second piece. Both
        readings are live here -- "et" finishes the split word, and
        " ticket" would start it fresh one position later -- so both are
        returned.
        """
        assert forbidden_opener_continuations("This document is a tick", "ticket") == frozenset(
            {"et", " ticket"}
        )

    def test_blocks_only_the_final_word_of_a_multi_word_type(self):
        """ "failure" alone is content; "failure record" is the breach.

        Blocking the first word would forbid a legitimate complement noun
        at a position the detector never flags, making the constraint
        wider than the instrument that verifies it.
        """
        assert forbidden_opener_continuations("This document is a", "failure_record") == frozenset()
        assert forbidden_opener_continuations(
            "This document is a failure", "failure_record"
        ) == frozenset({" record"})

    def test_blocks_a_registered_expansion_by_its_final_word(self):
        """The spelled-out form is a candidate phrase like any other.

        The expansion path is the shape the first measured breach took, so
        a constraint wired only to the bare token would leave it standing.

        Both candidate forms are live at this position and both are
        returned: " record" completes the expansion, and " adr" would
        complete the bare token form one word further into the same
        complement window. The detector flags either.
        """
        assert forbidden_opener_continuations(
            "This document serves as an architecture decision", "adr"
        ) == frozenset({" record", " adr"})

    def test_blocks_the_bare_token_form_alongside_its_expansion(self):
        assert forbidden_opener_continuations("This document serves as an", "adr") == frozenset(
            {" adr"}
        )

    def test_matching_is_case_insensitive(self):
        assert forbidden_opener_continuations("THIS DOCUMENT IS A", "ticket") == frozenset(
            {" ticket"}
        )

    def test_an_underscore_doc_type_splits_into_words(self):
        assert forbidden_opener_continuations(
            "This document is a steering", "steering_document"
        ) == frozenset({" document"})

    def test_a_multi_word_verb_opens_the_window(self):
        """ "serves as" must be matched whole, not by its first word."""
        assert forbidden_opener_continuations("This document serves as a", "ticket") == frozenset(
            {" ticket"}
        )


class TestSilentShapes:
    """Positions the detector would not flag, and so must not be blocked."""

    def test_stays_silent_without_a_deictic_subject(self):
        """A type word after a named subject is a title mention.

        This is the sanctioned descriptive style the abstraction prompt
        itself models, and the gate that keeps the constraint from
        rewriting it.
        """
        assert forbidden_opener_continuations("The guideline describes a", "ticket") == frozenset()

    def test_stays_silent_before_a_classifying_verb(self):
        assert forbidden_opener_continuations("This document", "ticket") == frozenset()

    def test_an_unregistered_verb_does_not_open_the_window(self):
        """ "governs" introduces what the document is about, not what it is."""
        assert forbidden_opener_continuations("This document governs a", "ticket") == frozenset()

    def test_stays_silent_after_a_complement_breaker(self):
        """A connective marks the phrase as content, not classification.

        "the partitioning of the ticket store" is the false-positive shape
        the detector's calibration sized and deliberately admits.
        """
        assert (
            forbidden_opener_continuations(
                "This document describes the partitioning of the", "ticket"
            )
            == frozenset()
        )

    def test_stays_silent_past_the_complement_window(self):
        """A phrase deep in the predicate is content, not a class."""
        deep = "This document is a carefully considered and deliberately bounded revision of"
        assert forbidden_opener_continuations(deep, "ticket") == frozenset()

    def test_stays_silent_once_the_first_sentence_is_terminated(self):
        """Only the opening sentence is inspected, so only it is constrained.

        Past the terminator the detector cannot fire, and a constraint
        still masking tokens would be shaping prose no instrument checks.
        """
        assert (
            forbidden_opener_continuations("This document records a policy. It is a", "ticket")
            == frozenset()
        )

    def test_a_terminator_just_emitted_closes_the_window(self):
        assert forbidden_opener_continuations("This document is a policy.", "ticket") == frozenset()

    def test_none_doc_type_is_silent(self):
        assert forbidden_opener_continuations("This document is a", None) == frozenset()

    def test_empty_text_is_silent(self):
        assert forbidden_opener_continuations("", "ticket") == frozenset()


# (prefix, next surface, doc_type). The next surface is spelled with its
# leading space so the pair reads the way the tokenizer emits it.
_EXTENT_CASES = [
    ("This document is a", " ticket", "ticket"),
    ("This document is an", " essay", "essay"),
    ("This document serves as an", " adr", "adr"),
    ("This document serves as an architecture decision", " record", "adr"),
    ("This document is a failure", " record", "failure_record"),
    ("This document is a", " failure", "failure_record"),
    ("This document constitutes a", " ticket", "ticket"),
    ("This document describes a", " ticket", "ticket"),
    ("This document governs a", " ticket", "ticket"),
    ("The guideline is a", " ticket", "ticket"),
    ("This document describes the partitioning of the", " ticket", "ticket"),
    ("This document is a carefully considered and deliberately bounded", " ticket", "ticket"),
    ("This document is a", " policy", "ticket"),
]


@pytest.mark.parametrize(("prefix", "surface", "doc_type"), _EXTENT_CASES)
def test_the_constraint_and_the_detector_have_the_same_extent(prefix, surface, doc_type):
    """A surface is blocked exactly when emitting it would be a finding.

    The two directions matter equally. A constraint narrower than the
    detector leaves breaches to the post-generation recording posture,
    which is the ceiling this mechanism exists to lift. A constraint wider
    than the detector reshapes openers no instrument flags, and the
    benchmark's clause (f) counter -- the only measurement of this
    change -- cannot see that it happened.

    Editing either side alone turns this red.
    """
    blocked = surface in forbidden_opener_continuations(prefix, doc_type)
    flagged = bool(find_type_restating_opener(f"{prefix}{surface} that tracks work.", doc_type))

    assert blocked == flagged


def test_growing_the_verb_registry_reaches_the_constraint(monkeypatch):
    """The constraint reads the registry rather than carrying a copy.

    The registry's own comment promises that growing it is a data change,
    not a code change. A constraint that copy-pasted the verb tuple passes
    every other test in this file and silently breaks that promise; this
    is the only test that catches it.
    """
    grown = abstraction_utils._CLASSIFYING_VERBS + (("embodies",),)
    monkeypatch.setattr(abstraction_utils, "_CLASSIFYING_VERBS", grown)

    assert forbidden_opener_continuations("This document embodies a", "ticket") == frozenset(
        {" ticket"}
    )
    assert find_type_restating_opener("This document embodies a ticket that tracks work.", "ticket")


def test_growing_the_expansion_registry_reaches_the_constraint(monkeypatch):
    """The same promise, for the spelled-out-form registry."""
    grown = dict(abstraction_utils._DOC_TYPE_EXPANSIONS)
    grown["wp"] = (("work", "plan"),)
    monkeypatch.setattr(abstraction_utils, "_DOC_TYPE_EXPANSIONS", grown)

    assert forbidden_opener_continuations("This document is a work", "wp") == frozenset(
        {" plan", " wp"}
    )
