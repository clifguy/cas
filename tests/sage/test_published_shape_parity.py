"""Typed aliases publish their shape, and both surfaces publish the same one.

A typed alias validates with an ``AfterValidator``, which says nothing to a
schema generator; the alias states its shape to the schema through a
``PublishedShape`` marker instead. The marker is schema-only, so the refusal a
caller receives is still the validator's own code.

``test_mcp_parameter_shapes_match_rest`` is the surface gate: each parameter in
the committed MCP catalog is paired with the field of the same name on its REST
operation in the committed specification, and the two must publish the same
``pattern`` and ``format``. The pairs are read from the catalog and the
specification, so a parameter added to either surface is compared without an
edit here.
"""

from __future__ import annotations

import json
import re
import typing
from typing import Any

import pytest
from pydantic import TypeAdapter, ValidationError

from sage.models import schemas
from sage.models.schemas import PublishedShape
from scripts.dump_mcp_catalog import CATALOG_PATH
from tests.helpers.published_shape import published_shape_of
from tests.sage.test_mcp_tool_conformance import (
    _SURFACES_BY_NAME,
    _find_operation,
    _load_spec,
    _mapped_tool_pairs,
    _resolve_expected_operation_id,
    _resolve_ref,
)

_SHAPE_KEYS = ("pattern", "format")


# ---------------------------------------------------------------------------
# The aliases and their markers
# ---------------------------------------------------------------------------


def _shaped_aliases() -> dict[str, tuple[object, PublishedShape]]:
    """Every alias in the models module that carries a ``PublishedShape``."""
    found: dict[str, tuple[object, PublishedShape]] = {}
    for name, value in vars(schemas).items():
        if typing.get_origin(value) is not typing.Annotated:
            continue
        marker = published_shape_of(value)
        if marker is not None:
            found[name] = (value, marker)
    return found


_ALIASES = _shaped_aliases()

# The five shape-bearing aliases carry a marker today. A floor rather than a
# list, so an alias gaining a marker needs no edit and one losing it fails.
MIN_SHAPED_ALIASES: int = 5


def _non_null_arm(schema: dict[str, Any]) -> dict[str, Any]:
    arms = [a for a in schema.get("anyOf", ()) if a.get("type") != "null"]
    return arms[0] if len(arms) == 1 else schema


def test_shaped_alias_floor() -> None:
    assert len(_ALIASES) >= MIN_SHAPED_ALIASES, sorted(_ALIASES)


@pytest.mark.parametrize("mode", ["validation", "serialization"])
@pytest.mark.parametrize("name", sorted(_ALIASES))
def test_alias_publishes_its_declared_shape(name: str, mode: str) -> None:
    alias, marker = _ALIASES[name]
    published = _non_null_arm(TypeAdapter(alias).json_schema(mode=mode))
    expected: dict[str, str] = {}
    if marker.pattern is not None:
        serialized = mode == "serialization" and marker.serialized_pattern is not None
        expected["pattern"] = marker.serialized_pattern if serialized else marker.pattern
    if marker.format is not None:
        expected["format"] = marker.format
    assert expected, f"{name}: a marker publishing nothing"
    assert {k: published[k] for k in _SHAPE_KEYS if k in published} == expected


@pytest.mark.parametrize("name", sorted(_ALIASES))
def test_refusal_code_is_the_validators_own(name: str) -> None:
    # The marker constrains nothing, so the refusal is the validator's
    # structured code and never a generic pattern or format error. The
    # surface-level envelopes are held by the per-alias refusal tests on
    # each transport; this pins the alias they both run.
    alias, _marker = _ALIASES[name]
    with pytest.raises(ValidationError) as exc:
        TypeAdapter(alias).validate_python("§ not a value of any shape")
    (error,) = exc.value.errors()
    assert error["type"].startswith("invalid_"), error


# ---------------------------------------------------------------------------
# MCP catalog <-> REST specification
# ---------------------------------------------------------------------------


def _catalog_tools() -> dict[str, dict[str, Any]]:
    catalog = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))
    return {tool["name"]: tool for tools in catalog["surfaces"].values() for tool in tools}


