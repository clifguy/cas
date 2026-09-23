"""A result field that qualifies a success is named where MCP callers read.

An MCP tool publishes no output schema, so a caller learns what a result field
means only from the tool description. Most fields can go unexplained there
without harm, but some change what a success *means*: a report can say
``status: restored`` while a sibling flag says the record still describes a
different copy. A description condensed past such a field lets the success
read as unqualified.

Such a field declares itself at its definition with the ``x-qualifies-success``
extension, whose value names the status field it qualifies. The declaration
sits on the response-model property in the specification and on the mirrored
Pydantic field, so a new qualifying field is covered from the moment it is
added. Every tool whose JSON success response reaches a declared field must
name it in its published description.

The tool-to-response relation is read from the specification: a tool's paired
operation, and every component schema its 2xx JSON responses reach. A field
reached by no tool -- behind an event stream, or on an MCP-only tool -- would
escape that walk, so every declared field is also required to be reached.

The gate holds a description to *naming* each declared field, not to explaining
it well: a description that mentioned a field without saying what its value
means would pass. What the explanation says is held by the disclosure-parity
gate against the paired operation, and by review.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from typing import Any, Final

import pytest
from pydantic import BaseModel

from sage.models import schemas
from sage.models.enums import ReabstractOutcome
from tests.helpers.published_tool import published_tools
from tests.sage.test_mcp_tool_conformance import (
    _SURFACES_BY_NAME,
    _find_operation,
    _load_spec,
    _mapped_tool_pairs,
    _resolve_expected_operation_id,
    _resolve_ref,
)

MARKER: Final[str] = "x-qualifies-success"

_SCHEMA_REF_PREFIX: Final[str] = "#/components/schemas/"

#: Every declared field today. Pinned so the reach check cannot pass on an
#: empty set, and so adding a declaration is a deliberate, visible change.
EXPECTED_MARKED: Final[frozenset[tuple[str, str]]] = frozenset(
    {
        ("SourceFileRestoreReport", "provenance_verified"),
        ("SourceFileRestoreReport", "record_refreshed"),
    }
)


def _marked_properties(spec: dict[str, Any]) -> dict[tuple[str, str], str]:
    """``{(schema, property): qualified status field}`` for every declared field."""
    found: dict[tuple[str, str], str] = {}
    for schema_name, schema in (spec.get("components", {}).get("schemas") or {}).items():
        for prop, node in (schema.get("properties") or {}).items():
            if isinstance(node, dict) and MARKER in node:
                found[(schema_name, prop)] = node[MARKER]
    return found


def _reachable_schemas(spec: dict[str, Any], node: Any, seen: set[str]) -> Iterator[str]:
    """Names of the component schemas a schema node reaches, transitively."""
    if isinstance(node, dict):
        ref = node.get("$ref")
        if isinstance(ref, str) and ref.startswith(_SCHEMA_REF_PREFIX):
            name = ref[len(_SCHEMA_REF_PREFIX) :]
            if name not in seen:
                seen.add(name)
                yield name
                yield from _reachable_schemas(spec, _resolve_ref(spec, ref), seen)
        for key in ("anyOf", "oneOf", "allOf"):
            for arm in node.get(key) or []:
                yield from _reachable_schemas(spec, arm, seen)
        for child in (node.get("properties") or {}).values():
            yield from _reachable_schemas(spec, child, seen)
        for key in ("items", "additionalProperties"):
            if isinstance(node.get(key), dict):
                yield from _reachable_schemas(spec, node[key], seen)


def _qualifiers_reached(spec: dict[str, Any], operation: dict[str, Any]) -> set[tuple[str, str]]:
    """Declared fields an operation's 2xx JSON responses reach."""
    seen: set[str] = set()
    for status, response in (operation.get("responses") or {}).items():
        if not str(status).startswith("2"):
            continue
        media = (response.get("content") or {}).get("application/json") or {}
        for _ in _reachable_schemas(spec, media.get("schema"), seen):
            pass
    marked = _marked_properties(spec)
    return {key for key in marked if key[0] in seen}


def _tool_qualifiers() -> dict[str, set[tuple[str, str]]]:
    """Declared fields each mapped tool's response reaches, for tools reaching any."""
    reached: dict[str, set[tuple[str, str]]] = {}
    for surface_name, tool_name in _mapped_tool_pairs():
        surface = _SURFACES_BY_NAME[surface_name]
        spec = _load_spec(surface.spec_path)
        operation = _find_operation(spec, _resolve_expected_operation_id(surface, tool_name))
        if operation is None:
            continue
        fields = _qualifiers_reached(spec, operation)
        if fields:
            reached[tool_name] = fields
    return reached


def _unnamed(description: str, fields: set[str]) -> list[str]:
    """Fields a description does not name as a double-backticked identifier."""
    return sorted(f for f in fields if not re.search(rf"``{re.escape(f)}``", description))


_TOOL_QUALIFIERS: Final[dict[str, set[tuple[str, str]]]] = _tool_qualifiers()


def test_some_tool_reaches_a_declared_field() -> None:
    """The per-tool check below is parametrized over a non-empty set."""
    assert "restore_vault_source_file" in _TOOL_QUALIFIERS


