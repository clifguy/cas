"""Discovery inspection publication uses real disposable Postgres artifacts."""

import json
from dataclasses import asdict, replace

import pytest

from sage.adapters.content_store_postgres import PostgresContentStore
from sage.services.discovery_inspection import (
    InspectionAttempt as CapturedAttempt,
)
from sage.services.discovery_inspection import (
    capture_inspection_attempt,
    publish_inspection,
)
from sage.services.discovery_selection import (
    EvidencePackage,
    SelectionConfig,
    select_discovery,
    selection_identity,
)
from sage.source_adapters.structure import SourceFact
from tests.sage.test_postgres_graph_store import _doc
from tests.sage.test_processing_coverage import _chunks, _evidence


def Identity():
    return selection_identity(
        source_hash=_doc(1).source_content_hash,
        adapter_version="markdown:1",
        tokenizer_identity="test-count:1",
        config=SelectionConfig(),
    )


def Package(identity, **changes):
    return replace(
        EvidencePackage(
            identity=identity,
            observed_values=4,
            observed_characters=23,
            selected=(),
            observations=(),
            limitations=(),
            inspection_complete=True,
            interrupted=False,
            associations_complete=True,
            selected_tokens=0,
            boundary=None,
        ),
        **changes,
    )


async def test_inspection_publication_preserves_coherent_generated_artifacts(
    pg_pool, postgres_graph_store
):
    await postgres_graph_store.insert_document(_doc(1))
    store = PostgresContentStore(pg_pool)
    generation = await store.begin_processing_attempt(_doc(1).id)
    assert await store.publish_processing(
        _doc(1).id,
        generation,
        _evidence(),
        chunks=_chunks("retained"),
        semantic_abstract="retained abstract",
    )
    before = await store.read_processing_artifacts(_doc(1).id)
    attempt = CapturedAttempt(
        _doc(1).id, generation, Identity().processing_identity().model_dump_json()
    )
    assert await publish_inspection(store, attempt, Package(Identity()))
    after = await store.read_processing_artifacts(_doc(1).id)
    assert after[0].inspection.scope == "complete"
    assert after[0].inspection.counts[0].observed == 4
    assert after[0].inspection.counts[0].total == 4
    assert after[0].inspection.identity == Identity().processing_identity()
    assert after[0].projection == before[0].projection
    assert after[0].passage_embeddings == before[0].passage_embeddings
    assert after[1] == before[1]
    assert after[2] == before[2] == "retained abstract"


@pytest.mark.parametrize("interrupted", [False, True])
async def test_partial_inspection_keeps_unknown_remainder(
    pg_pool, postgres_graph_store, interrupted
):
    await postgres_graph_store.insert_document(_doc(1))
    store = PostgresContentStore(pg_pool)
    generation = await store.begin_processing_attempt(_doc(1).id)
    attempt = CapturedAttempt(
        _doc(1).id, generation, Identity().processing_identity().model_dump_json()
    )
    package = Package(
        Identity(),
        inspection_complete=False,
        interrupted=interrupted,
        limitations=("inspected_value_limit",),
    )
    assert await publish_inspection(store, attempt, package)
    coverage, _, _ = await store.read_processing_artifacts(_doc(1).id)
    assert coverage.inspection.scope == "limited"
    assert coverage.inspection.unknown_remainder
    assert coverage.inspection.counts[0].observed == 4
    assert coverage.inspection.counts[0].represented == 4
    assert coverage.inspection.counts[0].total is None
    assert ("inspection_interrupted" in coverage.inspection.limitations) == interrupted
    assert coverage.passage_embeddings.scope == "unknown"
    assert coverage.abstraction_evidence.scope == "unknown"


async def test_delayed_inspection_cannot_overwrite_new_attempt(pg_pool, postgres_graph_store):
    await postgres_graph_store.insert_document(_doc(1))
    older, newer = PostgresContentStore(pg_pool), PostgresContentStore(pg_pool)
    generation = await older.begin_processing_attempt(_doc(1).id)
    attempt = CapturedAttempt(
        _doc(1).id, generation, Identity().processing_identity().model_dump_json()
    )
    assert await publish_inspection(older, attempt, Package(Identity()))
    replacement = await newer.begin_processing_attempt(_doc(1).id)
    current = CapturedAttempt(
        _doc(1).id, replacement, Identity().processing_identity().model_dump_json()
    )
    assert await publish_inspection(newer, current, Package(Identity(), observed_values=8))
    assert not await publish_inspection(older, attempt, Package(Identity()))
    coverage, _, _ = await newer.read_processing_artifacts(_doc(1).id)
    assert coverage.inspection.generation == replacement
    assert coverage.inspection.counts[0].observed == 8


