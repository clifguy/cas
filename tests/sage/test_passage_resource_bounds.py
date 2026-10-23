"""Resource guards at the shared passage boundary, before tokenizer allocation."""

import asyncio
import csv
import io
import threading

import numpy as np
import pytest

from sage.adapters.embedding_nomic import NomicEmbeddingProvider
from sage.adapters.stubs import StubEmbeddingProvider
from sage.models.enums import PipelineStatus, SourceType
from sage.models.schemas import IngestRequest
from sage.services.ingestion import IngestionService
from sage.services.passage_split import embedding_input, split_passage
from sage.source_adapters.base import ProjectionResult
from sage.source_adapters.structured_data_adapter import StructuredDataAdapter
from tests.helpers.pipeline_wait import await_pipeline_idle
from tests.sage.test_ingestion import _create_test_file

CANDIDATE_LIMIT = 32768


@pytest.mark.parametrize("shape", ["rows", "field", "blank-lines"])
def test_split_never_offers_a_whole_large_section_to_the_predicate(shape):
    text = {
        "rows": '42,"漢字🙂","{"key":"value"}"\r\n' * 4000,
        "field": "x" * (CANDIDATE_LIMIT * 5) + "\r\nlast",
        "blank-lines": "first\r\n" + "\r\n" * CANDIDATE_LIMIT + "tail",
    }[shape]
    seen = []

    def fits(candidate):
        assert len(candidate) <= CANDIDATE_LIMIT, "unbounded tokenizer candidate"
        seen.append(len(candidate))
        # A pathological WordPiece word may count as just one [UNK].
        return True

    pieces = split_passage(text, fits)

    assert "".join(pieces) == text
    assert max(map(len, pieces)) <= CANDIDATE_LIMIT
    assert len(pieces) > 1
    assert seen
    assert not any(a.endswith("\r") and b.startswith("\n") for a, b in zip(pieces, pieces[1:]))


@pytest.mark.parametrize(
    "heading,text",
    [
        (heading, ('event,"quoted\nmultiline 漢🙂",json\r\n' * 3000) + "x" * 70000)
        for heading in ("", "normal heading")
    ]
    + [("h" * (CANDIDATE_LIMIT * 2), "Body retained behind an oversized heading.")],
    ids=["empty", "normal", "oversized"],
)
def test_shared_sizing_bounds_actual_tokenizer_calls(heading, text):
    class BoundedProvider(StubEmbeddingProvider):
        def count_tokens(self, text):
            assert len(text) <= CANDIDATE_LIMIT, "unbounded tokenizer input"
            return len(text.encode()) + 2

    service = object.__new__(IngestionService)
    service._embedding = BoundedProvider(max_input_tokens=256)

    assert not service._fits_embedding_input(heading)(text)
    chunks = service._passages_for_sections("doc", [(heading, text)])

    assert "".join(c.content for c in chunks) == text
    assert all(c.heading_path == heading and c.section_index == 0 for c in chunks)
    assert [c.chunk_index for c in chunks] == list(range(len(chunks)))
    if len(heading) < CANDIDATE_LIMIT:
        assert all(service._fits_embedding_input(heading)(c.content) for c in chunks)
    else:
        assert len(chunks) == 1  # Existing exceptional crowded-heading semantics.


