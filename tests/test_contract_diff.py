"""The contract-diff detector classifies published-contract changes (CAS-ADR-008).

``scripts/contract_diff.py`` compares two snapshots of the published contract --
both OpenAPI specifications and the MCP tool catalog -- and reports each change a
caller could observe as a finding carrying one of two minor categories:
``capability`` (a caller can do something new) or ``caller-adaptation`` (a
caller must change what it does). Everything else, descriptive text above all, is
invisible to it.

Every rule is exercised twice. A positive case asserts the finding's category and
where it points, so a detector that reports *something* for any change cannot
pass by being noisy. A negative twin changes only text the detector must ignore,
so a detector that treats every diff as a finding fails there instead.
"""

from __future__ import annotations

import copy
from collections.abc import Callable
from typing import Any

import pytest

from scripts.contract_diff import (
    CALLER_ADAPTATION,
    CAPABILITY,
    Contract,
    diff_contracts,
    diff_mcp_catalog,
    diff_openapi,
)

SPEC: dict[str, Any] = {
    "openapi": "3.1.0",
    "info": {"title": "Fixture API", "version": "1.0", "description": "Fixture."},
    "paths": {
        "/things": {
            "get": {
                "summary": "List things.",
                "description": "Lists things.",
                "operationId": "list_things",
                "parameters": [
                    {
                        "name": "limit",
                        "in": "query",
                        "required": False,
                        "description": "Page size.",
                        "schema": {"type": "integer", "maximum": 100, "default": 10},
                    }
                ],
                "responses": {
                    "200": {
                        "description": "OK.",
                        "headers": {
                            "X-Page": {
                                "description": "Page number.",
                                "required": False,
                                "schema": {"type": "integer"},
                            }
                        },
                        "content": {
                            "application/json": {"schema": {"$ref": "#/components/schemas/Thing"}}
                        },
                    },
                    "404": {"description": "Not found."},
                },
            },
            "post": {
                "summary": "Create a thing.",
                "requestBody": {
                    "required": True,
                    "content": {
                        "application/json": {
                            "schema": {"$ref": "#/components/schemas/ThingRequest"}
                        }
                    },
                },
                "responses": {"201": {"description": "Created."}},
            },
        }
    },
    "components": {
        "schemas": {
            "Thing": {
                "type": "object",
                "description": "A thing.",
                "properties": {
                    "id": {"type": "string", "nullable": True, "description": "Identifier."},
                    "kind": {
                        "type": "string",
                        "enum": ["a", "b"],
                        "description": "Kind.",
                    },
                    "note": {"type": ["string", "null"], "description": "Note."},
                },
                "required": ["id", "kind"],
            },
            "ThingRequest": {
                "type": "object",
                "description": "Create request.",
                "properties": {
                    "name": {
                        "type": "string",
                        "maxLength": 50,
                        "description": "Name.",
                    },
                    "tag": {"type": "string", "description": "Tag."},
                },
                "required": ["name"],
                "additionalProperties": True,
            },
            "Other": {"type": "string", "description": "Other."},
        }
    },
}

CATALOG: dict[str, Any] = {
    "title": "Fixture catalog",
    "description": "Fixture.",
    "surfaces": {
        "sage": [
            {
                "name": "search",
                "title": None,
                "description": "Search documents.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string"},
                        "limit": {"type": "integer", "default": 10},
                    },
                    "required": ["query"],
                },
                "outputSchema": None,
                "annotations": {"readOnlyHint": True},
            },
            {
                "name": "get_document",
                "title": None,
                "description": "Read a document.",
                "inputSchema": {
                    "type": "object",
                    "properties": {"document_id": {"type": "string"}},
                    "required": ["document_id"],
                },
                "outputSchema": None,
                "annotations": {"readOnlyHint": True},
            },
        ],
        "sage_maint": [
            {
                "name": "reload_vault",
                "title": None,
                "description": "Reload a vault.",
                "inputSchema": {
                    "type": "object",
                    "properties": {"vault_id": {"type": "string"}},
                    "required": ["vault_id"],
                },
                "outputSchema": None,
                "annotations": {"destructiveHint": False},
            }
        ],
    },
}


