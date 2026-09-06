"""Tests for the extracted Reciprocal Rank Fusion helper.

``rrf_fuse`` was lifted out of ``RetrievalService._hybrid_rrf`` so the keyword
backend fidelity harness can fuse alternative keyword arms through the identical
formula. These tests pin the exact fused scores (so a silent change to ``k`` or
the ``+1`` offset is caught), prove the fusion ranks by *rank position* rather
than raw score magnitude (the central premise of the native-FTS-vs-managed
evaluation), and assert the production method is a pass-through to the helper.
"""

from __future__ import annotations

import pytest

from sage.adapters.interfaces import SearchResult
from sage.services.retrieval import RetrievalService
from sage.utils.rrf import DEFAULT_RRF_K, rrf_fuse

K = DEFAULT_RRF_K  # 60


def _result(doc_id: str, score: float = 0.0, heading_path: str = "body") -> SearchResult:
    return SearchResult(
        document_id=doc_id,
        heading_path=heading_path,
        content=f"content of {doc_id}",
        score=score,
    )


def test_rrf_fuse_hand_computed_scores() -> None:
    """Fused scores equal the hand-computed 1/(k+rank+1) sums and sort order.

    vector = [d1, d2, d3], keyword = [d2, d4]. d2 appears in both lists so its
    contributions accumulate; the remaining docs each contribute from a single
    list. The expected order is purely a consequence of those sums.
    """
    vector = [_result("d1"), _result("d2"), _result("d3")]
    keyword = [_result("d2"), _result("d4")]

    fused = rrf_fuse(vector, keyword, limit=10)

    expected = {
        "d1": 1 / (K + 0 + 1),  # vector rank 0
        "d2": 1 / (K + 1 + 1) + 1 / (K + 0 + 1),  # vector rank 1 + keyword rank 0
        "d3": 1 / (K + 2 + 1),  # vector rank 2
        "d4": 1 / (K + 1 + 1),  # keyword rank 1
    }
    # Order: d2 (two contributions) > d1 (1/61) > d4 (1/62) > d3 (1/63).
    assert [r.document_id for r in fused] == ["d2", "d1", "d4", "d3"]
    for r in fused:
        assert r.score == pytest.approx(expected[r.document_id])


def test_rrf_fuse_keys_on_the_document_not_the_chunk() -> None:
    """The two arms must fuse on the entity they both rank: the document.

    The arms rank different things. The semantic arm returns one row per chunk,
    while the keyword arm's match unit is the document and it returns one row
    per document. Keyed on ``(document_id, heading_path)`` the two would almost
    never collide, so a document both arms agree on would receive one
    contribution instead of two and fusion would degenerate into an interleave
    of two independent rankings.

    Here ``d1`` holds the vector arm's ranks 0 and 1 and the keyword arm's rank
    0. It must appear once, scored from its *best* rank in each arm, so it
    outranks ``d2`` -- which a chunk-keyed fusion would not guarantee, because
    ``d1``'s two chunks would split its contributions across two keys.
    """
    vector = [_result("d1", heading_path="S1"), _result("d1", heading_path="S2"), _result("d2")]
    keyword = [_result("d1", heading_path="S2")]

    fused = rrf_fuse(vector, keyword, limit=10)

    assert [r.document_id for r in fused] == ["d1", "d2"], "one entry per document"
    assert fused[0].score == pytest.approx(1 / (K + 0 + 1) + 1 / (K + 0 + 1)), (
        "best rank from each arm, accumulated -- not the chunk's own rank"
    )
    assert fused[0].matched_chunk_count == 2, (
        "collapsing chunks must carry how many the document contributed, or the "
        "reranking signal downstream is silently lost"
    )


