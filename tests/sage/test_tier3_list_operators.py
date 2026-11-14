"""List operators, facets and sort for typed (tier3) metadata fields.

A ``tier3_metadata`` filter value is a literal compared for exact equality.
For a field whose ``metadata_schema`` declares ``type: array`` the value may
instead be an operator object -- ``contains``, ``any``, ``all`` or ``none`` --
testing membership, with operands checked against the field's ``items``
schema. ``target="facets"`` aggregates declared tier3 fields named
``tier3:<field>``, and catalog ``sort_by`` accepts ``tier3:<field>`` for a
declared scalar.

The fixture is built so each assertion discriminates:

* ``a`` holds ``[alpha, beta]`` and ``b`` holds ``[alpha]``: equality and
  containment disagree on both, and ``all`` in either order separates them.
* ``c`` is a ``probe`` without the field: ``none`` must keep it, every other
  operator must drop it, and a facet counts it as absent.
* ``d`` is an ``other`` document whose doc_type declares ``systems`` as a
  *string* holding ``"alpha"``: an un-narrowed operator must not reach it,
  while a facet over the un-narrowed slice counts its value, since its
  doc_type declares the field.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from sage.api.errors import (
    InvalidFilterShapeError,
    InvalidFilterValueError,
    InvalidParameterError,
    SAGEError,
)
from sage.config import VaultConfig
from sage.models.enums import RetrievalMode, RetrievalTarget, SourceType
from sage.models.schemas import DiscoverRequest, IngestRequest, RetrievalFilters
from sage.services.ingestion import IngestionService
from sage.services.lifecycle import LifecycleService
from sage.services.retrieval import RetrievalService
from sage.source_adapters.markdown_adapter import MarkdownAdapter

_SYSTEMS = ["alpha", "beta", "gamma"]


def _config_dict(tmp_vault_dir: Path) -> dict:
    return {
        "vault": {
            "id": "test_tier3_list_vault",
            "name": "Test Tier3 List Vault",
            "owner": "testuser",
            "storage_root": str(tmp_vault_dir / "sources"),
            "brain_root": str(tmp_vault_dir / "brain"),
            "visibility": "personal",
        },
        "document_types": {
            "doc_types": [
                {
                    "value": "probe",
                    "label": "Probe",
                    "metadata_schema": {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {
                            "systems": {
                                "type": "array",
                                "items": {"type": "string", "enum": _SYSTEMS},
                            },
                            "probe_id": {"type": "string"},
                            "last_reviewed": {"type": ["string", "null"], "format": "date"},
                            "owner_ref": {
                                "type": "object",
                                "properties": {"name": {"type": "string"}},
                            },
                        },
                    },
                },
                {
                    "value": "other",
                    "label": "Other",
                    "metadata_schema": {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {"systems": {"type": "string"}},
                    },
                },
                {"value": "misc", "label": "Miscellaneous"},
            ]
        },
        "lifecycle": {
            "base_states_required": True,
            "states": [
                {"value": "active", "label": "Active"},
                {"value": "completed", "label": "Completed"},
                {"value": "archived", "label": "Archived", "is_terminal": True},
                {"value": "relocated", "label": "Relocated", "is_terminal": True},
            ],
            "transitions": [
                {"from_state": "(new)", "action": "ingest", "to_state": "active"},
                {
                    "from_state": "active",
                    "action": "supersede",
                    "to_state": "archived",
                    "creates_edge": "supersedes",
                },
                {"from_state": "active", "action": "complete", "to_state": "completed"},
                {"from_state": "active", "action": "archive", "to_state": "archived"},
                {"from_state": "completed", "action": "archive", "to_state": "archived"},
                {"from_state": "archived", "action": "reactivate", "to_state": "active"},
                {"from_state": "active", "action": "relocate", "to_state": "relocated"},
            ],
        },
        "metadata_extraction": {},
        "edge_inference": {},
    }


@pytest.fixture
def list_config(tmp_vault_dir):
    return VaultConfig.model_validate(_config_dict(tmp_vault_dir))


@pytest.fixture
def list_ingestion(
    graph_store,
    lock_manager,
    stub_content_store,
    stub_embedding_provider,
    stub_abstraction_provider,
    list_config,
):
    return IngestionService(
        graph_store=graph_store,
        lock_manager=lock_manager,
        content_store=stub_content_store,
        embedding_provider=stub_embedding_provider,
        abstraction_provider=stub_abstraction_provider,
        config=list_config,
        source_adapters={SourceType.MARKDOWN: MarkdownAdapter()},
        lifecycle_service=LifecycleService(graph_store, lock_manager, list_config),
    )


@pytest.fixture
def list_retrieval(graph_store, stub_content_store, stub_embedding_provider, list_config):
    return RetrievalService(
        graph_store=graph_store,
        content_store=stub_content_store,
        embedding_provider=stub_embedding_provider,
        config=list_config,
    )


async def _seed(
    tmp_vault_dir: Path,
    ingestion: IngestionService,
    extra: dict | None = None,
    base: dict | None = None,
):
    rows = (
        base
        if base is not None
        else {
            "a": (
                "probe",
                {"probe_id": "A", "systems": ["alpha", "beta"], "last_reviewed": "2026-03-01"},
            ),
            "b": ("probe", {"probe_id": "B", "systems": ["alpha"], "last_reviewed": "2026-01-15"}),
            "c": ("probe", {"probe_id": "C"}),
            "d": ("other", {"systems": "alpha"}),
        }
    )
    rows = {**rows, **(extra or {})}
    docs = {}
    for name, (doc_type, tier3) in rows.items():
        path = tmp_vault_dir / "sources" / f"{name}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"# {name}\n\nBody {name}.")
        result = await ingestion.ingest(
            IngestRequest(
                source=f"{name}.md",
                source_type=SourceType.MARKDOWN,
                metadata={"doc_type": doc_type},
                tier3_metadata=tier3,
            )
        )
        docs[name] = result.document
    return docs


async def _ids(retrieval, tier3: dict, doc_type: str | None = "probe") -> set[str]:
    response = await retrieval.discover(
        DiscoverRequest(
            mode=RetrievalMode.CATALOG,
            filters=RetrievalFilters(doc_type=doc_type, tier3_metadata=tier3),
            limit=50,
        )
    )
    return {hit.document.id for hit in response.results}


def _names(docs: dict, ids: set[str]) -> set[str]:
    by_id = {doc.id: name for name, doc in docs.items()}
    return {by_id[i] for i in ids}


# ---------------------------------------------------------------------------
# Operators
# ---------------------------------------------------------------------------


async def test_contains_matches_every_list_holding_the_value(
    tmp_vault_dir, list_ingestion, list_retrieval
):
    """``contains`` is membership, not equality: both lists hold alpha."""
    docs = await _seed(tmp_vault_dir, list_ingestion)
    matched = await _ids(list_retrieval, {"systems": {"contains": "alpha"}})
    assert _names(docs, matched) == {"a", "b"}


async def test_all_is_order_insensitive_containment(tmp_vault_dir, list_ingestion, list_retrieval):
    """Both spellings of the pair select a; neither selects b, which lacks beta."""
    docs = await _seed(tmp_vault_dir, list_ingestion)
    forward = await _ids(list_retrieval, {"systems": {"all": ["alpha", "beta"]}})
    reverse = await _ids(list_retrieval, {"systems": {"all": ["beta", "alpha"]}})
    assert _names(docs, forward) == {"a"}
    assert _names(docs, reverse) == {"a"}


async def test_any_matches_a_list_holding_at_least_one(
    tmp_vault_dir, list_ingestion, list_retrieval
):
    """gamma is held by nobody, so ``any`` reduces to beta: only a.

    Under ``all`` semantics this would be empty, which is what separates the two.
    """
    docs = await _seed(tmp_vault_dir, list_ingestion)
    matched = await _ids(list_retrieval, {"systems": {"any": ["beta", "gamma"]}})
    assert _names(docs, matched) == {"a"}


async def test_none_keeps_lists_without_the_values_and_documents_without_the_field(
    tmp_vault_dir, list_ingestion, list_retrieval
):
    docs = await _seed(tmp_vault_dir, list_ingestion)
    matched = await _ids(list_retrieval, {"systems": {"none": ["beta"]}})
    assert _names(docs, matched) == {"b", "c"}


async def test_operators_compose_and(tmp_vault_dir, list_ingestion, list_retrieval):
    """Two operators on one field must both hold."""
    docs = await _seed(tmp_vault_dir, list_ingestion)
    matched = await _ids(list_retrieval, {"systems": {"contains": "alpha", "none": ["beta"]}})
    assert _names(docs, matched) == {"b"}


async def test_unnarrowed_operator_reaches_only_doc_types_declaring_an_array(
    tmp_vault_dir, list_ingestion, list_retrieval
):
    """With no doc_type, d -- whose doc_type declares ``systems`` as a string
    holding "alpha" -- must not match ``contains`` nor ``none``."""
    docs = await _seed(tmp_vault_dir, list_ingestion)
    contains = await _ids(list_retrieval, {"systems": {"contains": "alpha"}}, doc_type=None)
    none = await _ids(list_retrieval, {"systems": {"none": ["beta"]}}, doc_type=None)
    assert _names(docs, contains) == {"a", "b"}
    assert _names(docs, none) == {"b", "c"}


async def test_bare_list_literal_keeps_exact_equality(
    tmp_vault_dir, list_ingestion, list_retrieval
):
    """The literal form is unchanged: a one-element list matches only b."""
    docs = await _seed(tmp_vault_dir, list_ingestion)
    matched = await _ids(list_retrieval, {"systems": ["alpha"]})
    reversed_pair = await _ids(list_retrieval, {"systems": ["beta", "alpha"]})
    assert _names(docs, matched) == {"b"}
    assert reversed_pair == set()


async def test_object_field_dict_literal_keeps_exact_equality(
    tmp_vault_dir, list_ingestion, list_retrieval
):
    """A dict on an object-typed field is a literal even when its keys look
    like operators, because only a list declaration admits the operator form."""
    docs = await _seed(
        tmp_vault_dir,
        list_ingestion,
        extra={"e": ("probe", {"probe_id": "E", "owner_ref": {"contains": "ops"}})},
    )
    matched = await _ids(list_retrieval, {"owner_ref": {"contains": "ops"}})
    assert _names(docs, matched) == {"e"}


async def test_keyword_listing_and_content_prefilter_agree_with_catalog(
    tmp_vault_dir, list_ingestion, list_retrieval
):
    """The keyword ``*`` listing and the semantic/keyword pre-filter resolve
    the operator to the same documents as catalog mode."""
    docs = await _seed(tmp_vault_dir, list_ingestion)
    tier3 = {"systems": {"all": ["beta", "alpha"]}}
    catalog = await _ids(list_retrieval, tier3)

    listing = await list_retrieval.discover(
        DiscoverRequest(
            mode=RetrievalMode.KEYWORD,
            query="*",
            filters=RetrievalFilters(doc_type="probe", tier3_metadata=tier3),
            limit=50,
        )
    )
    prefilter, _ = await list_retrieval._content_filters(
        DiscoverRequest(
            mode=RetrievalMode.SEMANTIC,
            query="body",
            filters=RetrievalFilters(doc_type="probe", tier3_metadata=tier3),
        )
    )

    assert _names(docs, catalog) == {"a"}
    assert {hit.document.id for hit in listing.results} == catalog
    assert set(prefilter["document_id"]) == catalog


# ---------------------------------------------------------------------------
# Refusals
# ---------------------------------------------------------------------------


async def test_undeclared_enum_operand_is_refused_with_the_vocabulary(
    tmp_vault_dir, list_ingestion, list_retrieval
):
    await _seed(tmp_vault_dir, list_ingestion)
    with pytest.raises(InvalidFilterValueError) as excinfo:
        await _ids(list_retrieval, {"systems": {"contains": "alpha_x"}})
    detail = excinfo.value.detail
    assert detail["field"] == "tier3_metadata.systems"
    assert detail["value"] == "alpha_x"
    assert detail["valid_values"] == sorted(_SYSTEMS)


async def test_operator_on_a_scalar_field_is_refused(tmp_vault_dir, list_ingestion, list_retrieval):
    await _seed(tmp_vault_dir, list_ingestion)
    with pytest.raises(InvalidFilterShapeError) as excinfo:
        await _ids(list_retrieval, {"probe_id": {"contains": "A"}})
    assert excinfo.value.detail["field"] == "tier3_metadata.probe_id"


async def test_operator_on_a_field_no_doc_type_declares_as_array_is_refused_unnarrowed(
    tmp_vault_dir, list_ingestion, list_retrieval
):
    await _seed(tmp_vault_dir, list_ingestion)
    with pytest.raises(InvalidFilterShapeError):
        await _ids(list_retrieval, {"probe_id": {"any": ["A"]}}, doc_type=None)


@pytest.mark.parametrize(
    "operator_value",
    [
        {"contains": ["alpha"]},
        {"any": []},
        {"all": "alpha"},
        {"contains": "alpha", "near": "beta"},
        {"none": [3]},
    ],
    ids=["contains-list", "any-empty", "all-scalar", "unknown-key", "wrong-item-type"],
)
async def test_malformed_operator_is_refused(
    tmp_vault_dir, list_ingestion, list_retrieval, operator_value
):
    await _seed(tmp_vault_dir, list_ingestion)
    with pytest.raises(InvalidFilterShapeError):
        await _ids(list_retrieval, {"systems": operator_value})


# ---------------------------------------------------------------------------
# Facets
# ---------------------------------------------------------------------------


async def _facets(retrieval, fields: list[str], doc_type: str | None = "probe", limit=None):
    response = await retrieval.discover(
        DiscoverRequest(
            mode=RetrievalMode.CATALOG,
            target=RetrievalTarget.FACETS,
            filters=RetrievalFilters(doc_type=doc_type) if doc_type else None,
            facet_fields=fields,
            facet_value_limit=limit,
        )
    )
    return {row.field: row for row in response.results}


async def test_list_facet_counts_each_value_once_per_document(
    tmp_vault_dir, list_ingestion, list_retrieval
):
    """A list field counts like tags; c is absent; d's string counts once.

    f repeats alpha, which must still count once for f. d's doc_type
    declares the field as a string, so its value counts beside the lists.
    m's doc_type does not declare the field, so m is outside the population,
    not absent.
    """
    await _seed(
        tmp_vault_dir,
        list_ingestion,
        extra={
            "f": ("probe", {"probe_id": "F", "systems": ["alpha", "alpha"]}),
            "m": ("misc", None),
        },
    )
    rows = await _facets(list_retrieval, ["tier3:systems"], doc_type=None)
    row = rows["tier3:systems"]
    assert row.values == {"alpha": 4, "beta": 1}
    assert row.total_distinct == 2
    assert row.absent_count == 1


async def test_list_facet_honors_the_value_cap(tmp_vault_dir, list_ingestion, list_retrieval):
    await _seed(tmp_vault_dir, list_ingestion)
    row = (await _facets(list_retrieval, ["tier3:systems"], limit=1))["tier3:systems"]
    assert row.values == {"alpha": 2}
    assert row.total_distinct == 2


async def test_scalar_facet_counts_per_value_and_follows_fixed_fields(
    tmp_vault_dir, list_ingestion, list_retrieval
):
    await _seed(tmp_vault_dir, list_ingestion)
    response = await list_retrieval.discover(
        DiscoverRequest(
            mode=RetrievalMode.CATALOG,
            target=RetrievalTarget.FACETS,
            filters=RetrievalFilters(doc_type="probe"),
            facet_fields=["tier3:probe_id", "doc_type"],
        )
    )
    assert [row.field for row in response.results] == ["doc_type", "tier3:probe_id"]
    fixed, tier3 = response.results
    assert fixed.absent_count is None
    assert tier3.values == {"A": 1, "B": 1, "C": 1}
    assert tier3.absent_count == 0


@pytest.mark.parametrize("field", ["tier3:nonexistent", "tier3:owner_ref"])
async def test_facet_on_undeclared_or_object_field_is_refused(
    tmp_vault_dir, list_ingestion, list_retrieval, field
):
    await _seed(tmp_vault_dir, list_ingestion)
    with pytest.raises(InvalidParameterError):
        await _facets(list_retrieval, [field])


# ---------------------------------------------------------------------------
# Sort
# ---------------------------------------------------------------------------


async def _sorted(retrieval, sort_by: str, doc_type: str | None) -> list[str]:
    response = await retrieval.discover(
        DiscoverRequest(
            mode=RetrievalMode.CATALOG,
            filters=RetrievalFilters(doc_type=doc_type) if doc_type else None,
            sort_by=sort_by,
            limit=50,
        )
    )
    return [hit.document.id for hit in response.results]


async def _sorted_names(retrieval, docs, order: str) -> list[str]:
    response = await retrieval.discover(
        DiscoverRequest(
            mode=RetrievalMode.CATALOG,
            filters=RetrievalFilters(doc_type="probe"),
            sort_by="tier3:last_reviewed",
            sort_order=order,
            limit=50,
        )
    )
    by_id = {doc.id: name for name, doc in docs.items()}
    return [by_id[hit.document.id] for hit in response.results]


async def test_tier3_sort_orders_values_and_puts_absent_last_both_ways(
    tmp_vault_dir, list_ingestion, list_retrieval
):
    docs = await _seed(
        tmp_vault_dir,
        list_ingestion,
        extra={"g": ("probe", {"probe_id": "G", "last_reviewed": None})},
    )
    ascending = await _sorted_names(list_retrieval, docs, "asc")
    descending = await _sorted_names(list_retrieval, docs, "desc")
    assert ascending[:2] == ["b", "a"]
    assert descending[:2] == ["a", "b"]
    assert set(ascending[2:]) == {"c", "g"}
    assert set(descending[2:]) == {"c", "g"}
    # The tiebreak keeps the absent tail in one order regardless of direction.
    assert ascending[2:] == descending[2:]


@pytest.mark.parametrize("field", ["tier3:systems", "tier3:owner_ref", "tier3:nonexistent"])
async def test_tier3_sort_on_non_scalar_or_undeclared_field_is_refused(
    tmp_vault_dir, list_ingestion, list_retrieval, field
):
    await _seed(tmp_vault_dir, list_ingestion)
    with pytest.raises(SAGEError) as excinfo:
        await list_retrieval.discover(
            DiscoverRequest(
                mode=RetrievalMode.CATALOG,
                filters=RetrievalFilters(doc_type="probe"),
                sort_by=field,
            )
        )
    assert excinfo.value.code == "invalid_parameter"


# ---------------------------------------------------------------------------
# Surface parity
# ---------------------------------------------------------------------------


@pytest.fixture
async def both_surfaces(list_config):
    """The list-field vault served by MCP and by the Core API at once."""
    import json

    from httpx import ASGITransport, AsyncClient
    from mcp.types import TextContent

    from sage.adapters.stubs import (
        StubAbstractionProvider,
        StubContentStore,
        StubEmbeddingProvider,
    )
    from sage.app import create_app
    from sage.mcp_server import _vaults as mcp_vaults
    from sage.mcp_server import mcp
    from tests.sage.conftest import initialize_services_for_test

    vault_id = list_config.vault.id
    app = create_app(config=list_config)
    async with initialize_services_for_test(
        list_config,
        content_store=StubContentStore(),
        embedding_provider=StubEmbeddingProvider(),
        abstraction_provider=StubAbstractionProvider(),
    ) as services:
        mcp_vaults[vault_id] = services
        app.state.vault_registry = {vault_id: services}
        try:
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:

                async def call(arguments: dict) -> tuple[dict, dict]:
                    result = await mcp.call_tool("search", {"vault_id": vault_id, **arguments})
                    assert isinstance(result[0], TextContent)
                    resp = await client.post(f"/sage_vaults/{vault_id}/discover", json=arguments)
                    return json.loads(result[0].text), resp.json()

                yield call
        finally:
            mcp_vaults.pop(vault_id, None)


@pytest.mark.parametrize(
    ("code", "tier3"),
    [
        ("invalid_filter_value", {"systems": {"contains": "alpha_x"}}),
        ("invalid_filter_shape", {"probe_id": {"contains": "A"}}),
    ],
)
async def test_operator_refusals_match_on_both_surfaces(both_surfaces, code, tier3):
    mcp_envelope, http_body = await both_surfaces(
        {"mode": "catalog", "filters": {"doc_type": "probe", "tier3_metadata": tier3}}
    )
    assert mcp_envelope["error"] == http_body["code"] == code
    assert mcp_envelope["message"] == http_body["message"]
    assert mcp_envelope["detail"] == http_body["detail"]


async def test_tier3_facet_and_sort_are_accepted_on_both_surfaces(both_surfaces):
    """The published input schemas admit a tier3:<field> facet and sort key."""
    facets_mcp, facets_http = await both_surfaces(
        {"target": "facets", "facet_fields": ["tier3:systems"]}
    )
    sort_mcp, sort_http = await both_surfaces({"mode": "catalog", "sort_by": "tier3:last_reviewed"})
    for body in (facets_mcp, facets_http):
        assert [row["field"] for row in body["results"]] == ["tier3:systems"], body
        assert body["results"][0]["absent_count"] == 0
    for body in (sort_mcp, sort_http):
        assert body["mode"] == "catalog", body


# ---------------------------------------------------------------------------
# Declaration idioms and stored shapes
# ---------------------------------------------------------------------------
#
# ``refd`` declares its fields through the idioms a vault may use: items by
# ``$ref``, a list made nullable by ``anyOf``, a list of objects, number and
# pattern-constrained items, a nullable scalar and an object. ``dual``
# declares three of the same names differently -- ``systems`` with another
# enum, ``owner_ref`` as a list, ``rank`` as a list -- so un-narrowed terms
# meet both declarations.

_REFD_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "$defs": {
        "system": {"type": "string", "enum": ["alpha", "beta"]},
        "banned": {"enum": ["root"]},
    },
    "definitions": {"reserved": {"enum": ["admin"]}},
    "properties": {
        "rule": {"type": "object", "oneOf": [{"required": ["contains"]}, {"required": ["any"]}]},
        "pair_or_text": {"anyOf": [{"type": "array", "items": True}, {"type": "string"}]},
        "guarded": {
            "type": "array",
            "items": {"type": "string", "not": {"$ref": "#/definitions/reserved"}},
        },
        "split": {"type": "array", "items": {"anyOf": [{"enum": ["s1"]}, {"enum": ["s2"]}]}},
        "primary": {"type": "string", "enum": ["p", "q"]},
        "nested": {"type": "array", "items": {"allOf": [{"$ref": "#/properties/primary"}]}},
        "handles": {
            "type": "array",
            "items": {"type": "string", "not": {"$ref": "#/$defs/banned"}},
        },
        "fenced": {
            "type": "array",
            "items": {"type": "string", "not": {"$ref": "#/properties/primary"}},
        },
        "pick": {"oneOf": [{"type": "array", "items": {"type": "string"}}, {"type": "null"}]},
        "wrapped": {"allOf": [{"type": "array", "items": {"type": "string"}}]},
        "either": {"anyOf": [{"type": "string"}, {"type": "array", "items": {"type": "string"}}]},
        "halves": {"type": "array", "items": {"type": "number", "enum": [1.5, 2.5]}},
        "code": {"type": "string"},
        "systems": {"type": "array", "items": {"$ref": "#/$defs/system"}},
        "labels": {"anyOf": [{"type": "array", "items": {"type": "string"}}, {"type": "null"}]},
        "contacts": {
            "type": "array",
            "items": {"type": "object", "properties": {"name": {"type": "string"}}},
        },
        "scores": {"type": "array", "items": {"type": "number"}},
        "mixed": {"type": "array", "items": {"type": ["string", "null"]}},
        "codes": {"type": "array", "items": {"type": "string", "pattern": "^[a-z]+$"}},
        "rank": {"type": ["number", "null"]},
        "owner_ref": {"type": "object"},
    },
}

_DUAL_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "systems": {"type": "array", "items": {"type": "string", "enum": ["delta"]}},
        "owner_ref": {"type": "array", "items": {"type": "string"}},
        "rank": {"type": "array", "items": {"type": "number"}},
        "code": {"type": "integer"},
    },
}


@pytest.fixture
def idiom_config(tmp_vault_dir):
    raw = _config_dict(tmp_vault_dir)
    raw["document_types"]["doc_types"] = [
        {"value": "refd", "label": "Refd", "metadata_schema": _REFD_SCHEMA},
        {"value": "dual", "label": "Dual", "metadata_schema": _DUAL_SCHEMA},
    ]
    return VaultConfig.model_validate(raw)


@pytest.fixture
def idiom_ingestion(
    graph_store,
    lock_manager,
    stub_content_store,
    stub_embedding_provider,
    stub_abstraction_provider,
    idiom_config,
):
    return IngestionService(
        graph_store=graph_store,
        lock_manager=lock_manager,
        content_store=stub_content_store,
        embedding_provider=stub_embedding_provider,
        abstraction_provider=stub_abstraction_provider,
        config=idiom_config,
        source_adapters={SourceType.MARKDOWN: MarkdownAdapter()},
        lifecycle_service=LifecycleService(graph_store, lock_manager, idiom_config),
    )


@pytest.fixture
def idiom_retrieval(graph_store, stub_content_store, stub_embedding_provider, idiom_config):
    return RetrievalService(
        graph_store=graph_store,
        content_store=stub_content_store,
        embedding_provider=stub_embedding_provider,
        config=idiom_config,
    )


async def _seed_idioms(tmp_vault_dir: Path, ingestion: IngestionService) -> dict:
    rows = {
        "r1": (
            "refd",
            {
                "systems": ["alpha"],
                "labels": ["x"],
                "scores": [3.0],
                "mixed": ["x", None],
                "rank": 1,
                "owner_ref": {"name": "ops"},
                "nested": ["p"],
                "handles": ["x"],
                "fenced": ["z"],
                "pick": ["x"],
                "wrapped": ["w"],
                "either": ["x"],
                "halves": [1.5],
                "code": "1",
                "rule": {"contains": "x"},
            },
        ),
        "r2": ("refd", {"systems": ["beta"], "labels": None, "scores": [1], "rank": 1.0}),
        "r3": ("refd", {"scores": [1.0], "rank": None}),
        "r4": ("refd", {}),
        "u1": ("dual", {"systems": ["delta"], "owner_ref": ["ops"], "rank": [2], "code": 1}),
    }
    return await _seed(tmp_vault_dir, ingestion, base=rows)


async def test_ref_declared_items_resolve_for_operators_and_refusals(
    tmp_vault_dir, idiom_ingestion, idiom_retrieval
):
    docs = await _seed_idioms(tmp_vault_dir, idiom_ingestion)
    matched = await _ids(idiom_retrieval, {"systems": {"contains": "alpha"}}, doc_type="refd")
    assert _names(docs, matched) == {"r1"}
    with pytest.raises(InvalidFilterValueError) as excinfo:
        await _ids(idiom_retrieval, {"systems": {"contains": "zeta"}}, doc_type="refd")
    assert excinfo.value.detail["valid_values"] == ["alpha", "beta"]


async def test_nullable_list_is_a_list_for_operators_facets_and_sort(
    tmp_vault_dir, idiom_ingestion, idiom_retrieval
):
    docs = await _seed_idioms(tmp_vault_dir, idiom_ingestion)
    matched = await _ids(idiom_retrieval, {"labels": {"contains": "x"}}, doc_type="refd")
    assert _names(docs, matched) == {"r1"}
    row = (await _facets(idiom_retrieval, ["tier3:labels"], doc_type="refd"))["tier3:labels"]
    assert row.values == {"x": 1}
    assert row.absent_count == 3
    with pytest.raises(InvalidParameterError):
        await _sorted(idiom_retrieval, "tier3:labels", doc_type="refd")


async def test_list_of_objects_is_refused_for_operators_and_facets(
    tmp_vault_dir, idiom_ingestion, idiom_retrieval
):
    await _seed_idioms(tmp_vault_dir, idiom_ingestion)
    with pytest.raises(InvalidFilterShapeError):
        await _ids(idiom_retrieval, {"contacts": {"contains": {"name": "x"}}}, doc_type="refd")
    with pytest.raises(InvalidFilterShapeError):
        await _ids(idiom_retrieval, {"contacts": {"any": ["x"]}}, doc_type="refd")
    with pytest.raises(InvalidParameterError):
        await _facets(idiom_retrieval, ["tier3:contacts"], doc_type="refd")


async def test_integer_operand_matches_number_items(
    tmp_vault_dir, idiom_ingestion, idiom_retrieval
):
    """3 is a valid number and contains 3.0 under JSON numeric equality."""
    docs = await _seed_idioms(tmp_vault_dir, idiom_ingestion)
    matched = await _ids(idiom_retrieval, {"scores": {"contains": 3}}, doc_type="refd")
    assert _names(docs, matched) == {"r1"}


async def test_non_enum_items_constraint_is_refused_as_shape(
    tmp_vault_dir, idiom_ingestion, idiom_retrieval
):
    """A pattern violation has no vocabulary to list, so it is not
    ``invalid_filter_value`` with an empty one."""
    await _seed_idioms(tmp_vault_dir, idiom_ingestion)
    with pytest.raises(InvalidFilterShapeError) as excinfo:
        await _ids(idiom_retrieval, {"codes": {"contains": "ABC"}}, doc_type="refd")
    assert excinfo.value.detail["field"] == "tier3_metadata.codes"


async def test_operand_valid_under_any_declaring_doc_type_is_accepted(
    tmp_vault_dir, idiom_ingestion, idiom_retrieval
):
    """Un-narrowed, delta is valid only under dual's enum and selects u1;
    narrowed to refd, the same operand is refused against refd's enum."""
    docs = await _seed_idioms(tmp_vault_dir, idiom_ingestion)
    matched = await _ids(idiom_retrieval, {"systems": {"contains": "delta"}}, doc_type=None)
    assert _names(docs, matched) == {"u1"}
    with pytest.raises(InvalidFilterValueError):
        await _ids(idiom_retrieval, {"systems": {"contains": "delta"}}, doc_type="refd")


