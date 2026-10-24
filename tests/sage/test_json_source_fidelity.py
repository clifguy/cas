"""JSON authored occurrences and lexemes survive projection and native evidence."""

import hashlib
import json

import pytest

from sage.services.ingestion import _run_inspected_projection
from sage.source_adapters.base import SourceReadError
from sage.source_adapters.structure import FactLimits, source_facts
from sage.source_adapters.structured_data_adapter import StructuredDataAdapter

SOURCE = (
    r'{"title":"First title","title":"Last title","note":"Earlycedar","note":"Lateviolet",'
    r'"nested":{"note":"Innercedar","note":"Deepviolet"},'
    r'"numbers":[0.12345678901234567890123456789,123456789012345678901234567890,'
    r'1e+009,1.2300,-0,-0.000],"escaped":"caf\u00e9 \ud83d\ude00 \"quoted\""}'
)
NUMBERS = [
    "0.12345678901234567890123456789",
    "123456789012345678901234567890",
    "1e+009",
    "1.2300",
    "-0",
    "-0.000",
]


def lexical(text):
    return json.loads(text, object_pairs_hook=lambda pairs: pairs, parse_int=str, parse_float=str)


async def test_json_projection_preserves_occurrences_numbers_and_decoded_strings(tmp_path):
    path = tmp_path / "source.json"
    path.write_text(SOURCE)
    result = await StructuredDataAdapter().project(path)
    assert lexical(result.text) == lexical(SOURCE)
    assert all(token in result.text for token in NUMBERS)
    assert result.text.count('"note":') == 4
    assert "café 😀" in result.text
    assert result.title == "Last title"
    assert result.content_hash == hashlib.sha256(path.read_bytes()).hexdigest()
    assert result.adapter_version != "0.3.0"


@pytest.mark.parametrize("suffix", [".json", ".jsonl"])
async def test_json_native_evidence_retains_each_occurrence_and_exact_number(tmp_path, suffix):
    path = tmp_path / ("source" + suffix)
    path.write_text(SOURCE)
    stream = source_facts(path, None, FactLimits())
    facts = list(stream)
    assert [json.loads(f.rendering) for f in facts][:6] == [
        "First title",
        "Last title",
        "Earlycedar",
        "Lateviolet",
        "Innercedar",
        "Deepviolet",
    ]
    assert [f.rendering for f in facts][6:12] == NUMBERS
    assert len({f.location for f in facts}) == len(facts)
    assert stream.exhausted and not stream.limitations
    projection = await _run_inspected_projection(StructuredDataAdapter(), path, {})
    selected = "\n".join(x.input_text for x in projection.discovery_evidence.selected)
    assert all(
        term in selected for term in ["Earlycedar", "Lateviolet", "Innercedar", "Deepviolet"]
    )
    assert all(token in "\n".join(f.rendering for f in facts) for token in NUMBERS)


@pytest.mark.parametrize(
    "source", ['{"v":NaN}', '{"v":Infinity}', '{"v":01}', '{"v":1,}', '{"v":"\\x01"}']
)
async def test_json_refuses_non_json_syntax(tmp_path, source):
    path = tmp_path / "bad.json"
    path.write_text(source)
    with pytest.raises(SourceReadError):
        await StructuredDataAdapter().project(path)


@pytest.mark.parametrize("fail,keyword_limit", [(False, None), (True, None), (False, 200)])
async def test_json_storage_reads_search_and_sparse_inference(
    pg_pool, ingestion_service_no_abstraction, graph_store, tmp_vault_dir, fail, keyword_limit
):
    from sage.adapters.content_store_postgres import PostgresContentStore
    from sage.adapters.stubs import SeededEmbeddingProvider
    from sage.models.schemas import DiscoverRequest, IngestRequest, SourceType
    from sage.services.retrieval import RetrievalService
    from sage.services.utilities import UtilitiesService

    class BoundedProvider(SeededEmbeddingProvider):
        def __init__(self):
            super().__init__()
            self.calls = []

        async def embed(self, texts):
            self.calls.extend(texts)
            if fail:
                raise RuntimeError("controlled JSON embedding failure")
            return await super().embed(texts)

    service = ingestion_service_no_abstraction
    service._adapters[SourceType.STRUCTURED_DATA] = StructuredDataAdapter()
    content = PostgresContentStore(pg_pool)
    service._embedding = provider = BoundedProvider()
    # Minified, many small records, late nested duplicate; no parallel rounded oracle.
    records = [SOURCE] + ['{"n":1.2300,"pad":"' + "authored " * 12 + '"}'] * 200
    records += ['{"nested":{"note":"Latecypress","note":"Lastorchid"}}']
    raw = ("[" + ",".join(records) + "]").encode()
    path = tmp_vault_dir / "source.json"
    path.write_bytes(raw)
    limits = {"max_excerpts": 2, "max_tokens": 2000, "max_in_flight": 1}
    if keyword_limit is not None:
        limits["max_keyword_characters"] = keyword_limit
    result = await service.ingest(
        IngestRequest(source=str(path), config={"discovery_selection": limits})
    )
    doc = await graph_store.get_document(result.document.id)
    expected = await StructuredDataAdapter().project(path)
    assert doc.source_content_hash == "sha256:" + hashlib.sha256(raw).hexdigest()
    retained = tmp_vault_dir / "sources" / doc.source_path
    assert retained.read_bytes() == raw
    assert doc.adapter_version == StructuredDataAdapter.VERSION
    chunks = await content.get_all_chunks(doc.id)
    assert len(chunks) > 2
    assert "".join(chunk.content for chunk in chunks) == expected.text
    assert all(token in "".join(c.content for c in chunks) for token in NUMBERS)
    reader = UtilitiesService(graph_store, content, provider, service._config)
    read = await reader.read_projection(doc.id)
    assert read.projection_text == expected.text
    retrieval = RetrievalService(graph_store, content, provider, service._config)
    for term in [
        "Earlycedar",
        "Lateviolet",
        "Innercedar",
        "Deepviolet",
        "Latecypress",
        "Lastorchid",
    ]:
        response = await retrieval.discover(
            DiscoverRequest(mode="keyword", query=term, filters={"document_ids": [doc.id]})
        )
        assert bool(response.results) == (keyword_limit is None)
    # PostgreSQL's simple dictionary treats this authored decimal as one numeric lexeme.
    if keyword_limit is None:
        assert await content.search_bm25(NUMBERS[0])
    assert 0 < len(provider.calls) <= 2
    vectors = await content.read_embedding_vectors(doc.id, list(range(len(chunks))))
    assert (len(vectors) == 0) if fail else (0 < len(vectors) <= 2)
    assert all(c.content.count('"n": 1.2300') == c.content.count('"pad":') for c in chunks)
    if fail:
        assert "embedding_failed" in doc.processing_coverage.passage_embeddings.limitations
    assert doc.processing_coverage.projection.scope == "complete"
    assert doc.processing_coverage.keyword_indexing.scope == (
        "complete" if keyword_limit is None else "limited"
    )
    assert await content.update_document_surface_text(doc.id, doc.title, "Generatedphantom")
    async with pg_pool.connection() as conn:
        assert (
            await (
                await conn.execute(
                    "SELECT orienting FROM document_surface WHERE document_id=%s", (doc.id,)
                )
            ).fetchone()
        ) == ("Generatedphantom",)
    assert not await content.search_bm25("Generatedphantom")
    await service.stop_worker()


