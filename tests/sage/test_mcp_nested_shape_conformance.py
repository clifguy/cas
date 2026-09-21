"""The MCP tools publish the shape of every batch item they validate.

A batch tool takes its items as plain mappings and validates each against an
item model inside the tool body, so the argument model FastMCP builds from the
signature says nothing about the items. Left alone, ``tools/list`` publishes
each item as an open object with no properties, and a caller learns the shape
only by being refused once. The shapes are published from the same models the
tool bodies validate against, and this module holds the two together:

* no array argument on either surface publishes an item with no properties;
* each published item shape declares exactly the model's fields and required
  fields, at every nested object, checked against ``model_fields`` rather
  than against the model's own JSON Schema, so the publication cannot pass by
  agreeing with itself;
* every published object stays open (``additionalProperties: true``) while
  the models forbid extras, so the refusal of an undeclared key stays the
  server's to give. A client that coerces arguments to the published schema
  would otherwise drop the key before it was sent, silently (CAS-ADR-037);
* every item the model accepts is admitted by the published schema, so a
  client holding to the publication refuses nothing the server would accept;
* the names a refusal reports as accepted are the names published at the
  same location, which also shows that publishing a shape did not move
  validation out of the tool body.
"""

from __future__ import annotations

import asyncio
import enum
import functools
import inspect
import json
import types
import typing

import pytest
from mcp.types import TextContent
from pydantic import BaseModel

from sage.adapters.stubs import (
    StubAbstractionProvider,
    StubContentStore,
    StubEmbeddingProvider,
)
from sage.config import VaultConfig
from sage.mcp_server import _vaults as _mcp_vaults
from sage.mcp_server import mcp
from sage.models import mcp_items, schemas
from tests.sage.conftest import initialize_services_for_test

VAULT_ID = "test_vault"

#: Each batch argument and the item model its tool body validates against.
_NESTED_ITEM_MODELS: dict[tuple[str, str], str] = {
    ("bulk_ingest_document", "files"): "BulkIngestFileEntry",
    ("create_edges", "items"): "BulkLinkItem",
    ("update_lifecycles", "items"): "BulkLifecycleItem",
    ("update_metadata", "items"): "BulkMetadataItem",
}


def _model(name: str) -> type[BaseModel]:
    return getattr(schemas, name, None) or getattr(mcp_items, name)


@functools.cache
def _published_tools() -> dict[str, dict]:
    """Every tool's input schema, as ``tools/list`` publishes it on its surface.

    Listed on a worker thread with its own event loop, so an async test that
    reaches this first does not call ``asyncio.run`` inside its running loop.
    """
    from concurrent.futures import ThreadPoolExecutor

    from sage import mcp_server

    async def listing() -> dict[str, dict]:
        published: dict[str, dict] = {}
        for surface in ("sage", "sage_maint"):
            for tool in await mcp_server.build_partitioned_server(surface).list_tools():
                published[tool.name] = tool.inputSchema
        return published

    with ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, listing()).result()


def _branches(node: dict) -> list[dict]:
    """A schema node's alternatives, or the node itself when it has none."""
    for keyword in ("anyOf", "oneOf"):
        if keyword in node:
            return [branch for child in node[keyword] for branch in _branches(child)]
    return [node]


def _object_branches(node: dict) -> list[dict]:
    """The declared-property objects a node admits, looking through arrays."""
    found: list[dict] = []
    for branch in _branches(node):
        assert "$ref" not in branch, f"unresolved reference in a published shape: {branch}"
        if branch.get("type") == "array" and isinstance(branch.get("items"), dict):
            found.extend(_object_branches(branch["items"]))
        elif "properties" in branch:
            found.append(branch)
    return found


def _published_item(tool: str, argument: str) -> dict:
    items = _object_branches(_published_tools()[tool]["properties"][argument])
    assert len(items) == 1, f"{tool}.{argument} publishes {len(items)} item shapes"
    return items[0]


def _direct_models(annotation: object) -> list[type[BaseModel]]:
    """The models an annotation names directly, without entering their fields."""
    found: list[type[BaseModel]] = []
    pending = [annotation]
    while pending:
        current = pending.pop()
        if isinstance(current, type) and issubclass(current, BaseModel):
            if current not in found:
                found.append(current)
        elif isinstance(current, types.UnionType) or typing.get_origin(current) is not None:
            pending.extend(typing.get_args(current))
    return found


def _declared(model: type[BaseModel]) -> dict[str, typing.Any]:
    return {field.alias or name: field for name, field in model.model_fields.items()}