async def test_object_dict_literal_survives_a_list_declaration_elsewhere(
    tmp_vault_dir, idiom_ingestion, idiom_retrieval
):
    """refd declares owner_ref as an object, dual as a list: un-narrowed, a
    non-operator dict is refd's literal, not a refused operator object."""
    docs = await _seed_idioms(tmp_vault_dir, idiom_ingestion)
    matched = await _ids(idiom_retrieval, {"owner_ref": {"name": "ops"}}, doc_type=None)
    assert _names(docs, matched) == {"r1"}


async def test_sort_refused_when_any_in_scope_declaration_is_not_scalar(
    tmp_vault_dir, idiom_ingestion, idiom_retrieval
):
    """rank is a scalar in refd and a list in dual: un-narrowed it is refused,
    narrowed to refd it sorts."""
    await _seed_idioms(tmp_vault_dir, idiom_ingestion)
    with pytest.raises(InvalidParameterError):
        await _sorted(idiom_retrieval, "tier3:rank", doc_type=None)
    assert await _sorted(idiom_retrieval, "tier3:rank", doc_type="refd")


async def test_facet_groups_numbers_by_value_and_skips_null_elements(
    tmp_vault_dir, idiom_ingestion, idiom_retrieval
):
    """1 and 1.0 are one value; a null list element and a JSON-null scalar
    are not values, and the null scalar is absent."""
    await _seed_idioms(tmp_vault_dir, idiom_ingestion)
    rows = await _facets(
        idiom_retrieval, ["tier3:scores", "tier3:mixed", "tier3:rank"], doc_type="refd"
    )
    assert rows["tier3:scores"].values == {"1": 2, "3": 1}
    assert rows["tier3:mixed"].values == {"x": 1}
    assert rows["tier3:rank"].values == {"1": 2}
    assert rows["tier3:rank"].absent_count == 2