async def test_json_extreme_numbers_escaped_keys_and_surrogates_are_utf8_safe(tmp_path):
    source = (
        r'{"\u006eote":"first","note":"second","s":"\ud800",'
        r'"text":"\\ud800","n":1e99999999999999999999,"i":' + "9" * 5000 + "}"
    )
    path = tmp_path / "extreme.json"
    path.write_text(source)
    result = await StructuredDataAdapter().project(path)
    assert lexical(result.text) == lexical(source)
    assert "1e99999999999999999999" in result.text
    assert "9" * 5000 in result.text
    result.text.encode("utf-8")
    facts = list(source_facts(path, None, FactLimits()))
    assert [json.loads(f.rendering) for f in facts[:2]] == ["first", "second"]
    assert facts[0].location != facts[1].location
    assert all(f.rendering.encode("utf-8") for f in facts)
    assert facts[2].rendering != facts[3].rendering
    assert [json.loads(f.rendering) for f in facts[2:4]] == ["\ud800", r"\ud800"]


@pytest.mark.parametrize(
    "limits,source",
    [
        ({"max_depth": 2}, "[[[1]]]"),
        ({"max_parse_nodes": 3}, '{"x":1,"x":2}'),
        ({"max_source_bytes": 3}, '{"x":1}'),
    ],
)
async def test_json_fidelity_obeys_existing_work_admission(tmp_path, limits, source):
    path = tmp_path / "bounded.json"
    path.write_text(source)
    with pytest.raises(SourceReadError, match="safe projection work"):
        await StructuredDataAdapter().project(path, {"discovery_facts": limits})


async def test_json_escaped_control_key_survives_storage(
    pg_pool, ingestion_service_no_abstraction, tmp_vault_dir
):
    from sage.adapters.content_store_postgres import PostgresContentStore
    from sage.models.schemas import IngestRequest, SourceType
    from sage.services.utilities import UtilitiesService

    service = ingestion_service_no_abstraction
    service._adapters[SourceType.STRUCTURED_DATA] = StructuredDataAdapter()
    source = (
        r'{"title":"Control\u0000Title","\u0000":"Controlcedar\u0000 value",'
        r'"\\u0000":"Literalcedar\\u0000 value"}'
    )
    path = tmp_vault_dir / "controls.json"
    path.write_text(source)
    result = await service.ingest(IngestRequest(source=str(path)))
    content = PostgresContentStore(pg_pool)
    assert result.document.title == r"Control\u0000Title"
    package = await content.read_abstraction_evidence(result.document.id)
    contexts = {item.rendering: item.context for item in package.selected}
    assert contexts.get(r'"Controlcedar\u0000 value"') == r'"\u0000"'
    assert contexts.get(r'"Literalcedar\\u0000 value"') == r'"\\u0000"'
    assert await content.search_bm25("Controlcedar")
    assert await content.search_bm25("Literalcedar")
    reader = UtilitiesService(service._store, content, service._embedding, service._config)
    assert lexical((await reader.read_projection(result.document.id)).projection_text) == lexical(
        source
    )
    await service.stop_worker()


@pytest.mark.parametrize(
    "source,title",
    [
        ('{"title":"first","title":"","name":"fallback"}', "fallback"),
        ('{"title":"first","title":4,"name":"fallback"}', "fallback"),
        ('{"name":"first","name":"last"}', "last"),
        ('{"info":{"title":"first"},"info":{"title":"last"}}', "last"),
    ],
)
async def test_json_title_metadata_keeps_last_occurrence_precedence(tmp_path, source, title):
    path = tmp_path / "title.json"
    path.write_text(source)
    result = await StructuredDataAdapter().project(path)
    assert result.title == title
    assert lexical(result.text) == lexical(source)
