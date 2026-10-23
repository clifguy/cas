"""Readable authored content and cumulative embedding admission are independent."""

import os

import pytest

from sage.adapters.content_store_postgres import PostgresContentStore
from sage.adapters.interfaces import Chunk
from sage.adapters.stubs import SeededEmbeddingProvider
from sage.models.coverage import CoverageDimension
from sage.source_adapters.base import HeadingNode, ProjectionResult
from tests.sage.test_postgres_graph_store import _doc
from tests.sage.test_processing_coverage import _identity


async def test_absent_vectors_preserve_authored_reads_and_keyword_search(
    pg_pool, postgres_graph_store
):
    doc = _doc(1)
    await postgres_graph_store.insert_document(doc)
    content = PostgresContentStore(pg_pool)
    attempt = await content.begin_processing_attempt(doc.id)
    chunks = [
        Chunk(
            document_id=doc.id,
            heading_path="Root > Details",
            content='<entry value="saffron"/>',
            chunk_index=0,
        )
    ]
    evidence = {
        name: CoverageDimension()
        for name in ("projection", "keyword_indexing", "passage_embeddings")
    }
    assert await content.publish_processing(doc.id, attempt, evidence, chunks=chunks)
    async with pg_pool.connection() as conn:
        row = await (
            await conn.execute(
                "SELECT embedding IS NULL, content FROM chunks WHERE document_id = %s", (doc.id,)
            )
        ).fetchone()
    assert row == (True, '<entry value="saffron"/>')
    read = await content.get_all_chunks(doc.id)
    assert [(c.heading_path, c.content, c.embedding) for c in read] == [
        ("Root > Details", '<entry value="saffron"/>', None)
    ]
    results = await content.search_bm25("saffron")
    assert [(r.document_id, r.matched_chunk_count) for r in results] == [(doc.id, 1)]


async def test_embedding_admission_is_durable_and_fenced(pg_pool, postgres_graph_store):
    doc = _doc(1)
    await postgres_graph_store.insert_document(doc)
    content = PostgresContentStore(pg_pool)
    attempt = await content.begin_processing_attempt(doc.id)
    identity = _identity().model_copy(update={"configuration": {"zz": 1, "a": {"cc": 2, "b": 3}}})
    assert (
        await content.admit_embedding(
            doc.id, attempt, identity, "first", 3, max_inputs=2, max_tokens=5
        )
        == "admitted"
    )
    assert (
        await content.admit_embedding(
            doc.id, attempt, identity, "first", 3, max_inputs=2, max_tokens=5
        )
        == "consumed"
    )
    import json

    from sage.models.coverage import ProcessingIdentity

    reordered = ProcessingIdentity.model_validate(
        json.loads(json.dumps(identity.model_dump(), sort_keys=True))
    )
    assert reordered == identity and list(reordered.configuration) != list(identity.configuration)
    assert await content.embedding_scope_is_compatible(doc.id, reordered)
    assert (
        await content.admit_embedding(
            doc.id, attempt, reordered, "first", 3, max_inputs=2, max_tokens=5
        )
        == "consumed"
    )
    assert (
        await content.admit_embedding(
            doc.id, attempt, identity, "too-many-tokens", 3, max_inputs=2, max_tokens=5
        )
        == "limited"
    )
    newer = await content.begin_processing_attempt(doc.id)
    assert (
        await content.admit_embedding(
            doc.id, newer, identity, "first", 3, max_inputs=2, max_tokens=5
        )
        == "consumed"
    )
    assert (
        await content.admit_embedding(
            doc.id, attempt, identity, "stale", 1, max_inputs=2, max_tokens=5
        )
        == "stale"
    )
    assert (
        await content.admit_embedding(
            doc.id, newer, identity, "last", 2, max_inputs=2, max_tokens=5
        )
        == "admitted"
    )
    assert (
        await content.admit_embedding(
            doc.id, newer, identity, "over-inputs", 0, max_inputs=2, max_tokens=5
        )
        == "limited"
    )