def _mismatches(model: type[BaseModel], published: dict, path: str) -> list[str]:
    fields = _declared(model)
    problems: list[str] = []
    if set(published.get("properties", {})) != set(fields):
        problems.append(
            f"{path}: publishes {sorted(published.get('properties', {}))}, "
            f"{model.__name__} declares {sorted(fields)}"
        )
    required = {name for name, field in fields.items() if field.is_required()}
    if set(published.get("required", [])) != required:
        problems.append(
            f"{path}: publishes required {sorted(published.get('required', []))}, "
            f"{model.__name__} requires {sorted(required)}"
        )
    for name, field in fields.items():
        nested = _direct_models(field.annotation)
        if not nested:
            continue
        candidates = _object_branches(published.get("properties", {}).get(name, {}))
        for inner in nested:
            match = [c for c in candidates if set(c["properties"]) == set(_declared(inner))]
            if not match:
                problems.append(f"{path}.{name}: no published shape for {inner.__name__}")
                continue
            problems.extend(_mismatches(inner, match[0], f"{path}.{name}"))
    return problems


def _models_reached(model: type[BaseModel]) -> list[type[BaseModel]]:
    reached = [model]
    for current in reached:
        for field in current.model_fields.values():
            reached.extend(m for m in _direct_models(field.annotation) if m not in reached)
    return reached


def _definition_docstrings(model: type[BaseModel]) -> set[str]:
    """The docstrings of every model and enum a published item shape reaches."""
    classes: list[type] = list(_models_reached(model))
    for current in list(classes):
        pending = [field.annotation for field in current.model_fields.values()]
        while pending:
            annotation = pending.pop()
            if isinstance(annotation, type) and issubclass(annotation, enum.Enum):
                classes.append(annotation)
            elif isinstance(annotation, types.UnionType) or typing.get_origin(annotation):
                pending.extend(typing.get_args(annotation))
    return {inspect.cleandoc(cls.__doc__) for cls in classes if cls.__doc__}


def _descriptions(node: object) -> list[str]:
    found: list[str] = []
    if isinstance(node, dict):
        if isinstance(node.get("description"), str):
            found.append(node["description"])
        for key, value in node.items():
            if key != "description":
                found.extend(_descriptions(value))
    elif isinstance(node, list):
        for value in node:
            found.extend(_descriptions(value))
    return found


def _accepts_any_shape(prop: dict) -> bool:
    """Whether an argument admits a string, an array, and an object alike.

    Such an argument declares no shape by design: it exists to catch a key
    supplied at the wrong level and refuse it by name, whatever its value.
    """
    kinds = {branch.get("type") for branch in _branches(prop)}
    return {"string", "array", "object"} <= kinds


def _undeclared_item(array: dict) -> bool:
    """Whether an array schema publishes an item with no declared shape.

    Absent or empty ``items`` declare nothing, and neither does an object
    branch without properties, wherever it sits among the item's alternatives.
    """
    items = array.get("items")
    if not isinstance(items, dict) or not items:
        return True
    return any(
        branch == {} or (branch.get("type") == "object" and not branch.get("properties"))
        for branch in _branches(items)
    )


def _object_nodes(node: object) -> list[dict]:
    found: list[dict] = []
    if isinstance(node, dict):
        if "properties" in node:
            found.append(node)
        for value in node.values():
            found.extend(_object_nodes(value))
    elif isinstance(node, list):
        for value in node:
            found.extend(_object_nodes(value))
    return found


# ---------------------------------------------------------------------------
# Publication
# ---------------------------------------------------------------------------


def test_every_array_argument_publishes_its_item_shape():
    arrays = 0
    open_items: list[str] = []
    for tool, schema in sorted(_published_tools().items()):
        for name, prop in schema.get("properties", {}).items():
            if _accepts_any_shape(prop):
                continue
            for branch in _branches(prop):
                if branch.get("type") != "array":
                    continue
                arrays += 1
                if _undeclared_item(branch):
                    open_items.append(f"{tool}.{name}")

    assert arrays >= len(_NESTED_ITEM_MODELS), "the walk found too few arrays to check anything"
    assert open_items == [], "these arguments publish an item with no declared properties"


@pytest.mark.parametrize(
    ("array", "undeclared"),
    [
        ({"type": "array"}, True),
        ({"type": "array", "items": {}}, True),
        ({"type": "array", "items": {"type": "object"}}, True),
        ({"type": "array", "items": {"anyOf": [{"type": "object"}, {"type": "null"}]}}, True),
        ({"type": "array", "items": {"anyOf": [{}, {"type": "null"}]}}, True),
        ({"type": "array", "items": {"type": "string"}}, False),
        (
            {"type": "array", "items": {"type": "object", "properties": {"a": {}}}},
            False,
        ),
    ],
)
def test_undeclared_item_predicate(array: dict, undeclared: bool):
    """The gate above reads every shape an undeclared item is published in."""
    assert _undeclared_item(array) is undeclared


