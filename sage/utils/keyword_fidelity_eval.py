"""Keyword-backend fidelity evaluation.

Compares two keyword-search backends -- LanceDB native BM25 and Postgres native
full-text search (``ts_rank``) -- as the keyword arm of hybrid retrieval. Each
backend's ranked candidates are fused with the *same* vector ranking through the
production Reciprocal Rank Fusion (``sage.utils.rrf.rrf_fuse``); the two fused
top-K result sets are then compared. The question this answers: when the content
store moves to Postgres, does native ``ts_rank`` produce materially the same
fused results as LanceDB BM25, or is an external managed search service warranted?

The premise is that RRF consumes only *rank order*, discarding the per-backend
score magnitudes -- so two keyword backends with incomparable raw scores can
still produce near-identical fused output. This module measures whether they do.

Layering: the metrics, fusion-and-compare, recommendation, and scorecard render
are pure and have no database dependency, so they are unit-tested directly. The
``PostgresFTSBackend`` connect/index/query methods require ``psycopg`` (the
``eval`` optional-dependency extra) and a reachable Postgres; its SQL-building
and row-mapping are factored into pure helpers so most of it stays testable
without a live server.
"""

from __future__ import annotations

import asyncio
import math
import re
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, TypeVar

from sage.adapters.content_store_postgres import PostgresContentStore
from sage.adapters.interfaces import Chunk, SearchResult
from sage.storage.postgres.schema import TEXT_SEARCH_CONFIG, bootstrap_schema
from sage.utils.rrf import rrf_fuse

if TYPE_CHECKING:
    from psycopg_pool import AsyncConnectionPool

_T = TypeVar("_T")

# Default recommendation thresholds on the mean fused overlap@K across the query
# set. At or above HIGH the backends agree closely enough to default to native
# ts_rank; below LOW they diverge enough to justify a managed service; between is
# a judgement call surfaced as "borderline".
DEFAULT_HIGH_THRESHOLD = 0.90
DEFAULT_LOW_THRESHOLD = 0.75

# Rank-Biased Overlap persistence parameter. 0.9 weights roughly the top ten
# ranks; higher p spreads weight deeper. Standard default from Webber et al.
DEFAULT_RBO_P = 0.9

# Recommendation tokens.
REC_NATIVE = "native_ts_rank"
REC_MANAGED = "managed_azure"
REC_BORDERLINE = "borderline"

RankingFn = Callable[[str], list[SearchResult]]


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------


def _topk_doc_ids(results: list[SearchResult], k: int) -> list[str]:
    """Distinct document ids from a ranked result list, order-preserving, top-K.

    Multiple chunks of one document collapse to a single entry at the position
    of the document's best-ranked chunk -- mirroring the document-level dedup
    the production discover path applies before returning hits.
    """
    seen: set[str] = set()
    ids: list[str] = []
    for r in results:
        if r.document_id in seen:
            continue
        seen.add(r.document_id)
        ids.append(r.document_id)
        if len(ids) >= k:
            break
    return ids


def overlap_at_k(ranking_a: list[str], ranking_b: list[str], k: int) -> float:
    """Set overlap of two top-K id lists: ``|A_k ∩ B_k| / k``.

    Order-insensitive. ``k`` is the denominator, so two short lists that agree
    fully but return fewer than ``k`` ids each score below 1.0 -- the metric
    rewards filling the top-K with agreed results, not merely not disagreeing.
    """
    if k <= 0:
        raise ValueError("k must be positive")
    set_a = set(ranking_a[:k])
    set_b = set(ranking_b[:k])
    return len(set_a & set_b) / k


