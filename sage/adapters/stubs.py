"""Deterministic stub implementations for testing.

These return predictable results and require no external services.
"""

import math
from datetime import timedelta

from sage.adapters.interfaces import (
    SYNTHETIC_HEADER_HEADING_PATH,
    AbstractionProvider,
    Chunk,
    ContentStore,
    ContentStoreOptimizeSnapshot,
    EmbeddingProvider,
    SearchResult,
)


class StubContentStore(ContentStore):
    """In-memory content store for testing.

    Supports indexing, removal, semantic search (cosine similarity),
    BM25-style keyword search, and heading prefix retrieval for
    deterministic mode.
    """

    def __init__(self) -> None:
        self._store: dict[str, list[Chunk]] = {}

    async def index_chunks(self, document_id: str, chunks: list[Chunk]) -> None:
        self._store[document_id] = chunks

    async def replace_synthetic_header_chunk(self, document_id: str, chunk: Chunk) -> None:
        """Replace the synthetic header chunk for a document.

        Drops any existing chunk with
        ``heading_path == SYNTHETIC_HEADER_HEADING_PATH`` and inserts the
        new one; body chunks are left in place.
        """
        existing = self._store.get(document_id, [])
        body = [c for c in existing if c.heading_path != SYNTHETIC_HEADER_HEADING_PATH]
        self._store[document_id] = [chunk, *body]

    async def remove_document(self, document_id: str) -> None:
        self._store.pop(document_id, None)

    async def search_semantic(
        self,
        query_embedding: list[float],
        limit: int = 10,
        filters: dict[str, str | list[str]] | None = None,
    ) -> list[SearchResult]:
        """Cosine similarity search across all indexed chunks."""
        scored: list[tuple[float, Chunk]] = []
        for chunks in self._store.values():
            for chunk in chunks:
                if not _chunk_matches_filters(chunk, filters):
                    continue
                if chunk.embedding is not None:
                    sim = _cosine_similarity(query_embedding, chunk.embedding)
                    scored.append((sim, chunk))

        scored.sort(key=lambda x: x[0], reverse=True)
        return [
            SearchResult(
                document_id=chunk.document_id,
                heading_path=chunk.heading_path,
                content=chunk.content,
                score=score,
            )
            for score, chunk in scored[:limit]
        ]

    async def search_bm25(
        self,
        query: str,
        limit: int = 10,
        filters: dict[str, str | list[str]] | None = None,
    ) -> list[SearchResult]:
        """Simple term-frequency keyword search for testing."""
        terms = query.lower().split()
        if not terms:
            return []

        scored: list[tuple[float, Chunk]] = []
        for chunks in self._store.values():
            for chunk in chunks:
                if not _chunk_matches_filters(chunk, filters):
                    continue
                content_lower = chunk.content.lower()
                # Score = fraction of query terms found in content
                matches = sum(1 for t in terms if t in content_lower)
                if matches > 0:
                    score = matches / len(terms)
                    scored.append((score, chunk))

        scored.sort(key=lambda x: x[0], reverse=True)
        return [
            SearchResult(
                document_id=chunk.document_id,
                heading_path=chunk.heading_path,
                content=chunk.content,
                score=score,
            )
            for score, chunk in scored[:limit]
        ]

    async def update_chunk_metadata(
        self,
        document_id: str,
        metadata: dict[str, str | None],
    ) -> None:
        """Update metadata on stored chunks for a document."""
        chunks = self._store.get(document_id, [])
        for chunk in chunks:
            if "doc_type" in metadata:
                chunk.doc_type = metadata["doc_type"]
            if "lifecycle_status" in metadata:
                chunk.lifecycle_status = metadata["lifecycle_status"]
            if "project" in metadata:
                chunk.project = metadata["project"]

    async def get_chunks_by_heading_prefix(
        self, document_id: str, heading_prefix: str
    ) -> list[Chunk]:
        """Return chunks whose heading_path starts with the given prefix."""
        chunks = self._store.get(document_id, [])
        matched = [
            c
            for c in chunks
            if c.heading_path == heading_prefix or c.heading_path.startswith(heading_prefix + " > ")
        ]
        matched.sort(key=lambda c: c.chunk_index)
        return matched

    async def get_heading_paths(self, document_id: str) -> list[str]:
        """Return distinct heading paths in document order.

        Excludes the synthetic header chunk marker; that chunk
        is an internal retrieval surface and is not a real heading.
        """
        chunks = self._store.get(document_id, [])
        seen: set[str] = set()
        paths: list[str] = []
        for chunk in sorted(chunks, key=lambda c: c.chunk_index):
            if chunk.heading_path == SYNTHETIC_HEADER_HEADING_PATH:
                continue
            if chunk.heading_path not in seen:
                seen.add(chunk.heading_path)
                paths.append(chunk.heading_path)
        return paths

    async def has_chunks(self, document_id: str) -> bool:
        """Return True if at least one chunk exists for the document."""
        return len(self._store.get(document_id, [])) > 0

    async def get_all_chunks(self, document_id: str) -> list[Chunk]:
        """Return all chunks for a document in document order."""
        chunks = self._store.get(document_id, [])
        return sorted(chunks, key=lambda c: c.chunk_index)

    async def count_chunks(self) -> int:
        """Return the total number of chunk rows across all documents."""
        return sum(len(chunks) for chunks in self._store.values())

    async def optimize(self, cleanup_older_than: timedelta) -> ContentStoreOptimizeSnapshot:
        """No-op: the in-memory stub has no on-disk presence to reclaim.

        Returns a zero-valued snapshot so callers that route
        substrate-agnostically through the ContentStore interface receive
        a well-formed payload without special-casing.
        """
        return ContentStoreOptimizeSnapshot(
            pre_bytes=0,
            post_bytes=0,
            pre_versions=0,
            post_versions=0,
            pre_fragments=0,
            post_fragments=0,
            pre_small_fragments=0,
            post_small_fragments=0,
        )


