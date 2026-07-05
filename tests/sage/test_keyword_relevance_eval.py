"""Tests for the keyword-backend *relevance* evaluation.

The relevance layer grades the *absolute* answer quality of each keyword arm
(vector-only, LanceDB-BM25-fused, Postgres-``ts_rank``-fused) against a judged,
keyword-skewed gold set, rather than measuring divergence between the two arms.
These tests pin the graded-relevance metrics (NDCG@k) and rank-of-target metrics
(MRR, success@k) on both sides of every boundary, prove the evaluation scores the
*fused* output (not the raw keyword list), pin the go/no-go banding, and prove the
gold-target pinning forces a target into the disposable slice even when the
sampler would have omitted it.
"""

from __future__ import annotations

import math

import pytest

from sage.adapters.interfaces import SearchResult
from sage.utils.keyword_fidelity_eval import (
    ARM_BM25,
    ARM_TSRANK,
    ARM_VECTOR,
    GRADE_PRIMARY,
    GRADE_SECONDARY,
    REC_BORDERLINE,
    REC_MANAGED,
    REC_NATIVE,
    ArmRelevance,
    GoldQuery,
    dcg_at_k,
    ndcg_at_k,
    parse_gold_entry,
    reciprocal_rank,
    recommend_relevance,
    render_relevance_scorecard,
    run_relevance_eval,
    success_at_k,
)


def _r(doc_id: str, score: float = 0.0) -> SearchResult:
    return SearchResult(document_id=doc_id, heading_path="body", content=doc_id, score=score)


def _ranking(*doc_ids: str) -> list[SearchResult]:
    return [_r(d) for d in doc_ids]


# --- graded-relevance metric primitives ------------------------------------


def test_dcg_and_ndcg_reward_rank_position() -> None:
    """A relevant doc earns more NDCG the higher it ranks; absence scores 0.

    The log discount is the whole point: rank 1 must beat rank 3, and a ranking
    with none of the relevant docs in the top-K scores exactly 0.0.
    """
    gains = {"t": GRADE_PRIMARY}  # one relevant doc, grade 2

    ndcg_rank1 = ndcg_at_k(["t", "x", "y"], gains, 3)
    ndcg_rank3 = ndcg_at_k(["x", "y", "t"], gains, 3)

    assert ndcg_rank1 == pytest.approx(1.0)  # ideal placement
    # rank 3: DCG = 2 / log2(4) = 1.0; IDCG = 2 / log2(2) = 2.0 -> 0.5
    assert ndcg_rank3 == pytest.approx(0.5)
    assert ndcg_rank3 < ndcg_rank1
    # no relevant doc in the ranking -> 0.0 (not undefined, not 1.0)
    assert ndcg_at_k(["x", "y", "z"], gains, 3) == 0.0
    # DCG is the unnormalized numerator: grade-2 doc at rank 1 -> 2.0
    assert dcg_at_k(["t"], gains, 3) == pytest.approx(2.0)


def test_ndcg_graded_gains() -> None:
    """NDCG uses graded gains, not binary: grade-2 ahead of grade-1 wins.

    Two relevant docs (grades 2 and 1). Ordering the higher grade first is the
    ideal (NDCG 1.0); swapping them must score strictly less, at the
    hand-computed ratio.
    """
    gains = {"a": GRADE_PRIMARY, "b": GRADE_SECONDARY}

    assert ndcg_at_k(["a", "b"], gains, 3) == pytest.approx(1.0)

    idcg = GRADE_PRIMARY / math.log2(2) + GRADE_SECONDARY / math.log2(3)
    dcg_ba = GRADE_SECONDARY / math.log2(2) + GRADE_PRIMARY / math.log2(3)
    ndcg_ba = ndcg_at_k(["b", "a"], gains, 3)

    assert ndcg_ba == pytest.approx(dcg_ba / idcg)
    assert ndcg_ba < 1.0


def test_reciprocal_rank() -> None:
    """RR is 1/(rank of the first relevant doc), or 0 when none appears."""
    assert reciprocal_rank(["t", "x", "y"], {"t"}) == pytest.approx(1.0)
    assert reciprocal_rank(["x", "t", "y"], {"t"}) == pytest.approx(0.5)
    assert reciprocal_rank(["x", "y", "z"], {"t"}) == 0.0


def test_success_at_k_boundary() -> None:
    """success@k flips exactly at the k boundary (off-by-one guard).

    Target at rank 4: success@4 is a hit, success@3 is a miss. Both assertions
    are required so neither a ``< k`` nor a ``<= k`` off-by-one passes.
    """
    ranking = ["a", "b", "c", "t"]  # t at rank 4
    assert success_at_k(ranking, {"t"}, 4) == 1.0
    assert success_at_k(ranking, {"t"}, 3) == 0.0


# --- gold-set model + parsing ----------------------------------------------


def test_gold_loader_parses_and_defaults() -> None:
    """A well-formed entry parses; primary is grade 2, secondaries grade 1."""
    gq = parse_gold_entry(
        {"query": "CAS-ADR-042", "primary": "imports/x.md", "relevant": ["imports/y.md"]}
    )
    assert gq.query == "CAS-ADR-042"
    assert gq.primary_id == "imports/x.md"
    assert gq.relevant_ids == ["imports/y.md"]
    assert gq.gains() == {"imports/x.md": GRADE_PRIMARY, "imports/y.md": GRADE_SECONDARY}
    assert gq.relevant_set() == {"imports/x.md", "imports/y.md"}

    # `relevant` omitted -> just the primary target
    gq2 = parse_gold_entry({"query": "regconfig", "primary": "imports/z.md"})
    assert gq2.relevant_ids == []
    assert gq2.gains() == {"imports/z.md": GRADE_PRIMARY}


