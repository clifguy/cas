"""Deterministic stub implementations for testing.

These return predictable results and require no external services.
"""

import math
import re
from collections.abc import Sequence
from datetime import datetime, timedelta

from sage.adapters.interfaces import (
    HEADING_PATH_SEPARATOR,
    LEGACY_DOCUMENT_HEADER_HEADING_PATH,
    NON_CANONICAL_SOURCE_PATH_PATTERN,
    AbstractionProvider,
    Chunk,
    ContentStore,
    ContentStoreOptimizeSnapshot,
    DocumentSurface,
    EmbeddingProvider,
    FacetFieldCounts,
    GraphStore,
    KeywordQueryParse,
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

    **The passage reads model a migrated store.** A caller can seed a legacy
    document-level row here -- the port's marker block states when a real vault
    still holds one -- and the reads that merely enumerate or return passages
    (``get_heading_paths``, ``get_all_chunks``, ``search_semantic``) will include
    it, where the Postgres binding's passage scoping excludes it. The divergence
    is deliberate: the guarded behaviour is a property of the real binding, so a
    test asserting that a passage read excludes a legacy row is evidence about
    that binding and has to be written against it.

    ``search_bm25`` is guarded all the same, and that asymmetry is the point:
    there the marker changes what *matches*, and a double that let a legacy
    row's derived text satisfy a caller's term would be modelling the wrong
    contract rather than a narrower one.
    """

    def __init__(self) -> None:
        self._store: dict[str, list[Chunk]] = {}
        self._surfaces: dict[str, DocumentSurface] = {}

    async def index_chunks(self, document_id: str, chunks: list[Chunk]) -> None:
        self._store[document_id] = chunks

    async def upsert_document_surface(self, surface: DocumentSurface) -> None:
        """Write a document's document-level row; its passages are untouched.

        Stored, but deliberately not consulted by this stub's ``search_bm25``,
        which models neither the two-surface union nor the provenance bar. A
        test about either is not evidence about any binding and belongs against
        a real backend.
        """
        self._surfaces[surface.document_id] = surface

    async def remove_document_surface(self, document_id: str) -> None:
        self._surfaces.pop(document_id, None)

    def stored_document_surface(self, document_id: str) -> DocumentSurface | None:
        """Return the stored document-level row, for assertions in tests."""
        return self._surfaces.get(document_id)

    async def update_document_surface_text(
        self, document_id: str, matchable: str, orienting: str
    ) -> bool:
        surface = self._surfaces.get(document_id)
        if surface is None:
            return False
        surface.matchable = matchable
        surface.orienting = orienting
        return True

    async def update_indexed_structure(
        self, document_id: str, derived: Sequence[tuple[str, str]]
    ) -> int:
        wanted = dict(derived)
        written = 0
        for chunk in self._store.get(document_id, []):
            if chunk.heading_path in wanted:
                chunk.indexed_structure = wanted[chunk.heading_path]
                written += 1
        return written

    async def remove_document(self, document_id: str) -> None:
        self._store.pop(document_id, None)
        self._surfaces.pop(document_id, None)

    # -- migration off the single-surface layout (CAS-ADR-049) ---------------

    async def legacy_document_header_rows(self) -> list[tuple[str, list[float] | None]]:
        return [
            (document_id, chunk.embedding)
            for document_id, chunks in self._store.items()
            for chunk in chunks
            if chunk.heading_path == LEGACY_DOCUMENT_HEADER_HEADING_PATH
        ]

    async def delete_legacy_document_header_rows(self) -> int:
        removed = 0
        for document_id, chunks in self._store.items():
            kept = [c for c in chunks if c.heading_path != LEGACY_DOCUMENT_HEADER_HEADING_PATH]
            removed += len(chunks) - len(kept)
            self._store[document_id] = kept
        return removed

    # -- migration to the relative indexed structure (CAS-ADR-049 Decision 3) --

    async def passages_awaiting_indexed_structure(self) -> list[tuple[str, str]]:
        seen: dict[tuple[str, str], None] = {}
        for document_id, chunks in self._store.items():
            for chunk in chunks:
                if chunk.indexed_structure is None:
                    seen.setdefault((document_id, chunk.heading_path))
        return list(seen)

    async def passage_vector_ranks_indexed_structure(self) -> bool:
        """Always true: this double has no generated vector that could be stale.

        The Postgres binding's keyword vector is a stored generated column, so a
        vault provisioned before the decision carries one built from the address
        until a rebuild replaces it. Nothing here corresponds to that, and
        reporting a repair as pending would make the double ask for work it
        cannot perform.
        """
        return True

    async def migrate_indexed_structure(self, derived: Sequence[tuple[str, str, str]]) -> int:
        wanted = {(doc, path): structure for doc, path, structure in derived}
        written = 0
        for document_id, chunks in self._store.items():
            for chunk in chunks:
                if chunk.indexed_structure is not None:
                    continue
                structure = wanted.get((document_id, chunk.heading_path))
                if structure is None:
                    continue
                chunk.indexed_structure = structure
                written += 1
        return written

    async def search_semantic(
        self,
        query_embedding: list[float],
        limit: int = 10,
        filters: dict[str, str | list[str]] | None = None,
    ) -> list[SearchResult]:
        """Cosine similarity search across passages and document surfaces.

        Both surfaces compete in one ranking, as the real binding does
        (CAS-ADR-049). Similarity is not matching, so the provenance bar does
        not apply here and this double can model the union faithfully -- unlike
        ``search_bm25``, where it deliberately does not.
        """
        scored: list[tuple[float, SearchResult]] = []
        for chunks in self._store.values():
            for chunk in chunks:
                if not _chunk_matches_filters(chunk, filters):
                    continue
                if chunk.embedding is not None:
                    sim = _cosine_similarity(query_embedding, chunk.embedding)
                    scored.append(
                        (
                            sim,
                            SearchResult(
                                document_id=chunk.document_id,
                                heading_path=chunk.heading_path,
                                content=chunk.content,
                                score=sim,
                            ),
                        )
                    )

        for surface in self._surfaces.values():
            if not _chunk_matches_filters(surface, filters):
                continue
            if surface.embedding is not None:
                sim = _cosine_similarity(query_embedding, surface.embedding)
                scored.append(
                    (
                        sim,
                        # No excerpt and no passage count, as the real binding
                        # returns: a document-level row is not a passage, and
                        # its stored halves carry the index-side expansion
                        # rather than the document's own text (CAS-ADR-049).
                        SearchResult(
                            document_id=surface.document_id,
                            heading_path="",
                            content="",
                            score=sim,
                            matched_chunk_count=0,
                            is_document_surface=True,
                        ),
                    )
                )

        scored.sort(key=lambda x: x[0], reverse=True)
        return [result for _, result in scored[:limit]]

    async def search_bm25(
        self,
        query: str,
        limit: int = 10,
        filters: dict[str, str | list[str]] | None = None,
    ) -> list[SearchResult]:
        """Substring keyword search scoped to the document (CAS-ADR-048).

        The terms come from this store's own ``parse_keyword_query`` rather
        than from a second split of the query text, so the two cannot drift
        into stating one matching rule and applying another. A document matches
        when the union of its authored chunks carries every reported term --
        the terms need not co-occur in any one of them -- and a parse reporting
        alternatives is applied as one.

        The chunk stays the ranking and excerpt unit: a matching document is
        returned once, scored by its best chunk and carrying that chunk's
        ``heading_path`` and ``content``, with ``matched_chunk_count`` counting
        the authored chunks bearing on the query. ``limit`` is a document
        budget. Filter predicates select the slice before the union is taken,
        so a filter only ever narrows.

        A legacy document-header row is barred from the match union and from
        the count: it carries machine-generated and incidental text, which
        ranks and orients but never satisfies a match (CAS-ADR-049). It stays
        in the ranking pool, so a document's score still reflects it, while an
        authored passage always supplies the excerpt. Nothing writes such a row
        any more -- document-level text has a surface of its own -- but a vault
        awaiting its migration still holds one per document, and this is what
        keeps it out of a match until then.

        The document surface itself is stored by this double and never
        consulted here, so the two-surface match union is not modelled: a test
        turning on a title or a tag satisfying a match, or on derived text
        failing to, belongs against a real backend.

        A chunk's indexed text is its structure relative to its document and
        its content, so a term present only in a heading is findable while a
        document title that merely roots the path is not (CAS-ADR-049 Decision
        3). The two are not weighted apart here; the production binding ranks a
        heading match above a body one, which a test turning on that ordering
        must be written against.

        Matching is substring containment over lowercased text, and the parse
        splits the query on whitespace. Stopwords, stemming, exclusion and
        phrase adjacency are not modelled -- the parse reports their absence
        rather than pretending otherwise -- so a test turning on any of those
        belongs against a real backend. Alternation is the near miss: the
        matching rule above honours a parse that reports one, at the same
        document scope the production binding gives it, but this double's own
        parse never reports one, so a query written with ``or`` arrives here as
        terms that include the word. Only a substituted parse reaches the rule,
        which is what the pin in the conformance suite supplies.
        """
        parse = await self.parse_keyword_query(query)
        terms = parse.terms
        if not terms:
            return []

        def _searchable(chunk: Chunk) -> str:
            """The chunk's indexed text: its structure and its content.

            The structure is the passage's own, relative to its document
            (CAS-ADR-049 Decision 3) -- so a document title a source format made
            its top-level heading is not searchable through every passage of
            that document. A passage that carries no derived structure falls
            back to its address, which is parity with the binding's generated
            column rather than a divergence: both reproduce the pre-decision
            behaviour for a vault that has taken the column but not yet the
            migration.

            A legacy document-header row is the exception: its ``heading_path``
            is an internal sentinel rather than a heading someone wrote, so
            including it would let a query for one of the sentinel's own words
            score every such row in the store. Why such a row can still be here
            to exclude is stated at ``LEGACY_DOCUMENT_HEADER_HEADING_PATH`` in
            the port; this double guards by that marker where the Postgres
            binding guards by the chunk index, and the two are written together.

            This carve-out is not parity: the Postgres binding indexes the
            marker today, because its generated column weights ``heading_path``
            whole and the text-search configuration reads the sentinel's
            underscores as separators, leaving two ordinary lexemes at the
            heading weight. The rule here is the one CAS-ADR-049 supports --
            headings *within* a document rank, and a marker no author wrote is
            not one of them -- so the divergence is the binding's artefact
            rather than this double's licence, and a test turning on it is
            evidence about the double alone.
            """
            if chunk.heading_path == LEGACY_DOCUMENT_HEADER_HEADING_PATH:
                return chunk.content.lower()
            structure = (
                chunk.indexed_structure
                if chunk.indexed_structure is not None
                else chunk.heading_path
            )
            return f"{structure} {chunk.content}".lower()

        results: list[SearchResult] = []
        for document_id, chunks in self._store.items():
            # One pass over the filtered slice. The match, the ranking, the
            # excerpt and the count are four readings of the same per-chunk
            # term hits, and deriving them from one list is what makes that a
            # single computation rather than four parallel ones.
            scored = [
                (c, [t for t in terms if t in _searchable(c)])
                for c in chunks
                if _chunk_matches_filters(c, filters)
            ]
            # Guarded by the heading path, for the reason ``_searchable`` gives
            # and under the condition the port's marker block states.
            authored = [
                (c, hits)
                for c, hits in scored
                if c.heading_path != LEGACY_DOCUMENT_HEADER_HEADING_PATH
            ]

            # The match is decided on the union of the authored slice, so a
            # term carried only by the header never enters it.
            satisfied = sum(1 for t in terms if any(t in hits for _, hits in authored))
            if not (satisfied == len(terms) if parse.all_required else satisfied):
                continue

            # The ranking pool spans the whole slice, header included, so
            # derived text lifts a document that matched on its own authored
            # text. A chunk scores by the fraction of the query's terms it
            # carries, which makes co-occurrence a ranking signal rather than
            # a matching one.
            pool = [(len(hits) / len(terms), c) for c, hits in scored if hits]
            # An authored chunk always wins the excerpt; among equals, the one
            # earliest in document order.
            _, excerpt = min(
                pool,
                key=lambda ranked: (
                    ranked[1].heading_path == LEGACY_DOCUMENT_HEADER_HEADING_PATH,
                    -ranked[0],
                    ranked[1].chunk_index,
                ),
            )
            results.append(
                SearchResult(
                    document_id=document_id,
                    heading_path=excerpt.heading_path,
                    content=excerpt.content,
                    score=max(score for score, _ in pool),
                    matched_chunk_count=sum(1 for _, hits in authored if hits),
                )
            )

        results.sort(key=lambda r: (-r.score, r.document_id))
        return results[:limit]

    async def update_chunk_metadata(
        self,
        document_id: str,
        metadata: dict[str, str | None],
    ) -> None:
        """Update metadata on both of a document's surfaces.

        Both carry the same filter columns, so updating passages alone would
        leave the document matchable by its title under the values it had
        before the change.
        """
        targets = [*self._store.get(document_id, [])]
        surface = self._surfaces.get(document_id)
        if surface is not None:
            targets.append(surface)
        for target in targets:
            if "doc_type" in metadata:
                target.doc_type = metadata["doc_type"]
            if "lifecycle_status" in metadata:
                target.lifecycle_status = metadata["lifecycle_status"]
            if "project" in metadata:
                target.project = metadata["project"]

    async def parse_keyword_query(self, query: str) -> KeywordQueryParse:
        """Whitespace terms, lowercased -- no stopword, stemming, or operator model.

        The production binding parses through a text-search configuration and
        recognises exclusion, alternation, and phrases; this double does none of
        that, so it reports no exclusions, ``all_required=True``, and no
        adjacency. Assertions about stopwords, stemming, negation, ``or``, or
        quoted phrases belong against a real backend rather than here -- ``or``
        included, even though ``search_bm25`` honours an alternation it is
        handed, because nothing this method returns is one.

        ``search_bm25`` reads ``terms`` and ``all_required`` from what this
        returns, so those two agree by construction: whatever a substituted
        parse reports required is what the search requires. It reads neither
        ``excluded`` nor ``adjacent``, because it models neither. The agreement
        therefore spans two of the four fields, and a parse that began
        reporting the other two would widen the search silently -- extend the
        search alongside the parse, not the parse alone.
        """
        return KeywordQueryParse(
            terms=tuple(query.lower().split()), excluded=(), all_required=True, adjacent=False
        )

    async def get_chunks_by_heading_prefix(
        self, document_id: str, heading_prefix: str
    ) -> list[Chunk]:
        """Return chunks whose heading_path starts with the given prefix."""
        chunks = self._store.get(document_id, [])
        matched = [
            c
            for c in chunks
            if c.heading_path == heading_prefix
            or c.heading_path.startswith(heading_prefix + HEADING_PATH_SEPARATOR)
        ]
        matched.sort(key=lambda c: c.chunk_index)
        return matched

    async def get_heading_paths(self, document_id: str) -> list[str]:
        """Return distinct heading paths in document order.

        Unguarded, as this class's passage reads are; see the class docstring.
        """
        chunks = self._store.get(document_id, [])
        seen: set[str] = set()
        paths: list[str] = []
        for chunk in sorted(chunks, key=lambda c: c.chunk_index):
            if chunk.heading_path not in seen:
                seen.add(chunk.heading_path)
                paths.append(chunk.heading_path)
        return paths

    async def has_chunks(self, document_id: str) -> bool:
        """Return True if at least one chunk exists for the document."""
        return len(self._store.get(document_id, [])) > 0

    async def get_all_chunks(self, document_id: str) -> list[Chunk]:
        """Return all chunks for a document in document order.

        Unguarded, as this class's passage reads are; see the class docstring.
        """
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

    async def measured_byte_size(self) -> int:
        """Return 0: the in-memory stub has no on-disk footprint."""
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

    async def query_document_facets(
        self,
        filters: dict[str, object] | None = None,
        *,
        fields: Sequence[str] | None = None,
        value_limit: int | None = None,
    ) -> tuple[dict[str, FacetFieldCounts], int]:
        raise self._unsupported("query_document_facets")

    async def find_documents_by_title(self, title: str) -> list[Document]:
        return [d for d in self._docs.values() if d.title == title]

    async def search_metadata(
        self,
        query: str,
        limit: int = 20,
        filters: dict[str, object] | None = None,
    ) -> list[Document]:
        raise self._unsupported("search_metadata")

    async def search_abstracts(
        self,
        query: str,
        limit: int = 20,
        filters: dict[str, object] | None = None,
    ) -> list[Document]:
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

    async def find_documents_by_source_paths(self, source_paths: list[str]) -> dict[str, str]:
        wanted = set(source_paths)
        found: dict[str, str] = {}
        # Lowest document id per path wins, so the stub answers identically to
        # the durable store rather than following its own insertion order.
        for d in sorted(self._docs.values(), key=lambda doc: doc.id):
            if d.source_path in wanted and d.source_path not in found:
                found[d.source_path] = d.source_content_hash
        return found

    async def find_document_ids_by_source_paths(
        self, source_paths: list[str]
    ) -> dict[str, list[str]]:
        wanted = set(source_paths)
        found: dict[str, list[str]] = {}
        # Every id carrying the path, in id order -- not the lowest-id-wins
        # collapse the method above applies, which answers a different question.
        for d in sorted(self._docs.values(), key=lambda doc: doc.id):
            if d.source_path in wanted:
                found.setdefault(d.source_path, []).append(d.id)
        return found

    async def list_non_canonical_source_paths(self) -> dict[str, str]:
        # The port's own pattern, not a re-derivation of it. Asking the path
        # reducer directly would look equivalent and is not: it resolves the
        # `//` root the pattern deliberately offers, so the stub would answer a
        # narrower question than the durable store and let a service test pass
        # over candidates production would really see.
        return {
            doc_id: doc.source_path
            for doc_id, doc in self._docs.items()
            if doc.source_path is not None
            and re.search(NON_CANONICAL_SOURCE_PATH_PATTERN, doc.source_path)
        }

    async def remove_document(self, document_id: str) -> None:
        self._docs.pop(document_id, None)
        self._edges = {
            eid: e
            for eid, e in self._edges.items()
            if document_id not in (e.source_id, e.target_id)
        }
        self._staging = {
            sid: s
            for sid, s in self._staging.items()
            if document_id not in (s.source_id, s.target_id)
        }

    async def find_documents_ingested_between(
        self, since: datetime, until: datetime | None = None
    ) -> list[Document]:
        matched = [
            d
            for d in self._docs.values()
            if d.created_at >= since and (until is None or d.created_at < until)
        ]
        return sorted(matched, key=lambda d: d.created_at)

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

    async def clear_pipeline_error_for_statuses(self, statuses: list[str]) -> int:
        wanted = set(statuses)
        cleared = 0
        for doc_id, doc in list(self._docs.items()):
            if doc.pipeline_error is None or doc.pipeline_status not in wanted:
                continue
            self._docs[doc_id] = doc.model_copy(update={"pipeline_error": None})
            cleared += 1
        return cleared

    async def list_pending_metadata_documents(self) -> list[Document]:
        raise self._unsupported("list_pending_metadata_documents")

    async def measured_byte_size(self) -> int:
        return 0

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
