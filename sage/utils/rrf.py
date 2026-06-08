"""Reciprocal Rank Fusion of two ranked result lists.

Pure fusion math shared by the production hybrid retrieval path
(``RetrievalService._hybrid_rrf``) and the keyword-backend fidelity evaluation
harness. RRF combines two already-ranked lists by summing ``1 / (k + rank)``
contributions keyed on ``(document_id, heading_path)``. It consumes *rank
position only* and discards the per-list relevance scores, so two lists produced
by different scoring algorithms -- for instance LanceDB BM25 and Postgres
``ts_rank`` -- fuse on equal footing. Keeping the fusion in one place lets both
callers share an identical implementation rather than a hand-copied formula.
"""

from sage.adapters.interfaces import SearchResult

# Standard constant from the original Reciprocal Rank Fusion paper. Larger k
# flattens the rank-position weighting; 60 is the paper's recommended default.
DEFAULT_RRF_K = 60


def rrf_fuse(
    primary: list[SearchResult],
    secondary: list[SearchResult],
    limit: int,
    k: int = DEFAULT_RRF_K,
) -> list[SearchResult]:
    """Fuse two ranked result lists by Reciprocal Rank Fusion.

    Both input lists must already be ordered best-first. A result's fused score
    is ``sum(1 / (k + rank + 1))`` over every list it appears in, where ``rank``
    is its 0-based position in that list. Results are keyed on
    ``(document_id, heading_path)``; when the same key appears in both lists the
    contributions accumulate. The returned list is sorted by fused score
    descending and truncated to ``limit``; each returned ``SearchResult`` carries
    the fused score in its ``score`` field -- the original per-list scores are
    not preserved. When a key appears in both lists the ``primary`` list's result
    object supplies the content and heading metadata.

    Ties are broken by first-appearance order (``primary`` entries first, then
    ``secondary``-only entries), which Python's stable sort preserves.
    """
    rrf_scores: dict[tuple[str, str], float] = {}
    result_map: dict[tuple[str, str], SearchResult] = {}

    for rank, result in enumerate(primary):
        key = (result.document_id, result.heading_path)
        rrf_scores[key] = rrf_scores.get(key, 0.0) + 1.0 / (k + rank + 1)
        result_map[key] = result

    for rank, result in enumerate(secondary):
        key = (result.document_id, result.heading_path)
        rrf_scores[key] = rrf_scores.get(key, 0.0) + 1.0 / (k + rank + 1)
        if key not in result_map:
            result_map[key] = result

    ranked_keys = sorted(rrf_scores, key=lambda fused_key: rrf_scores[fused_key], reverse=True)

    fused: list[SearchResult] = []
    for key in ranked_keys[:limit]:
        original = result_map[key]
        fused.append(
            SearchResult(
                document_id=original.document_id,
                heading_path=original.heading_path,
                content=original.content,
                score=rrf_scores[key],
            )
        )
    return fused
