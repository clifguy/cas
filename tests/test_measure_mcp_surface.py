"""The MCP surface measurement renders each client's presented form faithfully.

A size gate is only as honest as the form it measures. The Claude Code form is
transcribed from the tool definitions that client's model receives; a
transform that removed more than the client does (``required``, say) would
report a smaller surface than any model is shown and pass a budget for the
wrong reason, so what the form keeps is asserted as carefully as what it drops.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from scripts.measure_mcp_surface import (
    CLAUDE_CODE_SCHEMA_URI,
    claude_code_form,
    measure,
    presented_schema,
    presented_size,
)


def _item_batch_schema() -> dict[str, Any]:
    """A schema shaped like a published batch tool's, as ``tools/list`` sends it."""
    return {
        "$defs": {"Mode": {"enum": ["light", "full"], "title": "Mode", "type": "string"}},
        "additionalProperties": False,
        "properties": {
            "items": {
                "description": "Items processed in order.",
                "items": {
                    "additionalProperties": True,
                    "properties": {
                        "edge_type": {
                            "description": "Typed relationship.",
                            "enum": ["supersedes", "references"],
                            "title": "Edge Type",
                            "type": "string",
                        },
                        "notes": {
                            "anyOf": [{"type": "string"}, {"type": "null"}],
                            "default": None,
                            "description": "Optional annotation.",
                            "title": "Notes",
                        },
                    },
                    "required": ["edge_type"],
                    "title": "Item",
                    "type": "object",
                },
                "title": "Items",
                "type": "array",
            },
            "limit": {"default": 10, "description": "Cap.", "title": "Limit", "type": "integer"},
            "mode": {
                "anyOf": [{"$ref": "#/$defs/Mode"}, {"type": "null"}],
                "default": None,
                "description": "Depth.",
                "title": "Mode",
            },
        },
        "required": ["items"],
        "title": "batchArguments",
        "type": "object",
    }


def test_claude_code_form_drops_titles_and_unions_keeps_required() -> None:
    presented = claude_code_form(_item_batch_schema())

    assert presented == {
        "$schema": CLAUDE_CODE_SCHEMA_URI,
        "properties": {
            "items": {
                "description": "Items processed in order.",
                "items": {
                    "additionalProperties": {},
                    "properties": {
                        "edge_type": {
                            "description": "Typed relationship.",
                            "enum": ["supersedes", "references"],
                            "type": "string",
                        },
                        "notes": {"default": None, "description": "Optional annotation."},
                    },
                    "required": ["edge_type"],
                    "type": "object",
                },
                "type": "array",
            },
            "limit": {"default": 10, "description": "Cap.", "type": "number"},
            "mode": {"default": None, "description": "Depth."},
        },
        "required": ["items"],
        "type": "object",
    }


def test_claude_code_form_does_not_mutate_its_input() -> None:
    schema = _item_batch_schema()
    before = json.dumps(schema, sort_keys=True)
    claude_code_form(schema)
    assert json.dumps(schema, sort_keys=True) == before


def test_property_named_title_survives() -> None:
    """Only the ``title`` keyword goes; a parameter that is named ``title`` stays."""
    schema = {
        "properties": {
            "title": {"description": "Tripwire.", "title": "Title"},
            "anyOf": {"description": "A parameter with an unlucky name.", "title": "Anyof"},
            "meta": {"default": {"title": "kept", "anyOf": 1}, "description": "A default."},
        },
        "title": "ingestArguments",
        "type": "object",
    }
    presented = claude_code_form(schema)
    assert presented["properties"] == {
        "title": {"description": "Tripwire."},
        "anyOf": {"description": "A parameter with an unlucky name."},
        "meta": {"default": {"title": "kept", "anyOf": 1}, "description": "A default."},
    }
    assert "title" not in presented


def test_raw_form_is_identity() -> None:
    schema = _item_batch_schema()
    assert presented_schema(schema, "raw") is schema


def test_unknown_form_is_refused() -> None:
    with pytest.raises(ValueError, match="unknown form"):
        presented_schema({}, "cowork")


def test_size_is_description_plus_compact_schema() -> None:
    schema = {"type": "object", "properties": {"a": {"type": "string", "title": "A"}}}
    compact = '{"properties":{"a":{"title":"A","type":"string"}},"type":"object"}'
    assert presented_size("Twelve chars", schema, "raw") == 12 + len(compact)
    assert presented_size(None, schema, "raw") == len(compact)
    presented = (
        '{"$schema":"http://json-schema.org/draft-07/schema#",'
        '"properties":{"a":{"type":"string"}},"type":"object"}'
    )
    assert presented_size("", schema, "claude_code") == len(presented)


def _refs_outside_unions(node: Any, in_union: bool = False, path: str = "$") -> list[str]:
    """Paths of every ``$ref`` in ``node`` not nested under an ``anyOf``/``oneOf`` arm."""
    if isinstance(node, list):
        return [
            p
            for i, sub in enumerate(node)
            for p in _refs_outside_unions(sub, in_union, f"{path}[{i}]")
        ]
    if not isinstance(node, dict):
        return []
    found = [path] if "$ref" in node and not in_union else []
    for key, value in node.items():
        if key in ("$defs", "definitions"):
            continue
        found += _refs_outside_unions(value, in_union or key in ("anyOf", "oneOf"), f"{path}.{key}")
    return found


def test_ref_walker_distinguishes_union_arms() -> None:
    schema = {
        "properties": {
            "a": {"anyOf": [{"$ref": "#/$defs/M"}, {"type": "null"}]},
            "b": {"$ref": "#/$defs/M"},
        }
    }
    assert _refs_outside_unions(schema) == ["$.properties.b"]


def test_every_published_ref_sits_inside_a_union() -> None:
    """The Claude Code form drops ``$defs`` because it drops the unions that use them.

    That holds only while every reference sits inside a union arm. A reference
    outside one would be presented dangling and mis-sized, so its appearance on
    either surface fails here rather than skewing the measurement silently.
    """
    from scripts.dump_mcp_catalog import build_catalog

    outside = {
        tool["name"]: refs
        for tools in build_catalog()["surfaces"].values()
        for tool in tools
        if (refs := _refs_outside_unions(tool["inputSchema"]))
    }
    assert not outside, f"references outside a union: {outside}"


def test_report_covers_every_rostered_tool() -> None:
    """Every tool on every surface is measured, and the totals are their sums."""
    from scripts.dump_mcp_catalog import build_catalog
    from tests.sage.mcp_surface_pin import EXPECTED_SURFACE

    report = measure(build_catalog())

    measured = {name: surface for surface, data in report.items() for name in data["tools"]}
    assert measured == EXPECTED_SURFACE
    for data in report.values():
        for form in ("raw", "claude_code"):
            assert data["total"][form] == sum(row[form] for row in data["tools"].values())
            assert all(row["claude_code"] <= row["raw"] + 60 for row in data["tools"].values())