async def test_mismatched_identity_is_refused_before_publication(pg_pool, postgres_graph_store):
    await postgres_graph_store.insert_document(_doc(1))
    store = PostgresContentStore(pg_pool)
    generation = await store.begin_processing_attempt(_doc(1).id)
    payload = Identity().processing_identity().model_dump()
    payload["configuration"] = {"selection_budget": 2}
    attempt = CapturedAttempt(_doc(1).id, generation, json.dumps(payload))
    with pytest.raises(ValueError, match="captured attempt identity"):
        await publish_inspection(store, attempt, Package(Identity()))
    coverage, _, _ = await store.read_processing_artifacts(_doc(1).id)
    assert coverage.inspection.scope == "unknown"


async def test_metadata_write_during_inspection_preserves_attempt(pg_pool, postgres_graph_store):
    await postgres_graph_store.insert_document(_doc(1))
    store = PostgresContentStore(pg_pool)
    generation = await store.begin_processing_attempt(_doc(1).id)
    attempt = CapturedAttempt(
        _doc(1).id, generation, Identity().processing_identity().model_dump_json()
    )
    await postgres_graph_store.update_document(_doc(1).id, {"title": "Metadata revised"})
    assert await publish_inspection(store, attempt, Package(Identity()))
    document = await postgres_graph_store.get_document(_doc(1).id)
    assert document.title == "Metadata revised"
    assert document.processing_coverage.inspection.generation == generation


async def test_bounded_summary_does_not_fabricate_full_inventory(pg_pool, postgres_graph_store):
    await postgres_graph_store.insert_document(_doc(1))
    store = PostgresContentStore(pg_pool)
    generation = await store.begin_processing_attempt(_doc(1).id)
    attempt = CapturedAttempt(
        _doc(1).id, generation, Identity().processing_identity().model_dump_json()
    )
    package = Package(Identity(), limitations=tuple(f"limit_{i}" for i in range(40)))
    assert await publish_inspection(store, attempt, package)
    coverage, _, _ = await store.read_processing_artifacts(_doc(1).id)
    assert len(coverage.inspection.limitations) == 16
    assert coverage.inspection.limitations[-1] == "additional_inspection_limitations_omitted"
    assert coverage.inspection.manifest is None
    assert coverage.inspection.ranges == []


async def test_real_selection_roundtrip_uses_frozen_effective_identity(
    pg_pool, postgres_graph_store
):
    await postgres_graph_store.insert_document(_doc(1))
    store = PostgresContentStore(pg_pool)
    generation = await store.begin_processing_attempt(_doc(1).id)
    config = SelectionConfig(max_excerpts=1)
    defaults = asdict(config)
    attempt = capture_inspection_attempt(
        _doc(1).id,
        generation,
        source_hash=_doc(1).source_content_hash,
        adapter_version="markdown:1",
        tokenizer_identity="words:1",
        config=config,
    )
    # Later mutable vault defaults cannot reconstruct or alter the captured identity.
    defaults["max_excerpts"] = 2
    facts = [
        SourceFact("body", "/text", f"/body/{i}", i, str(i), value, value)
        for i, value in enumerate(("first authored text", "second authored text"))
    ]
    package = select_discovery(
        facts,
        source_hash=_doc(1).source_content_hash,
        adapter_version="markdown:1",
        tokenizer=lambda text: len(text.split()),
        tokenizer_identity="words:1",
        config=config,
    )
    assert len(package.selected) == 1
    assert package.inspection_complete
    assert json.loads(package.identity.configuration_json)["selection"]["max_excerpts"] == 1
    assert json.loads(attempt.identity_json)["configuration"] == json.loads(
        package.identity.configuration_json
    )
    assert await publish_inspection(store, attempt, package)
    coverage, _, _ = await store.read_processing_artifacts(_doc(1).id)
    assert coverage.inspection.scope == "complete"
    assert coverage.inspection.counts[0].represented == 2
    assert coverage.inspection.identity.configuration["selection"]["max_excerpts"] == 1
    assert coverage.passage_embeddings.scope == "unknown"
    for field, value in (("tokenizer_identity", "words:2"), ("adapter_version", "markdown:2")):
        mismatch = replace(package, identity=replace(package.identity, **{field: value}))
        with pytest.raises(ValueError, match="captured attempt identity"):
            await publish_inspection(store, attempt, mismatch)