def rbo(ranking_a: list[str], ranking_b: list[str], p: float = DEFAULT_RBO_P) -> float:
    """Rank-Biased Overlap (extrapolated) of two ranked id lists.

    Order-*sensitive*, top-weighted agreement in ``[0, 1]``: two identical
    rankings score 1.0; the same set in reversed order scores strictly below
    1.0. Uses the extrapolated RBO of Webber, Moffat & Zobel (2010) so finite
    conjoint lists reach exactly 1.0. ``p`` is the persistence parameter.
    """
    if not 0.0 < p < 1.0:
        raise ValueError("p must be in the open interval (0, 1)")
    len_a, len_b = len(ranking_a), len(ranking_b)
    if len_a == 0 and len_b == 0:
        return 1.0
    depth = max(len_a, len_b)

    seen_a: set[str] = set()
    seen_b: set[str] = set()
    weighted_sum = 0.0
    intersection = 0
    for d in range(1, depth + 1):
        if d <= len_a:
            seen_a.add(ranking_a[d - 1])
        if d <= len_b:
            seen_b.add(ranking_b[d - 1])
        intersection = len(seen_a & seen_b)
        weighted_sum += (intersection / d) * (p**d)

    # Extrapolation term carries the deepest agreement to the residual tail.
    tail = (intersection / depth) * (p**depth)
    return tail + ((1.0 - p) / p) * weighted_sum


# ---------------------------------------------------------------------------
# Per-query comparison and aggregation
# ---------------------------------------------------------------------------


@dataclass
class QueryComparison:
    """The fidelity verdict for a single query."""

    query: str
    topk_lancedb: list[str]
    topk_postgres: list[str]
    fused_overlap_at_k: float
    fused_rbo: float
    identical_topk: bool
    raw_keyword_overlap_at_k: float


@dataclass
class FidelityResult:
    """Aggregate fidelity verdict over the whole query set."""

    k: int
    num_queries: int
    rbo_p: float
    high_threshold: float
    low_threshold: float
    mean_fused_overlap_at_k: float
    mean_fused_rbo: float
    pct_identical_topk: float
    mean_raw_keyword_overlap_at_k: float
    recommendation: str
    per_query: list[QueryComparison] = field(default_factory=list)


def compare_query(
    query: str,
    vector: list[SearchResult],
    bm25: list[SearchResult],
    ts_rank: list[SearchResult],
    k: int,
    *,
    rbo_p: float = DEFAULT_RBO_P,
) -> QueryComparison:
    """Fuse each keyword arm with the shared vector arm and compare the top-K.

    ``vector`` is the constant arm. ``bm25`` (LanceDB) and ``ts_rank`` (Postgres)
    are the two keyword arms under test. Both fusions use the identical
    ``rrf_fuse`` the production path uses, so any divergence is attributable to
    the keyword backends, not to the fusion.
    """
    fuse_limit = len(vector) + max(len(bm25), len(ts_rank))
    fused_a = rrf_fuse(vector, bm25, limit=fuse_limit)
    fused_b = rrf_fuse(vector, ts_rank, limit=fuse_limit)

    topk_a = _topk_doc_ids(fused_a, k)
    topk_b = _topk_doc_ids(fused_b, k)

    raw_overlap = overlap_at_k(_topk_doc_ids(bm25, k), _topk_doc_ids(ts_rank, k), k)

    return QueryComparison(
        query=query,
        topk_lancedb=topk_a,
        topk_postgres=topk_b,
        fused_overlap_at_k=overlap_at_k(topk_a, topk_b, k),
        fused_rbo=rbo(topk_a, topk_b, rbo_p),
        identical_topk=topk_a == topk_b,
        raw_keyword_overlap_at_k=raw_overlap,
    )


def recommend(
    mean_fused_overlap_at_k: float,
    mean_raw_keyword_overlap_at_k: float,
    *,
    high: float = DEFAULT_HIGH_THRESHOLD,
    low: float = DEFAULT_LOW_THRESHOLD,
) -> str:
    """Map the fused *and* unfused mean overlap@K to a go/no-go token.

    Both arms fuse against the same vector ranking, so the fused figure cannot
    see a keyword arm that returns nothing: the fusion degenerates to the
    vector result and agreement stays high. It measures how far fusion washes
    the keyword arm out, not whether the candidates agree. Only the unfused
    comparison discriminates them on keyword semantics, which is what a
    keyword backend is being qualified on, so it gates the verdict alongside
    the fused figure rather than being reported next to it (CAS-ADR-048).

    Native is recommended only when both figures clear ``high``; either one
    below ``low`` escalates. A qualification that consults the fused figure
    alone will pass a backend whose match semantics are inverted.
    """
    worst = min(mean_fused_overlap_at_k, mean_raw_keyword_overlap_at_k)
    if worst >= high:
        return REC_NATIVE
    if worst < low:
        return REC_MANAGED
    return REC_BORDERLINE


