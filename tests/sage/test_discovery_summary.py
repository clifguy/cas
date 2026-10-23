"""Bounded selection summaries survive attempt-fenced durable/public reads."""

import json
from dataclasses import replace
from pathlib import Path

import jsonschema
import pytest
import yaml
from httpx import ASGITransport, AsyncClient

from sage.adapters.content_store_postgres import PostgresContentStore
from sage.adapters.stubs import StubAbstractionProvider, StubEmbeddingProvider
from sage.app import create_app
from sage.mcp_server import _vaults, build_partitioned_server
from sage.models.coverage import CoverageDimension, ProcessingCoverage
from sage.services.discovery_inspection import capture_inspection_attempt, publish_inspection
from sage.services.discovery_selection import SelectionConfig, select_discovery
from sage.source_adapters.structure import SourceFact
from tests.sage.conftest import initialize_services_for_test
from tests.sage.test_postgres_graph_store import _doc


def _package(kind):
    config = SelectionConfig(max_excerpts=1) if kind == "selection_limited" else SelectionConfig()
    values = ("alpha text", "alpha text", "beta text")
    if kind == "association_limited":
        config = SelectionConfig(max_association_runs=1)
        values = ("alpha text", "beta text", "alpha text")
    flags = iter((False, True))
    package = select_discovery(
        [
            SourceFact("body", "text", f"line:{index + 1}", index, str(index), value, value)
            for index, value in enumerate(values)
        ],
        source_hash=_doc(1).source_content_hash,
        adapter_version="markdown:1",
        tokenizer=lambda text: len(text.split()),
        tokenizer_identity="words:1",
        config=config,
        cancelled=(lambda: next(flags)) if kind == "interrupted" else None,
    )
    return package, config


async def _publish(store, kind):
    package, config = _package(kind)
    generation = await store.begin_processing_attempt(_doc(1).id)
    attempt = capture_inspection_attempt(
        _doc(1).id,
        generation,
        source_hash=_doc(1).source_content_hash,
        adapter_version="markdown:1",
        tokenizer_identity="words:1",
        config=config,
    )
    assert await publish_inspection(store, attempt, package)
    return package, attempt


@pytest.fixture
async def summary_services(pg_pool, postgres_graph_store, minimal_config):
    store = PostgresContentStore(pg_pool)
    async with initialize_services_for_test(
        minimal_config,
        graph_store=postgres_graph_store,
        content_store=store,
        embedding_provider=StubEmbeddingProvider(),
        abstraction_provider=StubAbstractionProvider(),
    ) as services:
        _vaults["test_vault"] = services
        try:
            yield services
        finally:
            _vaults.pop("test_vault", None)


@pytest.mark.parametrize(
    ("kind", "scope", "outcome", "inputs", "tokens", "occurrences", "associations"),
    [
        ("complete", "complete", "complete", 2, 4, 3, True),
        ("selection_limited", "complete", "limited", 1, 2, 1, True),
        ("association_limited", "complete", "limited", 2, 4, 1, False),
        ("interrupted", "limited", "interrupted", 0, 0, 0, True),
    ],
)
async def test_selection_summary_real_publish_rest_and_mcp(
    summary_services, kind, scope, outcome, inputs, tokens, occurrences, associations
):
    services = summary_services
    await services.graph_store.insert_document(_doc(1))
    package, attempt = await _publish(services.content_store, kind)
    coverage, _, _ = await services.content_store.read_processing_artifacts(_doc(1).id)
    body = coverage.model_dump(mode="json")
    expected = {
        "outcome": outcome,
        "selected_inputs": inputs,
        "selected_tokens": tokens,
        "established_occurrences": occurrences,
        "associations_complete": associations,
        "boundary": package.boundary,
    }
    assert body["inspection"].get("selection_summary") == expected
    assert body["inspection"]["scope"] == scope
    assert body["inspection"]["generation"] == attempt.generation
    assert body["inspection"]["identity"]["tokenization"] == "words:1"
    assert body["passage_embeddings"]["scope"] == "unknown"
    assert body["abstraction_evidence"]["scope"] == "unknown"
    app = create_app(config=services.config)
    app.state.vault_registry = {"test_vault": services}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(f"/sage_vaults/test_vault/documents/{_doc(1).id}")
        assert response.status_code == 200
        assert response.json()["processing_coverage"]["inspection"]["selection_summary"] == expected
    ordinary = build_partitioned_server("sage")
    result = await ordinary.call_tool(
        "get_document", {"vault_id": "test_vault", "document_id": _doc(1).id}
    )
    assert (
        json.loads(result[0].text)["processing_coverage"]["inspection"]["selection_summary"]
        == expected
    )