async def test_repeated_facet_name_yields_one_row(tmp_vault_dir, idiom_ingestion, idiom_retrieval):
    await _seed_idioms(tmp_vault_dir, idiom_ingestion)
    response = await idiom_retrieval.discover(
        DiscoverRequest(
            mode=RetrievalMode.CATALOG,
            target=RetrievalTarget.FACETS,
            filters=RetrievalFilters(doc_type="refd"),
            facet_fields=["tier3:scores", "tier3:scores"],
        )
    )
    assert [row.field for row in response.results] == ["tier3:scores"]


async def test_references_inside_items_resolve_against_the_doc_type_schema(
    tmp_vault_dir, idiom_ingestion, idiom_retrieval
):
    """``nested`` reaches its enum through ``allOf`` and a ``$ref`` into
    ``properties``; ``handles`` refers to ``$defs`` and ``fenced`` to
    ``properties`` from inside ``not``, which classification leaves alone."""
    docs = await _seed_idioms(tmp_vault_dir, idiom_ingestion)
    matched = await _ids(idiom_retrieval, {"nested": {"contains": "p"}}, doc_type="refd")
    assert _names(docs, matched) == {"r1"}
    with pytest.raises(InvalidFilterValueError) as excinfo:
        await _ids(idiom_retrieval, {"nested": {"contains": "z"}}, doc_type="refd")
    assert excinfo.value.detail["valid_values"] == ["p", "q"]
    matched = await _ids(idiom_retrieval, {"handles": {"contains": "x"}}, doc_type="refd")
    assert _names(docs, matched) == {"r1"}
    with pytest.raises(InvalidFilterShapeError):
        await _ids(idiom_retrieval, {"handles": {"contains": "root"}}, doc_type="refd")
    matched = await _ids(idiom_retrieval, {"fenced": {"contains": "z"}}, doc_type="refd")
    assert _names(docs, matched) == {"r1"}
    with pytest.raises(InvalidFilterShapeError):
        await _ids(idiom_retrieval, {"fenced": {"contains": "p"}}, doc_type="refd")


