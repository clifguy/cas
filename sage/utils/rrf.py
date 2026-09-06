"""Reciprocal Rank Fusion of two ranked result lists.

Pure fusion math shared by the production hybrid retrieval path
(``RetrievalService._hybrid_rrf``) and the keyword-backend fidelity evaluation
harness. RRF combines two already-ranked lists by summing ``1 / (k + rank)``
contributions keyed on the document. It consumes *rank position only* and
discards the per-list relevance scores, so two lists produced by different
scoring algorithms -- for instance LanceDB BM25 and Postgres ``ts_rank`` --
fuse on equal footing. Keeping the fusion in one place lets both callers share
an identical implementation rather than a hand-copied formula.

The document is the key because it is the entity both arms rank. The semantic
arm returns one row per chunk; the keyword arm's match unit is the document
(CAS-ADR-048) and it returns one row per document. Keying on the chunk instead
would leave the arms colliding only when they happened to pick the same
passage, so a document both arms agree on would take one contribution rather
than two, and the fusion would degenerate into an interleave of two
independent rankings.
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

    Both input lists must already be ordered best-first. A document's fused
    score is ``sum(1 / (k + rank + 1))`` over every list it appears in, where
    ``rank`` is the 0-based position of its *best* entry in that list. Results
    are keyed on ``document_id``; when a document appears in both lists the
    contributions accumulate, and further entries for a document already seen
    in the same list add nothing -- a document is ranked by its best passage,
    not rewarded for having many. The returned list is sorted by fused score
    descending and truncated to ``limit``; each returned ``SearchResult``
    carries the fused score in its ``score`` field -- the original per-list
    scores are not preserved. The ``primary`` list's best entry supplies the
    content and heading metadata.

    ``matched_chunk_count`` carries how many distinct passages the document
    contributed across both lists, so collapsing them does not discard the
    reranking signal the count is there to give. Document-level rows are left
    out of that tally and out of the count carried across: they are not
    passages (CAS-ADR-049 Decision 5), and a tally keyed on the heading path
    would otherwise count one as a passage of its own, since it carries an
    empty path. The count carried across is the largest any row reported, not
    whichever the representative row happened to hold -- an arm whose match
    unit is the document reports its whole count on one row, and the row the
    fusion carries forward may come from the other arm.

    A fused row is document-level only when *every* row the document
    contributed was, so a document that matched a passage in either arm keeps
    that passage's excerpt and heading.

    Ties are broken by first-appearance order (``primary`` entries first, then
    ``secondary``-only entries), which Python's stable sort preserves.
    """
    rrf_scores: dict[str, float] = {}
    result_map: dict[str, SearchResult] = {}
    passages: dict[str, set[str]] = {}
    carried_counts: dict[str, int] = {}
    all_surface: dict[str, bool] = {}

    for source in (primary, secondary):
        seen_in_source: set[str] = set()
        for rank, result in enumerate(source):
            doc_id = result.document_id
            passages.setdefault(doc_id, set())
            if not result.is_document_surface:
                passages[doc_id].add(result.heading_path)
            carried_counts[doc_id] = max(carried_counts.get(doc_id, 0), result.matched_chunk_count)
            all_surface[doc_id] = all_surface.get(doc_id, True) and result.is_document_surface
            if doc_id not in seen_in_source:
                seen_in_source.add(doc_id)
                rrf_scores[doc_id] = rrf_scores.get(doc_id, 0.0) + 1.0 / (k + rank + 1)
            if doc_id not in result_map:
                result_map[doc_id] = result

    ranked_ids = sorted(rrf_scores, key=lambda doc_id: rrf_scores[doc_id], reverse=True)

    fused: list[SearchResult] = []
    for doc_id in ranked_ids[:limit]:
        original = result_map[doc_id]
        fused.append(
            SearchResult(
                document_id=doc_id,
                heading_path=original.heading_path,
                content=original.content,
                score=rrf_scores[doc_id],
                matched_chunk_count=max(len(passages[doc_id]), carried_counts[doc_id]),
                is_document_surface=all_surface[doc_id],
            )
        )
    return fused
