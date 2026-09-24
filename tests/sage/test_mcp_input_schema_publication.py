"""Optional MCP parameters publish their type directly, and free text survives a number.

An optional parameter's argument model admits null, so Pydantic renders it as
``anyOf: [<arm>, {type: null}]``. Some clients discard ``anyOf`` when they
load a tool, leaving the parameter with a description and no type, and are
then free to send an all-digit query as a JSON number. So the published copy
carries the non-null arm alone. What the server accepts is unchanged: an
explicit null still means the same as omission.

Free-text parameters additionally accept a JSON number and read it as the
digits it spells, for a client that ignores the schema outright.

Anti-coincidental-pass: the surface checks count what they inspect, so a
listing that came back empty or read the wrong attribute cannot pass; the
enum check compares against the model's values and asserts the ``$ref`` is
gone, so a ``type`` added beside a surviving ``$ref`` fails it; the numeric
query check spies on the request the retrieval service receives, so a tool
that swallowed the error without searching fails it; and a bool, and a
numeric identifier, are asserted still refused, so a backstop that coerced
everything fails those.
"""

from __future__ import annotations

import copy
import json
from typing import Any

import pytest
from mcp.types import TextContent

from sage._mcp_schema import flatten_optional_parameters
from sage.adapters.stubs import (
    StubAbstractionProvider,
    StubContentStore,
    StubEmbeddingProvider,
)
from sage.config import VaultConfig
from sage.mcp_server import _vaults as _mcp_vaults
from sage.mcp_server import mcp
from sage.models.enums import FacetField, RetrievalMode
from tests.helpers.published_tool import published_tool, published_tools
from tests.sage.conftest import initialize_services_for_test

VAULT_ID = "test_vault"
_NULL = {"type": "null"}
_TRIPWIRE_MARK = "Tripwire, not a functional argument"

#: Fewer top-level properties than this means the listing is not the surface.
MIN_PROPERTIES_INSPECTED = 150


# ---------------------------------------------------------------------------
# The transform, on a hand-built schema
# ---------------------------------------------------------------------------


def _fixture_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "text": {"anyOf": [{"type": "string"}, _NULL], "default": None, "description": "d"},
            "mode": {"anyOf": [{"$ref": "#/$defs/Mode"}, _NULL], "default": None},
            "names": {
                "anyOf": [{"type": "array", "items": {"$ref": "#/$defs/Name"}}, _NULL],
                "default": None,
            },
            "three": {"anyOf": [{"$ref": "#/$defs/Kept"}, {"type": "integer"}, _NULL]},
            "either": {"anyOf": [{"type": "string"}, {"type": "integer"}]},
            "count": {"anyOf": [{"type": "integer"}, _NULL], "default": 5},
        },
        "$defs": {
            "Mode": {"type": "string", "enum": ["a", "b"]},
            "Name": {"type": "string", "enum": ["x"], "description": "The type, not the field."},
            "Kept": {"type": "string", "enum": ["k"]},
            "Item": {
                "type": "object",
                "properties": {"title": {"anyOf": [{"type": "string"}, _NULL]}},
            },
        },
    }


def test_transform_flattens_top_level_nullable_unions() -> None:
    """Each two-arm union with a null arm becomes its other arm; the default stays."""
    out = flatten_optional_parameters(_fixture_schema())
    props = out["properties"]

    # The null default stays: descriptions say what a null (or omitted) value does.
    assert props["text"] == {"type": "string", "description": "d", "default": None}
    assert props["mode"] == {"type": "string", "enum": ["a", "b"], "default": None}
    # A reference nested inside the arm is inlined too, without the type's description.
    assert props["names"] == {
        "type": "array",
        "items": {"type": "string", "enum": ["x"]},
        "default": None,
    }
    assert props["count"] == {"type": "integer", "default": 5}


def test_transform_leaves_other_shapes_alone() -> None:
    """Three arms, a union without null, and nested properties are untouched."""
    original = _fixture_schema()
    out = flatten_optional_parameters(original)

    assert out["properties"]["three"] == original["properties"]["three"]
    assert out["properties"]["either"] == original["properties"]["either"]
    assert out["$defs"]["Item"] == original["$defs"]["Item"]


def test_transform_keeps_defs_only_while_referenced() -> None:
    """``Mode`` and ``Name`` were inlined and are gone; ``Kept`` is still referenced."""
    out = flatten_optional_parameters(_fixture_schema())

    assert "Mode" not in out["$defs"]
    assert "Name" not in out["$defs"]
    assert "Kept" in out["$defs"]
    # Never referenced to begin with, so not this transform's to drop.
    assert "Item" in out["$defs"]


def test_transform_drops_empty_defs() -> None:
    schema = {
        "type": "object",
        "properties": {"mode": {"anyOf": [{"$ref": "#/$defs/Mode"}, _NULL]}},
        "$defs": {"Mode": {"type": "string", "enum": ["a"]}},
    }
    assert "$defs" not in flatten_optional_parameters(schema)


def test_transform_does_not_mutate_its_input() -> None:
    original = _fixture_schema()
    snapshot = copy.deepcopy(original)
    flatten_optional_parameters(original)
    assert original == snapshot


# ---------------------------------------------------------------------------
# The published surfaces
# ---------------------------------------------------------------------------


def _top_level_properties() -> list[tuple[str, str, dict[str, Any]]]:
    return [
        (tool_name, name, prop)
        for tool_name, tool in published_tools().items()
        for name, prop in (tool.parameters.get("properties") or {}).items()
    ]


def _is_tripwire(prop: dict[str, Any]) -> bool:
    return str(prop.get("description", "")).startswith(_TRIPWIRE_MARK)


