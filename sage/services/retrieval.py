"""Retrieval service: semantic, keyword, and deterministic discover modes.

Covers behavioral tests BH-020, BH-027 through BH-030, BH-059 through BH-061.

Semantic mode:
  - Pure vector search (default) or hybrid vector+BM25 via RRF (BH-027, BH-028).
  - Filters by lifecycle_status != failed pipeline (BH-020).
  - Scope gating: all, authoritative, specific, filtered.

Keyword mode (BH-059, BH-060, BH-061):
  - BM25-only search. No query embedding required.
  - Same pipeline and scope gating as semantic mode.

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
    Document,
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

    @staticmethod
    def _fetch_limit(request: DiscoverRequest) -> int:
        """Compute content store fetch limit.

        Always over-fetch because results are deduplicated by document
        (one hit per document) and filtered by pipeline status and scope.
        Fetch more when filters are active since those discard additional
        documents post-search.
        """
        multiplier = 10 if request.filters else 5
        return request.limit * multiplier

    async def discover(self, request: DiscoverRequest) -> DiscoverResponse:
        """Dispatch to the appropriate retrieval mode handler."""
        if request.mode == RetrievalMode.SEMANTIC:
            return await self._semantic(request)
        elif request.mode == RetrievalMode.KEYWORD:
            return await self._keyword(request)
        elif request.mode == RetrievalMode.DETERMINISTIC:
            return await self._deterministic(request)
        else:
            # Verification mode deferred to slice 4
            raise MissingFieldError(
                "mode",
                f"Retrieval mode '{request.mode}' is not yet implemented",
            )

    # ------------------------------------------------------------------
    # Keyword retrieval (BH-059, BH-060, BH-061)
    # ------------------------------------------------------------------

    async def _keyword(self, request: DiscoverRequest) -> DiscoverResponse:
        """BM25-only search. No embedding required.

        A query of "*" returns all documents (useful for filter-only
        drill-downs from the dashboard).
        """
        if not request.query:
            raise MissingFieldError("query", "query is required for keyword mode")

        if request.query.strip() == "*":
            # Filter-only listing: return all documents matching filters
            hits = await self._list_filtered(request)
        else:
            fetch_limit = self._fetch_limit(request)
            results = await self._content.search_bm25(request.query, fetch_limit)
            hits = await self._results_to_hits(results, request)
            hits = await self._boost_metadata_matches(hits, request)

        return DiscoverResponse(
            mode=RetrievalMode.KEYWORD,
            results=hits[:request.limit],
            total_available=len(hits),
        )

    async def _list_filtered(self, request: DiscoverRequest) -> list[DiscoverHit]:
        """Return all documents matching scope and filter criteria.

        Used for dashboard drill-downs where no content search is needed.
        """
        all_docs = await self._graph.list_all_documents()
        hits: list[DiscoverHit] = []

        for doc in all_docs:
            if doc.pipeline_status == PipelineStatus.FAILED:
                continue
            if not self._passes_scope(doc, request):
                continue

            summary = DocumentSummary(
                id=doc.id,
                title=doc.title,
                lifecycle_status=doc.lifecycle_status,
                source_type=doc.source_type,
                source_path=doc.source_path,
                version_label=doc.version_label,
                project=doc.project,
                doc_type=doc.doc_type,
                tags=doc.tags,
            )
            hits.append(DiscoverHit(
                document=summary,
                chunk_content=None,
                heading_path=None,
                relevance_score=None,
            ))

        return hits

    # ------------------------------------------------------------------
    # Semantic retrieval (BH-020, BH-027, BH-028)
    # ------------------------------------------------------------------

    async def _semantic(self, request: DiscoverRequest) -> DiscoverResponse:
        if not request.query:
            raise MissingFieldError("query", "query is required for semantic mode")

        # Over-fetch when filters are active so post-filter results
        # aren't depleted by non-matching documents.
        fetch_limit = self._fetch_limit(request)

        # Embed the query
        embeddings = await self._embedding.embed([request.query])
        query_embedding = embeddings[0]

        if request.use_hybrid:
            # Hybrid: RRF fusion of vector + BM25 (BH-027)
            results = await self._hybrid_rrf(
                query_embedding, request.query, fetch_limit
            )
        else:
            # Pure vector (BH-028)
            results = await self._content.search_semantic(
                query_embedding, fetch_limit
            )

        # Filter results: exclude failed-pipeline documents (BH-020)
        hits = await self._results_to_hits(results, request)
        hits = await self._boost_metadata_matches(hits, request)

        return DiscoverResponse(
            mode=RetrievalMode.SEMANTIC,
            results=hits[:request.limit],
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
        """Convert SearchResults to DiscoverHits, applying pipeline and scope filters.

        Deduplicates by document ID, keeping only the highest-scoring chunk
        per document. This prevents a single document with many matching
        chunks from crowding out other documents in the results.
        """
        seen_docs: dict[str, DiscoverHit] = {}
        doc_cache: dict[str, object | None] = {}

        for result in results:
            # Skip if we already have a higher-scoring chunk for this document
            if result.document_id in seen_docs:
                continue

            # Cache document lookups
            if result.document_id not in doc_cache:
                doc_cache[result.document_id] = await self._graph.get_document(
                    result.document_id
                )
            doc = doc_cache[result.document_id]
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
                source_path=doc.source_path,
                version_label=doc.version_label,
                project=doc.project,
                doc_type=doc.doc_type,
                tags=doc.tags,
            )

            hit = DiscoverHit(
                document=summary,
                chunk_content=result.content,
                heading_path=result.heading_path or None,
                relevance_score=result.score,
            )
            seen_docs[result.document_id] = hit

        return list(seen_docs.values())

    async def _boost_metadata_matches(
        self,
        hits: list[DiscoverHit],
        request: DiscoverRequest,
    ) -> list[DiscoverHit]:
        """Prepend documents matching the query by metadata identity fields.

        Searches title, source_path, and tags in the graph store. Documents
        found by metadata that aren't already in hits are inserted at the
        top with a synthetic score above the highest content-search score.
        This ensures documents whose identity (filename, codes, tags)
        matches the query are always surfaced, even when BM25 ranks their
        long body content low.
        """
        if not request.query:
            return hits

        metadata_docs = await self._graph.search_metadata(
            request.query, limit=request.limit
        )
        if not metadata_docs:
            return hits

        # Determine boost score: above the highest existing score
        max_score = max((h.relevance_score or 0.0 for h in hits), default=0.0)
        boost_base = max_score + 0.1 if max_score > 0 else 1.0

        existing_ids = {h.document.id for h in hits}
        boosted: list[DiscoverHit] = []

        for i, doc in enumerate(metadata_docs):
            if doc.id in existing_ids:
                continue
            if doc.pipeline_status == PipelineStatus.FAILED:
                continue
            if not self._passes_scope(doc, request):
                continue

            summary = DocumentSummary(
                id=doc.id,
                title=doc.title,
                lifecycle_status=doc.lifecycle_status,
                source_type=doc.source_type,
                source_path=doc.source_path,
                version_label=doc.version_label,
                project=doc.project,
                doc_type=doc.doc_type,
                tags=doc.tags,
            )
            boosted.append(DiscoverHit(
                document=summary,
                chunk_content=None,
                heading_path=None,
                relevance_score=boost_base - (i * 0.001),
            ))

        return boosted + hits

    def _passes_scope(self, doc: Document, request: DiscoverRequest) -> bool:
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
            if filters.pipeline_status:
                if doc.pipeline_status.value != filters.pipeline_status:
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
            source_path=doc.source_path,
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
