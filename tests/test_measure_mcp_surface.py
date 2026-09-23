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