def test_no_published_parameter_offers_a_null_arm() -> None:
    properties = _top_level_properties()
    assert len(properties) >= MIN_PROPERTIES_INSPECTED

    offending = [
        f"{tool}.{name}"
        for tool, name, prop in properties
        if any(_NULL in (prop.get(key) or ()) for key in ("anyOf", "oneOf"))
        # The 3.1 list spelling carries the same null arm under another name.
        or (isinstance(prop.get("type"), list) and "null" in prop["type"])
    ]
    assert offending == []


def test_every_typed_parameter_publishes_a_type() -> None:
    properties = _top_level_properties()
    assert len(properties) >= MIN_PROPERTIES_INSPECTED

    untyped = [
        f"{tool}.{name}"
        for tool, name, prop in properties
        if "type" not in prop and not _is_tripwire(prop)
    ]
    assert untyped == []


@pytest.mark.parametrize(
    ("tool", "param", "expected_type"),
    [
        ("search", "query", "string"),
        ("search", "heading_path", "string"),
        ("search", "min_relevance", "number"),
        ("search", "filters", "object"),
        ("search", "facet_fields", "array"),
        ("chain", "limit", "integer"),
    ],
)
def test_flattened_forms(tool: str, param: str, expected_type: str) -> None:
    prop = published_tool(tool).parameters["properties"][param]

    assert prop["type"] == expected_type
    assert "anyOf" not in prop
    # Kept, so a description reading "Null (default) ..." matches its schema.
    assert "default" in prop
    assert prop["default"] is None
    assert prop["description"]


def test_flattened_array_keeps_its_items_inlined() -> None:
    prop = published_tool("search").parameters["properties"]["facet_fields"]
    assert prop["items"]["enum"] == [f.value for f in FacetField]
    assert "$ref" not in prop["items"]
    # The inlined definition's description describes the type, not this parameter.
    assert "description" not in prop["items"]


def test_enum_parameter_publishes_its_values() -> None:
    prop = published_tool("search").parameters["properties"]["mode"]

    assert "$ref" not in prop
    assert prop["type"] == "string"
    assert prop["enum"] == [m.value for m in RetrievalMode]


def _refs(node: Any) -> list[str]:
    if isinstance(node, dict):
        found = [node["$ref"]] if isinstance(node.get("$ref"), str) else []
        for value in node.values():
            found.extend(_refs(value))
        return found
    if isinstance(node, list):
        return [ref for item in node for ref in _refs(item)]
    return []


def test_every_published_ref_resolves() -> None:
    dangling = []
    for name, tool in published_tools().items():
        defs = tool.parameters.get("$defs") or {}
        for ref in _refs(tool.parameters):
            if ref.removeprefix("#/$defs/") not in defs:
                dangling.append(f"{name}: {ref}")
    assert dangling == []


# ---------------------------------------------------------------------------
# What the server accepts, through the real MCP transport
# ---------------------------------------------------------------------------


@pytest.fixture
async def vault_services(minimal_vault_config_dict, tmp_vault_dir):
    """Stub-backed services registered on the MCP vault registry."""
    config = VaultConfig.model_validate(minimal_vault_config_dict)
    async with initialize_services_for_test(
        config,
        content_store=StubContentStore(),
        embedding_provider=StubEmbeddingProvider(),
        abstraction_provider=StubAbstractionProvider(),
    ) as services:
        _mcp_vaults[VAULT_ID] = services
        try:
            yield services
        finally:
            _mcp_vaults.pop(VAULT_ID, None)


@pytest.fixture
def discover_requests(vault_services, monkeypatch) -> list[Any]:
    """Each request the retrieval service receives; the real service still runs."""
    seen: list[Any] = []
    service = vault_services.retrieval_service
    original = service.discover

    async def recording(request):
        seen.append(request)
        return await original(request)

    monkeypatch.setattr(service, "discover", recording)
    return seen


async def _call(tool: str, **arguments: Any) -> dict[str, Any]:
    result = await mcp.call_tool(tool, {"vault_id": VAULT_ID, **arguments})
    assert isinstance(result, list) and len(result) == 1
    assert isinstance(result[0], TextContent)
    return json.loads(result[0].text)


async def test_all_digit_query_sent_as_number_searches(discover_requests) -> None:
    envelope = await _call("search", mode="keyword", query=104188062)

    assert "error" not in envelope, envelope
    assert len(discover_requests) == 1
    assert discover_requests[0].query == "104188062"
    assert isinstance(discover_requests[0].query, str)


async def test_numeric_search_heading_path_is_read_as_text(discover_requests) -> None:
    await _call(
        "search",
        mode="deterministic",
        document_id="0123abcd_some_document",
        heading_path=2026,
    )

    assert len(discover_requests) == 1
    assert discover_requests[0].heading_path == "2026"


async def test_numeric_read_section_heading_path_reaches_the_lookup(vault_services) -> None:
    envelope = await _call("read_section", document_id="0123abcd_some_document", heading_path=2026)

    # The ordinary lookup ran and found no such document; the argument was not refused.
    assert envelope.get("error") == "document_not_found", envelope


async def test_bool_is_not_coerced(vault_services) -> None:
    envelope = await _call("search", mode="keyword", query=True)

    assert envelope["error"] == "invalid_parameter"
    assert envelope["detail"]["parameter"] == "query"


async def test_numeric_identifier_is_not_coerced(vault_services) -> None:
    envelope = await _call("search", mode="catalog", document_id=12345678)

    assert envelope["error"] == "invalid_parameter"
    assert envelope["detail"]["parameter"] == "document_id"


async def test_explicit_null_still_accepted(discover_requests) -> None:
    envelope = await _call(
        "search", mode="catalog", query=None, heading_path=None, min_relevance=None
    )

    assert "error" not in envelope, envelope
    assert len(discover_requests) == 1