def test_rrf_fuse_uses_rank_not_score() -> None:
    """Fusion ranks by list position, ignoring the per-result score magnitude.

    The keyword list is ordered best-first but its *scores* are inverted: the
    rank-0 element carries a tiny score and the rank-1 element a huge one. A
    fusion that summed raw scores (the exact mistake this evaluation exists to
    rule out) would put the huge-score element first. Correct RRF keeps the
    rank-0 element ahead, scored 1/61 vs 1/62.
    """
    vector: list[SearchResult] = []
    keyword = [_result("k_first", score=0.01), _result("k_last", score=99.0)]

    fused = rrf_fuse(vector, keyword, limit=10)

    assert [r.document_id for r in fused] == ["k_first", "k_last"]
    assert fused[0].score == pytest.approx(1 / (K + 0 + 1))  # 1/61, the larger
    assert fused[1].score == pytest.approx(1 / (K + 1 + 1))  # 1/62, the smaller
    assert fused[0].score > fused[1].score


class _FixedContentStore:
    """Minimal content store returning fixed vector and BM25 lists.

    Deliberately not a full ``ContentStore`` subclass: ``_hybrid_rrf`` touches
    only ``search_semantic`` and ``search_bm25``, so a fake exposing just those
    two keeps the delegation test focused.
    """

    def __init__(self, vector: list[SearchResult], bm25: list[SearchResult]) -> None:
        self._vector = vector
        self._bm25 = bm25

    async def search_semantic(self, query_embedding, limit, filters=None):  # noqa: ANN001
        return self._vector[:limit]

    async def search_bm25(self, query, limit, filters=None):  # noqa: ANN001
        return self._bm25[:limit]


async def test_hybrid_rrf_delegates_unchanged() -> None:
    """``RetrievalService._hybrid_rrf`` is a pass-through to ``rrf_fuse``.

    Guards the refactor: the production method must fetch both arms and fuse
    them through the shared helper with no behavioural drift. Inputs include a
    document (``b``) present in both arms so accumulation is exercised.
    """
    vector = [_result("a"), _result("b")]
    bm25 = [_result("b"), _result("c")]
    store = _FixedContentStore(vector, bm25)
    service = RetrievalService(
        graph_store=None,  # type: ignore[arg-type]
        content_store=store,  # type: ignore[arg-type]
        embedding_provider=None,  # type: ignore[arg-type]
        config=None,  # type: ignore[arg-type]
    )

    fused = await service._hybrid_rrf([0.0] * 4, "query", limit=10, filters=None)
    expected = rrf_fuse(vector, bm25, limit=10)

    assert [(r.document_id, r.heading_path) for r in fused] == [
        (r.document_id, r.heading_path) for r in expected
    ]
    for got, want in zip(fused, expected, strict=True):
        assert got.score == pytest.approx(want.score)


# ---------------------------------------------------------------------------
# A document-level row is not a passage (CAS-ADR-049 Decision 5)
# ---------------------------------------------------------------------------


def _surface_result(doc_id: str, score: float = 0.0) -> SearchResult:
    """A document-level row as a binding emits one: no excerpt, no passages."""
    return SearchResult(
        document_id=doc_id,
        heading_path="",
        content="",
        score=score,
        matched_chunk_count=0,
        is_document_surface=True,
    )


def test_rrf_does_not_count_a_surface_row_as_a_passage() -> None:
    """Fusion counts passages, and a document-level row is not one.

    The fusion tallies the distinct passages a document contributed across both
    arms. A document-level row carries an empty heading path, so a tally keyed
    on that path counts it as a passage of its own -- inflating the count of a
    document that matched partly through its surface, and inventing a count of
    one for a document that matched through nothing else.
    """
    vector = [_surface_result("d1"), _result("d1", heading_path="S1")]
    keyword = [_result("d1", heading_path="S2")]

    [fused] = rrf_fuse(vector, keyword, limit=10)

    assert fused.matched_chunk_count == 2, "the document-level row was tallied as a third passage"
    assert fused.is_document_surface is False, (
        "a document that contributed real passages is not a document-level hit"
    )


