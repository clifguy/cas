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

from datetime import datetime, timezone

from sage.models.schemas import Document
from sage.services.document_surface import compose_document_surface
from scripts.measure_title_rank import (
    _DOCUMENT_COLUMNS,
    ArmResult,
    _as_embedding,
    _document_from_row,
    _recomposition_control,
    _render,
    _renderings,
    _surface_row,
)

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


# ---------------------------------------------------------------------------
# Which side of the binding a run can see
# ---------------------------------------------------------------------------


def _row(title: str, **overrides) -> dict:
    """One `documents` row, in the shape a run projects it.

    Built as a column mapping rather than as a record, so the tests below reach
    the script through ``_document_from_row`` the way a run does. A record
    constructed by hand would exercise the composition while leaving the
    row-to-record step -- the one that meets whatever a real vault stores --
    untested.
    """
    now = datetime.now(timezone.utc).isoformat()
    row = {
        "id": "0000000a_doc",
        "title": title,
        "source_type": "markdown",
        "source_path": "imports/0000000a_doc.md",
        "lifecycle_status": "active",
        "version_label": None,
        "project": "CAS",
        "tags": ["retrieval"],
        "authority_scope": None,
        "doc_type": "adr",
        "source_content_hash": f"sha256:{0:064x}",
        "adapter_version": "1",
        "created_by": "t",
        "created_at": now,
        "last_modified_by": "t",
        "updated_at": now,
        "semantic_abstract": "Generated summary.",
    }
    return row | overrides


def _record(title: str, **overrides) -> Document:
    """One vault record, hydrated the way a run hydrates one."""
    return _document_from_row(_row(title, **overrides))


class _AttributeRecorder:
    """A record that remembers which of its fields were read."""

    def __init__(self, wrapped: Document) -> None:
        self._wrapped = wrapped
        self.read: set[str] = set()

    def __getattr__(self, name: str):
        self.read.add(name)
        return getattr(self._wrapped, name)


def test_the_projection_covers_every_field_the_composition_reads():
    """The columns a run selects are asked of the composition, not of a list.

    Recomposing a surface hands the production composition a record built from
    the columns this script projects, so a field the composition reads and the
    projection omits would arrive absent -- and the run would report figures
    for text that quietly lost a half. Reading the requirement off the
    composition itself is what keeps the two from drifting: a later edit that
    reaches for a new field reds here and names it, rather than being caught by
    whoever next compares two reports.
    """
    recorder = _AttributeRecorder(_record("Graph Level"))

    compose_document_surface("0000000a_doc", recorder)  # type: ignore[arg-type]

    assert recorder.read, "the recorder saw no field, so it is not in the call path"
    missing = recorder.read - set(_DOCUMENT_COLUMNS)
    assert not missing, f"the composition reads {sorted(missing)}, which no column supplies"


def test_seeding_verbatim_carries_the_vault_text_through_unchanged():
    """The default arm measures the state a deployed vault is actually in.

    Its stored text may predate any number of changes to the composition, and
    reproducing it exactly is the whole point: this is the arm that says what a
    caller querying the live vault would get today.

    The record is handed in and deliberately ignored, so the first assertion
    below is what keeps it from being scenery: without it the record could be
    dropped entirely and this would still pass, proving only that nothing was
    available to recompose from rather than that the flag suppressed a
    recomposition that had something to say.
    """
    record = _record("Graph Level")
    copied = ("0000000a_doc", "stale matchable", "stale orienting", None, "adr", "active", "CAS")

    assert compose_document_surface("0000000a_doc", record).matchable != copied[1], (
        "the record composes to the stored text, so ignoring it would be unobservable"
    )

    row = _surface_row(copied, record, recompose=False)

    assert row.matchable == "stale matchable"
    assert row.orienting == "stale orienting"


def test_recomposing_replaces_the_stored_text_with_what_the_record_composes():
    """The arm that can see an index-side change.

    The stored text here carries the compound whole, which is what a vault
    ingested under a narrower split holds; the recomposition carries its parts
    too. A run that ignored the flag would seed the first in both arms and
    report a tie whatever the transform did.

    Both halves are asserted, and the derived one is not decoration. A seeding
    that recomposed the authored half and kept the stale derived half is the
    mirror of the control-side defect the failure log already carries: the
    control would report a row rewritten while the row actually seeded still
    held the old stem expansion -- the ranking input the flag exists to move --
    and the run would report a difference it had not made.
    """
    record = _record("graphLevel", source_path="imports/graphLevel.md")
    composed = compose_document_surface("0000000a_doc", record)
    copied = ("0000000a_doc", "graphLevel", "stale orienting", None, "adr", "active", "CAS")

    row = _surface_row(copied, record, recompose=True)

    assert {"graph", "level"} <= set(row.matchable.lower().split())
    assert "graphLevel" in row.matchable, "the unsplit form must survive the widening"
    assert row.matchable != copied[1], "the stored text was seeded despite the flag"
    assert row.orienting == composed.orienting, "the derived half kept its stale text"
    assert row.orienting != copied[2], "the derived half must have moved, or nothing is tested"