@pytest.mark.parametrize(("tool", "argument"), sorted(_NESTED_ITEM_MODELS))
def test_published_shapes_carry_no_definition_prose(tool: str, argument: str):
    """A model's or enum's docstring is written for a maintainer, not a caller.

    Field descriptions are published; the class-level prose the model schema
    attaches to each definition is not, nor the root item's title.
    """
    published = _published_item(tool, argument)
    docstrings = _definition_docstrings(_model(_NESTED_ITEM_MODELS[(tool, argument)]))

    assert docstrings, "no docstring reached; the check compares nothing"
    assert "title" not in published and "description" not in published
    leaked = [text[:60] for text in _descriptions(published) if text in docstrings]
    assert leaked == [], f"definition docstrings published: {leaked}"


@pytest.mark.parametrize(("tool", "argument"), sorted(_NESTED_ITEM_MODELS))
def test_published_item_properties_match_the_model(tool: str, argument: str):
    model = _model(_NESTED_ITEM_MODELS[(tool, argument)])

    assert _mismatches(model, _published_item(tool, argument), f"{tool}.{argument}") == []


def test_published_item_shapes_stay_open_where_the_models_forbid():
    nodes: list[dict] = []
    for (tool, argument), name in sorted(_NESTED_ITEM_MODELS.items()):
        nodes.extend(_object_nodes(_published_item(tool, argument)))
        tolerant = [
            m.__name__
            for m in _models_reached(_model(name))
            if m.model_config.get("extra") != "forbid"
        ]
        assert tolerant == [], f"{tool}.{argument} reaches models that accept extras: {tolerant}"

    assert len(nodes) >= 5, "too few published objects to be checking anything"
    closed = [
        sorted(node["properties"]) for node in nodes if node.get("additionalProperties") is not True
    ]
    assert closed == [], "a published object is not open; a coercing client would strip its extras"


_DOC_A = "0123abcd_first"
_DOC_B = "4567cdef_second"

#: Items the models accept, exercising every declared field at least once
#: (nulls, the ``doc_id`` alias, and nested patch objects included). A client
#: that coerces to the published schema must let each of them through.
_ACCEPTED_ITEMS: dict[tuple[str, str], list[dict]] = {
    ("bulk_ingest_document", "files"): [
        {"file_path": "notes/a.md"},
        {
            "transfer_token": "opaque-token",
            "sha256": "sha256:" + "a" * 64,
            "source_type": "markdown",
            "parsed_metadata": {
                "title": None,
                "date": "2026-09-21",
                "project": "P",
                "codes": ["PV07"],
                "version": "v1.0",
                "doc_type": "note",
            },
            "tier3_metadata": {"ticket_id": "T-0001"},
        },
        {"file_path": "b.md", "parsed_metadata": {"tags": ["one, two"]}, "sha256": None},
    ],
    ("create_edges", "items"): [
        {"source_id": _DOC_A, "target_id": _DOC_B, "edge_type": "references"},
        {
            "source_id": _DOC_A,
            "target_id": _DOC_B,
            "edge_type": "depends_on",
            "source_valid_from_version": _DOC_A,
            "target_valid_from_version": _DOC_B,
            "notes": "n",
            "rationale": "r",
            "rationale_kind": "manual",
        },
        {
            "source_id": _DOC_A,
            "target_id": None,
            "edge_type": "retracts",
            "retracted_edge_id": "123e4567-e89b-12d3-a456-426614174000",
        },
        {
            "source_id": _DOC_A,
            "target_id": _DOC_B,
            "edge_type": "sync_target",
            "synced_from_version": _DOC_B,
            "synced_from_content_hash": "sha256:" + "b" * 64,
        },
    ],
    ("update_lifecycles", "items"): [
        {"document_id": _DOC_A, "action": "complete"},
        {"doc_id": _DOC_A, "action": "supersede", "successor_id": _DOC_B},
        {"document_id": _DOC_A, "action": "complete", "successor_id": None},
        {
            "document_id": _DOC_A,
            "action": "relocate",
            "relocated_to": {
                "vault_id": "elsewhere",
                "document_id": _DOC_B,
                "server_address": None,
                "source_content_hash": "sha256:" + "c" * 64,
                "relocated_at": "2026-09-21T12:00:00Z",
            },
        },
    ],
    ("update_metadata", "items"): [
        {"document_id": _DOC_A, "title": "t", "project": None},
        {
            "doc_id": _DOC_A,
            "tags": {"add": ["x"], "remove": ["y"]},
            "tier3_metadata": {"set": {"k": 1}, "unset": ["j"]},
            "document_date": "2026-09-21",
            "version_label": "v2",
            "doc_type": "note",
            "authority_scope": None,
            "expected_version": _DOC_A,
        },
    ],
}