async def test_unresolvable_items_reference_is_refused_not_raised(tmp_vault_dir):
    """An items reference the doc_type schema cannot resolve refuses every
    operand rather than escaping as an internal error."""
    from sage.services._tier3_query import normalize_tier3_filter

    raw = _config_dict(tmp_vault_dir)
    raw["document_types"]["doc_types"] = [
        {
            "value": "broken",
            "label": "Broken",
            "metadata_schema": {
                "type": "object",
                "properties": {
                    "tags2": {
                        "type": "array",
                        "items": {"type": "string", "not": {"$ref": "#/$defs/missing"}},
                    }
                },
            },
        }
    ]
    config = VaultConfig.model_validate(raw)
    with pytest.raises(InvalidFilterShapeError):
        normalize_tier3_filter(config, {"tags2": {"contains": "x"}}, "broken")


async def test_oneof_nullable_and_allof_wrapped_lists_are_lists(
    tmp_vault_dir, idiom_ingestion, idiom_retrieval
):
    docs = await _seed_idioms(tmp_vault_dir, idiom_ingestion)
    pick = await _ids(idiom_retrieval, {"pick": {"contains": "x"}}, doc_type="refd")
    wrapped = await _ids(idiom_retrieval, {"wrapped": {"contains": "w"}}, doc_type="refd")
    assert _names(docs, pick) == {"r1"}
    assert _names(docs, wrapped) == {"r1"}
    with pytest.raises(InvalidParameterError):
        await _sorted(idiom_retrieval, "tier3:wrapped", doc_type="refd")


