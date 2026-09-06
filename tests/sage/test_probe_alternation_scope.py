"""The alternation-scope probe's own arithmetic and framing.

The probe reports the recall change that document-scoping a top-level
alternation makes, and its output is what a later reader will trust instead of
re-deriving it -- so a probe that miscounts, or that sweeps a query shape other
than the one it claims, is worse than none. These cover the parts that decide
what the report claims: that every query it issues really is an alternation
whose other branch is absent from any corpus, that the cross-passage pairs
really do require assembling across passages, the rates, and the vector
passthrough a naive materialization silently corrupts.

The pair selection carries the most weight and the least visibility. A pair one
passage happens to hold is answered identically by both arms, so a selection
that admitted such pairs would report the change as small -- a wrong number
rather than an obvious failure, and the second table is the only place the
change shows up at all.

The database-touching halves are exercised by running the probe; what is pinned
here is everything that could go wrong without a server to notice.
"""

from __future__ import annotations

from scripts.probe_alternation_scope import (
    _ABSENT_BRANCH,
    ArmResult,
    _as_embedding,
    _cross_passage_pairs,
    _pair_queries,
    _renderings,
)


def _chunk(document_id: str, content: str):
    """A corpus row in the shape ``_read_corpus`` hands back."""
    return (document_id, "Section", content, 0, None, "adr", "active", "CAS")


def _no_stemming(word: str) -> str:
    """A configuration that reduces nothing, so a fixture's words are its lexemes.

    Every fixture below is written in words no English stemmer touches, so this
    is what the real map would return for them -- stated rather than assumed,
    since the selection now decides on lexemes and a test that hid which it was
    reading could not tell the two apart.
    """
    return word


# ---------------------------------------------------------------------------
# The query shape under measurement
# ---------------------------------------------------------------------------


def test_every_query_is_an_alternation():
    """A sweep of bare titles would measure the change it exists to detect at zero.

    The whole finding rests on the query carrying a top-level ``or``: without
    one, both arms answer a plain conjunction and the report is two identical
    columns that read as "no effect" rather than as a probe pointed at the
    wrong shape.
    """
    for query in _renderings("ADR-049: Document-Level Text").values():
        assert " or " in query, f"{query!r} is not an alternation"


def test_the_absent_branch_leads_and_the_title_follows():
    """The title must survive into the query, not be replaced by the disjunct."""
    renderings = _renderings("Document-Level Text")

    assert renderings["verbatim"] == f"{_ABSENT_BRANCH} or Document-Level Text"
    assert renderings["lowercase"] == f"{_ABSENT_BRANCH} or document-level text"
    assert renderings["uppercase"] == f"{_ABSENT_BRANCH} or DOCUMENT-LEVEL TEXT"


def test_the_absent_branch_cannot_be_carried_by_a_corpus():
    """A disjunct a document might carry would make the other branch unreadable.

    Anti-coincidental-pass: if the token were an ordinary word, every query
    would match on that branch alone and both arms would report inflated,
    equal recall -- a tie produced by the fixture rather than by the code.
    """
    assert _ABSENT_BRANCH.isalpha()
    assert len(_ABSENT_BRANCH) > 12, "a short token risks colliding with real text"
    assert " " not in _ABSENT_BRANCH, "a multi-word branch would be a conjunction of its own"


def test_the_sweep_covers_the_renderings_the_title_instrument_uses():
    """Four forms, held equal to the title sweep's so the reports read together."""
    renderings = _renderings("ADR-049: Document-Level Text")

    assert set(renderings) == {"verbatim", "lowercase", "uppercase", "separators folded"}
    assert renderings["separators folded"] != renderings["verbatim"], (
        "the folded rendering must differ from the raw title, or the sweep "
        "reports four columns of the same measurement"
    )
    assert len(set(renderings.values())) > 1


# ---------------------------------------------------------------------------
# The cross-passage pair, which is what makes the second family measure anything
# ---------------------------------------------------------------------------