@pytest.mark.parametrize("tool_name", sorted(_TOOL_QUALIFIERS))
def test_tool_names_every_success_qualifier(tool_name: str) -> None:
    fields = {prop for _, prop in _TOOL_QUALIFIERS[tool_name]}
    missing = _unnamed(published_tools()[tool_name].description or "", fields)
    assert not missing, (
        f"{tool_name}: its response carries {missing}, which qualify a success status, "
        "but the published description does not name them. MCP callers see no output "
        "schema; say what each value means for the status it qualifies."
    )


def test_every_declared_field_is_reached_by_a_tool() -> None:
    """No declaration escapes the walk, and the declared set is the expected one."""
    declared: set[tuple[str, str]] = set()
    for surface in {s for s, _ in _mapped_tool_pairs()}:
        declared |= set(_marked_properties(_load_spec(_SURFACES_BY_NAME[surface].spec_path)))
    reached = set().union(*_TOOL_QUALIFIERS.values()) if _TOOL_QUALIFIERS else set()
    assert declared == EXPECTED_MARKED
    unreached = sorted(declared - reached)
    assert not unreached, (
        f"declared fields no tool's JSON success response reaches: {unreached}. "
        "A field behind an event stream or on an MCP-only tool needs its tool "
        "mapped here before it can be held to disclosure."
    )


def _pydantic_marked() -> dict[tuple[str, str], str]:
    found: dict[tuple[str, str], str] = {}
    for name, model in vars(schemas).items():
        if not (isinstance(model, type) and issubclass(model, BaseModel)):
            continue
        if model.__module__ != schemas.__name__:
            continue
        for field_name, info in model.model_fields.items():
            extra = info.json_schema_extra
            if isinstance(extra, dict) and MARKER in extra:
                found[(name, field_name)] = extra[MARKER]  # type: ignore[assignment]
    return found


def test_declaration_agrees_between_model_and_spec() -> None:
    """The Pydantic mirror and the specification declare the same fields alike."""
    spec = _load_spec(_SURFACES_BY_NAME["sage_core"].spec_path)
    in_spec = _marked_properties(spec)
    assert _pydantic_marked() == in_spec
    for (schema_name, prop), status_field in in_spec.items():
        properties = spec["components"]["schemas"][schema_name]["properties"]
        assert status_field in properties, (
            f"{schema_name}.{prop} qualifies {status_field!r}, which {schema_name} lacks"
        )
    other = {s for s, _ in _mapped_tool_pairs()} - {"sage_core"}
    for surface in other:
        assert not _marked_properties(_load_spec(_SURFACES_BY_NAME[surface].spec_path)), (
            f"{surface} declares a qualifier; extend the model agreement check to it"
        )


@pytest.mark.parametrize("combinator", ["anyOf", "oneOf", "allOf"])
def test_walk_reaches_a_declared_field_through_a_combinator_and_ref(combinator: str) -> None:
    """A declared field behind a union arm and a nested reference is reached.

    Only a 2xx response counts: the same marker behind the 400 is not reached.
    """
    spec = {
        "components": {
            "schemas": {
                "Outer": {"properties": {"inner": {"$ref": "#/components/schemas/Inner"}}},
                "Inner": {
                    "properties": {
                        "status": {"type": "string"},
                        "flag": {"type": "boolean", MARKER: "status"},
                    }
                },
                "Unreached": {"properties": {"flag": {"type": "boolean", MARKER: "status"}}},
            }
        }
    }
    operation = {
        "responses": {
            "200": {
                "content": {
                    "application/json": {
                        "schema": {combinator: [{"$ref": "#/components/schemas/Outer"}]}
                    }
                }
            },
            "400": {
                "content": {
                    "application/json": {"schema": {"$ref": "#/components/schemas/Unreached"}}
                }
            },
        }
    }
    assert _qualifiers_reached(spec, operation) == {("Inner", "flag")}


def test_naming_check_has_teeth() -> None:
    """Only a double-backticked name counts; a bare mention does not."""
    description = "``record_refreshed`` false means...; provenance_verified is also returned."
    assert _unnamed(description, {"record_refreshed", "provenance_verified"}) == [
        "provenance_verified"
    ]


def test_recompute_deferred_description_names_every_outcome() -> None:
    """Both surfaces name each per-document outcome, derived from its definition."""
    values = [o.value for o in ReabstractOutcome]
    description = published_tools()["recompute_deferred_vault_abstracts"].description or ""
    missing = [v for v in values if f"``{v}``" not in description]
    assert not missing, f"tool description omits outcomes {missing}"

    surface = _SURFACES_BY_NAME["sage_core"]
    spec = _load_spec(surface.spec_path)
    op_id = _resolve_expected_operation_id(surface, "recompute_deferred_vault_abstracts")
    op_description = (_find_operation(spec, op_id) or {}).get("description") or ""
    missing_spec = [v for v in values if not re.search(rf"`{re.escape(v)}`", op_description)]
    assert not missing_spec, f"operation description omits outcomes {missing_spec}"