def _resolved(spec: dict[str, Any], node: dict[str, Any]) -> dict[str, Any]:
    """``node`` with local references followed and a null arm dropped."""
    while "$ref" in node:
        node = _resolve_ref(spec, node["$ref"])
    for key in ("anyOf", "oneOf"):
        arms = [a for a in node.get(key, ()) if a.get("type") != "null"]
        if len(arms) == 1:
            return _resolved(spec, arms[0])
    return node


def _shape(node: dict[str, Any]) -> dict[str, str]:
    return {k: node[k] for k in _SHAPE_KEYS if k in node}


def _pairs(
    spec: dict[str, Any], mcp: dict[str, Any], rest: dict[str, Any], path: str
) -> list[tuple[str, dict[str, str], dict[str, str]]]:
    """``(path, mcp_shape, rest_shape)`` for every node both schemas declare."""
    mcp, rest = _resolved(spec, mcp), _resolved(spec, rest)
    found = [(path, _shape(mcp), _shape(rest))]
    if "items" in mcp and "items" in rest:
        found.extend(_pairs(spec, mcp["items"], rest["items"], f"{path}[]"))
    rest_props = rest.get("properties") or {}
    for name, prop in (mcp.get("properties") or {}).items():
        if name in rest_props:
            found.extend(_pairs(spec, prop, rest_props[name], f"{path}.{name}"))
    return found


def _operation_fields(spec: dict[str, Any], op: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Each path, query and JSON request-body field of ``op``, by name."""
    fields: dict[str, dict[str, Any]] = {}
    for param in op.get("parameters", []):
        param = _resolved(spec, param)
        if param.get("name"):
            fields[param["name"]] = param.get("schema", {})
    body = _resolved(spec, op.get("requestBody") or {})
    schema = ((body.get("content") or {}).get("application/json") or {}).get("schema")
    if schema:
        fields.update(_resolved(spec, schema).get("properties") or {})
    return fields


_ALIAS_OF_RE = re.compile(r"^Alias for `([a-z_]+)`")


def _shape_rows() -> list[tuple[str, dict[str, str], dict[str, str]]]:
    tools = _catalog_tools()
    rows: list[tuple[str, dict[str, str], dict[str, str]]] = []
    for surface_name, tool_name in _mapped_tool_pairs():
        tool = tools.get(tool_name)
        if tool is None:
            continue
        surface = _SURFACES_BY_NAME[surface_name]
        spec = _load_spec(surface.spec_path)
        op = _find_operation(spec, _resolve_expected_operation_id(surface, tool_name))
        fields = _operation_fields(spec, op or {})
        for name, prop in tool["inputSchema"].get("properties", {}).items():
            # An argument documented as an alias for another stands for that
            # field, so it publishes that field's shape.
            alias = _ALIAS_OF_RE.match(prop.get("description") or "")
            counterpart = name if name in fields else (alias and alias.group(1))
            if counterpart and counterpart in fields:
                rows.extend(_pairs(spec, prop, fields[counterpart], f"{tool_name}.{name}"))
    return rows


_ROWS = _shape_rows()

# Floors against a pairing that silently resolves nothing: the count of paired
# nodes, and at least one pair carrying each kind of shape. 203 nodes pair
# today; the floor sits below that so ordinary movement does not trip it.
MIN_PAIRED_NODES: int = 180
_SHAPES_THAT_MUST_PAIR = ({"format": "uuid"}, {"format": "date"})


def test_mcp_parameter_shapes_match_rest() -> None:
    divergent = [
        f"{path}: MCP {mcp!r} vs REST {rest!r}" for path, mcp, rest in _ROWS if mcp != rest
    ]
    assert not divergent, (
        "An MCP parameter publishes a different shape from its REST counterpart:\n"
        + "\n".join(divergent)
    )


def test_shape_pairing_is_not_vacuous() -> None:
    assert len(_ROWS) >= MIN_PAIRED_NODES, len(_ROWS)
    shaped = [mcp for _path, mcp, _rest in _ROWS if mcp]
    for expected in _SHAPES_THAT_MUST_PAIR:
        assert expected in shaped, f"no paired parameter publishes {expected!r}"
    assert any("pattern" in mcp for mcp in shaped), "no paired parameter publishes a pattern"
    # A top-level alias argument has no REST field of its own name; it is
    # paired through the field it stands for.
    assert any(re.fullmatch(r"\w+\.doc_id", path) for path, _mcp, _rest in _ROWS), (
        "no alias argument was paired"
    )