def test_rrf_marks_a_document_that_matched_only_through_its_surface() -> None:
    """A document with no passage in either arm stays a document-level hit.

    The representative row a fusion carries forward is the first one seen, so
    reading the flag off that row alone would report whichever arm happened to
    rank first. The question is about the document: every row it contributed
    was a document-level row, so the fused row is one too, and it counts no
    passages.
    """
    vector = [_surface_result("d1")]
    keyword = [_surface_result("d1")]

    [fused] = rrf_fuse(vector, keyword, limit=10)

    assert fused.is_document_surface is True
    assert fused.matched_chunk_count == 0, "a document-level hit counts no passages"


def test_rrf_keeps_the_larger_count_across_arms() -> None:
    """The passage count survives fusion when one arm reports it in bulk.

    The keyword arm's match unit is the document: it returns one row carrying
    the count of passages that matched, rather than one row per passage. Fusing
    that against a vector arm's single row must not discard the larger number
    just because the row it carried forward came from the other arm.
    """
    vector = [_result("d1", heading_path="S1")]
    keyword = [_result("d1", heading_path="S1")]
    keyword[0].matched_chunk_count = 3

    [fused] = rrf_fuse(vector, keyword, limit=10)

    assert fused.matched_chunk_count == 3, (
        "the keyword arm's passage count was discarded by the fusion"
    )


def test_rrf_carries_a_passage_excerpt_when_the_surface_outranks_it() -> None:
    """A document that matched passages is returned carrying one of them.

    A document surface out-ranking the document's own passages is what the
    surface exists to do for a title-shaped query, so the highest-ranked row
    for such a document is routinely the one that carries no excerpt. Taking
    it as the representative reports a document as having matched two
    passages while showing neither -- an empty excerpt and no heading beside
    a count of two.
    """
    vector = [_surface_result("d1"), _result("d1", heading_path="S1")]
    keyword = [_result("d1", heading_path="S2")]

    [fused] = rrf_fuse(vector, keyword, limit=10)

    assert fused.heading_path == "S1", "the surface row was carried forward"
    assert fused.content == "content of d1"
    assert fused.matched_chunk_count == 2
    assert fused.is_document_surface is False


def test_rrf_keeps_the_surface_row_when_the_document_has_no_passage() -> None:
    """Preferring a passage must not invent one.

    The rival to the fix above: a rule that always looks past a surface row
    would leave a document that matched through nothing else with no
    representative at all, or with a passage belonging to another document.
    """
    vector = [_surface_result("d1")]
    keyword = [_surface_result("d1")]

    [fused] = rrf_fuse(vector, keyword, limit=10)

    assert fused.is_document_surface is True
    assert fused.heading_path == ""
    assert fused.content == ""


def test_rrf_counts_a_headingless_passage_as_a_passage() -> None:
    """The fusion tests the flag, not the empty heading path.

    A document with no headings contributes a passage whose heading path is
    empty, which is the one input that separates the flag from the heuristic.
    A tally reading the heading path classifies it as document-level: no
    passage counted, and the fused row marked as carrying none.
    """
    [fused] = rrf_fuse([_result("d1", heading_path="")], [], limit=10)

    assert fused.matched_chunk_count == 1, "a headingless passage was not counted"
    assert fused.is_document_surface is False, (
        "a headingless passage was classified as a document-level row"
    )


def test_rrf_does_not_displace_a_passage_row_with_a_later_surface_row() -> None:
    """Displacement runs one way only.

    The rule has two halves -- a passage displaces a held surface row, and
    nothing displaces a passage. A rival that swaps on any change of flag
    satisfies the first and breaks the second, which is invisible while every
    fixture puts the surface first. Here the passage arrives first, which is
    the ordinary order for a body-shaped query.
    """
    vector = [_result("d1", heading_path="S1"), _surface_result("d1")]

    [fused] = rrf_fuse(vector, [], limit=10)

    assert fused.heading_path == "S1", "a later surface row displaced the passage"
    assert fused.content == "content of d1"
    assert fused.is_document_surface is False