def run_fidelity_eval(
    queries: Iterable[str],
    vector_fn: RankingFn,
    bm25_fn: RankingFn,
    ts_rank_fn: RankingFn,
    k: int,
    *,
    rbo_p: float = DEFAULT_RBO_P,
    high: float = DEFAULT_HIGH_THRESHOLD,
    low: float = DEFAULT_LOW_THRESHOLD,
) -> FidelityResult:
    """Run the full evaluation over a query set and aggregate the verdict.

    ``vector_fn``/``bm25_fn``/``ts_rank_fn`` each map a query to that arm's
    ranked ``SearchResult`` list. Keeping them injectable lets the harness drive
    real backends while the unit tests drive deterministic fakes.
    """
    comparisons = [
        compare_query(
            q,
            vector_fn(q),
            bm25_fn(q),
            ts_rank_fn(q),
            k,
            rbo_p=rbo_p,
        )
        for q in queries
    ]
    n = len(comparisons)
    if n == 0:
        raise ValueError("query set is empty")

    mean_overlap = sum(c.fused_overlap_at_k for c in comparisons) / n
    mean_rbo = sum(c.fused_rbo for c in comparisons) / n
    pct_identical = sum(1 for c in comparisons if c.identical_topk) / n
    mean_raw = sum(c.raw_keyword_overlap_at_k for c in comparisons) / n

    return FidelityResult(
        k=k,
        num_queries=n,
        rbo_p=rbo_p,
        high_threshold=high,
        low_threshold=low,
        mean_fused_overlap_at_k=mean_overlap,
        mean_fused_rbo=mean_rbo,
        pct_identical_topk=pct_identical,
        mean_raw_keyword_overlap_at_k=mean_raw,
        recommendation=recommend(mean_overlap, mean_raw, high=high, low=low),
        per_query=comparisons,
    )


# ---------------------------------------------------------------------------
# Scorecard rendering
# ---------------------------------------------------------------------------

_RECOMMENDATION_PROSE = {
    REC_NATIVE: (
        "**Default to native Postgres `ts_rank`.** Both the unfused keyword "
        "rankings and the fused top-K agree closely with LanceDB BM25 across "
        "the query set; an external managed search service is not warranted "
        "for the keyword arm."
    ),
    REC_MANAGED: (
        "**Escalate to a managed search service (Azure AI Search).** Native "
        "`ts_rank` diverges from LanceDB BM25 -- on the unfused keyword "
        "rankings, after fusion, or both -- enough that the keyword arm would "
        "degrade under a native-FTS cloud content store."
    ),
    REC_BORDERLINE: (
        "**Borderline.** The weaker of the unfused and fused agreement figures "
        "sits between the thresholds; weigh the per-query divergences and the "
        "cost of a managed service before deciding."
    ),
}