async def test_explicit_scope_expansion_is_attributable(pg_pool, postgres_graph_store):
    doc = _doc(1)
    await postgres_graph_store.insert_document(doc)
    content = PostgresContentStore(pg_pool)
    attempt = await content.begin_processing_attempt(doc.id)
    identity = _identity()
    assert (
        await content.admit_embedding(
            doc.id,
            attempt,
            identity,
            "original",
            5,
            max_inputs=2,
            max_tokens=5,
        )
        == "admitted"
    )
    changed = identity.model_copy(update={"policy": "expanded-policy"})
    assert (
        await content.admit_embedding(
            doc.id,
            attempt,
            changed,
            "expanded",
            1,
            max_inputs=2,
            max_tokens=5,
        )
        == "scope_changed"
    )
    assert await content.authorize_embedding_scope(
        doc.id, attempt, "Owner requested expanded scope"
    )
    assert (
        await content.admit_embedding(
            doc.id,
            attempt,
            changed,
            "expanded",
            1,
            max_inputs=2,
            max_tokens=5,
        )
        == "admitted"
    )
    async with pg_pool.connection() as conn:
        ledger = (
            await (
                await conn.execute(
                    "SELECT embedding_accounting FROM documents WHERE id = %s",
                    (doc.id,),
                )
            ).fetchone()
        )[0]
    assert ledger["authorization"]["decision"] == "Owner requested expanded scope"
    assert ledger["authorization"]["previous_tokens"] == 5


async def test_partial_vector_updates_preserve_content_and_reject_stale(
    pg_pool, postgres_graph_store
):
    doc = _doc(1)
    await postgres_graph_store.insert_document(doc)
    content = PostgresContentStore(pg_pool)
    attempt = await content.begin_processing_attempt(doc.id)
    chunks = [
        Chunk(document_id=doc.id, heading_path="A", content="alpha", chunk_index=0),
        Chunk(document_id=doc.id, heading_path="B", content="beta", chunk_index=1),
    ]
    evidence = {
        name: CoverageDimension()
        for name in ("projection", "keyword_indexing", "passage_embeddings")
    }
    assert await content.publish_processing(doc.id, attempt, evidence, chunks=chunks)
    from sage.storage.postgres.schema import EMBEDDING_DIM

    vector = [1.0] + [0.0] * (EMBEDDING_DIM - 1)
    assert await content.publish_embedding_increment(
        doc.id, attempt, {0: vector}, CoverageDimension()
    )
    newer = await content.begin_processing_attempt(doc.id)
    assert not await content.publish_embedding_increment(
        doc.id, attempt, {1: vector}, CoverageDimension()
    )
    read = await content.get_all_chunks(doc.id)
    assert [c.content for c in read] == ["alpha", "beta"]
    assert await content.read_embedding_vectors(doc.id, [0, 1]) == {0: vector}
    assert newer > attempt
    with pytest.raises(ValueError, match="128 established references"):
        await content.publish_embedding_increment(
            doc.id,
            newer,
            {i: vector for i in range(129)},
            CoverageDimension(),
        )


async def test_inference_failure_keeps_prior_increment_and_full_keyword_scope(
    pg_pool,
    ingestion_service_no_abstraction,
    graph_store,
):
    from sage.adapters.stubs import SeededEmbeddingProvider
    from sage.source_adapters.base import HeadingNode, ProjectionResult

    class FailingAfterFirst(SeededEmbeddingProvider):
        def __init__(self):
            super().__init__()
            self.calls = []

        async def embed(self, texts):
            self.calls.append(list(texts))
            if len(self.calls) == 2:
                raise RuntimeError("injected inference failure")
            return await super().embed(texts)

    doc = _doc(1)
    await graph_store.insert_document(doc)
    service = ingestion_service_no_abstraction
    service._content_store = content = PostgresContentStore(pg_pool)
    service._embedding = FailingAfterFirst()
    headings = [HeadingNode(1, f"S{i}", f"S{i}", f"authored saffron{i}") for i in range(12)]
    projection = ProjectionResult("all authored", headings, doc.source_content_hash, "1", "Title")
    attempt = await content.begin_processing_attempt(doc.id)
    with pytest.raises(RuntimeError, match="injected inference failure"):
        await service._stage2_indexing(doc.id, projection, attempt)
    stored = await content.get_all_chunks(doc.id)
    assert len(stored) == 12
    assert (await content.search_bm25("saffron11"))[0].document_id == doc.id
    vectors = await content.read_embedding_vectors(doc.id, list(range(12)))
    assert 0 < len(vectors) < 12
    current = await graph_store.get_document(doc.id)
    assert current.processing_coverage.projection.scope == "complete"
    assert current.processing_coverage.keyword_indexing.scope == "complete"
    assert "embedding_failed" in current.processing_coverage.passage_embeddings.limitations
    # This is established partial work, so startup recovery still resumes indexing.
    async with pg_pool.connection() as conn:
        row = await (
            await conn.execute(
                "SELECT processing_resume_stage FROM documents WHERE id = %s", (doc.id,)
            )
        ).fetchone()
    assert row == ("indexing",)