def test_a_pair_spans_two_passages():
    """The pair's terms come from different passages, not from the richest one.

    Says only that, and its name now says only that. It was written as the pin
    on the co-occurrence guard and is not one: on this fixture the per-passage
    rarest words are ``beside`` and ``betaword``, which no passage holds
    together, so the guard never fires and the selection returns the same pair
    with it removed. ``test_a_document_whose_own_candidates_co_occur_is_dropped``
    is the fixture that reaches it.
    """
    chunks = [
        _chunk("d1", "alphaword betaword together in the first passage"),
        _chunk("d1", "alphaword again with gammaword beside it"),
    ]

    first, second = _cross_passage_pairs(chunks, _no_stemming)["d1"]

    assert {first, second} != {"alphaword", "betaword"}
    assert not ({first, second} <= {"alphaword", "betaword", "together", "passage"}), (
        "the pair was drawn from the first passage alone"
    )


def test_the_guard_reads_lexemes_rather_than_the_words_as_written():
    """Two spellings of one lexeme are one term to the index, and must be here.

    The failure this closes is silent and it inflates the before arm rather
    than emptying it. ``documents`` and ``document`` are one lexeme, so a
    passage holding the plural satisfies a query naming the singular -- and a
    guard comparing the words as written calls that pair cross-passage, hands
    the before arm a query it answers within one unit, and reports the reach as
    the change's baseline. Against the cas corpus a raw-word guard admitted 30
    of 199 pairs this way.

    Here ``alphaword`` and ``alphawords`` are one lexeme. Reading lexemes, the
    two candidates are ``gammaword`` and that lexeme, which the second passage
    holds together, so the guard rejects them and the document is dropped.
    Reading the words as written, the same fixture yields the pair
    ``('alphaword', 'alphawords')`` -- a conjunction of a term with a second
    spelling of itself, which every passage carrying either already satisfies.
    """
    chunks = [
        _chunk("d1", "alphaword betaword"),
        _chunk("d1", "alphawords betaword gammaword"),
    ]
    stemmed = {"alphawords": "alphaword"}

    assert _cross_passage_pairs(chunks, lambda w: stemmed.get(w, w)) == {}
    assert _cross_passage_pairs(chunks, _no_stemming) == {"d1": ("alphaword", "alphawords")}, (
        "positive control: read as written, the fixture yields exactly the pair "
        "the stemming rejects, so the emptiness above is the guard and not the shape"
    )


def test_a_document_whose_own_candidates_co_occur_is_dropped():
    """The co-occurrence guard, isolated from the distinctness check beside it.

    The two candidates here are ``gammaword`` (the second passage's rarest) and
    ``alphaword`` (the first's), which are distinct -- so the ``first !=
    second`` test admits them -- and the second passage carries both, so only
    the co-occurrence guard rejects the pair. Its siblings above and below are
    both satisfied by that distinctness check alone and stay green with the
    guard removed, which is what makes this fixture rather than those the one
    that pins it: without it the guard is unreachable and the second table
    could silently fill with pairs that measure nothing.
    """
    chunks = [
        _chunk("d1", "alphaword betaword"),
        _chunk("d1", "alphaword betaword gammaword"),
    ]

    assert _cross_passage_pairs(chunks, _no_stemming) == {}


def test_a_word_the_configuration_discards_is_not_a_candidate():
    """A term that renders to nothing degenerates the pair into one lexeme.

    ``_stem_map`` reports no lexeme for a word the configuration drops, and a
    pair holding such a word asks for a single required term -- which the
    before arm answers within one passage, exactly the case the pair exists to
    avoid. Rare in practice, since a stopword is seldom a passage's rarest
    word, which is why it has to be pinned rather than left to the corpus.
    """
    chunks = [
        _chunk("d1", "because alphaword"),
        _chunk("d1", "because betaword"),
    ]

    def _drops_because(word: str):
        return None if word == "because" else word

    assert set(_cross_passage_pairs(chunks, _drops_because)["d1"]) == {"alphaword", "betaword"}, (
        "the discarded word was selected, so the pair asks for one lexeme"
    )