async def test_selection_summary_stale_attempt_retains_current_evidence(
    pg_pool, postgres_graph_store
):
    await postgres_graph_store.insert_document(_doc(1))
    store = PostgresContentStore(pg_pool)
    old, attempt = await _publish(store, "complete")
    _, current = await _publish(store, "selection_limited")
    assert not await publish_inspection(store, attempt, old)
    coverage, _, _ = await store.read_processing_artifacts(_doc(1).id)
    assert coverage.inspection.generation == current.generation
    assert coverage.inspection.model_dump().get("selection_summary", {}).get("outcome") == "limited"


def test_selection_summary_legacy_absence():
    assert "selection_summary" in CoverageDimension.model_fields
    legacy = CoverageDimension()
    assert "selection_summary" not in legacy.model_dump()
    assert "selection_summary" not in ProcessingCoverage().model_dump_json()


@pytest.mark.parametrize("scope", ["unknown", "pending", "not_applicable"])
def test_selection_summary_cannot_establish_uninspected_scope(scope):
    package, _ = _package("complete")
    valid = dict(
        scope="limited",
        identity=package.identity.processing_identity(),
        selection_summary={
            "outcome": "complete",
            "selected_inputs": 0,
            "selected_tokens": 0,
            "established_occurrences": 0,
            "associations_complete": True,
        },
    )
    assert (
        CoverageDimension.model_validate(valid).model_dump()["selection_summary"]["outcome"]
        == "complete"
    )
    with pytest.raises(ValueError, match="unestablished"):
        CoverageDimension.model_validate({**valid, "scope": scope})


@pytest.mark.parametrize(
    "field,value",
    [
        ("selected_inputs", -1),
        ("selected_inputs", 4097),
        ("selected_tokens", 32769),
        ("established_occurrences", 400001),
        ("boundary", "x" * 513),
    ],
)
def test_selection_summary_bounds_follow_published_schema(field, value):
    package, _ = _package("complete")
    valid = dict(
        scope="limited",
        identity=package.identity.processing_identity(),
        selection_summary={
            "outcome": "complete",
            "selected_inputs": 1,
            "selected_tokens": 2,
            "established_occurrences": 1,
            "associations_complete": True,
        },
    )
    assert (
        CoverageDimension.model_validate(valid).model_dump()["selection_summary"]["selected_inputs"]
        == 1
    )
    with pytest.raises(ValueError):
        CoverageDimension.model_validate(
            {**valid, "selection_summary": {**valid["selection_summary"], field: value}}
        )


async def test_selection_summary_metadata_forgery_cannot_override_durable_evidence(
    summary_services,
):
    services = summary_services
    await services.graph_store.insert_document(_doc(1))
    await _publish(services.content_store, "selection_limited")
    before = await services.content_store.read_processing_artifacts(_doc(1).id)
    assert "selection_summary" in before[0].inspection.model_dump()
    app = create_app(config=services.config)
    app.state.vault_registry = {"test_vault": services}
    body = {
        "items": [
            {
                "document_id": _doc(1).id,
                "processing_coverage": {
                    "inspection": {"selection_summary": {"outcome": "complete"}}
                },
            }
        ]
    }
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/sage_vaults/test_vault/metadata", json=body)
        assert response.status_code in (400, 422)
        assert "processing_coverage" in response.text
    result = await build_partitioned_server("sage").call_tool(
        "update_metadata", {"vault_id": "test_vault", **body}
    )
    assert "processing_coverage" in result[0].text
    assert await services.content_store.read_processing_artifacts(_doc(1).id) == before