def render_scorecard(result: FidelityResult) -> str:
    """Render the fidelity result as a one-page Markdown scorecard."""
    lines: list[str] = []
    lines.append("# Keyword-backend fidelity scorecard")
    lines.append("")
    lines.append(
        "Native Postgres `ts_rank` vs LanceDB BM25 as the hybrid keyword arm, "
        "each fused with the same vector ranking through the production "
        "Reciprocal Rank Fusion."
    )
    lines.append("")
    lines.append("## Aggregate")
    lines.append("")
    lines.append(f"- Queries: **{result.num_queries}**, K = **{result.k}**")
    lines.append(f"- Mean fused overlap@{result.k}: **{result.mean_fused_overlap_at_k:.3f}**")
    lines.append(f"- Mean fused RBO (p={result.rbo_p}): **{result.mean_fused_rbo:.3f}**")
    lines.append(f"- Top-K identical (exact order): **{result.pct_identical_topk:.1%}** of queries")
    lines.append("")
    lines.append("## Raw vs fused")
    lines.append("")
    lines.append(
        f"- Mean *raw* keyword overlap@{result.k} (BM25 vs ts_rank, before "
        f"fusion): **{result.mean_raw_keyword_overlap_at_k:.3f}**"
    )
    lines.append(
        f"- Mean *fused* overlap@{result.k} (after RRF): **{result.mean_fused_overlap_at_k:.3f}**"
    )
    lines.append("")
    lines.append(
        "Reciprocal Rank Fusion consumes only **rank order**, not the raw BM25 or "
        "`ts_rank` score magnitudes, and both arms fuse against the same vector "
        "ranking. A gap between the raw and fused figures above is therefore not "
        "reassurance: it is the degree to which fusion hides a keyword arm that "
        "disagrees, up to and including one that returns nothing, whose fused "
        "result is simply the vector result. The raw figure is the one that "
        "discriminates the backends on keyword semantics, so the recommendation "
        "below reads the weaker of the two rather than the fused figure alone."
    )
    lines.append("")
    lines.append("## Per-query")
    lines.append("")
    lines.append(f"| Query | overlap@{result.k} | RBO | identical | raw overlap |")
    lines.append("|---|---|---|---|---|")
    for c in result.per_query:
        ident = "yes" if c.identical_topk else "no"
        lines.append(
            f"| {c.query} | {c.fused_overlap_at_k:.2f} | {c.fused_rbo:.2f} "
            f"| {ident} | {c.raw_keyword_overlap_at_k:.2f} |"
        )
    lines.append("")
    lines.append("## Recommendation")
    lines.append("")
    lines.append(_RECOMMENDATION_PROSE[result.recommendation])
    lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Relevance metrics (graded NDCG + rank-of-target MRR / success@k)
# ---------------------------------------------------------------------------
#
# The divergence layer above asks "do the two keyword arms rank the same?".
# This layer asks the harder question "does each arm rank the *right answer*
# highly?" -- absolute relevance against a judged gold set, the go/no-go gate
# for keeping native ts_rank as the cloud keyword arm. The metrics are pure and
# graded: NDCG@k rewards highly-graded targets at shallow ranks; MRR and
# success@k track where the primary target lands.

# Relevance grades for the gold set. The primary (known-item) target is grade 2;
# secondary-relevant docs are grade 1. Binary judgement is the special case where
# only primaries are supplied.
GRADE_PRIMARY = 2.0
GRADE_SECONDARY = 1.0

# Arm identifiers. Each query is scored under three arms: the vector-only ranking
# (the floor -- what hybrid retrieval would return with no keyword arm), and the
# two keyword arms each fused with that same vector ranking through ``rrf_fuse``.
ARM_VECTOR = "vector"
ARM_BM25 = "bm25_fused"
ARM_TSRANK = "tsrank_fused"

# Human-readable arm labels for the scorecard.
_ARM_LABELS = {
    ARM_VECTOR: "vector-only",
    ARM_BM25: "LanceDB BM25 (fused)",
    ARM_TSRANK: "Postgres ts_rank (fused)",
}

# Adequacy tolerance for the go/no-go banding. ``ts_rank`` is "adequate" when its
# absolute relevance (NDCG@k and success@k) trails LanceDB BM25 by no more than
# this on the worse of the two metrics; a gap at or beyond twice this escalates.
# A calibration knob, surfaced in the scorecard so the per-arm gap stays auditable
# rather than load-bearing.
DEFAULT_REL_DELTA = 0.05


def dcg_at_k(ranked_ids: list[str], gains: dict[str, float], k: int) -> float:
    """Discounted Cumulative Gain at ``k`` with graded gains.

    ``gains`` maps a document id to its relevance grade; ids absent from the map
    contribute zero. The standard log discount ``gain / log2(rank + 1)`` rewards
    relevant docs at shallow ranks. ``ranked_ids`` is 1-based for the discount.
    """
    if k <= 0:
        raise ValueError("k must be positive")
    total = 0.0
    for rank, doc_id in enumerate(ranked_ids[:k], start=1):
        gain = gains.get(doc_id, 0.0)
        if gain:
            total += gain / math.log2(rank + 1)
    return total


