"""Tests for the keyword-backend fidelity evaluation.

The harness compares LanceDB BM25 against Postgres ``ts_rank`` as the keyword arm
of hybrid retrieval, fusing each with the same vector arm through the production
RRF and comparing fused top-K. These tests pin the metrics on both sides of
every threshold, prove the harness fuses-then-compares (rather than comparing the
raw keyword lists), and -- behind a skip guard -- prove the Postgres arm orders
by relevance rather than physical row order.
"""

from __future__ import annotations

import os

import pytest

from sage.adapters.interfaces import SearchResult
from sage.utils.keyword_fidelity_eval import (
    REC_BORDERLINE,
    REC_MANAGED,
    REC_NATIVE,
    PostgresFTSBackend,
    _validate_config,
    _validate_schema,
    overlap_at_k,
    rbo,
    recommend,
    render_scorecard,
    run_fidelity_eval,
)


def _r(doc_id: str, score: float = 0.0) -> SearchResult:
    return SearchResult(document_id=doc_id, heading_path="body", content=doc_id, score=score)


def _ranking(*doc_ids: str) -> list[SearchResult]:
    return [_r(d) for d in doc_ids]


# --- metrics ---------------------------------------------------------------


def test_overlap_at_k() -> None:
    assert overlap_at_k(["d1", "d2", "d3"], ["d1", "d2", "d3"], 3) == 1.0
    assert overlap_at_k(["a", "b", "c"], ["x", "y", "z"], 3) == 0.0
    assert overlap_at_k(
        ["a", "b", "c", "d", "e", "f"], ["a", "b", "c", "x", "y", "z"], 6
    ) == pytest.approx(0.5)


def test_rbo_is_order_sensitive() -> None:
    # Identical conjoint lists -> exactly 1.0 (extrapolated RBO).
    assert rbo(["x", "y", "z"], ["x", "y", "z"]) == pytest.approx(1.0)
    # Same set, reversed: overlap@k would call this a perfect match; RBO must not.
    assert overlap_at_k(["x", "y"], ["y", "x"], 2) == 1.0
    assert rbo(["x", "y"], ["y", "x"], p=0.9) == pytest.approx(0.9)
    assert rbo(["x", "y"], ["y", "x"], p=0.9) < 1.0
    # Partial agreement (shared head, divergent tail): closed form 1 - 0.5*p.
    assert rbo(["x", "y"], ["x", "z"], p=0.9) == pytest.approx(1 - 0.5 * 0.9)


def test_recommend_thresholds() -> None:
    """Both metrics gate the verdict, on both sides of both thresholds."""
    assert recommend(0.95, 0.95) == REC_NATIVE
    assert recommend(0.90, 0.90) == REC_NATIVE  # boundary is inclusive
    assert recommend(0.80, 0.95) == REC_BORDERLINE
    assert recommend(0.95, 0.80) == REC_BORDERLINE  # the raw metric can hold it back
    assert recommend(0.75, 0.75) == REC_BORDERLINE  # >= low is not "managed"
    assert recommend(0.50, 0.95) == REC_MANAGED
    assert recommend(0.95, 0.50) == REC_MANAGED  # and it can escalate on its own


def test_recommend_escalates_when_the_unfused_arms_diverge() -> None:
    """The verdict reads the metric that discriminates the candidates.

    Fusing both keyword arms against a shared vector ranking masks an arm that
    returns nothing: the fused result degenerates to the vector result and
    agreement stays perfect. A recommendation keyed on fused output alone
    therefore cannot see the divergence the evaluation exists to detect, and
    would qualify a backend whose keyword semantics are inverted (CAS-ADR-048
    consequences).
    """
    assert recommend(1.0, 0.0) == REC_MANAGED, (
        "perfect fused agreement with zero unfused agreement is the masking "
        "case, not a passing grade"
    )


# --- fuse-then-compare -----------------------------------------------------


def test_run_fidelity_eval_fuses_then_compares() -> None:
    """Keyword arms differ, but fused top-K converges -> fusion masks the gap.

    The vector arm dominates; BM25 boosts a doc already in the top-5 (v2) and
    ts_rank boosts a different in-top-5 doc (v4). The fused top-5 SET is the same
    {v1..v5} for both arms even though the raw keyword arms share nothing --
    which is exactly why the fused figure cannot carry the verdict alone.
    """
    vector = _ranking("v1", "v2", "v3", "v4", "v5", "v6", "v7")
    bm25 = _ranking("v2")
    ts_rank = _ranking("v4")

    result = run_fidelity_eval(
        ["q"],
        vector_fn=lambda q: vector,
        bm25_fn=lambda q: bm25,
        ts_rank_fn=lambda q: ts_rank,
        k=5,
    )

    (c,) = result.per_query
    assert c.fused_overlap_at_k == pytest.approx(1.0)
    # The harness must report the raw divergence too, else it could be ignoring
    # the keyword arm and trivially reporting agreement everywhere.
    assert c.raw_keyword_overlap_at_k < 1.0
    assert c.raw_keyword_overlap_at_k == pytest.approx(0.0)
    assert result.recommendation == REC_MANAGED, (
        "the arms agree only after fusion; the recommendation must read the "
        "unfused comparison it already computes"
    )


def test_run_fidelity_eval_detects_divergence() -> None:
    """Keyword arms inject disjoint docs into the top-K -> low agreement.

    Both keyword arms outrank the vector tail and pull three of their own docs
    into the fused top-5; only the shared vector head (v1,v2,v3) survives in
    both, giving overlap@5 = 0.6 < low threshold -> managed.
    """
    vector = _ranking("v1", "v2", "v3", "v4", "v5")
    bm25 = _ranking("a1", "a2", "a3")
    ts_rank = _ranking("b1", "b2", "b3")

    result = run_fidelity_eval(
        ["q"],
        vector_fn=lambda q: vector,
        bm25_fn=lambda q: bm25,
        ts_rank_fn=lambda q: ts_rank,
        k=5,
    )

    (c,) = result.per_query
    assert c.fused_overlap_at_k == pytest.approx(0.6)
    assert result.recommendation == REC_MANAGED


