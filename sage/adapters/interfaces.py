"""Abstract base classes for swappable external dependencies.

Production implementations: LanceDB (ContentStore), sentence-transformers
(EmbeddingProvider), MLX/Qwen3 (AbstractionProvider). Stubs in stubs.py
provide deterministic behavior for testing.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass

# Reserved heading_path marker for the per-document synthetic header chunk
# carrying title, source filename stem, tags, semantic_abstract, and
# case-split identifier tokens (T-0038, F9). Body chunks never use this
# marker; backfill and stage-3 refresh match on it via equality.
SYNTHETIC_HEADER_HEADING_PATH = "__document_header__"


@dataclass
class Chunk:
    """A chunk of document content for indexing."""

    document_id: str
    heading_path: str
    content: str
    embedding: list[float] | None = None
    chunk_index: int = 0
    doc_type: str | None = None
    lifecycle_status: str | None = None
    project: str | None = None


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
    async def replace_synthetic_header_chunk(self, document_id: str, chunk: Chunk) -> None:
        """Replace the synthetic document-header chunk for a document.

        Targeted delete-where + insert + FTS rebuild scoped to the chunk
        with ``heading_path == SYNTHETIC_HEADER_HEADING_PATH``. Body chunks
        for the document are not touched. Used by Stage 3 abstraction
        completion and reabstract to refresh the header once
        ``semantic_abstract`` is populated (T-0038).
        """

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
        self,
        document_id: str,
        metadata: dict[str, str | None],
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
    async def get_heading_paths(self, document_id: str) -> list[str]:
        """Return distinct heading paths for a document in document order.

        Used to populate available_headings in HeadingNotFoundError
        so callers can see what headings actually exist.
        """

    @abstractmethod
    async def has_chunks(self, document_id: str) -> bool:
        """Return True if at least one chunk exists for the document.

        Lightweight existence check without loading chunk content.
        Used by reabstract for synchronous validation before
        dispatching background work.
        """

    @abstractmethod
    async def get_all_chunks(self, document_id: str) -> list[Chunk]:
        """Return all chunks for a document in document order.

        Used by export_projection to reconstruct the projection text.
        """

    @abstractmethod
    async def count_chunks(self) -> int:
        """Return the total number of chunk rows across all documents.

        Returns 0 when the underlying table has not been created yet.
        """


class EmbeddingProvider(ABC):
    """Interface for text embedding (sentence-transformers in production)."""

    @abstractmethod
    async def embed(self, texts: list[str]) -> list[list[float]]:
        """Embed a batch of texts. Returns list of embedding vectors."""


class AbstractionProvider(ABC):
    """Interface for semantic abstract generation (MLX/Qwen3 in production)."""

    @abstractmethod
    async def generate_abstract(self, text: str, max_tokens: int, doc_type: str | None) -> str:
        """Generate a density-proportional semantic abstract.

        doc_type is surfaced to the model so it can pick the right
        descriptive verbs (prescribes, argues, narrates, defines) and
        avoid restating identifying metadata the agent already sees.
        Pass None when no doc_type is available.
        """