def ndcg_at_k(ranked_ids: list[str], gains: dict[str, float], k: int) -> float:
    """Normalized DCG@k: ``DCG / ideal-DCG`` in ``[0, 1]``.

    The ideal DCG places the highest available grades at the shallowest ranks.
    Returns 0.0 when no relevant doc is reachable (empty ``gains``) -- a ranking
    that surfaces none of the relevant docs scores 0, never undefined.
    """
    dcg = dcg_at_k(ranked_ids, gains, k)
    ideal_grades = sorted(gains.values(), reverse=True)
    idcg = 0.0
    for rank, gain in enumerate(ideal_grades[:k], start=1):
        if gain:
            idcg += gain / math.log2(rank + 1)
    if idcg == 0.0:
        return 0.0
    return dcg / idcg


def reciprocal_rank(ranked_ids: list[str], relevant_ids: set[str]) -> float:
    """Reciprocal rank of the first relevant id: ``1 / rank``, or 0.0 if none."""
    for rank, doc_id in enumerate(ranked_ids, start=1):
        if doc_id in relevant_ids:
            return 1.0 / rank
    return 0.0


def success_at_k(ranked_ids: list[str], relevant_ids: set[str], k: int) -> float:
    """1.0 if any relevant id appears in the top-``k``, else 0.0."""
    if k <= 0:
        raise ValueError("k must be positive")
    return 1.0 if any(doc_id in relevant_ids for doc_id in ranked_ids[:k]) else 0.0


# ---------------------------------------------------------------------------
# Gold-set model
# ---------------------------------------------------------------------------


@dataclass
class GoldQuery:
    """One judged query: a primary (known-item) target plus optional secondaries.

    ``primary_id`` / ``relevant_ids`` carry stable selectors (e.g. a vault
    ``source_path``) in the on-disk gold set; the harness resolves them to
    document ids before scoring. The metrics treat the primary as grade 2 and
    each secondary as grade 1.
    """

    query: str
    primary_id: str
    relevant_ids: list[str] = field(default_factory=list)

    def gains(self) -> dict[str, float]:
        """Map every relevant id to its grade (primary 2, secondaries 1)."""
        graded: dict[str, float] = {self.primary_id: GRADE_PRIMARY}
        for rid in self.relevant_ids:
            graded.setdefault(rid, GRADE_SECONDARY)
        return graded

    def relevant_set(self) -> set[str]:
        """The set of all relevant ids (primary + secondaries)."""
        return {self.primary_id, *self.relevant_ids}


def parse_gold_entry(entry: dict[str, object]) -> GoldQuery:
    """Parse one gold-set mapping into a ``GoldQuery``, failing loudly on gaps.

    A missing ``query`` or ``primary`` is a stale/typo'd gold set, not a
    zero-score query -- raise so the eval cannot silently grade against nothing.
    """
    query = entry.get("query")
    primary = entry.get("primary")
    if not query or not primary:
        raise ValueError(f"gold entry needs both 'query' and 'primary': {entry!r}")
    relevant = entry.get("relevant") or []
    if not isinstance(relevant, list):
        raise ValueError(f"gold entry 'relevant' must be a list: {entry!r}")
    return GoldQuery(
        query=str(query),
        primary_id=str(primary),
        relevant_ids=[str(rid) for rid in relevant],
    )


# ---------------------------------------------------------------------------
# Relevance aggregation + recommendation
# ---------------------------------------------------------------------------


@dataclass
class ArmRelevance:
    """Aggregate relevance for one arm across the gold set."""

    arm: str
    mean_ndcg_at_k: float
    mean_reciprocal_rank: float
    mean_success_at_k: float


@dataclass
class QueryRelevance:
    """Per-query relevance for every arm, with the scored top-K retained."""

    query: str
    per_arm: dict[str, dict[str, float]]
    topk: dict[str, list[str]]


@dataclass
class RelevanceResult:
    """Aggregate absolute-relevance verdict over the gold set."""

    k: int
    num_queries: int
    delta: float
    arms: dict[str, ArmRelevance]
    recommendation: str
    per_query: list[QueryRelevance] = field(default_factory=list)