def test_a_document_offering_no_such_pair_is_dropped():
    """Silence beats a weaker query.

    Every word here lives in both passages, so no pair can require assembling
    them. Substituting a co-occurring pair would answer identically on both
    arms and pull the reported change toward zero -- a dilution that would read
    as the change being small rather than as the query being wrong.
    """
    chunks = [
        _chunk("d1", "alphaword betaword gammaword"),
        _chunk("d1", "gammaword betaword alphaword"),
    ]

    assert _cross_passage_pairs(chunks, _no_stemming) == {}


def test_a_single_passage_document_is_dropped():
    """One passage cannot hold a pair across two of them."""
    chunks = [_chunk("d1", "alphaword betaword gammaword deltaword")]

    assert _cross_passage_pairs(chunks, _no_stemming) == {}


def test_both_terms_come_from_the_document():
    """A term the document does not carry would make the query unanswerable."""
    chunks = [
        _chunk("d1", "alphaword in the first passage"),
        _chunk("d1", "betaword in the second passage"),
    ]

    assert set(_cross_passage_pairs(chunks, _no_stemming)["d1"]) == {"alphaword", "betaword"}


def test_the_pair_family_carries_the_alternation_and_its_control():
    """Two forms per document: the shape measured, and the control that reads it."""
    forms = _pair_queries({"d1": ("alphaword", "betaword")})["d1"]

    assert forms["alternation"] == f"{_ABSENT_BRANCH} or alphaword betaword"
    assert forms["bare conjunction"] == "alphaword betaword"
    assert " or " not in forms["bare conjunction"], (
        "the control must not be an alternation, or it measures the same thing twice"
    )


# ---------------------------------------------------------------------------
# The rates
# ---------------------------------------------------------------------------


def test_the_rates_are_over_the_titles_queried():
    arm = ArmResult(rendering="verbatim", rank_1=3, recalled=4, total=5)

    assert arm.rank_1_rate == 3 / 5
    assert arm.recall_rate == 4 / 5


def test_an_empty_sweep_reports_zero_rather_than_dividing_by_zero():
    """A vault with no active titles reports 0%, not a crash.

    Anti-coincidental-pass: stated as its own test because the guard is a
    falsy check on ``total``, which is exactly the kind of branch that goes
    untested until it fires against a real corpus at an inconvenient moment.
    """
    arm = ArmResult(rendering="verbatim")

    assert arm.rank_1_rate == 0.0
    assert arm.recall_rate == 0.0


def test_rank_1_is_a_subset_of_recall_by_construction():
    """Ranking first entails being found, and the sweep counts them that way."""
    arm = ArmResult(rendering="verbatim", rank_1=2, recalled=2, total=4)

    assert arm.rank_1_rate <= arm.recall_rate


# ---------------------------------------------------------------------------
# The vector passthrough
# ---------------------------------------------------------------------------


def test_a_vector_read_back_as_its_text_literal_passes_through_whole():
    """The defect this guards is silent and total.

    Read over a connection with no pgvector type registered, an embedding
    arrives as ``'[0.1,0.2]'``. Materializing that with ``list`` yields a list
    of *characters*, which Postgres rejects -- and had it not rejected it, every
    seeded embedding in the measurement would have been garbage while the
    keyword figures still looked plausible.
    """
    literal = "[0.1,-0.2,0.3]"

    assert _as_embedding(literal) == literal
    assert _as_embedding(literal) != list(literal)


def test_a_real_sequence_is_materialized():
    """A driver that does hand back a sequence still yields a list."""
    assert _as_embedding((0.1, 0.2)) == [0.1, 0.2]


def test_an_absent_vector_stays_absent():
    """A passage with no embedding is not given one by the harness."""
    assert _as_embedding(None) is None