async def test_real_ingest_publishes_settled_inspection_before_inference_failure(
    ingestion_service, tmp_vault_dir, monkeypatch
):
    from sage.models.schemas import IngestRequest

    source = tmp_vault_dir / "settled-inspection.md"
    source.write_text("# Authored heading\n\nThis authored body must be inspected.")

    async def refuse(texts):
        raise RuntimeError("controlled embedding failure after inspection")

    monkeypatch.setattr(ingestion_service._embedding, "embed", refuse)
    result = await ingestion_service.ingest(IngestRequest(source=str(source)))
    read = await ingestion_service._store.get_document(result.document.id)
    assert read.processing_coverage.inspection.scope == "complete"
    assert read.processing_coverage.inspection.counts[0].observed > 0
    assert read.processing_coverage.inspection.identity.source_hash == read.source_content_hash
    assert read.processing_coverage.inspection.identity.configuration["adapter"] == {
        "dialect": None
    }
    assert read.pipeline_status.value == "failed"
    assert read.processing_coverage.passage_embeddings.scope == "unknown"


async def test_ingest_inspection_captures_effective_adapter_configuration_before_projection(
    ingestion_service, tmp_vault_dir, monkeypatch
):
    from copy import deepcopy

    import sage.services.ingestion as ingestion
    from sage.models.schemas import IngestRequest

    ingestion_service._config.adapter_defaults = {
        "markdown": {"dialect": "pandoc", "nested": {"base": 1, "choice": "vault"}}
    }
    overrides = {"nested": {"choice": "request", "extra": 2}}
    expected = {"dialect": "pandoc", "nested": {"base": 1, "choice": "request", "extra": 2}}
    project = ingestion._run_projection
    captured = []

    async def mutate_defaults(adapter, path, config, **kwargs):
        captured.append(deepcopy(config))
        ingestion_service._config.adapter_defaults["markdown"]["nested"]["base"] = 99
        return await project(adapter, path, config, **kwargs)

    monkeypatch.setattr(ingestion, "_run_projection", mutate_defaults)
    source = tmp_vault_dir / "inspection-defaults.md"
    source.write_text("# Authored\n\nThe frozen settings describe this projection.")
    result = await ingestion_service.ingest(IngestRequest(source=str(source), config=overrides))
    dimension = result.document.processing_coverage.inspection
    assert dimension.scope == "complete"
    assert dimension.identity.configuration["adapter"] == captured[0] == expected
    assert dimension.identity.tokenization == "utf8-byte-tokens:1"
    assert dimension.identity.configuration["facts"]["max_rows"] == 100_000
    assert dimension.identity.configuration["selection"]["max_values"] == 100_000


async def test_real_recompute_publishes_new_inspection_generation(ingestion_service, tmp_vault_dir):
    from sage.models.schemas import IngestRequest
    from tests.helpers.pipeline_wait import drain_abstraction_queue

    source = tmp_vault_dir / "inspection-recompute.md"
    source.write_text("# Authored\n\nA retained source is inspected on each recomputation.")
    result = await ingestion_service.ingest(IngestRequest(source=str(source)))
    old = result.document.processing_coverage.inspection
    assert old.scope == "complete"
    await ingestion_service.recompute_pipeline(result.document.id)
    await drain_abstraction_queue(ingestion_service)
    updated = await ingestion_service._store.get_document(result.document.id)
    new = updated.processing_coverage.inspection
    assert new.scope == "complete"
    assert new.generation > old.generation
    assert new.identity == old.identity
    assert new.counts == old.counts


async def test_xlsx_inspection_freezes_adapter_internal_defaults_before_projection(
    ingestion_service, tmp_vault_dir, monkeypatch
):
    from openpyxl import Workbook

    import sage.services.ingestion as ingestion
    import sage.source_adapters.xlsx_adapter as xlsx
    from sage.models.enums import SourceType
    from sage.models.schemas import IngestRequest

    ingestion_service._adapters[SourceType.XLSX] = xlsx.XlsxAdapter()
    source = tmp_vault_dir / "default-preview.xlsx"
    book = Workbook()
    book.active.append(["Authored heading"])
    book.active.append(["Authored cell"])
    book.save(source)
    project = ingestion._run_projection
    captured = []

    async def mutate_internal_default(adapter, path, config, **kwargs):
        captured.append(dict(config or {}))
        result = await project(adapter, path, config, **kwargs)
        monkeypatch.setattr(xlsx, "_DEFAULT_PREVIEW_ROWS", 99)
        return result

    monkeypatch.setattr(ingestion, "_run_projection", mutate_internal_default)
    result = await ingestion_service.ingest(
        IngestRequest(source=str(source), source_type=SourceType.XLSX)
    )
    actual = result.document.processing_coverage.inspection.identity.configuration["adapter"]
    assert actual == captured[0] == {"preview_rows": 5, "max_sheets": None}
    assert xlsx._DEFAULT_PREVIEW_ROWS == 99


