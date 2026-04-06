"""Retrieval service: semantic and deterministic discover modes.

Covers behavioral tests BH-020, BH-027 through BH-030.

Semantic mode:
  - Pure vector search (default) or hybrid vector+BM25 via RRF (BH-027, BH-028).
  - Filters by lifecycle_status != failed pipeline (BH-020).
  - Scope gating: all, authoritative, specific, filtered.

Deterministic mode:
  - Exact heading path prefix match within a single document (BH-029).
  - Rejects documents with failed pipeline (BH-021, via pipeline gate).
  - Returns 404 for non-existent heading paths (BH-030).
"""

from sage.adapters.interfaces import ContentStore, EmbeddingProvider, SearchResult
from sage.api.errors import (
    DocumentNotFoundError,
    HeadingNotFoundError,
    MissingFieldError,
    PipelineIncompleteError,
)
from sage.config import VaultConfig
from sage.models.enums import PipelineStatus, RetrievalMode, RetrievalScope
from sage.models.schemas import (
    DiscoverHit,
    DiscoverRequest,
    DiscoverResponse,
    DocumentSummary,
)
from sage.storage.graph_store import GraphStore

# RRF constant (standard value from the original Reciprocal Rank Fusion paper).
_RRF_K = 60


class RetrievalService:
    def __init__(
        self,
        graph_store: GraphStore,
        content_store: ContentStore,
        embedding_provider: EmbeddingProvider,
        config: VaultConfig,
    ) -> None:
        self._graph = graph_store
        self._content = content_store
        self._embedding = embedding_provider
        self._config = config

    async def discover(self, request: DiscoverRequest) -> DiscoverResponse:
        """Dispatch to the appropriate retrieval mode handler."""
        if request.mode == RetrievalMode.SEMANTIC:
            return await self._semantic(request)
        elif request.mode == RetrievalMode.DETERMINISTIC:
            return await self._deterministic(request)
        else:
            # Verification mode deferred to slice 4
            raise MissingFieldError(
                "mode",
                f"Retrieval mode '{request.mode}' is not yet implemented",
            )

    # ------------------------------------------------------------------
    # Semantic retrieval (BH-020, BH-027, BH-028)
    # ------------------------------------------------------------------

    async def _semantic(self, request: DiscoverRequest) -> DiscoverResponse:
        if not request.query:
            raise MissingFieldError("query", "query is required for semantic mode")

        # Embed the query
        embeddings = await self._embedding.embed([request.query])
        query_embedding = embeddings[0]

        if request.use_hybrid:
            # Hybrid: RRF fusion of vector + BM25 (BH-027)
            results = await self._hybrid_rrf(
                query_embedding, request.query, request.limit
            )
        else:
            # Pure vector (BH-028)
            results = await self._content.search_semantic(
                query_embedding, request.limit
            )

        # Filter results: exclude failed-pipeline documents (BH-020)
        hits = await self._results_to_hits(results, request)

        return DiscoverResponse(
            mode=RetrievalMode.SEMANTIC,
            results=hits,
            total_available=len(hits),
        )

    async def _hybrid_rrf(
        self,
        query_embedding: list[float],
        query_text: str,
        limit: int,
    ) -> list[SearchResult]:
        """Reciprocal Rank Fusion of vector and BM25 results (BH-027).

        RRF score = sum(1 / (k + rank)) across result lists.
        """
        # Fetch more candidates than needed so RRF can re-rank
        fetch_limit = limit * 3

        vector_results = await self._content.search_semantic(
            query_embedding, fetch_limit
        )
        bm25_results = await self._content.search_bm25(query_text, fetch_limit)

        # Build RRF scores keyed by (document_id, heading_path)
        rrf_scores: dict[tuple[str, str], float] = {}
        result_map: dict[tuple[str, str], SearchResult] = {}

        for rank, result in enumerate(vector_results):
            key = (result.document_id, result.heading_path)
            rrf_scores[key] = rrf_scores.get(key, 0.0) + 1.0 / (_RRF_K + rank + 1)
            result_map[key] = result

        for rank, result in enumerate(bm25_results):
            key = (result.document_id, result.heading_path)
            rrf_scores[key] = rrf_scores.get(key, 0.0) + 1.0 / (_RRF_K + rank + 1)
            if key not in result_map:
                result_map[key] = result

        # Sort by RRF score descending
        ranked_keys = sorted(rrf_scores, key=lambda k: rrf_scores[k], reverse=True)

        results = []
        for key in ranked_keys[:limit]:
            original = result_map[key]
            results.append(SearchResult(
                document_id=original.document_id,
                heading_path=original.heading_path,
                content=original.content,
                score=rrf_scores[key],
            ))

        return results

    async def _results_to_hits(
        self,
        results: list[SearchResult],
        request: DiscoverRequest,
    ) -> list[DiscoverHit]:
        """Convert SearchResults to DiscoverHits, applying pipeline and scope filters."""
        hits: list[DiscoverHit] = []
        for result in results:
            doc = await self._graph.get_document(result.document_id)
            if doc is None:
                continue

            # BH-020: failed pipeline = excluded from all retrieval
            if doc.pipeline_status == PipelineStatus.FAILED:
                continue

            # Scope gating
            if not self._passes_scope(doc, request):
                continue

            summary = DocumentSummary(
                id=doc.id,
                title=doc.title,
                lifecycle_status=doc.lifecycle_status,
                source_type=doc.source_type,
                version_label=doc.version_label,
                project=doc.project,
                doc_type=doc.doc_type,
                tags=doc.tags,
            )

            hits.append(DiscoverHit(
                document=summary,
                chunk_content=result.content,
                heading_path=result.heading_path or None,
                relevance_score=result.score,
            ))

        return hits

    def _passes_scope(self, doc, request: DiscoverRequest) -> bool:
        """Check whether a document passes scope and filter criteria."""
        filters = request.filters

        # Scope gating
        if request.scope == RetrievalScope.AUTHORITATIVE:
            if not doc.authority_scope:
                return False
        elif request.scope == RetrievalScope.SPECIFIC:
            if filters and filters.document_ids:
                if doc.id not in filters.document_ids:
                    return False
            else:
                return False
        elif request.scope == RetrievalScope.FILTERED:
            if not filters:
                return False

        # Apply filters (for FILTERED and ALL scopes)
        if filters:
            if filters.doc_type and doc.doc_type != filters.doc_type:
                return False
            if filters.project and doc.project != filters.project:
                return False
            if filters.lifecycle_status and doc.lifecycle_status != filters.lifecycle_status:
                return False
            if filters.tags:
                if not set(filters.tags).issubset(set(doc.tags)):
                    return False

        return True

    # ------------------------------------------------------------------
    # Deterministic retrieval (BH-029, BH-030)
    # ------------------------------------------------------------------

    async def _deterministic(self, request: DiscoverRequest) -> DiscoverResponse:
        if not request.document_id:
            raise MissingFieldError(
                "document_id", "document_id is required for deterministic mode"
            )
        if not request.heading_path:
            raise MissingFieldError(
                "heading_path", "heading_path is required for deterministic mode"
            )

        # Validate document exists
        doc = await self._graph.get_document(request.document_id)
        if doc is None:
            raise DocumentNotFoundError(request.document_id)

        # Pipeline gate (BH-021)
        if doc.pipeline_status == PipelineStatus.FAILED:
            raise PipelineIncompleteError(request.document_id)

        # Prefix match on heading_path (BH-029)
        chunks = await self._content.get_chunks_by_heading_prefix(
            request.document_id, request.heading_path
        )

        # BH-030: no matching headings = 404
        if not chunks:
            raise HeadingNotFoundError(request.heading_path, request.document_id)

        summary = DocumentSummary(
            id=doc.id,
            title=doc.title,
            lifecycle_status=doc.lifecycle_status,
            source_type=doc.source_type,
            version_label=doc.version_label,
            project=doc.project,
            doc_type=doc.doc_type,
            tags=doc.tags,
        )

        hits = [
            DiscoverHit(
                document=summary,
                chunk_content=chunk.content,
                heading_path=chunk.heading_path,
                relevance_score=None,  # Deterministic mode: no relevance score
            )
            for chunk in chunks
        ]

        return DiscoverResponse(
            mode=RetrievalMode.DETERMINISTIC,
            results=hits,
            total_available=len(hits),
        )
