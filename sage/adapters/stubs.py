"""Deterministic stub implementations for testing.

These return predictable results and require no external services.
"""

import math
from datetime import datetime, timedelta

from sage.adapters.interfaces import (
    SYNTHETIC_HEADER_HEADING_PATH,
    AbstractionProvider,
    Chunk,
    ContentStore,
    ContentStoreOptimizeSnapshot,
    EmbeddingProvider,
    GraphStore,
    SearchResult,
)
from sage.models.enums import ResolutionPolicy
from sage.models.graph_rows import EdgeQueryRow, LinkReadContext, OnConflict
from sage.models.schemas import Document, Edge, LinkRequest, StagingEdge, User


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

    async def count_retained_versions(self) -> int:
        """Return 0: the in-memory stub has no on-disk version history."""
        return 0

    async def count_small_fragments(self) -> int:
        """Return 0: the in-memory stub has no on-disk fragments."""
        return 0

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


class StubGraphStore(GraphStore):
    """In-memory graph store for hermetic tests.

    Seam-proof, not a full graph engine: documents, edges, staging edges, and
    users get straightforward in-memory CRUD plus simple counts, which covers
    vault-owner bootstrap, the substitutability path, and most hermetic service
    tests. Methods whose correctness depends on query/filter semantics, atomic
    multi-step transactions, lineage/retraction resolution, or graph traversal
    raise ``NotImplementedError`` until a test needs them, so a coincidental
    empty-result pass can never masquerade as real behavior. ``close`` records
    its call count so ownership/cleanup tests can assert it was not closed.
    """

    def __init__(self) -> None:
        self._docs: dict[str, Document] = {}
        self._edges: dict[str, Edge] = {}
        self._staging: dict[str, StagingEdge] = {}
        self._users: dict[str, User] = {}
        self.close_calls: int = 0

    @staticmethod
    def _unsupported(method: str) -> NotImplementedError:
        return NotImplementedError(
            f"StubGraphStore.{method} is not implemented; extend the stub when a "
            f"test needs this behavior rather than relying on an empty result."
        )

    # --- Lifecycle ---
    async def initialize(self, migrate: bool = False) -> None:
        return None

    async def close(self) -> None:
        self.close_calls += 1

    # --- Documents ---
    async def insert_document(self, doc: Document) -> None:
        self._docs[doc.id] = doc

    async def get_document(self, doc_id: str) -> Document | None:
        return self._docs.get(doc_id)

    async def update_document(self, doc_id: str, updates: dict) -> Document | None:
        doc = self._docs.get(doc_id)
        if doc is None:
            return None
        updated = doc.model_copy(update=updates)
        self._docs[doc_id] = updated
        return updated

    async def list_all_documents(self) -> list[Document]:
        return list(self._docs.values())

    async def query_documents(
        self,
        filters: dict[str, object] | None = None,
        limit: int = 100,
        offset: int = 0,
        sort_by: str | None = None,
        sort_order: str | None = None,
        *,
        default_exclude_failed: bool = True,
    ) -> tuple[list[Document], int]:
        raise self._unsupported("query_documents")

    async def find_by_source_path(self, source_path: str) -> list[Document]:
        return [d for d in self._docs.values() if d.source_path == source_path]

    async def find_documents_by_title(self, title: str) -> list[Document]:
        return [d for d in self._docs.values() if d.title == title]

    async def search_metadata(self, query: str, limit: int = 20) -> list[Document]:
        raise self._unsupported("search_metadata")

    async def search_abstracts(self, query: str, limit: int = 20) -> list[Document]:
        raise self._unsupported("search_abstracts")

    # --- Tier3 unique indexes ---
    async def ensure_tier3_unique_index(self, doc_type: str, field: str) -> None:
        raise self._unsupported("ensure_tier3_unique_index")

    async def drop_tier3_unique_index(self, doc_type: str, field: str) -> None:
        raise self._unsupported("drop_tier3_unique_index")

    async def tier3_unique_index_exists(self, doc_type: str, field: str) -> bool:
        raise self._unsupported("tier3_unique_index_exists")

    async def find_chain_heads_with_tier3_value(
        self, doc_type: str, field: str
    ) -> list[tuple[object, list[str]]]:
        raise self._unsupported("find_chain_heads_with_tier3_value")

    # --- Edges ---
    async def insert_edge(self, edge: Edge, on_conflict: OnConflict = "raise") -> tuple[Edge, bool]:
        existing = await self.find_edge_by_natural_key(
            edge.source_id, edge.target_id, edge.edge_type
        )
        if existing is not None:
            if on_conflict == "noop":
                return existing, False
            raise ValueError("edge natural key already exists")
        self._edges[edge.id] = edge
        return edge, True

    async def find_edge_by_natural_key(
        self, source_id: str, target_id: str | None, edge_type: str
    ) -> Edge | None:
        for e in self._edges.values():
            if e.source_id == source_id and e.target_id == target_id and e.edge_type == edge_type:
                return e
        return None

    async def supersede_atomic(
        self, predecessor_id: str, predecessor_updates: dict, edge: Edge
    ) -> Document | None:
        raise self._unsupported("supersede_atomic")

    async def insert_with_supersede_atomic(
        self,
        new_doc: Document,
        predecessor_id: str,
        predecessor_updates: dict,
        edge: Edge,
    ) -> tuple[Document, Document]:
        raise self._unsupported("insert_with_supersede_atomic")

    async def get_edges_by_source(self, source_id: str, edge_type: str | None = None) -> list[Edge]:
        return [
            e
            for e in self._edges.values()
            if e.source_id == source_id and (edge_type is None or e.edge_type == edge_type)
        ]

    async def get_edges_by_target(self, target_id: str, edge_type: str | None = None) -> list[Edge]:
        return [
            e
            for e in self._edges.values()
            if e.target_id == target_id and (edge_type is None or e.edge_type == edge_type)
        ]

    async def query_edges(
        self,
        *,
        filters: dict[str, object] | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> tuple[list[EdgeQueryRow], int]:
        raise self._unsupported("query_edges")

    async def get_supersedes_lineage(self, doc_id: str) -> list[str]:
        raise self._unsupported("get_supersedes_lineage")

    async def has_supersedes_successor(self, doc_id: str) -> bool:
        raise self._unsupported("has_supersedes_successor")

    async def has_supersedes_predecessor(self, doc_id: str) -> bool:
        raise self._unsupported("has_supersedes_predecessor")

    async def find_tombstone_candidates(self, lineage_ids: list[str]) -> list[str]:
        raise self._unsupported("find_tombstone_candidates")

    async def merge_atomic(
        self,
        merged_from_edge: Edge,
        tombstone_edge_ids: list[str],
        tombstone_version: str,
    ) -> None:
        raise self._unsupported("merge_atomic")

    async def read_link_context(
        self, request: LinkRequest, policy: ResolutionPolicy
    ) -> LinkReadContext:
        raise self._unsupported("read_link_context")

    async def get_retracts_for_edges(self, edge_ids: list[str]) -> dict[str, list[Edge]]:
        raise self._unsupported("get_retracts_for_edges")

    async def get_edge(self, edge_id: str) -> Edge | None:
        return self._edges.get(edge_id)

    async def delete_edge(self, edge_id: str) -> bool:
        return self._edges.pop(edge_id, None) is not None

    async def find_documents_by_hashes(self, hashes: list[str]) -> dict[str, str]:
        wanted = set(hashes)
        return {
            d.source_content_hash: d.id
            for d in self._docs.values()
            if d.source_content_hash in wanted
        }

    # --- Staging edges ---
    async def list_staging_edges(self) -> list[StagingEdge]:
        return list(self._staging.values())

    async def get_staging_edge(self, edge_id: str) -> StagingEdge | None:
        return self._staging.get(edge_id)

    async def insert_staging_edge(
        self, edge: StagingEdge, on_conflict: OnConflict = "raise"
    ) -> tuple[StagingEdge, bool]:
        for e in self._staging.values():
            if (
                e.source_id == edge.source_id
                and e.target_id == edge.target_id
                and e.edge_type == edge.edge_type
            ):
                if on_conflict == "noop":
                    return e, False
                raise ValueError("staging edge natural key already exists")
        self._staging[edge.id] = edge
        return edge, True

    async def delete_staging_edge(self, edge_id: str) -> bool:
        return self._staging.pop(edge_id, None) is not None

    async def count_staging_edges(self) -> int:
        return len(self._staging)

    # --- Statistics ---
    async def get_document_counts_by_field(self, field: str) -> dict[str, int]:
        raise self._unsupported("get_document_counts_by_field")

    async def get_edge_counts_by_type(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for e in self._edges.values():
            counts[e.edge_type] = counts.get(e.edge_type, 0) + 1
        return counts

    async def get_total_document_count(self) -> int:
        return len(self._docs)

    async def get_total_edge_count(self) -> int:
        return len(self._edges)

    async def get_last_ingestion_at(self) -> datetime | None:
        raise self._unsupported("get_last_ingestion_at")

    async def count_documents_by_pipeline_status(self, status: str) -> int:
        raise self._unsupported("count_documents_by_pipeline_status")

    async def list_pending_metadata_documents(self) -> list[Document]:
        raise self._unsupported("list_pending_metadata_documents")

    # --- Traversal ---
    async def traverse(
        self, start_id: str, edge_type: str | None, direction: str, depth: int
    ) -> list[dict]:
        raise self._unsupported("traverse")

    async def chain_walk(self, start_id: str, edge_type: str) -> list[dict]:
        raise self._unsupported("chain_walk")

    async def list_provenance_edges(self, edge_types: list[str]) -> list[dict]:
        raise self._unsupported("list_provenance_edges")

    async def head_with_hash_for_chain(self, target_id: str, edge_type: str = "supersedes") -> dict:
        raise self._unsupported("head_with_hash_for_chain")

    # --- Users ---
    async def insert_user(self, user: User) -> None:
        self._users[user.id] = user

    async def get_user(self, user_id: str) -> User | None:
        return self._users.get(user_id)

    async def get_user_by_display_name(self, display_name: str) -> User | None:
        for u in self._users.values():
            if u.display_name == display_name:
                return u
        return None

    async def list_users(self) -> list[User]:
        return list(self._users.values())
