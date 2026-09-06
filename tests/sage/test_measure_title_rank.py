"""The title-rank instrument's own arithmetic and framing.

CAS-ADR-049 Decision 8 is a standing bound, so the instrument that checks it
outlives the change that introduced it -- and an instrument that miscounts is
worse than none, because its output is what a later reader will trust instead of
re-deriving. These cover the parts that decide what the report claims: the
renderings swept, the rates, and the vector passthrough that a naive
materialization silently corrupts.

The database-touching halves are exercised by running the script; what is pinned
here is everything that could go wrong without a server to notice.
"""

from __future__ import annotations

from scripts.measure_title_rank import ArmResult, _as_embedding, _renderings

# ---------------------------------------------------------------------------
# The renderings the bound is defined over
# ---------------------------------------------------------------------------


def test_the_sweep_covers_the_renderings_the_decision_names():
    """Case, separators and word boundaries -- not the verbatim title alone.

    Decision 8 holds "however a caller types the separators, the case, and the
    word boundaries of the title", so a sweep over the verbatim form would
    report on a narrower guarantee than the one being claimed.
    """
    renderings = _renderings("ADR-049: Document-Level Text")

    assert renderings["verbatim"] == "ADR-049: Document-Level Text"
    assert renderings["lowercase"] == "adr-049: document-level text"
    assert renderings["uppercase"] == "ADR-049: DOCUMENT-LEVEL TEXT"
    assert renderings["separators folded"] != renderings["verbatim"], (
        "the folded rendering must differ from the raw title, or the sweep "
        "reports four columns of the same measurement"
    )


def test_every_rendering_is_measured_separately():
    """One column per rendering, so a form that regressed cannot be averaged away."""
    assert len(_renderings("Some Title")) == 4
    assert len(set(_renderings("ADR-001: Alpha Beta").values())) > 1


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