async def test_operator_on_a_string_or_list_union_is_refused(
    tmp_vault_dir, idiom_ingestion, idiom_retrieval
):
    await _seed_idioms(tmp_vault_dir, idiom_ingestion)
    with pytest.raises(InvalidFilterShapeError):
        await _ids(idiom_retrieval, {"either": {"contains": "x"}}, doc_type="refd")
    with pytest.raises(InvalidParameterError):
        await _facets(idiom_retrieval, ["tier3:either"], doc_type="refd")


async def test_integer_outside_a_number_enum_is_a_value_refusal(
    tmp_vault_dir, idiom_ingestion, idiom_retrieval
):
    """3 is a number of the right type outside the enum: a vocabulary
    refusal, not a type mismatch."""
    await _seed_idioms(tmp_vault_dir, idiom_ingestion)
    with pytest.raises(InvalidFilterValueError) as excinfo:
        await _ids(idiom_retrieval, {"halves": {"contains": 3}}, doc_type="refd")
    assert excinfo.value.detail["valid_values"] == ["1.5", "2.5"]


async def test_facet_keys_merge_values_with_the_same_text(
    tmp_vault_dir, idiom_ingestion, idiom_retrieval
):
    """refd stores the string "1", dual the number 1: one key, two documents,
    and total_distinct agrees with the keys returned."""
    await _seed_idioms(tmp_vault_dir, idiom_ingestion)
    row = (await _facets(idiom_retrieval, ["tier3:code"], doc_type=None))["tier3:code"]
    assert row.values == {"1": 2}
    assert row.total_distinct == 1


