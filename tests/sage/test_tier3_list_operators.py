"""List operators, facets and sort for typed (tier3) metadata fields.

A ``tier3_metadata`` filter value is a literal compared for exact equality.
For a field whose ``metadata_schema`` declares ``type: array`` the value may
instead be an operator object -- ``contains``, ``any``, ``all`` or ``none`` --
testing membership, with operands checked against the field's ``items``
schema. ``target="facets"`` aggregates declared tier3 fields named
``tier3:<field>``, and catalog ``sort_by`` accepts ``tier3:<field>`` for a
declared scalar.

The fixture is built so each assertion discriminates:

* ``a`` holds ``[mosyle, abm]`` and ``b`` holds ``[mosyle]``: equality and
  containment disagree on both, and ``all`` in either order separates them.
* ``c`` is a ``probe`` without the field: ``none`` must keep it, every other
  operator must drop it, and a facet counts it as absent.
* ``d`` is an ``other`` document whose doc_type declares ``systems`` as a
  *string* holding ``"mosyle"``: an un-narrowed operator must not reach it,
  and a facet over the un-narrowed slice must not count it.
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

_SYSTEMS = ["mosyle", "abm", "m365_admin"]


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


async def _seed(tmp_vault_dir: Path, ingestion: IngestionService, extra: dict | None = None):
    rows = {
        "a": (
            "probe",
            {"probe_id": "A", "systems": ["mosyle", "abm"], "last_reviewed": "2026-03-01"},
        ),
        "b": ("probe", {"probe_id": "B", "systems": ["mosyle"], "last_reviewed": "2026-01-15"}),
        "c": ("probe", {"probe_id": "C"}),
        "d": ("other", {"systems": "mosyle"}),
        **(extra or {}),
    }
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
    """``contains`` is membership, not equality: both lists hold mosyle."""
    docs = await _seed(tmp_vault_dir, list_ingestion)
    matched = await _ids(list_retrieval, {"systems": {"contains": "mosyle"}})
    assert _names(docs, matched) == {"a", "b"}


async def test_all_is_order_insensitive_containment(tmp_vault_dir, list_ingestion, list_retrieval):
    """Both spellings of the pair select a; neither selects b, which lacks abm."""
    docs = await _seed(tmp_vault_dir, list_ingestion)
    forward = await _ids(list_retrieval, {"systems": {"all": ["mosyle", "abm"]}})
    reverse = await _ids(list_retrieval, {"systems": {"all": ["abm", "mosyle"]}})
    assert _names(docs, forward) == {"a"}
    assert _names(docs, reverse) == {"a"}


async def test_any_matches_a_list_holding_at_least_one(
    tmp_vault_dir, list_ingestion, list_retrieval
):
    """m365_admin is held by nobody, so ``any`` reduces to abm: only a.

    Under ``all`` semantics this would be empty, which is what separates the two.
    """
    docs = await _seed(tmp_vault_dir, list_ingestion)
    matched = await _ids(list_retrieval, {"systems": {"any": ["abm", "m365_admin"]}})
    assert _names(docs, matched) == {"a"}


async def test_none_keeps_lists_without_the_values_and_documents_without_the_field(
    tmp_vault_dir, list_ingestion, list_retrieval
):
    docs = await _seed(tmp_vault_dir, list_ingestion)
    matched = await _ids(list_retrieval, {"systems": {"none": ["abm"]}})
    assert _names(docs, matched) == {"b", "c"}


async def test_operators_compose_and(tmp_vault_dir, list_ingestion, list_retrieval):
    """Two operators on one field must both hold."""
    docs = await _seed(tmp_vault_dir, list_ingestion)
    matched = await _ids(list_retrieval, {"systems": {"contains": "mosyle", "none": ["abm"]}})
    assert _names(docs, matched) == {"b"}


async def test_unnarrowed_operator_reaches_only_doc_types_declaring_an_array(
    tmp_vault_dir, list_ingestion, list_retrieval
):
    """With no doc_type, d -- whose doc_type declares ``systems`` as a string
    holding "mosyle" -- must not match ``contains`` nor ``none``."""
    docs = await _seed(tmp_vault_dir, list_ingestion)
    contains = await _ids(list_retrieval, {"systems": {"contains": "mosyle"}}, doc_type=None)
    none = await _ids(list_retrieval, {"systems": {"none": ["abm"]}}, doc_type=None)
    assert _names(docs, contains) == {"a", "b"}
    assert _names(docs, none) == {"b", "c"}


async def test_bare_list_literal_keeps_exact_equality(
    tmp_vault_dir, list_ingestion, list_retrieval
):
    """The literal form is unchanged: a one-element list matches only b."""
    docs = await _seed(tmp_vault_dir, list_ingestion)
    matched = await _ids(list_retrieval, {"systems": ["mosyle"]})
    reversed_pair = await _ids(list_retrieval, {"systems": ["abm", "mosyle"]})
    assert _names(docs, matched) == {"b"}
    assert reversed_pair == set()


async def test_object_field_dict_literal_keeps_exact_equality(
    tmp_vault_dir, list_ingestion, list_retrieval
):
    """A dict on an object-typed field is a literal even when its keys look
    like operators, because only an array declaration admits the operator form."""
    docs = await _seed(
        tmp_vault_dir,
        list_ingestion,
        extra={"e": ("probe", {"probe_id": "E", "owner_ref": {"name": "ops"}})},
    )
    matched = await _ids(list_retrieval, {"owner_ref": {"name": "ops"}})
    assert _names(docs, matched) == {"e"}


async def test_keyword_listing_and_content_prefilter_agree_with_catalog(
    tmp_vault_dir, list_ingestion, list_retrieval
):
    """The keyword ``*`` listing and the semantic/keyword pre-filter resolve
    the operator to the same documents as catalog mode."""
    docs = await _seed(tmp_vault_dir, list_ingestion)
    tier3 = {"systems": {"all": ["abm", "mosyle"]}}
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
        await _ids(list_retrieval, {"systems": {"contains": "mosyle_mdm"}})
    detail = excinfo.value.detail
    assert detail["field"] == "tier3_metadata.systems"
    assert detail["value"] == "mosyle_mdm"
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
        {"contains": ["mosyle"]},
        {"any": []},
        {"all": "mosyle"},
        {"contains": "mosyle", "near": "abm"},
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
    """A list field counts like tags; c is absent; d's string is not counted.

    f repeats mosyle, which must still count once for f. m's doc_type does
    not declare the field, so m is outside the population, not absent.
    """
    await _seed(
        tmp_vault_dir,
        list_ingestion,
        extra={
            "f": ("probe", {"probe_id": "F", "systems": ["mosyle", "mosyle"]}),
            "m": ("misc", None),
        },
    )
    rows = await _facets(list_retrieval, ["tier3:systems"], doc_type=None)
    row = rows["tier3:systems"]
    assert row.values == {"mosyle": 3, "abm": 1}
    assert row.total_distinct == 2
    assert row.absent_count == 1


async def test_list_facet_honors_the_value_cap(tmp_vault_dir, list_ingestion, list_retrieval):
    await _seed(tmp_vault_dir, list_ingestion)
    row = (await _facets(list_retrieval, ["tier3:systems"], limit=1))["tier3:systems"]
    assert row.values == {"mosyle": 2}
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
        ("invalid_filter_value", {"systems": {"contains": "mosyle_mdm"}}),
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