def test_selection_summary_schema_model_and_typescript_contract():
    schema = yaml.safe_load(Path("docs/fs/sage/sage_core_api.openapi.yaml").read_text())[
        "components"
    ]["schemas"]
    assert "SelectionSummary" in schema
    field = CoverageDimension.model_fields.get("selection_summary")
    assert field is not None
    from sage.models.coverage import SelectionSummary

    expected = schema["SelectionSummary"]
    actual = SelectionSummary.model_json_schema()
    for name, prop in expected["properties"].items():
        for key in ("minimum", "maximum", "maxLength", "minLength", "enum"):
            if key in prop:
                assert actual["properties"][name][key] == prop[key]
    sample = {
        "outcome": "limited",
        "selected_inputs": 1,
        "selected_tokens": 2,
        "established_occurrences": 3,
        "associations_complete": False,
    }
    jsonschema.validate(SelectionSummary.model_validate(sample).model_dump(mode="json"), expected)
    types = Path("app/src/api/types.ts").read_text()
    assert "export interface SelectionSummary" in types
    for name in expected["properties"]:
        assert name in types.split("export interface SelectionSummary", 1)[1].split("}", 1)[0]


@pytest.mark.parametrize("dimension", ["passage_embeddings", "abstraction_evidence"])
def test_selection_summary_cannot_assert_inference_achievement(dimension):
    package, _ = _package("complete")
    inspection = CoverageDimension.model_validate(
        {
            "scope": "limited",
            "identity": package.identity.processing_identity(),
            "selection_summary": {
                "outcome": "limited",
                "selected_inputs": 0,
                "selected_tokens": 0,
                "established_occurrences": 0,
                "associations_complete": True,
            },
        }
    )
    assert "selection_summary" in inspection.model_dump()
    with pytest.raises(ValueError, match="inspection"):
        ProcessingCoverage.model_validate({dimension: inspection})


async def test_selection_summary_refuses_interrupted_package_with_selected_inputs(
    pg_pool, postgres_graph_store
):
    await postgres_graph_store.insert_document(_doc(1))
    store = PostgresContentStore(pg_pool)
    package, attempt = await _publish(store, "complete")
    assert (
        "selection_summary"
        in (await store.read_processing_artifacts(_doc(1).id))[0].inspection.model_dump()
    )
    invalid = replace(package, inspection_complete=False, interrupted=True)
    with pytest.raises(ValueError, match="interrupted.*selected"):
        await publish_inspection(store, attempt, invalid)


async def test_selection_summary_keeps_native_unresolved_scope_limited(
    pg_pool, postgres_graph_store, tmp_path
):
    from sage.source_adapters.structure import FactLimits, source_facts

    await postgres_graph_store.insert_document(_doc(1))
    path = tmp_path / "embedded.json"
    path.write_text(json.dumps({"body": "[unresolved source text]"}))
    package = select_discovery(
        source_facts(path, None, FactLimits()),
        source_hash=_doc(1).source_content_hash,
        adapter_version="structured_data:1",
        tokenizer=lambda text: len(text.split()),
        tokenizer_identity="words:1",
    )
    assert not package.inspection_complete
    store = PostgresContentStore(pg_pool)
    generation = await store.begin_processing_attempt(_doc(1).id)
    attempt = capture_inspection_attempt(
        _doc(1).id,
        generation,
        source_hash=_doc(1).source_content_hash,
        adapter_version="structured_data:1",
        tokenizer_identity="words:1",
    )
    assert await publish_inspection(store, attempt, package)
    inspection = (await store.read_processing_artifacts(_doc(1).id))[0].inspection
    assert inspection.scope == "limited"
    assert inspection.model_dump().get("selection_summary", {}).get("outcome") == "limited"


@pytest.mark.parametrize("tool_name", ["get_document", "search"])
def test_published_read_tools_explain_selection_summary(tool_name):
    from tests.helpers.published_tool import published_text

    description = published_text(tool_name)
    for qualifier in (
        "inspection.selection_summary",
        "complete|limited|interrupted",
        "exact selected_inputs/selected_tokens (identity.tokenization)",
        "established_occurrences lower-bound unless associations_complete",
        "boundary=last inspected location; no inference coverage",
    ):
        assert qualifier in description