async def test_facet_population_excludes_doc_types_declaring_an_object(
    tmp_vault_dir, idiom_ingestion, idiom_retrieval
):
    """owner_ref is an object in refd and a list in dual: un-narrowed, only
    dual's documents are counted, and refd's are neither values nor absent."""
    await _seed_idioms(tmp_vault_dir, idiom_ingestion)
    row = (await _facets(idiom_retrieval, ["tier3:owner_ref"], doc_type=None))["tier3:owner_ref"]
    assert row.values == {"ops": 1}
    assert row.absent_count == 0


# Classification is one rule applied to a property, its items and every
# composition branch: the declared ``type`` decides first, composition only
# where no type is declared. Each row varies one idiom.

_KIND_CASES = [
    ("plain list", {"type": "array", "items": {"type": "string"}}, {}, "array"),
    (
        "object beside a oneOf of required keys",
        {"type": "object", "oneOf": [{"required": ["a"]}, {"required": ["b"]}]},
        {},
        "object",
    ),
    (
        "list beside an anyOf of size constraints",
        {"type": "array", "items": {"type": "string"}, "anyOf": [{"minItems": 1}, {"maxItems": 0}]},
        {},
        "array",
    ),
    (
        "union branch with a boolean items",
        {"anyOf": [{"type": "array", "items": True}, {"type": "string"}]},
        {},
        "composite",
    ),
    (
        "list whose items are a oneOf of objects",
        {
            "type": "array",
            "items": {"oneOf": [{"type": "object", "required": ["n"]}, {"type": "object"}]},
        },
        {},
        "composite",
    ),
    (
        "list reached through an anchor",
        {"$ref": "#anch"},
        {"$defs": {"l": {"$anchor": "anch", "type": "array", "items": {"type": "string"}}}},
        "array",
    ),
    (
        "scalar type beside a branch naming another type",
        {"type": "string", "anyOf": [{"minLength": 1}, {"type": "object"}]},
        {},
        "scalar",
    ),
    ("list declared beside another type", {"type": ["array", "string"]}, {}, "composite"),
    (
        "nullable list by type",
        {"type": ["array", "null"], "items": {"type": "string"}},
        {},
        "array",
    ),
    (
        "union with an object branch",
        {"anyOf": [{"type": "object"}, {"type": "string"}]},
        {},
        "object",
    ),
    (
        "oneOf of a scalar and a list",
        {"oneOf": [{"type": "string"}, {"type": "array", "items": {"type": "string"}}]},
        {},
        "composite",
    ),
    (
        "keywords beside a followed reference win",
        {"$ref": "#/$defs/s", "type": "array", "items": {"type": "string"}},
        {"$defs": {"s": {"type": "string"}}},
        "array",
    ),
    (
        "keywords beside an allOf are kept",
        {"type": "array", "items": {"type": "string"}, "allOf": [{"minItems": 1}]},
        {},
        "array",
    ),
    (
        "pointer through an array index",
        {"$ref": "#/$defs/u/anyOf/0"},
        {"$defs": {"u": {"anyOf": [{"type": "array", "items": {"type": "string"}}, {}]}}},
        "array",
    ),
    (
        "percent-encoded pointer",
        {"$ref": "#/$defs/my%20list"},
        {"$defs": {"my list": {"type": "array", "items": {"type": "string"}}}},
        "array",
    ),
    (
        "escaped pointer segment",
        {"$ref": "#/$defs/a~1b"},
        {"$defs": {"a/b": {"type": "array", "items": {"type": "string"}}}},
        "array",
    ),
    (
        "reference to a bundled $id",
        {"$ref": "urn:example:list"},
        {"$defs": {"l": {"$id": "urn:example:list", "type": "array", "items": {"type": "string"}}}},
        "array",
    ),
    (
        "anchor inside a branch list",
        {"$ref": "#lst"},
        {
            "$defs": {
                "u": {
                    "anyOf": [
                        {"$anchor": "lst", "type": "array", "items": {"type": "string"}},
                        {"type": "string"},
                    ]
                }
            }
        },
        "array",
    ),
    (
        "anyOf beside a nullable oneOf",
        {
            "anyOf": [{"minItems": 1}, {"maxItems": 0}],
            "oneOf": [{"type": "array", "items": {"type": "string"}}, {"type": "null"}],
        },
        {},
        "array",
    ),
    (
        "union of two references",
        {"anyOf": [{"$ref": "#/$defs/s"}, {"$ref": "#/$defs/l"}]},
        {"$defs": {"s": {"type": "string"}, "l": {"type": "array", "items": {"type": "string"}}}},
        "composite",
    ),
    (
        "outer keywords win over a nullable branch",
        {
            "items": {"type": "string"},
            "anyOf": [{"type": "array", "items": {"type": "object"}}, {"type": "null"}],
        },
        {},
        "array",
    ),
    (
        "null branch spelled as a type list",
        {"anyOf": [{"type": "array", "items": {"type": "string"}}, {"type": ["null"]}]},
        {},
        "array",
    ),
    ("object named in a type list", {"type": ["object", "string"]}, {}, "object"),
    ("boolean false items", {"type": "array", "items": False}, {}, "array"),
    (
        "allOf branch that is a reference",
        {"allOf": [{"$ref": "#/$defs/l"}]},
        {"$defs": {"l": {"type": "array", "items": {"type": "string"}}}},
        "array",
    ),
    (
        "allOf of two references",
        {"allOf": [{"$ref": "#/$defs/t"}, {"$ref": "#/$defs/l"}]},
        {
            "$defs": {
                "t": {"title": "listed"},
                "l": {"type": "array", "items": {"type": "string"}},
            }
        },
        "array",
    ),
    (
        "reference cycle stops",
        {"$ref": "#/$defs/a"},
        {"$defs": {"a": {"$ref": "#/$defs/a"}}},
        "scalar",
    ),
]