def _mutated(base: dict[str, Any], mutate: Callable[[dict[str, Any]], None]) -> dict[str, Any]:
    new = copy.deepcopy(base)
    mutate(new)
    return new


def _thing(spec: dict[str, Any]) -> dict[str, Any]:
    return spec["components"]["schemas"]["Thing"]


def _request(spec: dict[str, Any]) -> dict[str, Any]:
    return spec["components"]["schemas"]["ThingRequest"]


def _get(spec: dict[str, Any]) -> dict[str, Any]:
    return spec["paths"]["/things"]["get"]


def _limit(spec: dict[str, Any]) -> dict[str, Any]:
    return _get(spec)["parameters"][0]


def _search(catalog: dict[str, Any]) -> dict[str, Any]:
    return catalog["surfaces"]["sage"][0]


# ---------------------------------------------------------------------------
# Positive cases: each rule fires, with its category, at the site it names.
# ---------------------------------------------------------------------------

# (case id, mutation, expected category, expected kind, substring of the pointer)
OPENAPI_POSITIVE: list[tuple[str, Callable[[dict[str, Any]], None], str, str, str]] = [
    (
        "operation-added",
        lambda s: s["paths"].__setitem__(
            "/widgets", {"get": {"responses": {"200": {"description": "OK."}}}}
        ),
        CAPABILITY,
        "operation-added",
        "/widgets/get",
    ),
    (
        "operation-removed",
        lambda s: s["paths"]["/things"].pop("post"),
        CALLER_ADAPTATION,
        "operation-removed",
        "/things/post",
    ),
    (
        "optional-parameter-added",
        lambda s: _get(s)["parameters"].append(
            {"name": "offset", "in": "query", "required": False, "schema": {"type": "integer"}}
        ),
        CAPABILITY,
        "parameter-added",
        "parameters/query:offset",
    ),
    (
        "required-parameter-added",
        lambda s: _get(s)["parameters"].append(
            {"name": "scope", "in": "query", "required": True, "schema": {"type": "string"}}
        ),
        CALLER_ADAPTATION,
        "required-parameter-added",
        "parameters/query:scope",
    ),
    (
        "parameter-removed",
        lambda s: _get(s).__setitem__("parameters", []),
        CALLER_ADAPTATION,
        "parameter-removed",
        "parameters/query:limit",
    ),
    (
        "parameter-made-required",
        lambda s: _limit(s).__setitem__("required", True),
        CALLER_ADAPTATION,
        "parameter-made-required",
        "parameters/query:limit",
    ),
    (
        "property-added",
        lambda s: _thing(s)["properties"].__setitem__(
            "size", {"type": "integer", "description": "Size."}
        ),
        CAPABILITY,
        "property-added",
        "Thing/properties/size",
    ),
    (
        "property-removed",
        lambda s: _thing(s)["properties"].pop("note"),
        CALLER_ADAPTATION,
        "property-removed",
        "Thing/properties/note",
    ),
    (
        "required-added",
        lambda s: _request(s)["required"].append("tag"),
        CALLER_ADAPTATION,
        "required-added",
        "ThingRequest/required/tag",
    ),
    (
        "required-removed",
        lambda s: _request(s).__setitem__("required", []),
        CAPABILITY,
        "required-removed",
        "ThingRequest/required/name",
    ),
    (
        "enum-value-added",
        lambda s: _thing(s)["properties"]["kind"]["enum"].append("c"),
        CAPABILITY,
        "enum-value-added",
        "Thing/properties/kind/enum",
    ),
    (
        "enum-value-removed",
        lambda s: _thing(s)["properties"]["kind"]["enum"].remove("b"),
        CALLER_ADAPTATION,
        "enum-value-removed",
        "Thing/properties/kind/enum",
    ),
    (
        "type-widened",
        lambda s: _request(s)["properties"]["tag"].__setitem__("type", ["string", "null"]),
        CAPABILITY,
        "type-widened",
        "ThingRequest/properties/tag/type",
    ),
    (
        "nullable-declared",
        lambda s: _request(s)["properties"]["tag"].__setitem__("nullable", True),
        CAPABILITY,
        "type-widened",
        "ThingRequest/properties/tag/type",
    ),
    (
        "type-narrowed",
        lambda s: _thing(s)["properties"]["note"].__setitem__("type", "string"),
        CALLER_ADAPTATION,
        "type-narrowed",
        "Thing/properties/note/type",
    ),
    (
        "max-length-tightened",
        lambda s: _request(s)["properties"]["name"].__setitem__("maxLength", 20),
        CALLER_ADAPTATION,
        "constraint-tightened",
        "ThingRequest/properties/name/maxLength",
    ),
    (
        "pattern-added",
        lambda s: _request(s)["properties"]["tag"].__setitem__("pattern", "^[a-z]+$"),
        CALLER_ADAPTATION,
        "constraint-tightened",
        "ThingRequest/properties/tag/pattern",
    ),
    (
        "minimum-added",
        lambda s: _limit(s)["schema"].__setitem__("minimum", 1),
        CALLER_ADAPTATION,
        "constraint-tightened",
        "parameters/query:limit/schema/minimum",
    ),
    (
        "additional-properties-closed",
        lambda s: _request(s).__setitem__("additionalProperties", False),
        CALLER_ADAPTATION,
        "constraint-tightened",
        "ThingRequest/additionalProperties",
    ),
    (
        "max-length-loosened",
        lambda s: _request(s)["properties"]["name"].__setitem__("maxLength", 80),
        CAPABILITY,
        "constraint-loosened",
        "ThingRequest/properties/name/maxLength",
    ),
    (
        "maximum-removed",
        lambda s: _limit(s)["schema"].pop("maximum"),
        CAPABILITY,
        "constraint-loosened",
        "parameters/query:limit/schema/maximum",
    ),
    (
        "default-changed",
        lambda s: _limit(s)["schema"].__setitem__("default", 25),
        CALLER_ADAPTATION,
        "default-changed",
        "parameters/query:limit/schema/default",
    ),
    (
        "ref-retargeted",
        lambda s: _get(s)["responses"]["200"]["content"]["application/json"].__setitem__(
            "schema", {"$ref": "#/components/schemas/Other"}
        ),
        CALLER_ADAPTATION,
        "ref-retargeted",
        "responses/200",
    ),
    (
        "response-added",
        lambda s: _get(s)["responses"].__setitem__("409", {"description": "Conflict."}),
        CAPABILITY,
        "response-added",
        "responses/409",
    ),
    (
        "response-removed",
        lambda s: _get(s)["responses"].pop("404"),
        CALLER_ADAPTATION,
        "response-removed",
        "responses/404",
    ),
    (
        "response-header-added",
        lambda s: _get(s)["responses"]["200"]["headers"].__setitem__(
            "X-Total", {"required": False, "schema": {"type": "integer"}}
        ),
        CAPABILITY,
        "header-added",
        "responses/200/headers/x-total",
    ),
    (
        "response-header-removed",
        lambda s: _get(s)["responses"]["200"].pop("headers"),
        CALLER_ADAPTATION,
        "header-removed",
        "responses/200/headers/x-page",
    ),
    (
        "response-header-made-required",
        lambda s: _get(s)["responses"]["200"]["headers"]["X-Page"].__setitem__("required", True),
        CALLER_ADAPTATION,
        "header-made-required",
        "responses/200/headers/x-page",
    ),
    (
        "response-header-narrowed",
        lambda s: _get(s)["responses"]["200"]["headers"]["X-Page"]["schema"].__setitem__(
            "minimum", 1
        ),
        CALLER_ADAPTATION,
        "constraint-tightened",
        "responses/200/headers/x-page/schema/minimum",
    ),
    (
        "component-removed",
        lambda s: s["components"]["schemas"].pop("Other"),
        CALLER_ADAPTATION,
        "component-removed",
        "components/schemas/Other",
    ),
]