def _projection_with_limits(doc, headings, **limits):
    from sage.models.discovery import SelectionConfig
    from sage.services.discovery_selection import select_discovery
    from sage.source_adapters.base import ProjectionResult
    from sage.source_adapters.structure import SourceFact

    package = select_discovery(
        [SourceFact("document", "text", "line:1", 0, "document", "source", "source")],
        source_hash=doc.source_content_hash,
        adapter_version="test:1",
        tokenizer=len,
        tokenizer_identity="test:bytes",
        config=SelectionConfig(**limits),
    )
    return ProjectionResult(
        "source", headings, doc.source_content_hash, "test:1", "Source", discovery_evidence=package
    )


async def test_explicit_keyword_budget_is_independent_of_embedding_and_content(
    pg_pool,
    ingestion_service_no_abstraction,
    graph_store,
):
    from sage.adapters.stubs import SeededEmbeddingProvider
    from sage.models.schemas import DiscoverHit, DocumentSummary
    from sage.source_adapters.base import HeadingNode

    class Recording(SeededEmbeddingProvider):
        def __init__(self):
            super().__init__()
            self.calls = []

        async def embed(self, texts):
            self.calls.append(texts)
            return await super().embed(texts)

    doc = _doc(1)
    await graph_store.insert_document(doc)
    service = ingestion_service_no_abstraction
    service._embedding = provider = Recording()
    service._content_store = content = PostgresContentStore(pg_pool)
    headings = [HeadingNode(1, f"S{i}", f"S{i}", f"authored saffron{i}") for i in range(4)]
    projection = _projection_with_limits(
        doc, headings, max_excerpts=2, max_tokens=200, max_keyword_characters=30, max_in_flight=1
    )
    await service._stage2_indexing(doc.id, projection)
    stored = await content.get_all_chunks(doc.id)
    assert len(stored) == 4
    assert stored[-1].content.endswith("saffron3")
    assert len(provider.calls) == 2 and all(len(call) == 1 for call in provider.calls)
    assert sum(provider.count_tokens(text) for call in provider.calls for text in call) <= 200
    assert await content.search_bm25("saffron0")
    assert not await content.search_bm25("saffron3")
    current = await graph_store.get_document(doc.id)
    hit = DiscoverHit.from_summary(DocumentSummary.from_document(current))
    assert (
        hit.available_passage_count,
        hit.keyword_indexed_passage_count,
        hit.embedded_passage_count,
    ) == (4, 1, 2)
    assert current.processing_coverage.keyword_indexing.limitations == ["keyword_character_budget"]
    from sage.models.schemas import DiscoverRequest
    from sage.models.wire import to_wire
    from sage.services.retrieval import RetrievalService
    from sage.services.utilities import UtilitiesService

    retrieval = RetrievalService(graph_store, content, provider, service._config)
    response = await retrieval.discover(
        DiscoverRequest(
            mode="keyword",
            query="saffron0",
            filters={"document_ids": [doc.id]},
        )
    )
    wire = to_wire(response)
    assert len(wire["results"]) == 1
    served = wire["results"][0]
    assert (
        served["available_passage_count"],
        served["keyword_indexed_passage_count"],
        served["embedded_passage_count"],
        served["matched_passage_count"],
    ) == (4, 1, 2, 1)
    assert "limited" in response.hints["coverage_advisory"]
    empty = await retrieval.discover(
        DiscoverRequest(
            mode="keyword",
            query="saffron3",
            filters={"document_ids": [doc.id]},
        )
    )
    assert not empty.results and "not evidence" in empty.hints["coverage_advisory"]
    title_response = await retrieval.discover(
        DiscoverRequest(mode="keyword", query=doc.title, filters={"document_ids": [doc.id]})
    )
    assert title_response.results and title_response.results[0].matched_passage_count == 0
    reader = UtilitiesService(graph_store, content, provider, service._config)
    read = await reader.read_section(doc.id, "S3")
    assert "saffron3" in read.section_text
    assert to_wire(read)["processing_coverage"]["keyword_indexing"]["scope"] == "limited"


