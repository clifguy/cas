"""Deterministic stub implementations for testing.

These return predictable results and require no external services.
"""

from sage.adapters.interfaces import (
    AbstractionProvider,
    Chunk,
    ContentStore,
    EmbeddingProvider,
    SearchResult,
)


class StubContentStore(ContentStore):
    """In-memory content store for testing."""

    def __init__(self) -> None:
        self._store: dict[str, list[Chunk]] = {}

    async def index_chunks(self, document_id: str, chunks: list[Chunk]) -> None:
        self._store[document_id] = chunks

    async def remove_document(self, document_id: str) -> None:
        self._store.pop(document_id, None)

    async def search_semantic(
        self, query_embedding: list[float], limit: int = 10
    ) -> list[SearchResult]:
        return []

    async def search_bm25(self, query: str, limit: int = 10) -> list[SearchResult]:
        return []


class StubEmbeddingProvider(EmbeddingProvider):
    """Returns deterministic zero vectors for testing."""

    DIMENSIONS = 768

    async def embed(self, texts: list[str]) -> list[list[float]]:
        return [[0.0] * self.DIMENSIONS for _ in texts]


class StubAbstractionProvider(AbstractionProvider):
    """Returns deterministic abstract text for testing."""

    async def generate_abstract(self, text: str, max_tokens: int) -> str:
        return f"Stub abstract for {len(text)} chars of input."


class FailingAbstractionProvider(AbstractionProvider):
    """Always fails -- for testing BH-024 (LLM failure = failed status)."""

    async def generate_abstract(self, text: str, max_tokens: int) -> str:
        raise RuntimeError("LLM unavailable (simulated failure)")