@pytest.mark.parametrize(
    ("mutate", "category", "kind", "pointer_part"),
    [case[1:] for case in OPENAPI_POSITIVE],
    ids=[case[0] for case in OPENAPI_POSITIVE],
)
def test_openapi_rule_fires_with_its_category(
    mutate: Callable[[dict[str, Any]], None], category: str, kind: str, pointer_part: str
) -> None:
    findings = diff_openapi(SPEC, _mutated(SPEC, mutate), surface="sage_core_api")

    matching = [f for f in findings if f.kind == kind]
    assert matching, f"no {kind!r} finding; got {findings}"
    assert all(f.category == category for f in matching), matching
    assert any(pointer_part in f.pointer for f in matching), (
        f"{kind!r} finding does not point at {pointer_part!r}: {matching}"
    )
    assert all(f.surface == "sage_core_api" for f in findings)


MCP_POSITIVE: list[tuple[str, Callable[[dict[str, Any]], None], str, str, str]] = [
    (
        "tool-added",
        lambda c: c["surfaces"]["sage"].append(
            {
                "name": "traverse",
                "description": "Walk the graph.",
                "inputSchema": {"type": "object", "properties": {}},
                "outputSchema": None,
            }
        ),
        CAPABILITY,
        "tool-added",
        "sage/traverse",
    ),
    (
        "tool-removed",
        lambda c: c["surfaces"]["sage"].pop(1),
        CALLER_ADAPTATION,
        "tool-removed",
        "sage/get_document",
    ),
    (
        "tool-moved",
        lambda c: c["surfaces"]["sage_maint"].append(c["surfaces"]["sage"].pop(1)),
        CALLER_ADAPTATION,
        "tool-moved",
        "sage/get_document",
    ),
    (
        "optional-input-added",
        lambda c: _search(c)["inputSchema"]["properties"].__setitem__(
            "offset", {"type": "integer", "default": 0}
        ),
        CAPABILITY,
        "property-added",
        "sage/search/inputSchema/properties/offset",
    ),
    (
        "required-input-added",
        lambda c: (
            _search(c)["inputSchema"]["properties"].__setitem__("vault_id", {"type": "string"}),
            _search(c)["inputSchema"]["required"].append("vault_id"),
        ),
        CALLER_ADAPTATION,
        "required-added",
        "sage/search/inputSchema/required/vault_id",
    ),
    (
        "output-schema-declared",
        lambda c: _search(c).__setitem__("outputSchema", {"type": "object"}),
        CAPABILITY,
        "output-schema-added",
        "sage/search/outputSchema",
    ),
]


