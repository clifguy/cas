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

from sage.models.enums import SourceType
from sage.models.schemas import Document
from sage.services.document_surface import compose_document_surface
from scripts.measure_title_rank import (
    _DOCUMENT_COLUMNS,
    ArmResult,
    _as_embedding,
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


def _record(title: str, tags: list[str] | None = None) -> Document:
    """One vault record, with the fields the composition reads set for real."""
    now = datetime.now(timezone.utc)
    return Document(
        id="0000000a_doc",
        title=title,
        source_type=SourceType.MARKDOWN,
        source_path="imports/0000000a_doc.md",
        lifecycle_status="active",
        source_content_hash=f"sha256:{0:064x}",
        adapter_version="1",
        created_by="t",
        created_at=now,
        last_modified_by="t",
        updated_at=now,
        doc_type="adr",
        project="CAS",
        tags=tags or ["retrieval"],
        semantic_abstract="Generated summary.",
    )


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
    """
    copied = ("0000000a_doc", "graphLevel", "stale orienting", None, "adr", "active", "CAS")

    row = _surface_row(copied, _record("graphLevel"), recompose=True)

    assert {"graph", "level"} <= set(row.matchable.lower().split())
    assert "graphLevel" in row.matchable, "the unsplit form must survive the widening"
    assert row.matchable != copied[1], "the stored text was seeded despite the flag"


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
    settled = compose_document_surface("b", records["b"]).matchable

    rewritten = ("a", "graphLevel", "o", None, "adr", "active", "CAS")
    reproduced = ("b", settled, "o", None, "adr", "active", "CAS")

    assert _recomposition_control([rewritten, reproduced], records) == (1, 2)


def test_the_control_reports_nothing_rewritten_when_seeding_verbatim():
    """A default run rewrites no row, and says so rather than implying it did."""
    copied = ("a", "graphLevel", "o", None, "adr", "active", "CAS")

    assert _recomposition_control([copied], None) == (0, 1)