async def test_stage2_sizing_leaves_the_request_loop_responsive(ingestion_service, graph_store):
    from tests.sage.test_postgres_graph_store import _doc

    for index in (1, 2):
        await graph_store.insert_document(_doc(index))
    started = threading.Event()
    release = threading.Event()
    sizing_threads = []
    original_count = ingestion_service._embedding.count_tokens

    def count(text):
        sizing_threads.append(threading.current_thread().name)
        if not started.is_set():
            started.set()
            release.wait(1.0)
        return original_count(text)

    ingestion_service._embedding.count_tokens = count
    projection = ProjectionResult(
        text="event," * 2000,
        headings=[],
        content_hash="sha256:test",
        adapter_version="1",
        title="Test",
    )
    task = asyncio.create_task(ingestion_service._stage2_indexing("00000001_doc_1", projection))
    second = asyncio.create_task(ingestion_service._stage2_indexing("00000002_doc_2", projection))
    try:
        for _ in range(100):
            if started.is_set():
                break
            await asyncio.sleep(0.005)
        assert started.is_set()
        assert sizing_threads == [sizing_threads[0]], "only one sizing job may enter the worker"
        assert all(name.startswith("sage-passage") for name in sizing_threads)
        assert not second.done()
        assert not task.done(), "sizing must still be waiting while the loop runs"
    finally:
        release.set()
        await asyncio.gather(task, second)
    assert len(set(sizing_threads)) == 1, "all vaults must share one passage worker"


async def test_interrupted_headingless_source_recovers_through_bounded_sizing(
    tmp_vault_dir, ingestion_service, graph_store, stub_content_store
):
    text = ('event,"quoted\nfield 漢🙂",json\r\n' * 4000) + "x" * 70000
    _create_test_file(tmp_vault_dir, "large.md", text)
    # First persist the retained source and document through real ingestion.
    result = await ingestion_service.ingest(
        IngestRequest(source="large.md", source_type=SourceType.MARKDOWN), wait_for_pipeline=True
    )
    doc_id = result.document.id
    await stub_content_store.remove_document(doc_id)
    await graph_store.update_document(
        doc_id, {"pipeline_status": PipelineStatus.INDEXING_IN_PROGRESS.value}
    )
    original_count = ingestion_service._embedding.count_tokens
    counts = []

    def count(candidate):
        assert len(candidate) <= CANDIDATE_LIMIT, "recovery tokenizes complete retained source"
        counts.append(len(candidate))
        return original_count(candidate)

    ingestion_service._embedding.count_tokens = count
    assert await ingestion_service.recover_incomplete_documents() == 1
    terminal = await await_pipeline_idle(graph_store, doc_id, service=ingestion_service)
    assert terminal.pipeline_status == PipelineStatus.ABSTRACTION_COMPLETE
    chunks = await stub_content_store.get_all_chunks(doc_id)
    assert "".join(c.content for c in chunks) == text
    assert counts
    assert len(await graph_store.list_all_documents()) == 1
    assert await ingestion_service.recover_incomplete_documents() == 0
    await ingestion_service.stop_worker()


@pytest.mark.parametrize("target_bytes", [9000, 26 * 1024 * 1024], ids=["9kb", "26mb"])
async def test_csv_projection_preserves_records_across_bounded_passages(tmp_path, target_bytes):
    # JSON quotes, Unicode and quoted multiline cells exercise actual parser
    # record boundaries; source has CRLF while the quoted cell has lone CR/LF.
    row = '2026-10-21,security,"漢🙂 first\nsecond\rthird","{""result"":""ok""}"\r\n'
    repeats = target_bytes // len(row.encode()) + 1
    text = "date,type,detail,json\r\n" + row * repeats
    source = tmp_path / "events.csv"
    source.write_bytes(text.encode())
    from sage.source_adapters.structure import FactLimits

    # This large record-preservation control opts into the supported hard ceilings.
    ceilings = FactLimits.hard_limits()
    projection = await StructuredDataAdapter().project(
        source,
        config={"discovery_facts": {k: ceilings[k] for k in ("max_rows", "max_parse_nodes")}},
    )
    assert not projection.headings
    service = object.__new__(IngestionService)
    service._embedding = StubEmbeddingProvider()
    chunks = service._chunk_projection("doc", projection)
    assert "".join(c.content for c in chunks) == projection.text
    assert len(chunks) > 1
    assert all(len(c.content) <= CANDIDATE_LIMIT for c in chunks)
    assert all(service._fits_embedding_input("")(c.content) for c in chunks)
    # A fitting record stays whole despite its quoted embedded line ends.
    assert all(c.content.endswith("\r\n\n") for c in chunks[:-1])
    rows = (record for record in csv.reader(io.StringIO(projection.text, newline="")) if record)
    assert next(rows) == ["date", "type", "detail", "json"]
    count = 0
    for record in rows:
        assert record == ["2026-10-21", "security", "漢🙂 first\nsecond\rthird", '{"result":"ok"}']
        count += 1
    assert count == repeats


