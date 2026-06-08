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
    _build_index_sql,
    _build_search_sql,
    _row_to_result,
    _validate_config,
    _validate_table,
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
    assert recommend(0.95) == REC_NATIVE
    assert recommend(0.90) == REC_NATIVE  # boundary is inclusive
    assert recommend(0.80) == REC_BORDERLINE
    assert recommend(0.75) == REC_BORDERLINE  # >= low is not "managed"
    assert recommend(0.50) == REC_MANAGED


# --- fuse-then-compare -----------------------------------------------------


def test_run_fidelity_eval_fuses_then_compares() -> None:
    """Keyword arms differ, but fused top-K converges -> high agreement.

    The vector arm dominates; BM25 boosts a doc already in the top-5 (v2) and
    ts_rank boosts a different in-top-5 doc (v4). The fused top-5 SET is the same
    {v1..v5} for both arms even though the raw keyword arms share nothing.
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
    assert result.recommendation == REC_NATIVE
    # The harness must report the raw divergence too, else it could be ignoring
    # the keyword arm and trivially reporting agreement everywhere.
    assert c.raw_keyword_overlap_at_k < 1.0
    assert c.raw_keyword_overlap_at_k == pytest.approx(0.0)


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
    assert "native Postgres `ts_rank`" in card  # the go/no-go prose


# --- Postgres pure helpers (no DB) -----------------------------------------


def test_postgres_helpers_pure() -> None:
    ddl = _build_index_sql("eval_chunks", "english")
    joined = "\n".join(ddl)
    assert "eval_chunks" in joined
    assert "tsvector" in joined
    assert "GENERATED ALWAYS" in joined
    assert "to_tsvector('english'" in joined
    assert "USING GIN" in joined

    sql = _build_search_sql("eval_chunks", "english")
    assert "websearch_to_tsquery('english'" in sql
    assert "ts_rank(" in sql
    assert "ORDER BY score DESC" in sql
    assert "%s" in sql

    res = _row_to_result({"document_id": "d1", "heading_path": "h", "content": "c", "score": 0.5})
    assert res == SearchResult(document_id="d1", heading_path="h", content="c", score=0.5)

    with pytest.raises(ValueError):
        _validate_config("'; DROP TABLE x; --")
    with pytest.raises(ValueError):
        _validate_table("bad name!")


# --- Postgres integration (gated) ------------------------------------------


def test_pg_ts_rank_orders_by_relevance() -> None:
    """ts_rank ranks by relevance, not insertion order, and excludes non-matches.

    Corpus insertion order is the *wrong* answer: docA (term once) is inserted
    before docB (term repeated). A backend missing ``ORDER BY ts_rank`` would
    return docA first. docC has no match and must be excluded by the
    ``tsv @@ query`` predicate.
    """
    pytest.importorskip("psycopg")
    dsn = os.environ.get("SAGE_EVAL_PG_DSN")
    if not dsn:
        pytest.skip("set SAGE_EVAL_PG_DSN to a throwaway Postgres to run the ts_rank arm")

    from sage.utils.keyword_fidelity_eval import PostgresFTSBackend

    backend = PostgresFTSBackend(dsn, table="eval_chunks_fidelity_test", config="english")
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
