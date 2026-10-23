"""Bounded deterministic passage inference, separate from readable content publication."""

import asyncio
import json
from collections.abc import Iterable
from dataclasses import dataclass, replace

from sage.adapters.interfaces import Chunk, ContentStore, EmbeddingProvider
from sage.models.coverage import CoverageDimension, ProcessingIdentity
from sage.models.discovery import AuthoredExcerpt, EvidencePackage, SelectionConfig
from sage.models.schemas import Document
from sage.services.discovery_selection import select_discovery
from sage.services.passage_split import embedding_input
from sage.source_adapters.base import ProjectionResult
from sage.source_adapters.structure import SourceFact


@dataclass(frozen=True)
class PassageSelection:
    package: EvidencePackage
    config: SelectionConfig
    recovery_limitations: tuple[str, ...] = ()

    @property
    def identity(self) -> ProcessingIdentity:
        return self.package.identity.processing_identity()


class PassageWork(list[Chunk]):
    """Caller-local immutable admission binding traveling through the sizing seam."""

    def __init__(
        self,
        chunks: list[Chunk],
        document_id: str,
        attempt: int,
        selection: PassageSelection,
    ) -> None:
        super().__init__(chunks)
        self.document_id = document_id
        self.attempt = attempt
        self.selection = selection


def select_passages(
    chunks: list[Chunk],
    projection: ProjectionResult,
    provider: EmbeddingProvider,
    source_hash: str,
) -> PassageSelection:
    """Use the shared selector on complete passage inputs and exact inference tokens.

    A passage remains the same source slice and address whether selected or not.
    Unlike source-fact inspection, this selection's unit is the stored passage;
    occurrence runs therefore refer directly to stable chunk indexes.
    """
    prior = projection.discovery_evidence
    captured = json.loads(prior.identity.configuration_json) if prior else {}
    config = SelectionConfig(**captured.get("selection", {}))

    def facts() -> Iterable[SourceFact]:
        for chunk in chunks:
            yield SourceFact(
                chunk.heading_path or "document",
                "passage",
                f"chunk:{chunk.chunk_index}",
                chunk.chunk_index,
                str(chunk.section_key),
                chunk.content,
                chunk.content,
                chunk.heading_path,
            )

    def tokens(text: str) -> int:
        count = provider.count_tokens(text)
        # A provider may silently truncate an oversized input. Never admit one.
        return count if count <= provider.max_input_tokens else config.max_tokens + 1

    package = select_discovery(
        facts(),
        source_hash=source_hash,
        adapter_version=projection.adapter_version,
        tokenizer=tokens,
        tokenizer_identity=f"{type(provider).__name__}:exact:{provider.max_input_tokens}",
        config=config,
        adapter_configuration=captured.get("adapter", {}),
    )
    return PassageSelection(package, config)


def associated_indices(excerpt: AuthoredExcerpt) -> Iterable[int]:
    """Expand only established bounded source associations, incrementally."""
    for run in excerpt.associations:
        yield from range(run.start, run.end + 1)


def embedding_coverage(
    selection: PassageSelection,
    chunks: list[Chunk],
    limitations: list[str],
) -> CoverageDimension:
    represented = sum(c.embedding is not None for c in chunks)
    complete = represented == len(chunks)
    return CoverageDimension(
        scope="complete" if complete else "limited",
        identity=selection.identity,
        counts=[
            {
                "unit": "passages",
                "observed": len(chunks),
                "represented": represented,
                "total": len(chunks),
            }
        ],
        limitations=list(dict.fromkeys([*selection.recovery_limitations, *limitations]))[:16],
        unknown_remainder=False,
    )


async def restore_selected_vectors(
    store: ContentStore,
    document_id: str,
    selection: PassageSelection,
    chunks: list[Chunk],
) -> None:
    """Reuse only matching positively established inputs, never ambiguous legacy vectors."""
    coverage, previous, _ = await store.read_processing_artifacts(document_id)
    if coverage.passage_embeddings.identity != selection.identity:
        return
    old = {c.chunk_index: c for c in previous}
    representatives = [x.order for x in selection.package.selected]
    vectors = await store.read_embedding_vectors(document_id, representatives)
    for excerpt in selection.package.selected:
        vector = vectors.get(excerpt.order)
        prior = old.get(excerpt.order)
        if vector is None or prior is None:
            continue
        if embedding_input(prior.heading_path, prior.content) != excerpt.input_text:
            continue
        for index in associated_indices(excerpt):
            chunks[index].embedding = vector