@pytest.mark.parametrize(
    "override,expected_count,expected_scope", [(None, 1, "limited"), (3, 3, "limited")]
)
async def test_live_discovery_selection_configuration_precedence(
    ingestion_service, tmp_vault_dir, override, expected_count, expected_scope
):
    from sage.models.schemas import IngestRequest

    ingestion_service._config.adapter_defaults = {
        "markdown": {"discovery_selection": {"max_values": 1, "max_excerpts": 1}}
    }
    source = tmp_vault_dir / "configured-selection.md"
    source.write_text(
        "First authored paragraph.\n\nSecond authored paragraph.\n\nThird authored paragraph."
    )
    config = {"discovery_selection": {"max_values": override}} if override else None
    result = await ingestion_service.ingest(IngestRequest(source=str(source), config=config))
    dimension = result.document.processing_coverage.inspection
    assert dimension.scope == expected_scope
    assert dimension.counts[0].observed == expected_count
    assert dimension.counts[0].total is None
    assert dimension.identity.configuration["selection"]["max_values"] == expected_count
    assert dimension.identity.configuration["selection"]["max_excerpts"] == 1


@pytest.mark.parametrize(
    "key,value",
    [
        ("discovery_selection", {"max_values": 0}),
        ("discovery_selection", {"invented_limit": 1}),
        ("discovery_selection", []),
        ("discovery_facts", {"max_depth": 129}),
        ("discovery_facts", {"invented_limit": 1}),
    ],
)
async def test_invalid_discovery_configuration_refused_before_materialization(
    ingestion_service, tmp_vault_dir, monkeypatch, key, value
):
    from sage.api.errors import AdapterConfigInvalidError
    from sage.models.schemas import IngestRequest

    source = tmp_vault_dir / "invalid-inspection.md"
    source.write_text("A source whose materializer must not run.")

    def refuse_materialization(*args, **kwargs):
        pytest.fail("invalid discovery configuration reached source materialization")

    monkeypatch.setattr(ingestion_service, "_project_source", refuse_materialization)
    with pytest.raises(AdapterConfigInvalidError, match=key):
        await ingestion_service.ingest(IngestRequest(source=str(source), config={key: value}))


async def test_configured_fact_admission_refuses_before_adapter_projection(tmp_path, monkeypatch):
    from sage.services.ingestion import _run_inspected_projection
    from sage.source_adapters.base import SourceReadError
    from sage.source_adapters.structured_data_adapter import StructuredDataAdapter

    source = tmp_path / "oversized.json"
    source.write_text('{"authored": "Source exceeds the explicitly configured admission limit."}')
    adapter = StructuredDataAdapter()

    async def forbidden_projection(*args, **kwargs):
        pytest.fail("source admission refusal must precede projection materialization")

    monkeypatch.setattr(adapter, "project", forbidden_projection)
    with pytest.raises(SourceReadError, match="byte"):
        await _run_inspected_projection(
            adapter, source, {"discovery_facts": {"max_source_bytes": 4}}
        )


@pytest.mark.parametrize(
    "source_type", ["markdown", "docx", "pdf", "pptx", "xlsx", "structured_data"]
)
async def test_vault_configuration_rejects_invalid_discovery_reserved_settings(
    minimal_config, source_type
):
    from pydantic import ValidationError

    from sage.config import VaultConfig

    payload = minimal_config.model_dump(mode="json")
    payload["adapter_defaults"] = {source_type: {"discovery_selection": {"max_values": 0}}}
    with pytest.raises(ValidationError, match="discovery_selection"):
        VaultConfig.model_validate(payload)


async def test_maintenance_reprojection_uses_admission_guard_and_preserves_artifacts(
    ingestion_service, tmp_vault_dir, monkeypatch
):
    from sage.mcp_init import get_stack_config, resolve_stack_vault_source_store
    from sage.models.enums import SourceType
    from sage.models.schemas import IngestRequest
    from sage.source_adapters.structured_data_adapter import StructuredDataAdapter

    adapter = StructuredDataAdapter()
    ingestion_service._adapters[SourceType.STRUCTURED_DATA] = adapter
    source = tmp_vault_dir / "maintenance-admission.json"
    source.write_text('{"authored": "Retained source with a served representation."}')
    result = await ingestion_service.ingest(
        IngestRequest(source=str(source), source_type=SourceType.STRUCTURED_DATA)
    )
    before = await ingestion_service._content_store.read_processing_artifacts(result.document.id)
    ingestion_service._config.adapter_defaults = {
        "structured_data": {"discovery_facts": {"max_source_bytes": 4}}
    }

    async def forbidden_projection(*args, **kwargs):
        pytest.fail("maintenance reprojection bypassed original-materializer admission guard")

    monkeypatch.setattr(adapter, "project", forbidden_projection)
    store = resolve_stack_vault_source_store(get_stack_config())
    outcome = await ingestion_service._bring_passages_current(result.document, store)
    assert "source not projected" in outcome
    assert "SourceReadError" in outcome
    after = await ingestion_service._content_store.read_processing_artifacts(result.document.id)
    assert after == before