def test_the_recomposed_vector_is_the_stored_one():
    """Recomposing text does not re-embed the corpus.

    A vector rebuilt here would be a claim about the semantic arm that this
    instrument does not measure and could not afford to make; the keyword
    figures it does report never read one.
    """
    vector = "[0.1,-0.2]"
    copied = ("0000000a_doc", "graphLevel", "o", vector, "adr", "active", "CAS")

    assert _surface_row(copied, _record("graphLevel"), recompose=True).embedding == vector


def test_the_report_names_which_state_it_measured():
    """Two runs that disagree must be distinguishable after the fact.

    The figures from a verbatim run and a recomposed one answer different
    questions, and a saved report carries no other evidence of which it is.
    """

    def render(recomposed: bool) -> str:
        return _render("cas", 2, 1, 0.5, {}, {}, (0, 1), (1, 1), recomposed=recomposed)

    assert render(True) != render(False)
    assert "recomposed" in render(True)
    assert "as the vault stores them" in render(False)


def test_the_recomposition_control_counts_the_rows_it_rewrote():
    """A recomposed run that rewrote nothing had nothing to see.

    The flag's own control, in the shape the arms already have. Its absence is
    the same trap one level up: a corpus the composition reproduces exactly
    reports the same tie as a corpus it changed, and only this count separates
    them.
    """
    records = {"a": _record("graphLevel"), "b": _record("Graph Level")}
    # The row the composition reproduces is built by asking it, not by writing
    # out what it is thought to emit: a hand-copied string that drifts turns a
    # reproduced row into a rewritten one and inflates the very count under test.
    # Both halves, because the control compares both -- a row reproducing only
    # the authored half is a rewritten row, which is the point of the sibling
    # test below it.
    settled = compose_document_surface("b", records["b"])

    rewritten = ("a", "graphLevel", "o", None, "adr", "active", "CAS")
    reproduced = ("b", settled.matchable, settled.orienting, None, "adr", "active", "CAS")

    assert _recomposition_control([rewritten, reproduced], records) == (1, 2)


def test_the_control_reports_nothing_rewritten_when_seeding_verbatim():
    """A default run rewrites no row, and says so rather than implying it did."""
    copied = ("a", "graphLevel", "o", None, "adr", "active", "CAS")

    assert _recomposition_control([copied], None) == (0, 1)


def test_a_row_whose_tags_column_is_null_still_hydrates():
    """`documents.tags` is nullable; `Document.tags` is not.

    The store's own hydrator coalesces the null away, and a run that reaches a
    vault holding one must do the same or abort before producing any figure --
    the script takes an arbitrary vault, so the row it cannot hydrate is not
    hypothetical. Asserted as an empty list rather than merely "does not
    raise", because a hydrator that dropped the field entirely would also not
    raise and would then compose a surface missing its tags half.
    """
    record = _document_from_row(_row("Graph Level", tags=None))

    assert record.tags == []


def test_the_control_sees_a_rewrite_of_the_derived_half():
    """A surface has two halves and the recomposition rewrites both.

    `orienting` carries the source-filename stem and its expansion, which is
    exactly where a two-word lowerCamel compound tends to live, and it ranks --
    so a row rewritten only there has had a ranking input changed. A control
    reading `matchable` alone reports that row as untouched, which is the
    "nothing to see" reading the flag exists to distinguish from a measured
    tie. The fixture differs in the derived half and nowhere else, so a control
    that ignores it cannot pass.
    """
    record = _record("Graph Level", source_path="imports/graphLevel.md")
    settled = compose_document_surface("0000000a_doc", record)
    derived_only = (
        "0000000a_doc",
        settled.matchable,
        "stale orienting",
        None,
        "adr",
        "active",
        "CAS",
    )

    assert settled.matchable == derived_only[1], "the authored half must be identical"
    assert settled.orienting != derived_only[2], (
        "the derived half must differ, or nothing is tested"
    )
    assert _recomposition_control([derived_only], {"0000000a_doc": record}) == (1, 1)


def test_the_control_sees_a_rewrite_of_the_authored_half():
    """And the mirror, so neither half can be the only one read.

    The sibling above is satisfied by a control reading `orienting` alone, just
    as the original was satisfied by one reading `matchable` alone -- swapping
    which half is ignored is the obvious way to reintroduce the same defect.
    This row differs in the authored half and nowhere else, so the two together
    admit only a control that reads both.
    """
    record = _record("Graph Level")
    settled = compose_document_surface("0000000a_doc", record)
    authored_only = (
        "0000000a_doc",
        "stale matchable",
        settled.orienting,
        None,
        "adr",
        "active",
        "CAS",
    )

    assert settled.orienting == authored_only[2], "the derived half must be identical"
    assert settled.matchable != authored_only[1], "the authored half must differ"
    assert _recomposition_control([authored_only], {"0000000a_doc": record}) == (1, 1)
