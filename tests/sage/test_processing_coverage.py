"""Processing evidence stays distinct from pipeline outcome and replacement work."""

import pytest

from sage.adapters.content_store_postgres import PostgresContentStore
from sage.adapters.interfaces import Chunk
from sage.models.schemas import DocumentSummary, DocumentSummaryLight
from tests.sage.test_postgres_graph_store import _doc


async def test_legacy_and_terminal_records_have_unknown_coverage(postgres_graph_store):
    doc = _doc(1)
    await postgres_graph_store.insert_document(doc)
    read = await postgres_graph_store.get_document(doc.id)
    assert "processing_coverage" in type(read).model_fields
    assert read.processing_coverage.source_retention.scope == "unknown"
    assert read.processing_coverage.projection.scope == "unknown"
    assert DocumentSummaryLight.from_document(read).processing_coverage == read.processing_coverage
    assert DocumentSummary.from_document(read).processing_coverage == read.processing_coverage


async def test_replacement_and_stale_publication_preserve_served_artifacts(
    pg_pool, postgres_graph_store
):
    await postgres_graph_store.insert_document(_doc(1))
    store = PostgresContentStore(pg_pool)
    assert callable(getattr(store, "begin_processing_attempt", None)), (
        "durable attempt boundary missing"
    )
    from sage.models.coverage import CoverageDimension, ProcessingIdentity

    identity = ProcessingIdentity(
        source_hash=_doc(1).source_content_hash,
        adapter="markdown:1",
        policy="coverage-v1",
        configuration={},
        tokenization="unknown",
    )
    a = await store.begin_processing_attempt("00000001_doc_1")
    evidence = {
        "projection": CoverageDimension(
            scope="limited",
            identity=identity,
            counts=[{"unit": "characters", "observed": 3, "represented": 3, "total": None}],
            limitations=["inspection_unknown"],
        )
    }
    evidence["keyword_indexing"] = evidence["projection"]
    evidence["passage_embeddings"] = evidence["projection"]
    await store.publish_processing(
        "00000001_doc_1",
        a,
        evidence,
        chunks=[
            Chunk(document_id="00000001_doc_1", heading_path="A", content="old", chunk_index=0)
        ],
    )
    b = await store.begin_processing_attempt("00000001_doc_1")
    assert b > a
    assert (await store.get_all_chunks("00000001_doc_1"))[0].content == "old"
    assert not await store.publish_processing("00000001_doc_1", a, evidence, chunks=_chunks("bad"))
    assert (await store.get_all_chunks("00000001_doc_1"))[0].content == "old"
    assert await store.publish_processing(
        "00000001_doc_1",
        b,
        evidence,
        chunks=[
            Chunk(document_id="00000001_doc_1", heading_path="B", content="new", chunk_index=0)
        ],
    )
    assert (await store.get_all_chunks("00000001_doc_1"))[0].content == "new"


@pytest.mark.parametrize(
    "bad",
    [
        {
            "scope": "complete",
            "counts": [{"unit": "characters", "observed": 2, "represented": 2, "total": None}],
        },
        {
            "scope": "limited",
            "counts": [{"unit": "characters", "observed": 2, "represented": 3, "total": 2}],
        },
        {"scope": "unknown", "limitations": ["x" * 513]},
    ],
)
def test_coverage_rejects_unestablished_or_unbounded_claims(bad):
    assert "processing_coverage" in type(_doc(1)).model_fields
    from sage.models.coverage import CoverageDimension

    if bad["scope"] in ("complete", "limited"):
        bad = {**bad, "identity": _identity().model_dump(), "unknown_remainder": False}
    with pytest.raises(ValueError):
        CoverageDimension.model_validate(bad)


def _identity():
    from sage.models.coverage import ProcessingIdentity

    return ProcessingIdentity(
        source_hash=_doc(1).source_content_hash,
        adapter="markdown:1",
        policy="coverage-v1",
        configuration={},
        tokenization="unknown",
    )


def _evidence():
    from sage.models.coverage import CoverageDimension

    dimension = CoverageDimension(
        scope="limited",
        identity=_identity(),
        counts=[{"unit": "characters", "observed": 3, "represented": 3, "total": None}],
        limitations=["inspection_unknown"],
    )
    return {name: dimension for name in ("projection", "keyword_indexing", "passage_embeddings")}


def _chunks(text):
    return [Chunk(document_id=_doc(1).id, heading_path="Section", content=text, chunk_index=0)]