async def test_unrepresentable_effective_identity_refused_before_materializer(
    tmp_path, monkeypatch
):
    from sage.services.ingestion import _run_inspected_projection
    from sage.source_adapters.base import AdapterConfigError
    from sage.source_adapters.markdown_adapter import MarkdownAdapter

    source = tmp_path / "bounded-configuration.md"
    source.write_text("Authored source.")
    adapter = MarkdownAdapter()

    async def forbidden_projection(*args, **kwargs):
        pytest.fail("unrepresentable configuration reached source materialization")

    monkeypatch.setattr(adapter, "project", forbidden_projection)
    monkeypatch.setattr(adapter, "effective_config", lambda config: {"opaque_option": "x" * 70_000})
    with pytest.raises(AdapterConfigError, match="effective_configuration"):
        await _run_inspected_projection(adapter, source, {})


@pytest.mark.parametrize("entrypoint", ["_run_projection", "_run_inspected_projection"])
async def test_raw_configuration_bounds_precede_deepcopy(tmp_path, entrypoint):
    import sage.services.ingestion as ingestion
    from sage.source_adapters.base import AdapterConfigError
    from sage.source_adapters.markdown_adapter import MarkdownAdapter

    class RefuseCopy(dict):
        def __deepcopy__(self, memo):
            pytest.fail("raw configuration copied before bounded validation")

    source = tmp_path / "raw-configuration.md"
    source.write_text("Authored source.")
    raw = RefuseCopy(opaque_option="x" * 70_000)
    with pytest.raises(AdapterConfigError, match="discovery_configuration") as refused:
        await getattr(ingestion, entrypoint)(MarkdownAdapter(), source, raw)
    assert len(str(refused.value)) < 512
    assert refused.value.value == "<configuration omitted: exceeds representation bounds>"


async def test_raw_configuration_bounds_precede_recursive_merge(ingestion_service, monkeypatch):
    import sage.services.ingestion as ingestion
    from sage.models.enums import SourceType
    from sage.source_adapters.base import AdapterConfigError

    ingestion_service._config.adapter_defaults = {"markdown": {"opaque_option": "small"}}

    def forbidden_merge(*args, **kwargs):
        pytest.fail("raw configuration entered recursive merge before bounded validation")

    monkeypatch.setattr(ingestion, "_deep_merge_dicts", forbidden_merge)
    with pytest.raises(AdapterConfigError, match="discovery_configuration"):
        ingestion_service._merge_adapter_config(
            SourceType.MARKDOWN, {"opaque_option": "x" * 70_000}
        )


async def test_explicit_markdown_source_type_overrides_structured_suffix(
    ingestion_service, tmp_vault_dir
):
    from sage.models.enums import SourceType
    from sage.models.schemas import IngestRequest

    source = tmp_vault_dir / "narrative.json"
    authored = "This is authored narrative, rather than valid JSON."
    source.write_text(authored)
    result = await ingestion_service.ingest(
        IngestRequest(source=str(source), source_type=SourceType.MARKDOWN)
    )
    assert result.document.processing_coverage.inspection.scope == "complete"
    chunks = await ingestion_service._content_store.get_all_chunks(result.document.id)
    assert any(authored in chunk.content for chunk in chunks)


async def test_explicit_structured_source_type_keeps_native_refusal(
    ingestion_service, tmp_vault_dir
):
    from sage.api.errors import SourceUnreadableError
    from sage.models.enums import SourceType
    from sage.models.schemas import IngestRequest
    from sage.source_adapters.structured_data_adapter import StructuredDataAdapter

    ingestion_service._adapters[SourceType.STRUCTURED_DATA] = StructuredDataAdapter()
    source = tmp_vault_dir / "native.json"
    source.write_text("Authored narrative is not valid JSON.")
    with pytest.raises(SourceUnreadableError):
        await ingestion_service.ingest(
            IngestRequest(source=str(source), source_type=SourceType.STRUCTURED_DATA)
        )