@pytest.mark.parametrize(
    ("mutate", "category", "kind", "pointer_part"),
    [case[1:] for case in MCP_POSITIVE],
    ids=[case[0] for case in MCP_POSITIVE],
)
def test_mcp_rule_fires_with_its_category(
    mutate: Callable[[dict[str, Any]], None], category: str, kind: str, pointer_part: str
) -> None:
    findings = diff_mcp_catalog(CATALOG, _mutated(CATALOG, mutate))

    matching = [f for f in findings if f.kind == kind]
    assert matching, f"no {kind!r} finding; got {findings}"
    assert all(f.category == category for f in matching), matching
    assert any(pointer_part in f.pointer for f in matching), matching
    assert all(f.surface == "mcp" for f in findings)


def test_a_moved_tool_is_one_finding_not_a_removal_and_an_addition() -> None:
    """A move reports once, as the adaptation it is. Reporting it as a removal
    plus an addition would still be minor, but would tell the owner a tool
    was deleted when it was not."""
    moved = _mutated(
        CATALOG, lambda c: c["surfaces"]["sage_maint"].append(c["surfaces"]["sage"].pop(1))
    )

    kinds = sorted(f.kind for f in diff_mcp_catalog(CATALOG, moved))

    assert kinds == ["tool-moved"]