def test_gold_loader_rejects_missing_primary() -> None:
    """A stale/typo'd entry fails loudly rather than silently scoring zero."""
    with pytest.raises(ValueError):
        parse_gold_entry({"query": "no primary here"})
    with pytest.raises(ValueError):
        parse_gold_entry({"primary": "imports/x.md"})  # missing query


# --- aggregation + fuse-then-score (the core) ------------------------------


def test_run_relevance_eval_scores_each_arm() -> None:
    """Each arm is scored separately; the per-query breakdown is retained.

    Vector misses the target; LanceDB BM25 surfaces it (fused -> rank 2);
    ts_rank surfaces an unrelated doc. So only the BM25-fused arm should score.
    """
    gold = [GoldQuery(query="q", primary_id="t", relevant_ids=[])]

    result = run_relevance_eval(
        gold,
        vector_fn=lambda q: _ranking("a", "b"),
        bm25_fn=lambda q: _ranking("t"),
        ts_rank_fn=lambda q: _ranking("c"),
        k=2,
    )

    assert set(result.arms) == {ARM_VECTOR, ARM_BM25, ARM_TSRANK}

    bm = result.arms[ARM_BM25]
    assert bm.mean_success_at_k == 1.0
    assert bm.mean_reciprocal_rank == pytest.approx(0.5)  # target fused to rank 2
    assert bm.mean_ndcg_at_k == pytest.approx(1.0 / math.log2(3))  # 2/log2(3) / 2.0

    assert result.arms[ARM_TSRANK].mean_success_at_k == 0.0
    assert result.arms[ARM_VECTOR].mean_success_at_k == 0.0

    assert len(result.per_query) == 1
    q0 = result.per_query[0]
    assert q0.query == "q"
    assert set(q0.per_arm) == {ARM_VECTOR, ARM_BM25, ARM_TSRANK}
    assert q0.per_arm[ARM_BM25]["success"] == 1.0


def test_relevance_eval_fuses_not_raw_keyword_arm() -> None:
    """The eval scores the FUSED output, not the raw keyword list.

    The raw ts_rank arm does not contain the target at all, but the vector arm
    ranks it first. After fusion the target is rank 1, so the ts_rank-fused arm
    must score perfectly. An implementation that graded the raw ts_rank list
    would report 0 here -- the central correctness trap this test rules out.
    """
    gold = [GoldQuery(query="q", primary_id="t", relevant_ids=[])]

    result = run_relevance_eval(
        gold,
        vector_fn=lambda q: _ranking("t"),
        bm25_fn=lambda q: _ranking("t"),
        ts_rank_fn=lambda q: _ranking("a", "b", "c"),  # raw ts_rank: no target
        k=3,
    )

    ts = result.arms[ARM_TSRANK]
    assert ts.mean_ndcg_at_k == pytest.approx(1.0)
    assert ts.mean_reciprocal_rank == pytest.approx(1.0)
    assert ts.mean_success_at_k == 1.0


# --- go/no-go banding ------------------------------------------------------


def _arm(arm: str, ndcg: float, rr: float, success: float) -> ArmRelevance:
    return ArmRelevance(
        arm=arm,
        mean_ndcg_at_k=ndcg,
        mean_reciprocal_rank=rr,
        mean_success_at_k=success,
    )


def test_recommend_relevance_native_when_tsrank_matches_bm25() -> None:
    """ts_rank within tolerance of BM25 on NDCG and success -> keep native."""
    bm25 = _arm(ARM_BM25, ndcg=0.80, rr=0.70, success=1.0)
    ts = _arm(ARM_TSRANK, ndcg=0.78, rr=0.65, success=1.0)  # max gap 0.02
    assert recommend_relevance(ts, bm25) == REC_NATIVE


def test_recommend_relevance_escalate_when_tsrank_degrades() -> None:
    """A material absolute regression escalates; a middling gap is borderline."""
    bm25 = _arm(ARM_BM25, ndcg=0.80, rr=0.70, success=1.0)

    ts_bad = _arm(ARM_TSRANK, ndcg=0.55, rr=0.40, success=0.60)  # gap 0.40
    assert recommend_relevance(ts_bad, bm25) == REC_MANAGED

    ts_border = _arm(ARM_TSRANK, ndcg=0.73, rr=0.66, success=0.95)  # max gap 0.07
    assert recommend_relevance(ts_border, bm25) == REC_BORDERLINE


# --- scorecard render ------------------------------------------------------


def test_render_relevance_scorecard_sections() -> None:
    gold = [GoldQuery("alpha", "t", []), GoldQuery("beta", "u", [])]
    result = run_relevance_eval(
        gold,
        vector_fn=lambda q: _ranking("t", "u", "x"),
        bm25_fn=lambda q: _ranking("t"),
        ts_rank_fn=lambda q: _ranking("u"),
        k=3,
    )
    card = render_relevance_scorecard(result)

    assert "# Keyword-backend relevance scorecard" in card
    assert "NDCG@3" in card
    assert "MRR" in card
    assert "success@3" in card
    assert "## Recommendation" in card
    assert "## Per-query" in card
    # all three arms labelled in the aggregate
    lower = card.lower()
    assert "vector" in lower
    assert "bm25" in lower
    assert "ts_rank" in lower
    # the keyword-skew of the gold set is stated explicitly
    assert "keyword" in lower
