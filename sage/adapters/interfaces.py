"""Abstract base classes for swappable external dependencies.

Production implementations: LanceDB (ContentStore), sentence-transformers
(EmbeddingProvider), MLX/Qwen3 (AbstractionProvider). Stubs in stubs.py
provide deterministic behavior for testing.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass
class Chunk:
    """A chunk of document content for indexing."""

    document_id: str
    heading_path: str
    content: str
    embedding: list[float] | None = None
    chunk_index: int = 0


@dataclass
class SearchResult:
    """A result from content store search."""

    document_id: str
    heading_path: str
    content: str
    score: float


class ContentStore(ABC):
    """Interface for vector/full-text content store (LanceDB in production)."""

    @abstractmethod
    async def index_chunks(self, document_id: str, chunks: list[Chunk]) -> None:
        """Store embedded chunks for a document."""

    @abstractmethod
    async def remove_document(self, document_id: str) -> None:
        """Remove all chunks for a document (used in force re-ingestion)."""

    @abstractmethod
    async def search_semantic(
        self, query_embedding: list[float], limit: int = 10
    ) -> list[SearchResult]:
        """Vector similarity search."""

    @abstractmethod
    async def search_bm25(self, query: str, limit: int = 10) -> list[SearchResult]:
        """BM25 keyword search."""

    @abstractmethod
    async def get_chunks_by_heading_prefix(
        self, document_id: str, heading_prefix: str
    ) -> list[Chunk]:
        """Return chunks whose heading_path starts with the given prefix.

        Used by deterministic retrieval mode. Returns chunks in document
        order (by chunk_index).
        """


class EmbeddingProvider(ABC):
    """Interface for text embedding (sentence-transformers in production)."""

    @abstractmethod
    async def embed(self, texts: list[str]) -> list[list[float]]:
        """Embed a batch of texts. Returns list of embedding vectors."""


class AbstractionProvider(ABC):
    """Interface for semantic abstract generation (MLX/Qwen3 in production)."""

    @abstractmethod
    async def generate_abstract(self, text: str, max_tokens: int) -> str:
        """Generate a density-proportional semantic abstract."""