def recommend_relevance(
    ts_rank: ArmRelevance,
    bm25: ArmRelevance,
    *,
    delta: float = DEFAULT_REL_DELTA,
    escalate_delta: float | None = None,
) -> str:
    """Map the ``ts_rank``-vs-BM25 absolute-relevance gap to a go/no-go token.

    The gap is the worse of the NDCG@k and success@k regressions of ``ts_rank``
    relative to BM25 (the incumbent). At or within ``delta`` the keyword arm is
    adequate (keep native); at or beyond ``escalate_delta`` (default ``2*delta``)
    it has materially degraded (escalate); between is borderline.
    """
    if escalate_delta is None:
        escalate_delta = 2 * delta
    gap = max(
        bm25.mean_ndcg_at_k - ts_rank.mean_ndcg_at_k,
        bm25.mean_success_at_k - ts_rank.mean_success_at_k,
    )
    if gap <= delta:
        return REC_NATIVE
    if gap >= escalate_delta:
        return REC_MANAGED
    return REC_BORDERLINE


def run_relevance_eval(
    gold: list[GoldQuery],
    vector_fn: RankingFn,
    bm25_fn: RankingFn,
    ts_rank_fn: RankingFn,
    k: int,
    *,
    delta: float = DEFAULT_REL_DELTA,
) -> RelevanceResult:
    """Grade absolute relevance of each arm against the gold set.

    For every gold query the vector arm is scored alone and each keyword arm is
    fused with that same vector arm through the production ``rrf_fuse`` and
    document-deduped -- so the scored ranking is the shape the hybrid discover
    path returns, not the raw keyword list. Each arm is then graded with NDCG@k,
    MRR, and success@k against the query's judged labels.
    """
    if not gold:
        raise ValueError("gold set is empty")

    per_query: list[QueryRelevance] = []
    for gq in gold:
        vector = vector_fn(gq.query)
        bm25 = bm25_fn(gq.query)
        ts_rank = ts_rank_fn(gq.query)
        fuse_limit = len(vector) + max(len(bm25), len(ts_rank))
        fuse_limit = max(fuse_limit, 1)

        rankings = {
            ARM_VECTOR: _topk_doc_ids(vector, len(vector) + 1),
            ARM_BM25: _topk_doc_ids(rrf_fuse(vector, bm25, limit=fuse_limit), fuse_limit + 1),
            ARM_TSRANK: _topk_doc_ids(rrf_fuse(vector, ts_rank, limit=fuse_limit), fuse_limit + 1),
        }

        gains = gq.gains()
        relevant = gq.relevant_set()
        per_arm = {
            arm: {
                "ndcg": ndcg_at_k(ranking, gains, k),
                "rr": reciprocal_rank(ranking, relevant),
                "success": success_at_k(ranking, relevant, k),
            }
            for arm, ranking in rankings.items()
        }
        per_query.append(
            QueryRelevance(
                query=gq.query,
                per_arm=per_arm,
                topk={arm: ranking[:k] for arm, ranking in rankings.items()},
            )
        )

    n = len(per_query)
    arms = {
        arm: ArmRelevance(
            arm=arm,
            mean_ndcg_at_k=sum(q.per_arm[arm]["ndcg"] for q in per_query) / n,
            mean_reciprocal_rank=sum(q.per_arm[arm]["rr"] for q in per_query) / n,
            mean_success_at_k=sum(q.per_arm[arm]["success"] for q in per_query) / n,
        )
        for arm in (ARM_VECTOR, ARM_BM25, ARM_TSRANK)
    }

    return RelevanceResult(
        k=k,
        num_queries=n,
        delta=delta,
        arms=arms,
        recommendation=recommend_relevance(arms[ARM_TSRANK], arms[ARM_BM25], delta=delta),
        per_query=per_query,
    )


# ---------------------------------------------------------------------------
# Relevance scorecard rendering
# ---------------------------------------------------------------------------

_RELEVANCE_RECOMMENDATION_PROSE = {
    REC_NATIVE: (
        "**Keep native Postgres `ts_rank`.** On the keyword-skewed gold set its "
        "graded relevance (NDCG@k), MRR, and success@k stay within tolerance of "
        "LanceDB BM25; the keyword arm does not degrade under native FTS, so no "
        "external managed search service is warranted."
    ),
    REC_MANAGED: (
        "**Escalate beyond native `ts_rank`.** On the keyword-skewed gold set "
        "`ts_rank` retrieves materially worse than LanceDB BM25 (NDCG@k / "
        "success@k gap beyond tolerance); evaluate Azure AI Search or a "
        "self-managed Postgres BM25 extension before adopting native FTS."
    ),
    REC_BORDERLINE: (
        "**Borderline.** `ts_rank` trails LanceDB BM25 by a margin between the "
        "adequacy and escalation thresholds; weigh the per-query regressions and "
        "the cost of a managed service before deciding."
    ),
}


