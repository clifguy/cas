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

import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field

from sage.adapters.interfaces import SearchResult
from sage.utils.rrf import rrf_fuse

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
    *,
    high: float = DEFAULT_HIGH_THRESHOLD,
    low: float = DEFAULT_LOW_THRESHOLD,
) -> str:
    """Map the mean fused overlap@K to a go/no-go token."""
    if mean_fused_overlap_at_k >= high:
        return REC_NATIVE
    if mean_fused_overlap_at_k < low:
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
        recommendation=recommend(mean_overlap, high=high, low=low),
        per_query=comparisons,
    )


# ---------------------------------------------------------------------------
# Scorecard rendering
# ---------------------------------------------------------------------------

_RECOMMENDATION_PROSE = {
    REC_NATIVE: (
        "**Default to native Postgres `ts_rank`.** The fused top-K agrees closely "
        "with LanceDB BM25 across the query set; an external managed search "
        "service is not warranted for the keyword arm."
    ),
    REC_MANAGED: (
        "**Escalate to a managed search service (Azure AI Search).** Native "
        "`ts_rank` diverges from LanceDB BM25 enough after fusion that the "
        "keyword arm would degrade under a native-FTS cloud content store."
    ),
    REC_BORDERLINE: (
        "**Borderline.** Fused agreement sits between the thresholds; weigh the "
        "per-query divergences and the cost of a managed service before deciding."
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
        "`ts_rank` score magnitudes. The gap between the raw and fused figures "
        "above is the degree to which fusion washes out the two backends' "
        "scoring differences -- the structural reason native FTS may suffice."
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


def _validate_table(table: str) -> str:
    """Return ``table`` if it is a safe lowercase SQL identifier."""
    if not _IDENT_RE.match(table):
        raise ValueError(f"table name {table!r} is not a safe identifier")
    return table


def _build_index_sql(table: str, config: str) -> list[str]:
    """DDL to (re)create the eval chunk table with a GIN-indexed tsvector.

    ``tsv`` is a stored generated column over ``to_tsvector(config, content)``
    so the index stays in sync with content without a trigger; the GIN index
    over it is what ``ts_rank`` queries hit.
    """
    table = _validate_table(table)
    config = _validate_config(config)
    return [
        f"DROP TABLE IF EXISTS {table}",
        (
            f"CREATE TABLE {table} ("
            "document_id text NOT NULL, "
            "heading_path text NOT NULL, "
            "content text NOT NULL, "
            f"tsv tsvector GENERATED ALWAYS AS (to_tsvector('{config}', content)) STORED"
            ")"
        ),
        f"CREATE INDEX {table}_tsv_gin ON {table} USING GIN (tsv)",
    ]


def _build_search_sql(table: str, config: str) -> str:
    """Parameterized ``ts_rank`` search ordered by descending relevance.

    The query text is a bind parameter (``%s``); the regconfig is a validated
    literal. Returns rows ordered by ``ts_rank`` so the ordering is the
    backend's, not physical row order.
    """
    table = _validate_table(table)
    config = _validate_config(config)
    # table and config are interpolated only after allowlist validation above;
    # the query text is a bind parameter. Not a SQL-injection vector.
    return (
        f"SELECT document_id, heading_path, content, "  # noqa: S608
        f"ts_rank(tsv, websearch_to_tsquery('{config}', %s)) AS score "
        f"FROM {table} "
        f"WHERE tsv @@ websearch_to_tsquery('{config}', %s) "
        f"ORDER BY score DESC, document_id ASC "
        f"LIMIT %s"
    )


def _row_to_result(row: dict[str, object]) -> SearchResult:
    """Map a search row (document_id, heading_path, content, score) to SearchResult."""
    return SearchResult(
        document_id=str(row["document_id"]),
        heading_path=str(row["heading_path"]),
        content=str(row["content"]),
        score=float(row["score"]),  # type: ignore[arg-type]
    )


class PostgresFTSBackend:
    """Postgres native full-text-search keyword backend for the eval.

    Indexes the eval corpus into a throwaway table and ranks by ``ts_rank``.
    Requires ``psycopg`` (the ``eval`` extra) and a reachable Postgres named by
    ``dsn``. The harness owns the table end-to-end; nothing here touches a SAGE
    vault store.
    """

    def __init__(self, dsn: str, *, table: str = "eval_chunks", config: str = "english") -> None:
        import psycopg  # lazy: keeps the module importable without the eval extra

        self._table = _validate_table(table)
        self._config = _validate_config(config)
        self._conn = psycopg.connect(dsn, autocommit=True)

    def index(self, chunks: Iterable[tuple[str, str, str]]) -> int:
        """Recreate the table and bulk-load ``(document_id, heading_path, content)`` rows.

        Returns the number of rows loaded.
        """
        rows = list(chunks)
        with self._conn.cursor() as cur:
            for stmt in _build_index_sql(self._table, self._config):
                cur.execute(stmt)  # type: ignore[arg-type]
            if rows:
                cur.executemany(
                    # self._table is allowlist-validated in __init__; no injection vector.
                    f"INSERT INTO {self._table} (document_id, heading_path, content) "  # noqa: S608
                    "VALUES (%s, %s, %s)",  # type: ignore[arg-type]
                    rows,
                )
        return len(rows)

    def search_bm25(self, query: str, limit: int = 10) -> list[SearchResult]:
        """Rank the corpus by ``ts_rank`` for ``query`` (keyword arm B)."""
        sql = _build_search_sql(self._table, self._config)
        with self._conn.cursor() as cur:
            cur.execute(sql, (query, query, limit))  # type: ignore[arg-type]
            columns = [d.name for d in cur.description or []]
            return [_row_to_result(dict(zip(columns, row, strict=True))) for row in cur.fetchall()]

    def close(self) -> None:
        self._conn.close()