async def test_failed_recovery_isolated_with_retained_source_and_bounded_retries(
    tmp_vault_dir, ingestion_service, graph_store, stub_content_store
):
    text = "event,data\n" * 9000
    source = _create_test_file(tmp_vault_dir, "failing.md", text)
    result = await ingestion_service.ingest(
        IngestRequest(source="failing.md", source_type=SourceType.MARKDOWN), wait_for_pipeline=True
    )
    doc_id = result.document.id
    await stub_content_store.remove_document(doc_id)
    await graph_store.update_document(
        doc_id, {"pipeline_status": PipelineStatus.INDEXING_IN_PROGRESS.value}
    )
    attempts = []

    def failing_count(candidate):
        assert len(candidate) <= CANDIDATE_LIMIT, "unbounded failed recovery"
        if candidate:
            attempts.append(len(candidate))
            raise RuntimeError("synthetic sizing failure")
        return 0

    original_count = ingestion_service._embedding.count_tokens
    ingestion_service._embedding.count_tokens = failing_count
    assert await ingestion_service.recover_incomplete_documents() == 1
    terminal = await await_pipeline_idle(graph_store, doc_id, service=ingestion_service)
    assert terminal.pipeline_status == PipelineStatus.FAILED
    assert "RuntimeError: synthetic sizing failure" in terminal.pipeline_error
    assert len(attempts) == ingestion_service._config.abstraction.max_attempts
    assert source.read_text() == text
    assert terminal.source_content_hash == result.document.source_content_hash
    assert await ingestion_service.recover_incomplete_documents() == 0
    ingestion_service._embedding.count_tokens = original_count
    _create_test_file(tmp_vault_dir, "healthy.md", "Healthy short content.")
    healthy = await ingestion_service.ingest(
        IngestRequest(source="healthy.md", source_type=SourceType.MARKDOWN), wait_for_pipeline=True
    )
    assert healthy.document.pipeline_status == PipelineStatus.ABSTRACTION_COMPLETE
    assert len(await graph_store.list_all_documents()) == 2
    await ingestion_service.stop_worker()


@pytest.fixture
def bounded_nomic():
    """Use the actual production provider with only inference/weights replaced."""
    calls = []

    class Model:
        def tokenizer(self, text):
            calls.append(("tokenizer", len(text)))
            return {"input_ids": list(range(len(text) + 2))}

        def encode(self, texts, **kwargs):
            calls.extend(("encode", len(text)) for text in texts)
            return np.zeros((len(texts), 768))

    provider = object.__new__(NomicEmbeddingProvider)
    provider._model = Model()
    provider._executor = None
    yield provider, calls
    if provider._executor:
        provider._executor.shutdown(wait=True)


@pytest.mark.parametrize("operation", ["count", "embed"])
async def test_nomic_refuses_unbounded_work_before_tokenizer_or_model(bounded_nomic, operation):
    provider, calls = bounded_nomic
    with pytest.raises(ValueError, match="tokenizer work bound"):
        if operation == "count":
            provider.count_tokens("x" * (CANDIDATE_LIMIT + 1))
        else:
            await provider.embed(["small", "x" * (CANDIDATE_LIMIT + 1)])
    assert calls == []
    assert provider._executor is None
    # The boundary itself is admitted, including long WordPiece [UNK] input.
    if operation == "count":
        assert provider.count_tokens("x" * CANDIDATE_LIMIT) == CANDIDATE_LIMIT + 2
    else:
        assert len(await provider.embed(["x" * CANDIDATE_LIMIT])) == 1
    assert calls == [("tokenizer" if operation == "count" else "encode", CANDIDATE_LIMIT)]