@pytest.mark.parametrize(("tool", "argument"), sorted(_NESTED_ITEM_MODELS))
def test_what_the_model_accepts_the_publication_admits(tool: str, argument: str):
    """Nothing the server accepts is refused by a client holding to the schema.

    The direction matters: a published schema may be looser than the model --
    a document id's format is checked by a validator the schema cannot
    express, and the server still refuses it by name -- but never stricter.
    """
    import jsonschema

    assert set(_ACCEPTED_ITEMS) == set(_NESTED_ITEM_MODELS)
    model = _model(_NESTED_ITEM_MODELS[(tool, argument)])
    items = _ACCEPTED_ITEMS[(tool, argument)]
    published = _published_item(tool, argument)

    exercised = set().union(*items)
    assert exercised == set(_declared(model)), (
        f"fields no accepted item exercises: {sorted(set(_declared(model)) - exercised)}"
    )
    for item in items:
        model.model_validate(item)  # an item the model refuses proves nothing here
        jsonschema.validate(item, published)


def test_file_entry_fields_derive_from_the_model():
    from sage import app_tools

    entry = _model("BulkIngestFileEntry")

    assert entry.model_config.get("extra") == "forbid"
    assert app_tools._FILE_ENTRY_FIELDS == frozenset(entry.model_fields)  # noqa: SLF001


# ---------------------------------------------------------------------------
# What is refused is what is published
# ---------------------------------------------------------------------------


@pytest.fixture
async def vault_services(minimal_vault_config_dict, tmp_vault_dir):
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
            await asyncio.sleep(0.5)
            _mcp_vaults.pop(VAULT_ID, None)


_ABSENT = "00000000_absent_document"

#: A call planting ``fabricated_key`` in one object, the location the refusal
#: should name, and the path from the published item to that object.
_REFUSAL_PROBES: list[tuple[str, dict, str, tuple[str, ...]]] = [
    (
        "bulk_ingest_document",
        {"files": [{"file_path": "test/sample.md", "fabricated_key": 1}]},
        "files.0",
        (),
    ),
    (
        "bulk_ingest_document",
        {"files": [{"file_path": "test/sample.md", "parsed_metadata": {"fabricated_key": 1}}]},
        "files.0.parsed_metadata",
        ("parsed_metadata",),
    ),
    (
        "create_edges",
        {"items": [{"source_id": _ABSENT, "edge_type": "references", "fabricated_key": 1}]},
        "items.0",
        (),
    ),
    (
        "update_lifecycles",
        {"items": [{"document_id": _ABSENT, "action": "complete", "fabricated_key": 1}]},
        "items.0",
        (),
    ),
    (
        "update_metadata",
        {"items": [{"document_id": _ABSENT, "fabricated_key": 1}]},
        "items.0",
        (),
    ),
    (
        "update_metadata",
        {"items": [{"document_id": _ABSENT, "tags": {"add": ["x"], "fabricated_key": 1}}]},
        "items.0.tags",
        ("tags",),
    ),
]


def _published_at(tool: str, path: tuple[str, ...]) -> dict:
    argument = next(arg for (name, arg) in _NESTED_ITEM_MODELS if name == tool)
    node = _published_item(tool, argument)
    for key in path:
        branches = _object_branches(node["properties"][key])
        assert len(branches) == 1, f"{tool} {path}: {len(branches)} object shapes"
        node = branches[0]
    return node


@pytest.mark.parametrize(
    ("tool", "arguments", "parameter", "path"),
    _REFUSAL_PROBES,
    ids=[f"{probe[0]}:{probe[2]}" for probe in _REFUSAL_PROBES],
)
async def test_refusal_names_the_published_set(vault_services, tool, arguments, parameter, path):
    result = await mcp.call_tool(tool, {"vault_id": VAULT_ID, **arguments})
    assert isinstance(result, list) and len(result) == 1 and isinstance(result[0], TextContent)
    envelope = json.loads(result[0].text)

    assert envelope["error"] == "undeclared_key", envelope
    assert envelope["detail"]["parameter"] == parameter, envelope
    assert envelope["detail"]["key"] == "fabricated_key", envelope
    assert sorted(envelope["detail"]["recognized"]) == sorted(
        _published_at(tool, path)["properties"]
    ), envelope