async def test_exact_input_dedup_preserves_distinct_values_and_all_addresses(
    pg_pool,
    ingestion_service_no_abstraction,
    graph_store,
):
    from sage.adapters.stubs import SeededEmbeddingProvider
    from sage.source_adapters.base import HeadingNode

    class Recording(SeededEmbeddingProvider):
        def __init__(self):
            super().__init__()
            self.inputs = []

        async def embed(self, texts):
            self.inputs.extend(texts)
            return await super().embed(texts)

    doc = _doc(1)
    await graph_store.insert_document(doc)
    service = ingestion_service_no_abstraction
    service._embedding = provider = Recording()
    service._content_store = content = PostgresContentStore(pg_pool)
    headings = [
        HeadingNode(1, "Operation", "Operation", value)
        for value in ("value alpha", "value beta", "value alpha")
    ]
    projection = _projection_with_limits(doc, headings, max_excerpts=2)
    await service._stage2_indexing(doc.id, projection)
    assert len(provider.inputs) == 2

    assert any("value alpha" in text for text in provider.inputs)
    assert any("value beta" in text for text in provider.inputs)
    vectors = await content.read_embedding_vectors(doc.id, [0, 1, 2])
    assert len(vectors) == 3 and vectors[0] == vectors[2] and vectors[0] != vectors[1]
    assert [c.heading_path for c in await content.get_all_chunks(doc.id)] == ["Operation"] * 3
    found = await content.search_bm25("value")
    assert found[0].matched_passage_count == 3
    assert found[0].matched_chunk_count == 3
    # Recompute under the same source/selection cannot buy additional inference.
    await service._stage2_indexing(doc.id, projection)
    assert len(provider.inputs) == 2


async def test_backend_keyword_ceiling_keeps_the_projection_with_honest_scope(
    pg_pool,
    postgres_graph_store,
):
    doc = _doc(1)
    await postgres_graph_store.insert_document(doc)
    content = PostgresContentStore(pg_pool)
    attempt = await content.begin_processing_attempt(doc.id)
    body = " ".join(f"lexeme{i:07d}" for i in range(150000))
    available = CoverageDimension(
        scope="complete",
        identity=_identity(),
        unknown_remainder=False,
        counts=[{"unit": "passages", "observed": 1, "represented": 1, "total": 1}],
    )
    assert await content.publish_processing(
        doc.id,
        attempt,
        {
            "projection": available,
            "keyword_indexing": available,
            "passage_embeddings": CoverageDimension(),
        },
        chunks=[Chunk(document_id=doc.id, heading_path="All", content=body)],
    )
    stored = await content.get_all_chunks(doc.id)
    assert len(stored) == 1 and stored[0].content == body
    assert not stored[0].keyword_indexed
    coverage, _ = await content.read_processing_snapshot(doc.id)
    assert coverage.projection.scope == "complete"
    assert coverage.keyword_indexing.scope == "limited"
    assert coverage.keyword_indexing.counts[0].represented == 0
    assert coverage.keyword_indexing.limitations == ["backend_keyword_limit"]
    assert not await content.search_bm25("lexeme0000001")