# ---------------------------------------------------------------------------
# Negative controls: changes the detector must not see.
# ---------------------------------------------------------------------------

OPENAPI_NEGATIVE: list[tuple[str, Callable[[dict[str, Any]], None]]] = [
    (
        "descriptive-text",
        lambda s: (
            _get(s).__setitem__("summary", "Enumerate things."),
            _get(s).__setitem__("description", "Lists every thing, paged."),
            _thing(s).__setitem__("description", "A thing, described anew."),
            _thing(s)["properties"]["id"].__setitem__("description", "Opaque id."),
            _limit(s).__setitem__("description", "How many."),
            _thing(s).__setitem__("title", "Thing"),
            _thing(s)["properties"]["id"].__setitem__("example", "abc"),
            _thing(s)["properties"]["id"].__setitem__("examples", ["abc"]),
            _get(s)["responses"]["404"].__setitem__("description", "Nothing there."),
            _get(s)["responses"]["200"]["headers"]["X-Page"].__setitem__("description", "Page."),
        ),
    ),
    (
        "header-name-case",
        lambda s: _get(s)["responses"]["200"].__setitem__(
            "headers", {"x-page": _get(s)["responses"]["200"]["headers"]["X-Page"]}
        ),
    ),
    (
        "header-moved-to-a-component",
        lambda s: (
            s["components"].__setitem__(
                "headers", {"Page": _get(s)["responses"]["200"]["headers"]["X-Page"]}
            ),
            _get(s)["responses"]["200"]["headers"].__setitem__(
                "X-Page", {"$ref": "#/components/headers/Page"}
            ),
        ),
    ),
    (
        "ref-inlined-with-identical-shape",
        lambda s: _get(s)["responses"]["200"]["content"]["application/json"].__setitem__(
            "schema", copy.deepcopy(_thing(s))
        ),
    ),
    (
        "vendor-extensions",
        lambda s: (
            _get(s).__setitem__("x-internal", True),
            _thing(s).__setitem__("x-note", "anything"),
        ),
    ),
    ("info-version", lambda s: s["info"].__setitem__("version", "2.0")),
    ("top-level-tags", lambda s: s.__setitem__("tags", [{"name": "things"}])),
    (
        "enum-and-required-reordered",
        lambda s: (
            _thing(s)["properties"]["kind"].__setitem__("enum", ["b", "a"]),
            _thing(s).__setitem__("required", ["kind", "id"]),
        ),
    ),
    (
        "type-list-reordered",
        lambda s: _thing(s)["properties"]["note"].__setitem__("type", ["null", "string"]),
    ),
    (
        "nullable-respelled-for-openapi-3.1",
        lambda s: _thing(s)["properties"].__setitem__(
            "id", {"type": ["string", "null"], "description": "Identifier."}
        ),
    ),
    (
        "keys-reordered",
        lambda s: s["components"]["schemas"].__setitem__(
            "Thing", dict(reversed(list(_thing(s).items())))
        ),
    ),
]


@pytest.mark.parametrize(
    "mutate", [case[1] for case in OPENAPI_NEGATIVE], ids=[case[0] for case in OPENAPI_NEGATIVE]
)
def test_openapi_ignores_non_contract_change(mutate: Callable[[dict[str, Any]], None]) -> None:
    changed = _mutated(SPEC, mutate)
    assert changed != SPEC or mutate is OPENAPI_NEGATIVE[-1][1], "the control mutated nothing"

    assert diff_openapi(SPEC, changed, surface="sage_core_api") == []