async def test_two_connections_reject_delayed_older_attempt(pg_pool, postgres_graph_store):
    import asyncio

    await postgres_graph_store.insert_document(_doc(1))
    older, newer = PostgresContentStore(pg_pool), PostgresContentStore(pg_pool)
    admitted, settled, publishing = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def delayed():
        attempt = await older.begin_processing_attempt(_doc(1).id)
        admitted.set()
        await settled.wait()
        publishing.set()
        return await older.publish_processing(
            _doc(1).id, attempt, _evidence(), chunks=_chunks("old")
        )

    pending = asyncio.create_task(delayed())
    await admitted.wait()
    assert not publishing.is_set(), "older publication must remain held until newer work settles"
    attempt = await newer.begin_processing_attempt(_doc(1).id)
    assert await newer.publish_processing(_doc(1).id, attempt, _evidence(), chunks=_chunks("new"))
    settled.set()
    assert await pending is False
    coverage, chunks = await older.read_processing_snapshot(_doc(1).id)
    assert coverage.projection.generation == attempt
    assert chunks[0].content == "new"


@pytest.mark.parametrize("boundary", ["chunks_delete", "surface_insert", "coverage_update"])
async def test_publication_failure_rolls_back_every_artifact(
    boundary, pg_pool, postgres_graph_store
):
    from psycopg import errors

    from sage.adapters.interfaces import DocumentSurface

    await postgres_graph_store.insert_document(_doc(1))
    store = PostgresContentStore(pg_pool)
    old = await store.begin_processing_attempt(_doc(1).id)
    surface = DocumentSurface(document_id=_doc(1).id, matchable="old", orienting="old")
    assert await store.publish_processing(
        _doc(1).id, old, _evidence(), chunks=_chunks("old"), surface=surface
    )
    attempt = await store.begin_processing_attempt(_doc(1).id)
    table, event = {
        "chunks_delete": ("chunks", "DELETE"),
        "surface_insert": ("document_surface", "INSERT"),
        "coverage_update": ("documents", "UPDATE"),
    }[boundary]
    async with pg_pool.connection() as conn:
        await conn.execute(
            "CREATE FUNCTION refuse_publication() RETURNS trigger LANGUAGE plpgsql AS $$ "
            "BEGIN RAISE EXCEPTION 'injected publication failure'; END $$"
        )
        await conn.execute(
            f"CREATE TRIGGER refuse_publication BEFORE {event} ON {table} "
            "FOR EACH ROW EXECUTE FUNCTION refuse_publication()"
        )
    try:
        with pytest.raises(errors.RaiseException, match="injected publication failure"):
            await store.publish_processing(
                _doc(1).id, attempt, _evidence(), chunks=_chunks("new"), surface=surface
            )
    finally:
        async with pg_pool.connection() as conn:
            await conn.execute(f"DROP TRIGGER refuse_publication ON {table}")
            await conn.execute("DROP FUNCTION refuse_publication()")
    # Reopening the binding obtains another connection and sees only the old committed generation.
    coverage, chunks = await PostgresContentStore(pg_pool).read_processing_snapshot(_doc(1).id)
    assert chunks[0].content == "old"
    assert coverage.projection.generation == old
    assert coverage.attempt == attempt