# --- scorecard -------------------------------------------------------------


def test_render_scorecard_contains_required_sections() -> None:
    vector = _ranking("v1", "v2", "v3", "v4", "v5", "v6", "v7")
    result = run_fidelity_eval(
        ["alpha query", "beta query"],
        vector_fn=lambda q: vector,
        bm25_fn=lambda q: _ranking("v2"),
        ts_rank_fn=lambda q: _ranking("v4"),
        k=5,
    )
    card = render_scorecard(result)

    assert "# Keyword-backend fidelity scorecard" in card
    assert "## Aggregate" in card
    assert "overlap@5" in card
    assert "RBO" in card
    assert "## Raw vs fused" in card
    # The raw-vs-fused contrast: both the pre-fusion and post-fusion figures.
    assert "before fusion" in card.lower()
    assert "after rrf" in card.lower()
    # The AC-mandated narrative: RRF consumes rank order, not score magnitude.
    assert "Reciprocal Rank Fusion" in card
    assert "rank order" in card
    assert "## Per-query" in card
    assert "| Query |" in card
    assert "## Recommendation" in card
    assert "Escalate to a managed search service" in card, (
        "these arms agree only after fusion, so the rendered verdict must be "
        "the escalation the unfused comparison calls for"
    )
    # The raw-vs-fused narrative must not read a gap as reassurance: it is the
    # measure of how far fusion hides a divergent keyword arm.
    assert "not reassurance" in card, (
        "prose that presents the raw-to-fused gap as evidence for native FTS "
        "states the masking effect backwards"
    )


# --- Postgres pure helpers (no DB) -----------------------------------------


def test_postgres_helpers_pure() -> None:
    with pytest.raises(ValueError):
        _validate_config("'; DROP TABLE x; --")
    with pytest.raises(ValueError):
        _validate_schema("bad name!")


def test_postgres_backend_refuses_a_config_production_does_not_use() -> None:
    """A backend indexed differently from production measures something else.

    ``simple`` is an allowed text-search config, so it clears the injection
    allowlist -- but it neither drops stopwords nor stems, and a keyword arm
    built on it would be qualified against a corpus the substrate would never
    produce.
    """
    with pytest.raises(ValueError, match="does not match the production"):
        PostgresFTSBackend("postgresql://unused/db", config="simple")


# --- Postgres integration (gated) ------------------------------------------


def _eval_backend(schema: str) -> PostgresFTSBackend:
    """A harness backend over a throwaway schema on the suite's own Postgres.

    Gated on ``SAGE_TEST_PG_DSN`` rather than a separate variable: the arm now
    runs the production binding, so leaving it behind a gate the suite does not
    set would leave the qualification harness's only real coverage skipped in
    exactly the runs meant to catch drift in it.
    """
    pytest.importorskip("psycopg")
    dsn = os.environ.get("SAGE_TEST_PG_DSN")
    if not dsn:
        pytest.skip("set SAGE_TEST_PG_DSN to a throwaway Postgres to run the keyword arm")
    return PostgresFTSBackend(dsn, schema=schema)


def test_pg_ts_rank_orders_by_relevance() -> None:
    """ts_rank ranks by relevance, not insertion order, and excludes non-matches.

    Corpus insertion order is the *wrong* answer: docA (term once) is inserted
    before docB (term repeated). A backend missing ``ORDER BY ts_rank`` would
    return docA first. docC has no match and must be excluded by the
    ``tsv @@ query`` predicate.
    """
    backend = _eval_backend("eval_chunks_relevance")
    try:
        backend.index(
            [
                ("docA", "body", "the corpus mentions retrieval once here"),
                ("docB", "body", "retrieval retrieval retrieval saturates this retrieval chunk"),
                ("docC", "body", "an unrelated paragraph with no relevant terms"),
            ]
        )
        results = backend.search_bm25("retrieval", limit=3)
        ids = [r.document_id for r in results]

        assert ids[0] == "docB"
        assert "docA" in ids
        assert ids.index("docB") < ids.index("docA")
        assert "docC" not in ids
    finally:
        backend.close()


def test_pg_backend_answers_with_the_production_match_semantics() -> None:
    """The eval's keyword arm is the binding, not a restatement of it.

    A qualification harness that carries its own copy of the search query
    measures whatever that copy does. Here the arm is asked the one question
    that separates the contract from the behaviour it replaced: two chunks of
    one document, each holding one of the query's terms. Document-scoped
    conjunction matches it; the chunk-scoped query the harness used to restate
    does not, and a candidate backend judged against that restatement would be
    judged against a substrate that no longer exists.
    """
    backend = _eval_backend("eval_chunks_semantics")
    try:
        backend.index(
            [
                ("split", "S1", "alphaword only here"),
                ("split", "S2", "betaword only here"),
                ("partial", "S1", "alphaword only, with no partner term"),
            ]
        )

        ids = [r.document_id for r in backend.search_bm25("alphaword betaword", limit=10)]
        assert ids == ["split"], (
            "the terms span two chunks of one document, which the contract matches"
        )

        both = [r.document_id for r in backend.search_bm25("alphaword", limit=10)]
        assert set(both) == {"split", "partial"}, (
            "positive control: strictness, not an empty backend, culled the partial match"
        )
    finally:
        backend.close()