def test_mcp_ignores_description_title_and_annotation_text() -> None:
    changed = _mutated(
        CATALOG,
        lambda c: (
            _search(c).__setitem__("description", "Search, rewritten."),
            _search(c).__setitem__("title", "Search"),
            _search(c)["inputSchema"]["properties"]["query"].__setitem__("description", "Text."),
            c.__setitem__("description", "Catalog, rewritten."),
        ),
    )
    assert changed != CATALOG

    assert diff_mcp_catalog(CATALOG, changed) == []


def test_a_response_header_made_optional_is_an_adaptation() -> None:
    """A caller that relied on a guaranteed header can no longer."""
    required = _mutated(
        SPEC,
        lambda s: _get(s)["responses"]["200"]["headers"]["X-Page"].__setitem__("required", True),
    )

    findings = diff_openapi(required, SPEC, surface="core")

    assert [(f.kind, f.category) for f in findings] == [("header-made-optional", CALLER_ADAPTATION)]


def test_a_header_reference_resolves_on_either_side() -> None:
    """Moving a header into a component, or back out of one, changes nothing."""

    def to_component(spec: dict[str, Any]) -> None:
        headers = _get(spec)["responses"]["200"]["headers"]
        spec["components"]["headers"] = {"Page": headers["X-Page"]}
        headers["X-Page"] = {"$ref": "#/components/headers/Page"}

    referenced = _mutated(SPEC, to_component)

    assert diff_openapi(SPEC, referenced, surface="core") == []
    assert diff_openapi(referenced, SPEC, surface="core") == []


def test_a_component_renamed_with_its_shape_intact_reports_the_rename_only() -> None:
    """Each side's reference resolves against its own document.

    The old reference names a component the new document no longer has, so a
    comparison resolving both sides against the new document would report the
    unchanged shape as retargeted as well."""

    def rename(spec: dict[str, Any]) -> None:
        schemas = spec["components"]["schemas"]
        schemas["Item"] = schemas.pop("Thing")
        _get(spec)["responses"]["200"]["content"]["application/json"]["schema"] = {
            "$ref": "#/components/schemas/Item"
        }

    kinds = sorted(f.kind for f in diff_openapi(SPEC, _mutated(SPEC, rename), surface="core"))

    assert kinds == ["component-removed"]


def test_identical_contracts_have_no_findings() -> None:
    contract = Contract(sage_core_api=SPEC, cas_app_api=SPEC, mcp_catalog=CATALOG)

    findings, notes = diff_contracts(contract, copy.deepcopy(contract))

    assert findings == []
    assert notes == []


def test_an_artifact_absent_at_the_base_is_a_note_not_a_finding() -> None:
    """The first commit of a published artifact has no baseline to compare.

    Treating every tool in a newly committed catalog as added would make the
    change that starts publishing the catalog read as a flood of capability,
    which it is not: the tools already existed."""
    base = Contract(sage_core_api=SPEC, cas_app_api=SPEC, mcp_catalog=None)
    head = Contract(sage_core_api=SPEC, cas_app_api=SPEC, mcp_catalog=CATALOG)

    findings, notes = diff_contracts(base, head)

    assert findings == []
    assert len(notes) == 1 and "mcp" in notes[0]


def test_diff_contracts_reports_each_surface_it_compares() -> None:
    """The aggregate reaches all three artifacts, not only the first."""
    base = Contract(sage_core_api=SPEC, cas_app_api=SPEC, mcp_catalog=CATALOG)
    head = Contract(
        sage_core_api=_mutated(SPEC, lambda s: s["paths"]["/things"].pop("post")),
        cas_app_api=_mutated(SPEC, lambda s: _get(s)["responses"].pop("404")),
        mcp_catalog=_mutated(CATALOG, lambda c: c["surfaces"]["sage"].pop(1)),
    )

    findings, _ = diff_contracts(base, head)

    assert {f.surface for f in findings} == {"sage_core_api", "cas_app_api", "mcp"}