async def test_cancellation_retains_content_and_debit_without_replaying_uncertain_inference(
    pg_pool,
    ingestion_service_no_abstraction,
    graph_store,
):
    import asyncio

    from sage.adapters.stubs import SeededEmbeddingProvider
    from sage.source_adapters.base import HeadingNode

    class Held(SeededEmbeddingProvider):
        def __init__(self):
            super().__init__()
            self.entered = asyncio.Event()
            self.calls = 0
            self.cancelled = False

        async def embed(self, texts):
            self.calls += 1
            self.entered.set()
            if self.calls == 1:
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    self.cancelled = True
                    raise
            return await super().embed(texts)

    doc = _doc(1)
    await graph_store.insert_document(doc)
    service = ingestion_service_no_abstraction
    service._content_store = content = PostgresContentStore(pg_pool)
    service._embedding = provider = Held()
    projection = _projection_with_limits(
        doc,
        [HeadingNode(1, "Held", "Held", "authored saffron")],
        max_excerpts=1,
    )
    task = asyncio.create_task(service._stage2_indexing(doc.id, projection))
    await asyncio.wait_for(provider.entered.wait(), 5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert provider.cancelled
    assert (await content.search_bm25("saffron"))[0].document_id == doc.id
    coverage, chunks = await content.read_processing_snapshot(doc.id)
    assert len(chunks) == 1 and "saffron" in chunks[0].content
    assert coverage.passage_embeddings.counts[0].represented == 0
    assert "embedding_interrupted" in coverage.passage_embeddings.limitations
    await service._stage2_indexing(doc.id, projection)
    assert provider.calls == 1
    coverage, _ = await content.read_processing_snapshot(doc.id)
    assert coverage.passage_embeddings.scope == "limited"
    assert "embedding_consumed" in coverage.passage_embeddings.limitations
    assert "embedding_prior_uncertain_work" in coverage.passage_embeddings.limitations


@pytest.mark.skipif(
    not os.environ.get("SAGE_SELECTIVE_CSV_PROBE"),
    reason="explicit retained-source reproduction with real inference",
)
async def test_controlled_original_csv_probe(
    pg_pool,
    ingestion_service_no_abstraction,
    graph_store,
    monkeypatch,
):
    import csv
    import hashlib
    import json
    import re
    import time
    from pathlib import Path

    from sage.adapters.embedding_nomic import NomicEmbeddingProvider
    from sage.models.enums import SourceType
    from sage.services.ingestion import _run_inspected_projection
    from sage.source_adapters.structured_data_adapter import StructuredDataAdapter

    source = Path(os.environ["SAGE_SELECTIVE_CSV_PROBE"])
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    assert digest == os.environ["SAGE_SELECTIVE_CSV_SHA256"]
    provider = NomicEmbeddingProvider()
    source_calls = []
    actual_inference = []
    real_embed = provider.embed
    real_encode = provider._model.encode

    def encode(texts, *args, **kwargs):
        actual_inference.append(len(texts))
        return real_encode(texts, *args, **kwargs)

    async def record(texts):
        source_calls.append([provider.count_tokens(text) for text in texts])
        return await real_embed(texts)

    monkeypatch.setattr(provider, "embed", record)
    monkeypatch.setattr(provider._model, "encode", encode)
    started = time.perf_counter()
    projection = await _run_inspected_projection(
        StructuredDataAdapter(),
        source,
        None,
        source_hash="sha256:" + digest,
    )
    projected = time.perf_counter()
    doc = _doc(1).model_copy(
        update={
            "source_content_hash": "sha256:" + digest,
            "source_type": SourceType.STRUCTURED_DATA,
            "source_path": "imports/reproduction.csv",
            "title": "Controlled CSV reproduction",
        }
    )
    await graph_store.insert_document(doc)
    service = ingestion_service_no_abstraction
    service._embedding = provider
    service._content_store = content = PostgresContentStore(pg_pool)
    await service._stage2_indexing(doc.id, projection)
    indexed = time.perf_counter()
    chunks = await content.get_all_chunks(doc.id)
    assert (
        hashlib.sha256("".join(c.content for c in chunks).encode()).hexdigest()
        == hashlib.sha256(projection.text.encode()).hexdigest()
    )
    with source.open(encoding="utf-8-sig", newline="") as stream:
        header = next(csv.reader(stream))
    terms = [term for value in header for term in re.findall(r"[A-Za-z]{4,}", value)]
    assert terms
    keyword_hits = await content.search_bm25(terms[0])
    assert bool(keyword_hits)
    assert [hit.document_id for hit in keyword_hits] == [doc.id]
    current = await graph_store.get_document(doc.id)
    assert current.processing_coverage.projection.scope == "complete"
    assert current.processing_coverage.keyword_indexing.scope == "complete"
    embedded = current.processing_coverage.passage_embeddings.counts[0].represented
    total_passages = len(chunks)
    assert 0 < embedded <= total_passages
    assert sum(sum(call) for call in source_calls) <= 8192
    assert max(map(len, source_calls)) <= 8
    report = {
        "source_sha256": digest,
        "source_bytes": source.stat().st_size,
        "projection_seconds": projected - started,
        "indexing_seconds": indexed - projected,
        "available_passages": len(chunks),
        "embedded_passages": embedded,
        "provider_calls": len(source_calls),
        "actual_encode_calls": len(actual_inference),
        "actual_encode_input_counts": actual_inference,
        "selected_tokens": sum(sum(call) for call in source_calls),
        "max_provider_inputs": max(map(len, source_calls)),
        "limits": {
            "selected_tokens": 8192,
            "distinct_inputs": 1024,
            "in_flight": 8,
            "keyword_characters": None,
        },
        "useful_keyword_discovery_hits": len(keyword_hits),
        "coverage": current.processing_coverage.model_dump(mode="json"),
        "full_abstraction_acceptance": "pending T-0759",
    }
    Path(os.environ["SAGE_SELECTIVE_CSV_REPORT"]).write_text(json.dumps(report, indent=2))


async def test_provider_token_window_excludes_an_unsplittable_authored_heading(
    pg_pool, ingestion_service_no_abstraction, graph_store
):
    from sage.adapters.stubs import SeededEmbeddingProvider
    from sage.source_adapters.base import HeadingNode

    class Recording(SeededEmbeddingProvider):
        def __init__(self):
            super().__init__(max_input_tokens=16)
            self.calls = []

        async def embed(self, texts):
            self.calls.extend(texts)
            return await super().embed(texts)

    doc = _doc(1)
    await graph_store.insert_document(doc)
    service = ingestion_service_no_abstraction
    service._embedding = provider = Recording()
    service._content_store = content = PostgresContentStore(pg_pool)
    heading = "a" * 20
    projection = _projection_with_limits(
        doc, [HeadingNode(1, heading, heading, "authored saffron")]
    )
    await service._stage2_indexing(doc.id, projection)
    coverage, chunks = await content.read_processing_snapshot(doc.id)
    assert len(chunks) == 1 and chunks[0].content == f"# {heading}\n\nauthored saffron"
    assert coverage.passage_embeddings.counts[0].represented == 0
    assert coverage.passage_embeddings.scope == "limited"
    assert await content.search_bm25("saffron")
    assert not any("saffron" in text for text in provider.calls)
    assert not await content.read_embedding_vectors(doc.id, [0])


async def test_surface_metadata_changes_quarantine_vectors_but_orientation_preserves_them(
    pg_pool, postgres_graph_store
):
    from sage.adapters.interfaces import DocumentSurface
    from sage.storage.postgres.schema import EMBEDDING_DIM

    doc = _doc(1)
    await postgres_graph_store.insert_document(doc)
    content = PostgresContentStore(pg_pool)
    vector = [0.25] * EMBEDDING_DIM
    await content.upsert_document_surface(
        DocumentSurface(doc.id, "Authored title", "Derived description", vector)
    )
    assert await content.search_semantic(vector)
    assert await content.update_document_surface_text(
        doc.id, "Authored title", "Replacement derived description"
    )
    assert await content.search_semantic(vector)
    assert not await content.search_bm25("Replacement")
    async with pg_pool.connection() as conn:
        orientation = await (
            await conn.execute(
                "SELECT orienting FROM document_surface WHERE document_id = %s", (doc.id,)
            )
        ).fetchone()
    assert orientation == ("Replacement derived description",)
    assert await content.update_document_surface_text(
        doc.id, "New authored title", "Replacement derived description"
    )
    assert not await content.search_semantic(vector)
    assert await content.search_bm25("New authored title")
    async with pg_pool.connection() as conn:
        stored, authored = await (
            await conn.execute(
                "SELECT embedding, authored_embedding FROM document_surface WHERE document_id = %s",
                (doc.id,),
            )
        ).fetchone()
    assert list(stored) == vector and authored is False


class _RecordedProvider(SeededEmbeddingProvider):
    def __init__(self):
        super().__init__()
        self.calls = []
        self.fail = False

    async def embed(self, texts):
        self.calls.append(list(texts))
        if self.fail:
            raise RuntimeError("controlled provider failure")
        return await super().embed(texts)


async def test_recompute_preserves_title_vector(
    pg_pool, ingestion_service_no_abstraction, graph_store
):
    doc = _doc(1)
    await graph_store.insert_document(doc)
    service = ingestion_service_no_abstraction
    service._content_store = content = PostgresContentStore(pg_pool)
    service._embedding = provider = _RecordedProvider()
    provider._max_input_tokens = 16
    projection = ProjectionResult(
        "source",
        [HeadingNode(1, "H" * 20, "H" * 20, "body")],
        doc.source_content_hash,
        "1",
        doc.title,
    )
    await service._stage2_indexing(doc.id, projection)
    async with pg_pool.connection() as conn:
        before = await (
            await conn.execute(
                "SELECT embedding, authored_embedding FROM document_surface WHERE document_id=%s",
                (doc.id,),
            )
        ).fetchone()
    assert before[0] is not None and before[1] is True
    vector = list(before[0])
    calls = list(provider.calls)
    assert not await content.read_embedding_vectors(doc.id, [0])
    assert (await content.search_semantic(vector))[0].is_document_surface
    await service._stage2_indexing(doc.id, projection)
    async with pg_pool.connection() as conn:
        after = await (
            await conn.execute(
                "SELECT embedding, authored_embedding FROM document_surface WHERE document_id=%s",
                (doc.id,),
            )
        ).fetchone()
    assert list(after[0]) == vector and after[1] is True
    assert provider.calls == calls
    assert (await content.search_semantic(vector))[0].is_document_surface


@pytest.mark.parametrize(
    "change,fail", [("title", True), ("source", True), ("title", False), ("source", False)]
)
async def test_replacement_quarantines_old_surface_before_uncertain_inference(
    pg_pool, ingestion_service_no_abstraction, graph_store, change, fail
):
    doc = _doc(1)
    await graph_store.insert_document(doc)
    service = ingestion_service_no_abstraction
    service._content_store = content = PostgresContentStore(pg_pool)
    service._embedding = provider = _RecordedProvider()
    projection = ProjectionResult(
        "source",
        [HeadingNode(1, "Body", "Body", "unrelated authored body")],
        doc.source_content_hash,
        "1",
        doc.title,
    )
    await service._stage2_indexing(doc.id, projection)
    async with pg_pool.connection() as conn:
        before = await (
            await conn.execute(
                "SELECT embedding FROM document_surface WHERE document_id=%s",
                (doc.id,),
            )
        ).fetchone()
    if change == "title":
        await graph_store.update_document(doc.id, {"title": "Different authored title"})
    else:
        await graph_store.update_document(doc.id, {"source_content_hash": "sha256:" + "9" * 64})
    provider.fail = fail
    if fail:
        with pytest.raises(RuntimeError, match="controlled provider failure"):
            await service._stage2_indexing(doc.id, projection)
    else:
        await service._stage2_indexing(doc.id, projection)
    async with pg_pool.connection() as conn:
        after = await (
            await conn.execute(
                "SELECT embedding, authored_embedding FROM document_surface WHERE document_id=%s",
                (doc.id,),
            )
        ).fetchone()
    if fail:
        assert list(after[0]) == list(before[0]) and after[1] is False
        assert not any(
            hit.is_document_surface for hit in await content.search_semantic(list(before[0]))
        )
    else:
        assert after[0] is not None and after[1] is True


async def test_reindex_keeps_existing_vectors_on_unapproved_scope_change(
    pg_pool, ingestion_service_no_abstraction, graph_store
):
    from sage.services.selective_embedding import reindex_passages
    from scripts.reindex_chunks_with_heading_context import reindex_with_services

    doc = _doc(1)
    await graph_store.insert_document(doc)
    service = ingestion_service_no_abstraction
    service._content_store = content = PostgresContentStore(pg_pool)
    service._embedding = provider = _RecordedProvider()
    projection = _projection_with_limits(
        doc, [HeadingNode(1, "Body", "Body", "authored saffron")], max_excerpts=2
    )
    await service._stage2_indexing(doc.id, projection)
    before = await content.read_embedding_vectors(doc.id, [0])
    assert len(before) == 1
    current = await graph_store.get_document(doc.id)
    chunks = await content.get_all_chunks(doc.id)
    calls = list(provider.calls)
    with pytest.raises(ValueError, match="embedding scope changed"):
        await reindex_passages(content, provider, current, chunks)
    after = await graph_store.get_document(doc.id)
    assert after.processing_coverage == current.processing_coverage
    assert after.indexed_at == current.indexed_at
    assert await content.read_embedding_vectors(doc.id, [0]) == before
    assert await content.get_all_chunks(doc.id) == chunks
    assert provider.calls == calls
    # Simulate a stale optimistic preflight; the replacement transaction must
    # independently reject the incompatible persisted ledger before deleting rows.
    from unittest.mock import AsyncMock, patch

    with patch.object(content, "embedding_scope_is_compatible", AsyncMock(return_value=True)):
        with pytest.raises(ValueError, match="embedding scope changed"):
            await reindex_passages(content, provider, current, chunks)
    assert await content.read_embedding_vectors(doc.id, [0]) == before
    assert await content.get_all_chunks(doc.id) == chunks
    assert (await graph_store.get_document(doc.id)).indexed_at == current.indexed_at
    for _ in range(2):
        assert (
            await reindex_with_services(
                graph=graph_store,
                store=content,
                embedder=provider,
                execute=True,
                batch_size=8,
                reprocessing_decision="Operator approves bounded policy migration",
            )
            == 0
        )
        async with pg_pool.connection() as conn:
            ledger = (
                await (
                    await conn.execute(
                        "SELECT embedding_accounting FROM documents WHERE id=%s",
                        (doc.id,),
                    )
                ).fetchone()
            )[0]
        assert ledger["authorization"]["decision"] == "Operator approves bounded policy migration"
        assert len(ledger["inputs"]) == 1
        if _ == 0:
            authorized_calls = list(provider.calls)
        else:
            assert provider.calls == authorized_calls
        assert await content.read_embedding_vectors(doc.id, [0]) == before


async def test_trial_reads_atomic_sparse_evidence(
    pg_pool, ingestion_service_no_abstraction, graph_store
):
    from scripts.embedding_contention_trial import read_embedding_evidence

    doc = _doc(1)
    await graph_store.insert_document(doc)
    service = ingestion_service_no_abstraction
    service._content_store = content = PostgresContentStore(pg_pool)
    service._embedding = provider = _RecordedProvider()
    original_embed = provider.embed

    async def normalized(texts):
        import math

        return [
            [value / math.sqrt(sum(x * x for x in vector)) for value in vector]
            for vector in await original_embed(texts)
        ]

    provider.embed = normalized
    projection = _projection_with_limits(
        doc,
        [HeadingNode(1, f"H{i}", f"H{i}", f"Authored unique value {i}") for i in range(3)],
        max_excerpts=2,
    )
    await service._stage2_indexing(doc.id, projection)
    evidence = await read_embedding_evidence(content, doc.id, 768)
    assert evidence["embedded_passages"] == 2
    assert evidence["unembedded_passages"] == 1
    assert all(
        evidence[name]
        for name in ("vectors_valid", "embedding_coverage_valid", "embedding_accounting_valid")
    )
    async with pg_pool.connection() as conn:
        await conn.execute(
            "UPDATE chunks SET embedding=%s::vector WHERE document_id=%s AND chunk_index=0",
            ("[" + ",".join(["0"] * 768) + "]", doc.id),
        )
    assert (await read_embedding_evidence(content, doc.id, 768))["vectors_valid"] is False
    async with pg_pool.connection() as conn:
        await conn.execute(
            "UPDATE chunks SET embedding=NULL WHERE document_id=%s AND chunk_index=0", (doc.id,)
        )
    assert (await read_embedding_evidence(content, doc.id, 768))[
        "embedding_coverage_valid"
    ] is False
    async with pg_pool.connection() as conn:
        await conn.execute(
            "UPDATE documents SET embedding_accounting="
            "jsonb_set(embedding_accounting, '{tokens}', '0') WHERE id=%s",
            (doc.id,),
        )
    assert (await read_embedding_evidence(content, doc.id, 768))[
        "embedding_accounting_valid"
    ] is False
    async with pg_pool.connection() as conn:
        from psycopg.types.json import Jsonb

        ledger = evidence["embedding_accounting"].copy()
        ledger["identity"] = "wrong identity"
        await conn.execute(
            "UPDATE documents SET embedding_accounting=%s WHERE id=%s", (Jsonb(ledger), doc.id)
        )
    assert (await read_embedding_evidence(content, doc.id, 768))[
        "embedding_accounting_valid"
    ] is False