@pytest.mark.parametrize(
    ("schema", "extra", "kind"),
    [case[1:] for case in _KIND_CASES],
    ids=[case[0] for case in _KIND_CASES],
)
def test_field_kind_reads_declared_type_before_composition(schema, extra, kind):
    from sage.services._tier3_query import _resolve, field_kind, resolver_for

    root = {"type": "object", "properties": {"f": schema}, **extra}
    assert field_kind(_resolve(schema, resolver_for(root))) == kind


async def test_object_with_a_oneof_keeps_its_literal_filter(
    tmp_vault_dir, idiom_ingestion, idiom_retrieval
):
    """An operator-looking dict on an object declared with a sibling oneOf is
    a literal and matches the document storing it."""
    docs = await _seed_idioms(tmp_vault_dir, idiom_ingestion)
    matched = await _ids(idiom_retrieval, {"rule": {"contains": "x"}}, doc_type="refd")
    assert _names(docs, matched) == {"r1"}


def test_boolean_items_in_a_union_branch_is_refused_not_raised(idiom_config):
    from sage.services._tier3_query import (
        normalize_tier3_filter,
        resolve_tier3_facets,
        validate_tier3_sort,
    )

    with pytest.raises(InvalidFilterShapeError):
        normalize_tier3_filter(idiom_config, {"pair_or_text": {"contains": "x"}}, "refd")
    assert normalize_tier3_filter(idiom_config, {"pair_or_text": {"a": 1}}, "refd")
    with pytest.raises(InvalidParameterError):
        resolve_tier3_facets(idiom_config, ["tier3:pair_or_text"], "refd")
    with pytest.raises(InvalidParameterError):
        validate_tier3_sort(idiom_config, "tier3:pair_or_text", "refd")


def test_definitions_references_inside_items_resolve(idiom_config):
    from sage.services._tier3_query import normalize_tier3_filter

    assert normalize_tier3_filter(idiom_config, {"guarded": {"contains": "x"}}, "refd")
    with pytest.raises(InvalidFilterShapeError):
        normalize_tier3_filter(idiom_config, {"guarded": {"contains": "admin"}}, "refd")


def test_enum_split_across_items_branches_is_a_value_refusal(idiom_config):
    from sage.services._tier3_query import normalize_tier3_filter

    assert normalize_tier3_filter(idiom_config, {"split": {"any": ["s1", "s2"]}}, "refd")
    with pytest.raises(InvalidFilterValueError) as excinfo:
        normalize_tier3_filter(idiom_config, {"split": {"contains": "s3"}}, "refd")
    assert excinfo.value.detail["valid_values"] == ["s1", "s2"]


async def test_unnarrowed_value_refusal_lists_every_declaring_vocabulary(
    tmp_vault_dir, idiom_ingestion, idiom_retrieval
):
    await _seed_idioms(tmp_vault_dir, idiom_ingestion)
    with pytest.raises(InvalidFilterValueError) as excinfo:
        await _ids(idiom_retrieval, {"systems": {"contains": "zeta"}}, doc_type=None)
    assert excinfo.value.detail["valid_values"] == ["alpha", "beta", "delta"]


async def test_narrowed_facet_reads_only_the_named_doc_type_declaration(
    tmp_vault_dir, idiom_ingestion, idiom_retrieval
):
    """owner_ref is a list in dual and an object in refd: narrowed to dual it
    is counted, narrowed to refd it is refused."""
    await _seed_idioms(tmp_vault_dir, idiom_ingestion)
    row = (await _facets(idiom_retrieval, ["tier3:owner_ref"], doc_type="dual"))["tier3:owner_ref"]
    assert row.values == {"ops": 1}
    with pytest.raises(InvalidParameterError):
        await _facets(idiom_retrieval, ["tier3:owner_ref"], doc_type="refd")


async def test_tier3_facet_rows_follow_request_order(
    tmp_vault_dir, idiom_ingestion, idiom_retrieval
):
    await _seed_idioms(tmp_vault_dir, idiom_ingestion)
    requested = ["tier3:scores", "tier3:mixed", "tier3:code"]
    response = await idiom_retrieval.discover(
        DiscoverRequest(
            mode=RetrievalMode.CATALOG,
            target=RetrievalTarget.FACETS,
            filters=RetrievalFilters(doc_type="refd"),
            facet_fields=["doc_type", *requested],
        )
    )
    assert [row.field for row in response.results] == ["doc_type", *requested]


# Operands are judged by the doc_type's own validator against the
# declaration as written, not against the schema flattened for
# classification, and a value refusal lists only values that validator
# admits, and only for a closed enumeration.