def render_relevance_scorecard(result: RelevanceResult) -> str:
    """Render the relevance result as a one-page Markdown scorecard."""
    k = result.k
    lines: list[str] = []
    lines.append("# Keyword-backend relevance scorecard")
    lines.append("")
    lines.append(
        "Absolute retrieval relevance of each keyword arm on a deliberately "
        "**keyword-skewed** judged gold set (exact identifiers, rare/code-like "
        "tokens, low-semantic-overlap phrasings). Each keyword arm is fused with "
        "the same vector ranking through the production Reciprocal Rank Fusion; "
        "the vector-only arm is the floor."
    )
    lines.append("")
    lines.append("## Aggregate (per arm)")
    lines.append("")
    lines.append(f"- Queries: **{result.num_queries}**, K = **{k}**")
    lines.append(f"- Adequacy tolerance (delta): **{result.delta:.3f}**")
    lines.append("")
    lines.append(f"| Arm | NDCG@{k} | MRR | success@{k} |")
    lines.append("|---|---|---|---|")
    for arm in (ARM_VECTOR, ARM_BM25, ARM_TSRANK):
        a = result.arms[arm]
        lines.append(
            f"| {_ARM_LABELS[arm]} | {a.mean_ndcg_at_k:.3f} | "
            f"{a.mean_reciprocal_rank:.3f} | {a.mean_success_at_k:.3f} |"
        )
    lines.append("")
    bm25 = result.arms[ARM_BM25]
    ts_rank = result.arms[ARM_TSRANK]
    lines.append(
        f"`ts_rank` vs BM25 gap — NDCG@{k}: "
        f"**{bm25.mean_ndcg_at_k - ts_rank.mean_ndcg_at_k:+.3f}**, "
        f"success@{k}: **{bm25.mean_success_at_k - ts_rank.mean_success_at_k:+.3f}** "
        "(positive = `ts_rank` worse)."
    )
    lines.append("")
    lines.append("## Per-query")
    lines.append("")
    lines.append(f"| Query | NDCG@{k} (vec / bm25 / ts) | MRR (vec / bm25 / ts) |")
    lines.append("|---|---|---|")
    for q in result.per_query:
        v, b, t = q.per_arm[ARM_VECTOR], q.per_arm[ARM_BM25], q.per_arm[ARM_TSRANK]
        lines.append(
            f"| {q.query} | {v['ndcg']:.2f} / {b['ndcg']:.2f} / {t['ndcg']:.2f} "
            f"| {v['rr']:.2f} / {b['rr']:.2f} / {t['rr']:.2f} |"
        )
    lines.append("")
    lines.append("## Recommendation")
    lines.append("")
    lines.append(_RELEVANCE_RECOMMENDATION_PROSE[result.recommendation])
    lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Postgres FTS backend (requires the `eval` extra: psycopg)
# ---------------------------------------------------------------------------

# Text-search configurations we allow to be interpolated as SQL literals. The
# config name is not a bind parameter (it parameterizes the regconfig of an
# immutable generated column), so it must be validated against an allowlist
# rather than passed through from arbitrary input.
_ALLOWED_TS_CONFIGS = frozenset({"english", "simple"})


def _validate_config(config: str) -> str:
    """Return ``config`` if it is an allowed, safely-interpolatable ts config."""
    if config not in _ALLOWED_TS_CONFIGS:
        raise ValueError(
            f"text-search config {config!r} is not allowed; expected one of "
            f"{sorted(_ALLOWED_TS_CONFIGS)}"
        )
    return config


_IDENT_RE = re.compile(r"^[a-z_][a-z0-9_]*$")


def _validate_schema(schema: str) -> str:
    """Return ``schema`` if it is a safe lowercase SQL identifier."""
    if not _IDENT_RE.match(schema):
        raise ValueError(f"schema name {schema!r} is not a safe identifier")
    return schema


