"""Deterministic stub implementations for testing.

These return predictable results and require no external services.
"""

import math
import re
from collections.abc import Sequence
from datetime import datetime, timedelta, timezone

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
from sage.models.coverage import (
    AbstractionQuality,
    CoverageDimension,
    ProcessingCoverage,
    ProcessingIdentity,
    canonical_configuration_hash,
)
from sage.models.discovery import EvidencePackage
from sage.models.enums import ResolutionPolicy
from sage.models.graph_rows import EdgeQueryRow, LinkReadContext, OnConflict
from sage.models.schemas import Document, Edge, LinkRequest, StagingEdge


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
        self._embedding_accounting: dict[str, dict] = {}
        self._abstraction_state: dict[str, dict] = {}
        self._embedding_authorizations: dict[str, str] = {}

    def bind_processing_store(self, graph_store: GraphStore) -> None:
        self._processing_store = graph_store
        self._processing_delegate = None
        if hasattr(graph_store, "_pool"):
            from sage.adapters.content_store_postgres import PostgresContentStore

            self._processing_delegate = PostgresContentStore(graph_store._pool)

    async def read_processing_artifacts(
        self, document_id: str
    ) -> tuple[ProcessingCoverage, list[Chunk], str | None]:
        doc = await self._processing_store.get_document(document_id)
        return (
            doc.processing_coverage,
            await self.get_all_chunks(document_id),
            doc.semantic_abstract,
        )

    async def update_processing_status(
        self, document_id: str, attempt: int, updates: dict, *, processing_stage: str | None = None
    ) -> bool:
        if self._processing_delegate is not None:
            return await self._processing_delegate.update_processing_status(
                document_id, attempt, updates, processing_stage=processing_stage
            )
        doc = await self._processing_store.get_document(document_id)
        if doc.processing_coverage.attempt != attempt:
            return False
        await self._processing_store.update_document(
            document_id,
            updates,
            advance_version=False,
            processing_stage=processing_stage,
            advance_processing_attempt=False,
        )
        return True

    async def begin_processing_attempt(self, document_id: str) -> int:
        if self._processing_delegate is not None:
            return await self._processing_delegate.begin_processing_attempt(document_id)
        doc = await self._processing_store.get_document(document_id)
        attempt = doc.processing_coverage.attempt + 1
        coverage = doc.processing_coverage.model_copy(update={"attempt": attempt})
        await self._processing_store.update_document(
            document_id, {"processing_coverage": coverage}, advance_version=False
        )
        return attempt

    async def publish_processing(
        self,
        document_id: str,
        attempt: int,
        dimensions: dict[str, CoverageDimension],
        *,
        chunks: list[Chunk] | None = None,
        surface: DocumentSurface | None = None,
        semantic_abstract: str | None = None,
        abstraction_quality: AbstractionQuality | None = None,
        abstraction_candidate: dict | None = None,
        indexing_complete: bool = True,
        text_repair: bool = False,
        expected_abstract: str | None = None,
    ) -> bool:
        if text_repair and (
            dimensions
            or chunks is not None
            or semantic_abstract is None
            or abstraction_quality is not None
            or abstraction_candidate is not None
        ):
            raise ValueError("text repair cannot publish processing evidence or passages")
        if self._processing_delegate is not None:
            published = await self._processing_delegate.publish_processing(
                document_id,
                attempt,
                dimensions,
                chunks=chunks,
                surface=surface,
                semantic_abstract=semantic_abstract,
                abstraction_quality=abstraction_quality,
                abstraction_candidate=abstraction_candidate,
                indexing_complete=indexing_complete,
                text_repair=text_repair,
                expected_abstract=expected_abstract,
            )
            if published:
                if chunks is not None:
                    self._store[document_id] = chunks
                if surface is not None:
                    self._surfaces[document_id] = surface
            return published
        doc = await self._processing_store.get_document(document_id)
        if doc.processing_coverage.attempt != attempt or (
            text_repair and doc.semantic_abstract != expected_abstract
        ):
            return False
        if abstraction_quality is not None:
            from sage.models.abstraction_evidence import validate_quality_publication

            if surface is None:
                raise ValueError("abstract quality requires coherent document surface")
            validate_quality_publication(
                abstraction_quality,
                doc.source_content_hash,
                attempt,
                semantic_abstract,
                abstraction_candidate,
            )
        elif abstraction_candidate is not None:
            raise ValueError("private candidate requires quality publication")
        if chunks is not None and (ledger := self._embedding_accounting.get(document_id)):
            identity = dimensions["passage_embeddings"].identity
            if (
                identity is not None
                and ledger.get("source") == doc.source_content_hash
                and ledger["identity"]
                != canonical_configuration_hash(identity.model_dump(mode="json"))
            ):
                raise ValueError("embedding scope changed; explicit reprocessing decision required")
        if surface is not None and surface.embedding is None:
            previous = self._surfaces.get(document_id)
            if previous is not None and previous.embedding is not None:
                surface.embedding = previous.embedding
                surface.authored_embedding = bool(
                    previous.authored_embedding
                    and previous.matchable == surface.matchable
                    and doc.processing_coverage.passage_embeddings.identity is not None
                    and doc.processing_coverage.passage_embeddings.identity.source_hash
                    == doc.source_content_hash
                )
        coverage = ProcessingCoverage.model_validate(
            {
                **doc.processing_coverage.model_dump(),
                **{
                    name: {**value.model_dump(), "generation": attempt}
                    for name, value in dimensions.items()
                },
            }
        )
        if semantic_abstract is not None and not text_repair:
            coverage.served_abstraction_generation = attempt
        if abstraction_quality is not None or semantic_abstract is not None:
            coverage.abstraction_quality = abstraction_quality or AbstractionQuality()
        if abstraction_quality is not None and semantic_abstract is None:
            coverage.served_abstraction_generation = None
        updates = {"processing_coverage": coverage}
        if chunks is not None and indexing_complete:
            updates["indexed_at"] = datetime.now(timezone.utc)
        if semantic_abstract is not None or abstraction_quality is not None:
            updates["semantic_abstract"] = semantic_abstract
        await self._processing_store.update_document(
            document_id,
            updates,
            advance_version=False,
            processing_stage="abstraction" if chunks is not None and indexing_complete else None,
            advance_processing_attempt=False,
        )
        if abstraction_candidate is not None:
            self._abstraction_state.setdefault(document_id, {})["candidate"] = abstraction_candidate
        if chunks is not None:
            self._store[document_id] = chunks
        if surface is not None:
            self._surfaces[document_id] = surface
        return True

    async def save_abstraction_evidence(
        self, document_id: str, attempt: int, package: EvidencePackage
    ) -> bool:
        from sage.models.abstraction_evidence import encode_package

        if self._processing_delegate is not None:
            return await self._processing_delegate.save_abstraction_evidence(
                document_id, attempt, package
            )
        doc = await self._processing_store.get_document(document_id)
        if (
            doc.processing_coverage.attempt != attempt
            or doc.source_content_hash != package.identity.source_hash
        ):
            return False
        self._abstraction_state.setdefault(document_id, {})["package"] = encode_package(package)
        return True

    async def read_abstraction_evidence(self, document_id: str) -> EvidencePackage | None:
        from sage.models.abstraction_evidence import decode_package

        if self._processing_delegate is not None:
            return await self._processing_delegate.read_abstraction_evidence(document_id)
        data = self._abstraction_state.get(document_id, {}).get("package")
        if data is None:
            return None
        try:
            package = decode_package(data)
        except KeyError, TypeError, ValueError:
            return None
        doc = await self._processing_store.get_document(document_id)
        return package if package.identity.source_hash == doc.source_content_hash else None

    async def admit_abstraction(
        self,
        document_id: str,
        attempt: int,
        identity: ProcessingIdentity,
        input_tokens: int,
        output_tokens: int,
        *,
        max_calls: int,
        max_input: int,
        max_output: int,
    ) -> str:
        from sage.models.abstraction_evidence import reserve_generation

        if self._processing_delegate is not None:
            return await self._processing_delegate.admit_abstraction(
                document_id,
                attempt,
                identity,
                input_tokens,
                output_tokens,
                max_calls=max_calls,
                max_input=max_input,
                max_output=max_output,
            )
        doc = await self._processing_store.get_document(document_id)
        if doc.processing_coverage.attempt != attempt:
            return "stale"
        if doc.source_content_hash != identity.source_hash:
            return "scope_changed"
        state = self._abstraction_state.setdefault(document_id, {})
        result, ledger = reserve_generation(
            state.get("accounting"),
            identity,
            input_tokens,
            output_tokens,
            max_calls=max_calls,
            max_input=max_input,
            max_output=max_output,
        )
        if result == "admitted":
            state["accounting"] = ledger
        return result

    async def admit_embedding(
        self,
        document_id: str,
        attempt: int,
        identity: ProcessingIdentity,
        input_key: str,
        tokens: int,
        *,
        max_inputs: int,
        max_tokens: int,
    ) -> str:
        if self._processing_delegate is not None:
            return await self._processing_delegate.admit_embedding(
                document_id,
                attempt,
                identity,
                input_key,
                tokens,
                max_inputs=max_inputs,
                max_tokens=max_tokens,
            )
        doc = await self._processing_store.get_document(document_id)
        if doc.processing_coverage.attempt != attempt:
            return "stale"
        previous = self._embedding_accounting.get(document_id)
        if previous and previous.get("source") != identity.source_hash:
            self._embedding_accounting.pop(document_id)
        ledger = self._embedding_accounting.setdefault(
            document_id,
            {
                "identity": canonical_configuration_hash(identity.model_dump(mode="json")),
                "source": identity.source_hash,
                "inputs": {},
                "tokens": 0,
                "max_inputs": max_inputs,
                "max_tokens": max_tokens,
            },
        )
        if (
            ledger["identity"] != canonical_configuration_hash(identity.model_dump(mode="json"))
            or ledger["max_inputs"] != max_inputs
            or ledger["max_tokens"] != max_tokens
        ):
            return "scope_changed"
        if input_key in ledger["inputs"]:
            return "consumed"
        if len(ledger["inputs"]) >= max_inputs or ledger["tokens"] + tokens > max_tokens:
            return "limited"
        ledger["inputs"][input_key] = tokens
        ledger["tokens"] += tokens
        return "admitted"

    async def read_embedding_vectors(
        self, document_id: str, indices: list[int]
    ) -> dict[int, list[float]]:
        if self._processing_delegate is not None:
            return await self._processing_delegate.read_embedding_vectors(document_id, indices)
        return {
            c.chunk_index: c.embedding
            for c in self._store.get(document_id, [])
            if c.chunk_index in indices and c.embedding is not None
        }

    async def embedding_scope_is_compatible(
        self, document_id: str, identity: ProcessingIdentity
    ) -> bool:
        if self._processing_delegate is not None:
            return await self._processing_delegate.embedding_scope_is_compatible(
                document_id, identity
            )
        ledger = self._embedding_accounting.get(document_id)
        return (
            not ledger
            or ledger.get("source") != identity.source_hash
            or ledger["identity"] == canonical_configuration_hash(identity.model_dump(mode="json"))
        )

    async def authorize_embedding_scope(
        self,
        document_id: str,
        attempt: int,
        decision: str,
    ) -> bool:
        if self._processing_delegate is not None:
            return await self._processing_delegate.authorize_embedding_scope(
                document_id,
                attempt,
                decision,
            )
        doc = await self._processing_store.get_document(document_id)
        if doc.processing_coverage.attempt != attempt:
            return False
        if not decision.strip() or len(decision) > 512:
            raise ValueError("reprocessing requires a bounded operator decision")
        if self._embedding_authorizations.get(document_id) != decision:
            self._embedding_accounting.pop(document_id, None)
            self._embedding_authorizations[document_id] = decision
        return True

    async def publish_embedding_increment(
        self,
        document_id: str,
        attempt: int,
        vectors: dict[int, list[float]],
        evidence: CoverageDimension,
        *,
        indexing_complete: bool = False,
    ) -> bool:
        if self._processing_delegate is not None:
            success = await self._processing_delegate.publish_embedding_increment(
                document_id,
                attempt,
                vectors,
                evidence,
                indexing_complete=indexing_complete,
            )
            if not success:
                return False
        else:
            doc = await self._processing_store.get_document(document_id)
            if doc.processing_coverage.attempt != attempt:
                return False
            coverage = doc.processing_coverage.model_copy(
                update={
                    "passage_embeddings": CoverageDimension.model_validate(
                        {
                            **evidence.model_dump(),
                            "generation": attempt,
                        }
                    )
                }
            )
            await self._processing_store.update_document(
                document_id,
                {"processing_coverage": coverage},
                advance_version=False,
                advance_processing_attempt=False,
            )
        for chunk in self._store.get(document_id, []):
            if chunk.chunk_index in vectors:
                chunk.embedding = vectors[chunk.chunk_index]
        return True

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
        surface.authored_embedding &= surface.matchable == matchable
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

    async def documents_with_passages_longer_than(self, byte_bound: int) -> list[str]:
        return sorted(
            document_id
            for document_id, chunks in self._store.items()
            if any(
                len(c.heading_path.encode("utf-8")) + len(c.content.encode("utf-8")) > byte_bound
                for c in chunks
                if c.heading_path != LEGACY_DOCUMENT_HEADER_HEADING_PATH
            )
        )

    async def passages_awaiting_indexed_structure(self) -> list[tuple[str, str]]:
        seen: dict[tuple[str, str], None] = {}
        for document_id, chunks in self._store.items():
            for chunk in chunks:
                if chunk.indexed_structure is None:
                    seen.setdefault((document_id, chunk.heading_path))
        return list(seen)

    async def document_surface_vector_is_current(self) -> bool:
        """Always true, for the reason ``passage_vector_is_current`` gives."""
        return True

    async def rebuild_document_surface_vector(self) -> bool:
        """Never rebuilds: this double has no generated vector to rebuild."""
        return False

    async def passage_vector_is_current(self) -> bool:
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

        The budget is modelled as the binding spends it: each surface
        contributes its ``limit`` nearest rows, and those rows are collapsed to
        one per document before ``limit`` counts documents. A document is
        represented by its nearest passage wherever one was kept, scores its
        best across both surfaces, and counts the sections its kept passages
        belong to. A query with no norm has no similarity to anything and finds
        nothing, as on the binding.
        """
        if not any(query_embedding):
            return []
        passages: list[tuple[float, int, Chunk]] = []
        for chunks in self._store.values():
            for chunk in chunks:
                if not _chunk_matches_filters(chunk, filters):
                    continue
                # A zero vector has no similarity; the binding drops such a row.
                if chunk.embedding is not None and any(chunk.embedding):
                    sim = _cosine_similarity(query_embedding, chunk.embedding)
                    passages.append((sim, chunk.chunk_index, chunk))
        surfaces: list[tuple[float, str]] = []
        for surface in self._surfaces.values():
            if not surface.authored_embedding:
                continue
            if not _chunk_matches_filters(surface, filters):
                continue
            if surface.embedding is not None and any(surface.embedding):
                sim = _cosine_similarity(query_embedding, surface.embedding)
                surfaces.append((sim, surface.document_id))
        passages.sort(key=lambda x: (-x[0], x[2].document_id, x[1]))
        surfaces.sort(key=lambda x: (-x[0], x[1]))

        best: dict[str, float] = {}
        representative: dict[str, Chunk] = {}
        sections: dict[str, set[int]] = {}
        for sim, _, chunk in passages[:limit]:
            doc_id = chunk.document_id
            best[doc_id] = max(best.get(doc_id, sim), sim)
            representative.setdefault(doc_id, chunk)
            sections.setdefault(doc_id, set()).add(chunk.section_key)
        for sim, doc_id in surfaces[:limit]:
            best[doc_id] = max(best.get(doc_id, sim), sim)

        results: list[SearchResult] = []
        for doc_id in sorted(best, key=lambda d: (-best[d], d))[:limit]:
            chunk = representative.get(doc_id)
            if chunk is None:
                # No excerpt and no passage count, as the real binding returns:
                # a document-level row is not a passage, and its stored halves
                # carry the index-side expansion rather than the document's own
                # text (CAS-ADR-049).
                results.append(
                    SearchResult(
                        document_id=doc_id,
                        heading_path="",
                        content="",
                        score=best[doc_id],
                        matched_chunk_count=0,
                        is_document_surface=True,
                    )
                )
                continue
            results.append(
                SearchResult(
                    document_id=doc_id,
                    heading_path=chunk.heading_path,
                    content=chunk.content,
                    score=best[doc_id],
                    matched_chunk_count=len(sections[doc_id]),
                    section_index=chunk.section_key,
                )
            )
        return results

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
            return f"{structure} {chunk.authored_content}".lower()

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
                    matched_chunk_count=len({c.section_key for c, hits in authored if hits}),
                    section_index=excerpt.section_key,
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
        therefore spans two of the five fields, and a parse that began
        reporting the other two would widen the search silently -- extend the
        search alongside the parse, not the parse alone.

        ``document_scoped`` is the exception to that caution rather than
        another instance of it. This double matches over the union of a
        document's chunks and has no narrower unit to fall back to, so
        reporting document scope unconditionally states what its matching
        actually does; the field is true here because nothing about it is
        stubbed out, not because true is the quiet default. A test needing the
        other scope substitutes a parse, as it does for exclusion and adjacency.
        """
        return KeywordQueryParse(
            terms=tuple(query.lower().split()),
            excluded=(),
            all_required=True,
            adjacent=False,
            document_scoped=True,
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

    async def count_rows(self) -> int:
        """Return the total number of content-store rows across all documents.

        Both surfaces, as the port requires: this stub holds its passages and
        its document-level rows in two dicts, and counting only the first would
        report a store smaller than the one it is serving.
        """
        return sum(len(chunks) for chunks in self._store.values()) + len(self._surfaces)

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


class _ByteCountedBound:
    """A declared input bound over a byte count, for the test providers.

    A byte is the coarsest count the port admits -- no binding counts more
    tokens than bytes plus two -- so a double bounding by bytes divides text at
    least as finely as a real tokenizer would.
    """

    def __init__(self, max_input_tokens: int = 2048) -> None:
        self._max_input_tokens = max_input_tokens

    @property
    def max_input_tokens(self) -> int:
        return self._max_input_tokens

    def count_tokens(self, text: str) -> int:
        return len(text.encode("utf-8"))


class StubEmbeddingProvider(_ByteCountedBound, EmbeddingProvider):
    """Returns deterministic zero vectors for testing.

    A zero vector has no cosine similarity, so both content-store bindings drop
    rows embedded by this provider from the semantic arm, and a query it embeds
    finds nothing there. A test that needs a semantic hit uses
    ``SeededEmbeddingProvider``.
    """

    DIMENSIONS = 768

    async def embed(self, texts: list[str]) -> list[list[float]]:
        return [[0.0] * self.DIMENSIONS for _ in texts]


class SeededEmbeddingProvider(_ByteCountedBound, EmbeddingProvider):
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

    Seam-proof, not a full graph engine: documents, edges and staging edges
    get straightforward in-memory CRUD plus simple counts, which covers the
    substitutability path and most hermetic service tests. Methods whose
    correctness depends on query/filter semantics, atomic multi-step
    transactions, lineage/retraction resolution, or graph traversal raise
    ``NotImplementedError`` until a test needs them, so a coincidental
    empty-result pass can never masquerade as real behavior. ``close`` records
    its call count so ownership/cleanup tests can assert it was not closed.
    """

    def __init__(self) -> None:
        self._docs: dict[str, Document] = {}
        self._processing_stages: dict[str, str | None] = {}
        # Held beside the documents, as the durable store holds it in a column
        # the Document model does not carry.
        self._reprojection_skips: dict[str, tuple[str | None, str]] = {}
        self._edges: dict[str, Edge] = {}
        self._staging: dict[str, StagingEdge] = {}
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
    async def insert_document(self, doc: Document, *, processing_stage: str | None = None) -> None:
        if processing_stage not in (None, "indexing", "abstraction"):
            raise ValueError("invalid processing stage")
        if processing_stage is not None:
            doc = doc.model_copy(
                update={
                    "processing_coverage": doc.processing_coverage.model_copy(update={"attempt": 1})
                }
            )
        self._docs[doc.id] = doc
        self._processing_stages[doc.id] = processing_stage

    async def get_document(self, doc_id: str) -> Document | None:
        return self._docs.get(doc_id)

    async def update_document(
        self,
        doc_id: str,
        updates: dict,
        *,
        advance_version: bool = True,
        processing_stage: str | None = None,
        advance_processing_attempt: bool = True,
        expected_processing_attempt: int | None = None,
    ) -> Document | None:
        doc = self._docs.get(doc_id)
        if doc is None:
            return None
        if (
            expected_processing_attempt is not None
            and doc.processing_coverage.attempt != expected_processing_attempt
        ):
            return None
        if "version_token" in updates:
            raise ValueError("version_token is advanced, never set")
        if processing_stage not in (None, "indexing", "abstraction"):
            raise ValueError("invalid processing stage")
        if "processing_resume_stage" in updates:
            raise ValueError("processing stage uses the internal admission seam")
        invalidate_quality = bool(
            {"semantic_abstract", "source_content_hash"} & updates.keys()
            and "processing_coverage" not in updates
        )
        if processing_stage is not None:
            self._processing_stages[doc_id] = processing_stage
            if advance_processing_attempt:
                updates = {
                    **updates,
                    "processing_coverage": doc.processing_coverage.model_copy(
                        update={"attempt": doc.processing_coverage.attempt + 1}
                    ),
                }
        if invalidate_quality:
            updates = {
                **updates,
                "processing_coverage": updates.get(
                    "processing_coverage", doc.processing_coverage
                ).model_copy(
                    update={
                        "abstraction_quality": AbstractionQuality(),
                        "served_abstraction_generation": None,
                    }
                ),
            }
        if advance_version:
            updates = {**updates, "version_token": str(int(doc.version_token) + 1)}
        updated = doc.model_copy(update=updates)
        self._docs[doc_id] = updated
        return updated

    async def get_processing_stage(self, doc_id: str) -> str | None:
        return self._processing_stages.get(doc_id)

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

    async def latest_principal_names(self) -> dict[str, str | None]:
        raise self._unsupported("latest_principal_names")

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
        *,
        processing_stage: str | None = None,
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

    async def find_documents_by_hashes(
        self, hashes: list[str], *, prefer_lifecycle_statuses: frozenset[str]
    ) -> dict[str, str]:
        wanted = set(hashes)
        found: dict[str, str] = {}
        # A preferred lifecycle status outranks the id tie-break, and the
        # first survivor of that ordering wins -- so the stub answers
        # identically to the durable store rather than following its own
        # insertion order. One sort covers both arms the SQL spells
        # separately: an empty preference gives every document the same
        # leading key, leaving the id to decide alone. Python compares
        # strings by code point, which is the byte ordering the durable
        # store pins with COLLATE "C"; the two agree by construction rather
        # than by coincidence.
        for d in sorted(
            self._docs.values(),
            key=lambda doc: (doc.lifecycle_status not in prefer_lifecycle_statuses, doc.id),
        ):
            if d.source_content_hash in wanted and d.source_content_hash not in found:
                found[d.source_content_hash] = d.id
        return found

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

    async def record_reprojection_skip(
        self, document_id: str, adapter_version: str | None, reason: str
    ) -> None:
        if document_id in self._docs:
            self._reprojection_skips[document_id] = (adapter_version, reason)

    async def reprojection_skips(self) -> dict[str, str | None]:
        return {
            doc_id: version
            for doc_id, (version, _reason) in self._reprojection_skips.items()
            if doc_id in self._docs
        }

    async def clear_reprojection_skip(self, document_id: str) -> None:
        self._reprojection_skips.pop(document_id, None)

    async def remove_document(self, document_id: str) -> None:
        self._docs.pop(document_id, None)
        self._processing_stages.pop(document_id, None)
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

    async def count_documents_by_pipeline_status(
        self, status: str, exclude_lifecycle_statuses: Sequence[str] = ()
    ) -> int:
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

    async def drop_retired_users_table(self) -> bool:
        return False

    async def list_pending_metadata_documents(
        self,
        exclude_lifecycle_statuses: Sequence[str] = (),
        *,
        limit: int | None = None,
        offset: int = 0,
    ) -> list[Document]:
        raise self._unsupported("list_pending_metadata_documents")

    async def count_pending_metadata_documents(
        self, exclude_lifecycle_statuses: Sequence[str] = ()
    ) -> int:
        raise self._unsupported("count_pending_metadata_documents")

    async def measured_byte_size(self) -> int:
        return 0

    # --- Traversal ---
    async def traverse(
        self,
        start_id: str,
        edge_type: str | None,
        direction: str,
        depth: int,
        row_limit: int | None = None,
    ) -> list[dict]:
        raise self._unsupported("traverse")

    async def chain_walk(self, start_id: str, edge_type: str) -> list[dict]:
        raise self._unsupported("chain_walk")

    async def list_provenance_edges(self, edge_types: list[str]) -> list[dict]:
        raise self._unsupported("list_provenance_edges")

    async def head_with_hash_for_chain(self, target_id: str, edge_type: str = "supersedes") -> dict:
        raise self._unsupported("head_with_hash_for_chain")
