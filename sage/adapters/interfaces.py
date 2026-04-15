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
    doc_type: str | None = None


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
        self,
        query_embedding: list[float],
        limit: int = 10,
        filters: dict[str, str | list[str]] | None = None,
    ) -> list[SearchResult]:
        """Vector similarity search.

        filters: optional pre-filter predicates (e.g. {"doc_type": "patent_draft"}).
        Values may be a single string (equality) or a list of strings
        (IN clause).  When provided, only chunks matching all predicates
        are searched.
        """

    @abstractmethod
    async def search_bm25(
        self,
        query: str,
        limit: int = 10,
        filters: dict[str, str | list[str]] | None = None,
    ) -> list[SearchResult]:
        """BM25 keyword search.

        filters: optional pre-filter predicates (e.g. {"doc_type": "patent_draft"}).
        Values may be a single string (equality) or a list of strings
        (IN clause).  When provided, only chunks matching all predicates
        are searched.
        """

    @abstractmethod
    async def update_chunk_metadata(
        self, document_id: str, metadata: dict[str, str | None],
    ) -> None:
        """Update metadata columns on all chunks for a document.

        Used to sync content-store metadata when document metadata changes
        (e.g. doc_type reassignment via update_metadata).
        """

    @abstractmethod
    async def get_chunks_by_heading_prefix(
        self, document_id: str, heading_prefix: str
    ) -> list[Chunk]:
        """Return chunks whose heading_path starts with the given prefix.

        Used by deterministic retrieval mode. Returns chunks in document
        order (by chunk_index).
        """

    @abstractmethod
    async def get_all_chunks(self, document_id: str) -> list[Chunk]:
        """Return all chunks for a document in document order.

        Used by export_projection to reconstruct the projection text.
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