class PostgresFTSBackend:
    """Postgres native full-text-search keyword backend for the eval.

    Loads the eval corpus into a throwaway schema carrying the production
    content-store tables, then answers through ``PostgresContentStore`` itself.
    The delegation is the point: an evaluation that qualifies a keyword backend
    has to exercise the query the substrate actually runs. Restating that query
    here would let the two drift, and a harness measuring a backend production
    no longer runs cannot qualify anything -- least of all the match semantics
    the contract turns on, which is what a candidate backend is judged against.

    Requires ``psycopg`` (the ``eval`` extra) and a reachable Postgres named by
    ``dsn``. The harness owns the schema end-to-end; nothing here touches a
    SAGE vault store.
    """

    def __init__(self, dsn: str, *, schema: str = "eval_chunks", config: str = "english") -> None:
        self._schema = _validate_schema(schema)
        self._config = _validate_config(config)
        if self._config != TEXT_SEARCH_CONFIG:
            raise ValueError(
                f"text-search config {config!r} does not match the production "
                f"content store's {TEXT_SEARCH_CONFIG!r}; the eval must index and "
                "query exactly as production does or it measures something else"
            )
        self._dsn = dsn

    def _run(self, coro_fn: Callable[[AsyncConnectionPool], Awaitable[_T]]) -> _T:
        """Bridge the harness's synchronous surface onto the async store.

        The pool is opened and closed inside each call rather than held on the
        instance: ``asyncio.run`` builds a fresh event loop every time, and a
        pool carries worker tasks bound to the loop that opened it.
        """
        from psycopg_pool import AsyncConnectionPool as Pool

        async def _inner() -> _T:
            pool = Pool(
                self._dsn,
                kwargs={"options": f"-c search_path={self._schema},public"},
                open=False,
                min_size=1,
                max_size=2,
            )
            await pool.open()
            try:
                return await coro_fn(pool)
            finally:
                await pool.close()

        return asyncio.run(_inner())

    def index(self, chunks: Iterable[tuple[str, str, str]]) -> int:
        """Recreate the schema and load ``(document_id, heading_path, content)`` rows.

        Rows are grouped by document and written through the store's own
        ``index_chunks``, so the corpus takes the write path a caller takes.

        One deliberate departure from ingest: the passages carry no derived
        indexed structure, so the keyword vector's top weight reads the address
        (CAS-ADR-049 Decision 3, through the generated column's coalesce). This
        harness has no per-document title to derive one from -- it is handed
        paths and content, not records. Holding the indexing fixed is also what
        a comparability harness wants: it measures two keyword backends against
        each other, so the one thing that must not vary between its arms is how
        the corpus was indexed.

        Returns the number of rows loaded.
        """
        rows = list(chunks)
        grouped: dict[str, list[Chunk]] = {}
        for document_id, heading_path, content in rows:
            per_doc = grouped.setdefault(document_id, [])
            per_doc.append(
                Chunk(
                    document_id=document_id,
                    heading_path=heading_path,
                    content=content,
                    chunk_index=len(per_doc),
                )
            )

        async def _load(pool: AsyncConnectionPool) -> None:
            async with pool.connection() as conn:
                await conn.execute(f"DROP SCHEMA IF EXISTS {self._schema} CASCADE")  # noqa: S608
                await conn.execute(f"CREATE SCHEMA {self._schema}")  # noqa: S608
                await bootstrap_schema(conn, schema=self._schema, create_extensions=False)
            store = PostgresContentStore(pool)
            for document_id, doc_chunks in grouped.items():
                await store.index_chunks(document_id, doc_chunks)

        self._run(_load)
        return len(rows)

    def search_bm25(self, query: str, limit: int = 10) -> list[SearchResult]:
        """Rank the corpus for ``query`` through the production binding (keyword arm B)."""
        return self._run(lambda pool: PostgresContentStore(pool).search_bm25(query, limit=limit))

    def close(self) -> None:
        """Drop the throwaway schema. Pools are per-call and already closed."""

        async def _drop(pool: AsyncConnectionPool) -> None:
            async with pool.connection() as conn:
                await conn.execute(f"DROP SCHEMA IF EXISTS {self._schema} CASCADE")  # noqa: S608

        self._run(_drop)