def test_manifest_identity_bounds_and_unknown_units():
    import hashlib
    import json

    from sage.models.coverage import CoverageDimension, CoverageManifest, CoverageRange

    entries = [{"location": "/records", "start": 0, "end": 3}]
    payload = {"entries": entries, "unknown_remainder": True}
    digest = (
        "sha256:"
        + hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
    )
    manifest = CoverageManifest(
        generation=1,
        source_hash=_identity().source_hash,
        content_hash=digest,
        entries=entries,
        unknown_remainder=True,
    )
    limited = CoverageDimension(
        scope="limited",
        generation=1,
        identity=_identity(),
        manifest=manifest,
        counts=[{"unit": "records", "observed": 3, "represented": 2, "total": None}],
    )
    assert limited.counts[0].total is None
    assert limited.model_dump(mode="json")["manifest"]["content_hash"] == digest
    with pytest.raises(ValueError, match="manifest identity"):
        CoverageDimension.model_validate({**limited.model_dump(), "generation": 2})
    with pytest.raises(ValueError, match="manifest identity"):
        CoverageDimension.model_validate(
            {
                **limited.model_dump(),
                "identity": {**_identity().model_dump(), "source_hash": "sha256:" + "f" * 64},
            }
        )
    with pytest.raises(ValueError, match="manifest identity"):
        CoverageDimension.model_validate({**limited.model_dump(), "unknown_remainder": False})
    with pytest.raises(ValueError):
        CoverageManifest.model_validate(
            {**manifest.model_dump(), "content_hash": "sha256:" + "0" * 64}
        )
    oversized_entries = entries * 33
    oversized_digest = (
        "sha256:"
        + hashlib.sha256(
            json.dumps(
                {"entries": oversized_entries, "unknown_remainder": True},
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
    )
    with pytest.raises(ValueError):
        CoverageManifest.model_validate(
            {
                **manifest.model_dump(),
                "entries": oversized_entries,
                "content_hash": oversized_digest,
            }
        )
    assert len(CoverageDimension(ranges=[]).ranges) == 0
    with pytest.raises(ValueError):
        CoverageRange(location="/x", start=3, end=2)
    assert CoverageDimension(scope="not_applicable").scope == "not_applicable"


async def test_legacy_upgrade_is_idempotent_and_does_not_infer_success(
    pg_pool, pg_schema, postgres_graph_store
):
    from sage.storage.postgres.schema import bootstrap_schema

    await postgres_graph_store.insert_document(_doc(1))
    await postgres_graph_store.update_document(
        _doc(1).id, {"pipeline_status": "abstraction_complete"}
    )
    # Exercise a genuinely pre-extension store, not only repeated modern bootstrap.
    async with pg_pool.connection() as conn:
        await conn.execute("ALTER TABLE documents DROP COLUMN processing_coverage")
    for _ in range(2):
        async with pg_pool.connection() as conn:
            await bootstrap_schema(conn, schema=pg_schema)
        read = await postgres_graph_store.get_document(_doc(1).id)
        assert read.processing_coverage.attempt == 0
        assert read.processing_coverage.projection.scope == "unknown"
        assert read.pipeline_status.value == "abstraction_complete"


async def test_ingest_retention_survives_abstraction_failure(
    ingestion_service_failing_llm, tmp_vault_dir
):
    from sage.models.schemas import IngestRequest

    source = tmp_vault_dir / "delivered.md"
    source.write_text("# Source\n\nRetained even if later work fails.")
    response = await ingestion_service_failing_llm.ingest(IngestRequest(source=str(source)))
    read = await ingestion_service_failing_llm._store.get_document(response.document.id)
    assert read.pipeline_status.value == "failed"
    assert read.processing_coverage.source_retention.scope == "complete"
    assert (
        read.processing_coverage.source_retention.identity.source_hash == read.source_content_hash
    )
    assert read.processing_coverage.source_retention.counts[0].total == source.stat().st_size
    assert read.processing_coverage.source_retention.counts[0].represented == source.stat().st_size
    assert read.processing_coverage.inspection.scope == "complete"
    assert read.processing_coverage.abstraction_evidence.scope == "unknown"


async def test_same_achieved_scope_survives_distinct_pipeline_outcomes(
    pg_pool, postgres_graph_store
):
    from sage.models.coverage import CoverageDimension

    await postgres_graph_store.insert_document(_doc(1))
    store = PostgresContentStore(pg_pool)
    a = await store.begin_processing_attempt(_doc(1).id)
    retention = CoverageDimension(scope="complete", identity=_identity(), unknown_remainder=False)
    assert await store.publish_processing(_doc(1).id, a, {"source_retention": retention})
    assert await store.publish_processing(_doc(1).id, a, _evidence(), chunks=_chunks("old"))
    assert await store.update_processing_status(
        _doc(1).id, a, {"pipeline_status": "abstraction_complete"}
    )
    before = await postgres_graph_store.get_document(_doc(1).id)
    assert before.processing_coverage.source_retention.scope == "complete"
    assert before.processing_coverage.source_retention.identity == _identity()
    b = await store.begin_processing_attempt(_doc(1).id)
    assert await store.publish_processing(_doc(1).id, b, _evidence(), chunks=_chunks("new"))
    assert await store.update_processing_status(
        _doc(1).id, b, {"pipeline_status": "failed", "pipeline_error": "broader intent interrupted"}
    )
    after = await postgres_graph_store.get_document(_doc(1).id)
    assert before.pipeline_status != after.pipeline_status
    assert (
        before.processing_coverage.projection.counts == after.processing_coverage.projection.counts
    )
    assert before.processing_coverage.source_retention == after.processing_coverage.source_retention
    assert not await store.update_processing_status(
        _doc(1).id, a, {"pipeline_status": "abstraction_complete"}
    )
    assert (await postgres_graph_store.get_document(_doc(1).id)).pipeline_status.value == "failed"


@pytest.mark.parametrize("restamp", [True, False])
async def test_cancel_reabstract_then_recover_preserves_served_projection(
    ingestion_service, tmp_vault_dir, monkeypatch, restamp
):
    import asyncio

    from sage.models.schemas import IngestRequest
    from tests.helpers.pipeline_wait import drain_abstraction_queue

    source = tmp_vault_dir / "replacement.md"
    source.write_text("# Source\n\nThe original representation remains readable.")
    result = await ingestion_service.ingest(IngestRequest(source=str(source)))
    original = await ingestion_service._store.get_document(result.document.id)
    original_chunks = await ingestion_service._content_store.get_all_chunks(original.id)
    entered = asyncio.Event()
    provider = ingestion_service._abstraction.generate_abstract

    async def held(*args, **kwargs):
        entered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(ingestion_service._abstraction, "generate_abstract", held)
    await ingestion_service.reabstract(original.id)
    await asyncio.wait_for(entered.wait(), timeout=5)
    running = await ingestion_service._store.get_document(original.id)
    assert running.processing_coverage.projection == original.processing_coverage.projection
    assert running.semantic_abstract == original.semantic_abstract
    await ingestion_service.stop_worker(restamp=restamp)
    interrupted = await ingestion_service._store.get_document(original.id)
    assert interrupted.pipeline_status.value == (
        "abstraction_interrupted" if restamp else "abstraction_in_progress"
    )
    assert interrupted.processing_coverage.projection == original.processing_coverage.projection
    assert interrupted.semantic_abstract == original.semantic_abstract
    monkeypatch.setattr(ingestion_service._abstraction, "generate_abstract", provider)
    assert await ingestion_service.recover_incomplete_documents() == 1
    await drain_abstraction_queue(ingestion_service)
    recovered = await ingestion_service._store.get_document(original.id)
    assert recovered.pipeline_status.value == "abstraction_complete"
    assert recovered.processing_coverage.projection == original.processing_coverage.projection
    assert (
        recovered.processing_coverage.abstraction_evidence.generation
        > original.processing_coverage.abstraction_evidence.generation
    )
    assert [
        c.content for c in await ingestion_service._content_store.get_all_chunks(original.id)
    ] == [c.content for c in original_chunks]


async def test_projection_and_section_reads_preserve_generation(
    pg_pool, postgres_graph_store, minimal_config, stub_embedding_provider
):
    from sage.services.utilities import UtilitiesService

    await postgres_graph_store.insert_document(_doc(1))
    store = PostgresContentStore(pg_pool)
    attempt = await store.begin_processing_attempt(_doc(1).id)
    assert await store.publish_processing(_doc(1).id, attempt, _evidence(), chunks=_chunks("old"))
    service = UtilitiesService(postgres_graph_store, store, stub_embedding_provider, minimal_config)
    projection = await service.read_projection(_doc(1).id)
    section = await service.read_section(_doc(1).id, "Section")
    headings = await service.list_headings(_doc(1).id)
    assert headings.headings == ["Section"]
    assert headings.processing_coverage == projection.processing_coverage
    assert projection.projection_text == section.section_text == "old"
    assert projection.processing_coverage.projection.generation == attempt
    assert section.processing_coverage == projection.processing_coverage


async def test_search_snapshot_cannot_attach_new_coverage_to_old_excerpt(
    pg_pool, postgres_graph_store, monkeypatch
):
    import asyncio

    await postgres_graph_store.insert_document(_doc(1))
    reader = PostgresContentStore(pg_pool)
    writer = PostgresContentStore(pg_pool)
    a = await writer.begin_processing_attempt(_doc(1).id)
    assert await writer.publish_processing(_doc(1).id, a, _evidence(), chunks=_chunks("old"))
    fetched, release = asyncio.Event(), asyncio.Event()
    original = reader._search_bm25_across_document

    async def paused(*args, **kwargs):
        results = await original(*args, **kwargs)
        fetched.set()
        await release.wait()
        return results

    monkeypatch.setattr(reader, "_search_bm25_across_document", paused)
    pending = asyncio.create_task(reader.search_bm25("old"))
    await asyncio.wait_for(fetched.wait(), timeout=5)
    b = await writer.begin_processing_attempt(_doc(1).id)
    assert await writer.publish_processing(_doc(1).id, b, _evidence(), chunks=_chunks("new"))
    release.set()
    results = await pending
    assert results[0].content == "old"
    assert results[0].processing_coverage.projection.generation == a
    assert (
        await postgres_graph_store.get_document(_doc(1).id)
    ).processing_coverage.projection.generation == b


@pytest.mark.parametrize("scope", ["unknown", "pending", "not_applicable"])
def test_unestablished_scopes_remain_distinct_and_cannot_assert_counts(scope):
    from sage.models.coverage import CoverageDimension

    assert CoverageDimension(scope=scope).model_dump()["scope"] == scope
    with pytest.raises(ValueError, match="cannot assert counts"):
        CoverageDimension(
            scope=scope, counts=[{"unit": "bytes", "observed": 0, "represented": 0, "total": 0}]
        )


def test_empty_source_and_record_bounds_have_real_positive_controls():
    import hashlib
    import json

    from sage.models.coverage import (
        CoverageDimension,
        CoverageManifest,
        ProcessingCoverage,
        ProcessingIdentity,
    )

    empty = CoverageDimension(
        scope="complete",
        identity=_identity(),
        unknown_remainder=False,
        counts=[{"unit": "bytes", "observed": 0, "represented": 0, "total": 0}],
    )
    assert empty.counts[0].total == 0
    with pytest.raises(ValueError, match="each count unit"):
        CoverageDimension(scope="limited", identity=_identity(), counts=empty.counts * 2)
    for changes in [
        {"source_hash": "not-a-hash"},
        {"adapter": "x" * 513},
        {"configuration": {str(i): i for i in range(33)}},
        {"configuration": {"large": "x" * 4096}},
    ]:
        with pytest.raises(ValueError):
            ProcessingIdentity.model_validate({**_identity().model_dump(), **changes})
    entries = [{"location": "/records", "start": 0, "end": 3}] * 32
    payload = {"entries": entries, "unknown_remainder": True}
    digest = (
        "sha256:"
        + hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
    )
    manifest = CoverageManifest(
        generation=1,
        source_hash=_identity().source_hash,
        content_hash=digest,
        entries=entries,
        unknown_remainder=True,
    )
    assert len(manifest.entries) == 32
    long_entries = [{"location": "x" * 512, "start": 0, "end": 1}] * 32
    long_digest = (
        "sha256:"
        + hashlib.sha256(
            json.dumps(
                {"entries": long_entries, "unknown_remainder": True},
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
    )
    with pytest.raises(ValueError, match="size mismatch"):
        CoverageManifest.model_validate(
            {**manifest.model_dump(), "entries": long_entries, "content_hash": long_digest}
        )
    oversized = entries + entries[:1]
    over_digest = (
        "sha256:"
        + hashlib.sha256(
            json.dumps(
                {"entries": oversized, "unknown_remainder": True},
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
    )
    with pytest.raises(ValueError):
        CoverageManifest.model_validate(
            {**manifest.model_dump(), "entries": oversized, "content_hash": over_digest}
        )
    dimension = CoverageDimension(
        scope="limited",
        identity=_identity(),
        ranges=[{"location": "x" * 512, "start": 0, "end": 1}] * 64,
    )
    with pytest.raises(ValueError, match="summary exceeds"):
        ProcessingCoverage(
            **{
                name: dimension
                for name in (
                    "source_retention",
                    "inspection",
                    "projection",
                    "keyword_indexing",
                    "passage_embeddings",
                    "abstraction_evidence",
                )
            }
        )


@pytest.mark.parametrize("writer", ["update", "migration"])
async def test_standalone_artifact_rewrite_invalidates_achieved_scope(
    pg_pool, postgres_graph_store, writer
):
    await postgres_graph_store.insert_document(_doc(1))
    store = PostgresContentStore(pg_pool)
    attempt = await store.begin_processing_attempt(_doc(1).id)
    assert await store.publish_processing(_doc(1).id, attempt, _evidence(), chunks=_chunks("old"))
    if writer == "update":
        await store.update_indexed_structure(_doc(1).id, [("Section", "New title")])
    else:
        await store.migrate_indexed_structure([(_doc(1).id, "Section", "New title")])
    after = await postgres_graph_store.get_document(_doc(1).id)
    assert after.processing_coverage.keyword_indexing.scope == "unknown"
    assert after.processing_coverage.keyword_indexing.generation > attempt
    assert after.processing_coverage.projection.scope == "limited"
    assert after.processing_coverage.projection.generation == attempt


async def test_old_worker_shutdown_cannot_interrupt_a_newer_database_attempt(
    ingestion_service, tmp_vault_dir, monkeypatch
):
    import asyncio

    from sage.models.schemas import IngestRequest

    source = tmp_vault_dir / "shutdown.md"
    source.write_text("# Source\n\nStored before replacement.")
    result = await ingestion_service.ingest(IngestRequest(source=str(source)))
    entered = asyncio.Event()

    async def held(*args, **kwargs):
        entered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(ingestion_service._abstraction, "generate_abstract", held)
    await ingestion_service.reabstract(result.document.id)
    await asyncio.wait_for(entered.wait(), timeout=5)
    newer = await ingestion_service._content_store.begin_processing_attempt(result.document.id)
    assert await ingestion_service._content_store.update_processing_status(
        result.document.id, newer, {"pipeline_status": "abstraction_in_progress"}
    )
    await ingestion_service.stop_worker()
    read = await ingestion_service._store.get_document(result.document.id)
    assert read.processing_coverage.attempt == newer
    assert read.pipeline_status.value == "abstraction_in_progress"


async def test_hybrid_scored_response_keeps_snapshot_generation_after_new_publication(
    pg_pool, postgres_graph_store, minimal_config, stub_embedding_provider, monkeypatch
):
    from sage.models.schemas import DiscoverRequest
    from sage.services.retrieval import RetrievalService
    from sage.storage.postgres.schema import EMBEDDING_DIM

    await postgres_graph_store.insert_document(_doc(1))
    reader, writer = PostgresContentStore(pg_pool), PostgresContentStore(pg_pool)
    vector = [1.0] + [0.0] * (EMBEDDING_DIM - 1)
    chunks = _chunks("old")
    chunks[0].embedding = vector
    a = await writer.begin_processing_attempt(_doc(1).id)
    assert await writer.publish_processing(_doc(1).id, a, _evidence(), chunks=chunks)

    async def embed(texts):
        return [vector for _ in texts]

    monkeypatch.setattr(stub_embedding_provider, "embed", embed)
    service = RetrievalService(
        postgres_graph_store, reader, stub_embedding_provider, minimal_config
    )
    original = service._hybrid_rrf

    async def replace_after_fusion(*args, **kwargs):
        results = await original(*args, **kwargs)
        assert results and results[0].processing_coverage.projection.generation == a
        b = await writer.begin_processing_attempt(_doc(1).id)
        assert await writer.publish_processing(_doc(1).id, b, _evidence(), chunks=_chunks("new"))
        return results

    monkeypatch.setattr(service, "_hybrid_rrf", replace_after_fusion)
    response = await service.discover(
        DiscoverRequest(mode="semantic", query="old", use_hybrid=True)
    )
    assert response.results[0].chunk_content == "old"
    assert response.results[0].document.processing_coverage.projection.generation == a
    assert (
        await postgres_graph_store.get_document(_doc(1).id)
    ).processing_coverage.projection.generation > a


async def test_failed_recompute_preserves_prior_projection_and_abstract(
    ingestion_service, tmp_vault_dir, monkeypatch
):
    from sage.models.schemas import IngestRequest
    from tests.helpers.pipeline_wait import drain_abstraction_queue

    source = tmp_vault_dir / "recompute.md"
    source.write_text("# Source\n\nA served generation survives failed replacement embedding.")
    result = await ingestion_service.ingest(IngestRequest(source=str(source)))
    original = await ingestion_service._store.get_document(result.document.id)
    old_chunks = await ingestion_service._content_store.get_all_chunks(original.id)

    async def refuse(texts):
        raise RuntimeError("controlled replacement embedding failure")

    monkeypatch.setattr(ingestion_service._embedding, "embed", refuse)
    ingestion_service._config.abstraction.max_attempts = 1
    await ingestion_service.recompute_pipeline(original.id)
    await drain_abstraction_queue(ingestion_service)
    read = await ingestion_service._store.get_document(original.id)
    assert read.pipeline_status.value == "failed"
    assert read.processing_coverage.projection == original.processing_coverage.projection
    assert read.semantic_abstract == original.semantic_abstract
    assert [
        c.content for c in await ingestion_service._content_store.get_all_chunks(original.id)
    ] == [c.content for c in old_chunks]