async def embed_selected(
    store: ContentStore,
    provider: EmbeddingProvider,
    document_id: str,
    attempt: int,
    selection: PassageSelection,
    chunks: list[Chunk],
) -> None:
    """Debit before inference, publish after each bounded shared-provider call.

    The configured input window (default eight, hard ceiling 32) and total
    token budget bound in-flight work. Association writes use windows of 128 references.
    Uncertain debits are never replayed, so an interrupted call cannot reset a
    source allowance on retry or recompute.
    """
    limitations = [*selection.recovery_limitations, *selection.package.limitations]
    selected = selection.package.selected
    try:
        for start in range(0, len(selected), selection.config.max_in_flight):
            pending = []
            for excerpt in selected[start : start + selection.config.max_in_flight]:
                if chunks[excerpt.order].embedding is not None:
                    continue
                decision = await store.admit_embedding(
                    document_id,
                    attempt,
                    selection.identity,
                    excerpt.input_text,
                    excerpt.token_count,
                    max_inputs=selection.config.max_excerpts,
                    max_tokens=selection.config.max_tokens,
                )
                if decision == "stale":
                    raise RuntimeError("stale embedding admission")
                if decision != "admitted":
                    limitations.append("embedding_" + decision)
                    continue
                pending.append(excerpt)
            if not pending:
                continue
            vectors = await provider.embed([x.input_text for x in pending])
            if len(vectors) != len(pending):
                raise RuntimeError("embedding provider returned an incomplete increment")
            updates = {}
            for excerpt, vector in zip(pending, vectors, strict=True):
                for index in associated_indices(excerpt):
                    chunks[index].embedding = vector
                    updates[index] = vector
                    if len(updates) == 128:
                        if not await store.publish_embedding_increment(
                            document_id,
                            attempt,
                            updates,
                            embedding_coverage(selection, chunks, limitations),
                        ):
                            raise RuntimeError("stale embedding publication")
                        updates = {}
            if updates and not await store.publish_embedding_increment(
                document_id,
                attempt,
                updates,
                embedding_coverage(selection, chunks, limitations),
            ):
                raise RuntimeError("stale embedding publication")
        if not await store.publish_embedding_increment(
            document_id,
            attempt,
            {},
            embedding_coverage(selection, chunks, limitations),
            indexing_complete=True,
        ):
            raise RuntimeError("stale indexing completion")
    except (Exception, asyncio.CancelledError) as exc:
        limitations.append(
            "embedding_interrupted"
            if isinstance(exc, asyncio.CancelledError)
            else "embedding_failed"
        )
        # A failed database return can follow a committed transaction. Read the
        # authoritative artifact evidence, never infer success from local vectors.
        current, _, _ = await store.read_processing_artifacts(document_id)
        established = current.passage_embeddings.model_copy(
            update={
                "limitations": list(
                    dict.fromkeys(
                        [
                            *current.passage_embeddings.limitations,
                            *limitations,
                        ]
                    )
                )[:16],
            }
        )
        await store.publish_embedding_increment(
            document_id,
            attempt,
            {},
            established,
        )
        raise


async def reindex_passages(
    store: ContentStore,
    provider: EmbeddingProvider,
    doc: Document,
    chunks: list[Chunk],
    *,
    decision: str | None = None,
) -> bool:
    """The migration/script seam uses the same source ledger and incremental writer.

    Stored passages alone establish their availability, never historical full
    source projection. Ordinary second runs reuse vectors and consume no inference.
    Explicit scope expansion is attributable to the operator's recorded decision.
    """
    chunks = [replace(chunk, embedding=None) for chunk in chunks]
    projection = ProjectionResult(
        "",
        [],
        doc.source_content_hash,
        doc.adapter_version or "unknown",
        doc.title or "",
    )
    selection = select_passages(chunks, projection, provider, doc.source_content_hash)
    if decision is None:
        await restore_selected_vectors(store, doc.id, selection, chunks)
        coverage, previous, _ = await store.read_processing_artifacts(doc.id)
        if (
            coverage.passage_embeddings.identity == selection.identity
            and all(
                chunks[excerpt.order].embedding is not None
                for excerpt in selection.package.selected
            )
            and [(c.heading_path, c.content, c.keyword_indexed) for c in chunks]
            == [(c.heading_path, c.content, c.keyword_indexed) for c in previous]
        ):
            return False
    attempt = await store.begin_processing_attempt(doc.id)
    if decision is not None:
        if not await store.authorize_embedding_scope(doc.id, attempt, decision):
            raise RuntimeError("stale reprocessing decision")
    available = CoverageDimension(
        scope="limited",
        identity=selection.identity,
        counts=[
            {"unit": "passages", "observed": len(chunks), "represented": len(chunks), "total": None}
        ],
        limitations=["source_projection_completeness_unknown"],
        unknown_remainder=True,
    )
    keyword = CoverageDimension(
        scope="limited",
        identity=selection.identity,
        counts=[
            {
                "unit": "passages",
                "observed": len(chunks),
                "represented": sum(c.keyword_indexed for c in chunks),
                "total": None,
            }
        ],
        limitations=["source_keyword_completeness_unknown"],
        unknown_remainder=True,
    )
    if not await store.publish_processing(
        doc.id,
        attempt,
        {
            "projection": available,
            "keyword_indexing": keyword,
            "passage_embeddings": embedding_coverage(selection, chunks, ["embedding_pending"]),
        },
        chunks=chunks,
        indexing_complete=False,
    ):
        raise RuntimeError("stale passage reprocessing publication")
    await embed_selected(store, provider, doc.id, attempt, selection, chunks)
    return True