@pytest.mark.parametrize(
    "heading,body",
    [
        ("h " * 4000, "body data " * 6000),
        ("h " * CANDIDATE_LIMIT, "Short body."),
    ],
    ids=["crowded", "oversized"],
)
async def test_crowded_exception_fails_safely_without_losing_retained_source(
    tmp_vault_dir, ingestion_service, graph_store, bounded_nomic, heading, body
):
    provider, calls = bounded_nomic
    ingestion_service._embedding = provider
    text = "# " + heading + "\n\n" + body
    source = _create_test_file(tmp_vault_dir, "crowded.md", text)
    result = await ingestion_service.ingest(
        IngestRequest(source="crowded.md", source_type=SourceType.MARKDOWN), wait_for_pipeline=False
    )
    terminal = await await_pipeline_idle(graph_store, result.document.id, service=ingestion_service)
    assert terminal.pipeline_status == PipelineStatus.ABSTRACTION_SKIPPED
    assert terminal.semantic_abstract is None
    assert terminal.processing_coverage.abstraction_evidence.scope == "limited"
    assert terminal.pipeline_error is None
    assert terminal.processing_coverage.passage_embeddings.scope == "limited"
    assert terminal.processing_coverage.passage_embeddings.counts[0].represented == 0
    chunks = await ingestion_service._content_store.get_all_chunks(terminal.id)
    expected_passage = "# " + heading.rstrip() + "\n\n" + body.rstrip()
    assert "".join(c.content for c in chunks) == expected_passage
    assert terminal.processing_coverage.keyword_indexing.scope == "complete"
    assert source.read_text() == text
    assert terminal.source_content_hash == result.document.source_content_hash
    # Only the one-character authored document title reaches inference.
    assert [length for operation, length in calls if operation == "encode"] == [1]
    assert all(length <= CANDIDATE_LIMIT for _, length in calls)
    assert await ingestion_service.recover_incomplete_documents() == 0
    await ingestion_service.stop_worker()


def test_embedding_input_checks_combined_size_before_joining():
    class MustNotFormat(str):
        def __format__(self, spec):
            raise AssertionError("oversized input was joined before length check")

    heading = MustNotFormat("h" * (CANDIDATE_LIMIT // 2))
    content = MustNotFormat("c" * (CANDIDATE_LIMIT // 2))
    with pytest.raises(ValueError, match="tokenizer work bound"):
        embedding_input(heading, content)
    assert embedding_input("h", "c") == "h\n\nc"


async def test_small_crowded_exception_keeps_its_single_stored_passage(
    tmp_vault_dir, ingestion_service, graph_store, stub_content_store, bounded_nomic
):
    provider, calls = bounded_nomic
    ingestion_service._embedding = provider
    heading = "h " * 2000
    assert provider.count_tokens(embedding_input(heading, "")) > provider.max_input_tokens
    text = "# " + heading + "\n\nShort body."
    _create_test_file(tmp_vault_dir, "small-crowded.md", text)
    result = await ingestion_service.ingest(
        IngestRequest(source="small-crowded.md", source_type=SourceType.MARKDOWN),
        wait_for_pipeline=False,
    )
    terminal = await await_pipeline_idle(graph_store, result.document.id, service=ingestion_service)
    assert terminal.pipeline_status == PipelineStatus.ABSTRACTION_SKIPPED
    assert terminal.semantic_abstract is None
    assert terminal.processing_coverage.abstraction_evidence.scope == "limited"
    chunks = await stub_content_store.get_all_chunks(result.document.id)
    assert len(chunks) == 1
    assert chunks[0].heading_path == heading.rstrip()
    assert chunks[0].content == text.replace(heading, heading.rstrip()).rstrip()
    assert any(operation == "encode" for operation, _ in calls)
    assert all(length <= CANDIDATE_LIMIT for _, length in calls)
    await ingestion_service.stop_worker()