def _chunk_matches_filters(
    chunk: Chunk,
    filters: dict[str, str | list[str]] | None,
) -> bool:
    """Check whether a chunk matches all filter predicates."""
    if not filters:
        return True
    for key, value in filters.items():
        actual = getattr(chunk, key, None)
        if isinstance(value, list):
            if actual not in value:
                return False
        elif actual != value:
            return False
    return True


def _cosine_similarity(a: list[float], b: list[float]) -> float:
    """Compute cosine similarity between two vectors."""
    if len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(x * x for x in b))
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return dot / (norm_a * norm_b)


class StubEmbeddingProvider(EmbeddingProvider):
    """Returns deterministic zero vectors for testing."""

    DIMENSIONS = 768

    async def embed(self, texts: list[str]) -> list[list[float]]:
        return [[0.0] * self.DIMENSIONS for _ in texts]


class SeededEmbeddingProvider(EmbeddingProvider):
    """Returns deterministic embeddings seeded from text content.

    Produces distinct non-zero vectors so that cosine similarity tests
    return meaningful ranking. Each text gets a vector where dimensions
    are derived from the hash of the text.
    """

    DIMENSIONS = 768

    async def embed(self, texts: list[str]) -> list[list[float]]:
        import hashlib

        results = []
        for text in texts:
            h = hashlib.sha256(text.encode()).digest()
            # Use bytes to seed a deterministic vector
            vec = [0.0] * self.DIMENSIONS
            for i in range(min(len(h), self.DIMENSIONS)):
                vec[i] = (h[i] - 128) / 128.0
            results.append(vec)
        return results


class StubAbstractionProvider(AbstractionProvider):
    """Returns deterministic abstract text for testing."""

    async def generate_abstract(self, text: str, max_tokens: int, doc_type: str | None) -> str:
        return f"Stub abstract for {len(text)} chars of input."


class FailingAbstractionProvider(AbstractionProvider):
    """Always fails -- for testing BH-024 (LLM failure = failed status)."""

    async def generate_abstract(self, text: str, max_tokens: int, doc_type: str | None) -> str:
        raise RuntimeError("LLM unavailable (simulated failure)")