_OPERAND_DEFS = {
    "narrow": {"enum": ["a"]},
    "outer": {"$defs": {"inner": {"type": "string"}}, "$ref": "#/$defs/outer/$defs/inner"},
}

_OPERAND_PROPERTIES = {
    "both": {
        "type": "array",
        "items": {"allOf": [{"enum": ["a", "b", "c"]}, {"enum": ["b", "c", "d"]}]},
    },
    "narrowed": {"type": "array", "items": {"$ref": "#/$defs/narrow", "enum": ["a", "b", "c"]}},
    "nested_defs": {"type": "array", "items": {"$ref": "#/$defs/outer"}},
    "labelled": {
        "type": "array",
        "items": {"oneOf": [{"const": "a", "title": "A"}, {"const": "b", "title": "B"}]},
    },
    "mixed_forms": {"type": "array", "items": {"anyOf": [{"enum": ["a"]}, {"const": "b"}]}},
    "deep": {
        "type": "array",
        "items": {"anyOf": [{"anyOf": [{"enum": ["a"]}]}, {"enum": ["b"]}]},
    },
    "number_or_null": {
        "type": "array",
        "items": {"anyOf": [{"type": "number"}, {"enum": [None]}]},
    },
    "open": {
        "type": "array",
        "items": {"anyOf": [{"enum": ["a", "b"]}, {"type": "string", "pattern": "^x"}]},
    },
    "typed_members": {"type": "array", "items": {"enum": [True, 1.5, "s"]}},
    "anything": {"type": "array", "items": True},
    "nullable_enum": {"anyOf": [{"type": "array", "items": {"enum": ["a"]}}, {"type": "null"}]},
    "with_null": {"type": "array", "items": {"enum": ["a", None]}},
    "either_size": {
        "type": "array",
        "anyOf": [{"minItems": 2, "items": {"type": "string"}}, {"items": {"enum": ["a"]}}],
    },
    "rooted": {"type": "array"},
    "unresolved": {"$ref": "#/$defs/missing"},
    "sized": {"type": "array", "minItems": 3, "items": {"type": "string"}},
}


@pytest.fixture
def operand_config(tmp_vault_dir):
    raw = _config_dict(tmp_vault_dir)
    raw["document_types"]["doc_types"] = [
        {
            "value": "ops",
            "label": "Ops",
            "metadata_schema": {
                "type": "object",
                "required": ["sized"],
                "anyOf": [
                    {"properties": {"rooted": {"items": {"enum": ["a"]}}}},
                    {"properties": {"rooted": {"items": {"enum": ["b"]}}}},
                ],
                "$defs": _OPERAND_DEFS,
                "properties": _OPERAND_PROPERTIES,
            },
        }
    ]
    return VaultConfig.model_validate(raw)


def _refusal(config, field, operand):
    from sage.services._tier3_query import normalize_tier3_filter

    with pytest.raises(SAGEError) as excinfo:
        normalize_tier3_filter(config, {field: {"contains": operand}}, "ops")
    return excinfo.value


def _accepts(config, field, operand):
    from sage.services._tier3_query import normalize_tier3_filter

    return bool(normalize_tier3_filter(config, {field: {"contains": operand}}, "ops"))


def test_operand_satisfying_one_allof_branch_only_is_refused(operand_config):
    """a is in the first branch's enum but not the second's: unstorable, so
    refused, and the vocabulary is the intersection the validator admits."""
    assert _accepts(operand_config, "both", "b")
    refusal = _refusal(operand_config, "both", "a")
    assert isinstance(refusal, InvalidFilterValueError)
    assert refusal.detail["valid_values"] == ["b", "c"]


def test_operand_narrowed_by_a_referenced_enum_is_refused(operand_config):
    assert _accepts(operand_config, "narrowed", "a")
    refusal = _refusal(operand_config, "narrowed", "b")
    assert isinstance(refusal, InvalidFilterValueError)
    assert refusal.detail["valid_values"] == ["a"]


def test_reference_target_carrying_its_own_defs_admits_a_stored_value(operand_config):
    assert _accepts(operand_config, "nested_defs", "x")


@pytest.mark.parametrize(
    ("field", "vocabulary"),
    [("labelled", ["a", "b"]), ("mixed_forms", ["a", "b"]), ("deep", ["a", "b"])],
)
def test_enumerations_through_const_and_nested_branches_list_every_value(
    operand_config, field, vocabulary
):
    refusal = _refusal(operand_config, field, "z")
    assert isinstance(refusal, InvalidFilterValueError)
    assert refusal.detail["valid_values"] == vocabulary


@pytest.mark.parametrize("field", ["number_or_null", "open"])
def test_open_items_refuse_as_shape_without_a_vocabulary(operand_config, field):
    """Neither items schema is a closed enumeration: a refused operand has
    no list of valid values to report."""
    refusal = _refusal(operand_config, field, "zz")
    assert isinstance(refusal, InvalidFilterShapeError)


def test_vocabulary_renders_non_string_members_as_json(operand_config):
    refusal = _refusal(operand_config, "typed_members", "z")
    assert isinstance(refusal, InvalidFilterValueError)
    assert refusal.detail["valid_values"] == ["1.5", "s", "true"]


def test_unconstrained_items_admit_any_scalar(operand_config):
    assert _accepts(operand_config, "anything", "x")
    assert _accepts(operand_config, "anything", 3)


def test_list_level_constraints_do_not_judge_one_operand(operand_config):
    """minItems and the document's required fields bear on a whole stored
    document, not on one element."""
    assert _accepts(operand_config, "sized", "x")


def test_nullable_list_refuses_an_operand_its_list_branch_refuses(operand_config):
    """The null branch cannot hold a list, so it does not rescue the operand."""
    refusal = _refusal(operand_config, "nullable_enum", "z")
    assert isinstance(refusal, InvalidFilterValueError)
    assert refusal.detail["valid_values"] == ["a"]


def test_null_is_not_offered_as_a_valid_value(operand_config):
    refusal = _refusal(operand_config, "with_null", "z")
    assert isinstance(refusal, InvalidFilterValueError)
    assert refusal.detail["valid_values"] == ["a"]


def test_union_branch_failing_only_on_list_size_admits_the_operand(operand_config):
    """x fails the enum branch, but the minItems branch holds it in a longer list."""
    assert _accepts(operand_config, "either_size", "x")


def test_root_level_union_judges_the_element(operand_config):
    assert _accepts(operand_config, "rooted", "a")
    assert _accepts(operand_config, "rooted", "b")
    assert isinstance(_refusal(operand_config, "rooted", "c"), InvalidFilterShapeError)


def test_unresolvable_property_reference_reads_as_untyped(operand_config):
    refusal = _refusal(operand_config, "unresolved", "a")
    assert isinstance(refusal, InvalidFilterShapeError)
