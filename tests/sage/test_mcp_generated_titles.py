"""Published MCP input schemas carry no framework-generated ``title``.

Pydantic titles every property after its own name and the argument model after
the tool. A client forwarding the raw schema presents each one to the model as
a restatement of a name it already has, on every tool listing.

Anti-coincidental-pass: a walker that never descended into ``items`` or a
union arm would find no title and pass against a schema full of them, so the
walker is checked against a seeded fixture before it is trusted on the live
surfaces; and a strip that deleted every key spelled ``title`` would also pass
the surface check while unpublishing the ``title`` tripwire, so that parameter
is asserted to survive.
"""

from __future__ import annotations

from typing import Any

import pytest

from sage._mcp_schema import strip_schema_titles
from tests.helpers.published_tool import published_tools
from tests.sage.mcp_surface_pin import EXPECTED_SURFACE

_SUBSCHEMA_KEYS = ("items", "additionalProperties", "not", "contains", "propertyNames")
_SUBSCHEMA_LISTS = ("anyOf", "oneOf", "allOf", "prefixItems")
_SUBSCHEMA_MAPS = ("properties", "patternProperties", "$defs", "definitions")


def _titled_nodes(node: Any, path: str = "$") -> list[str]:
    """Every schema node under ``node`` carrying a ``title`` keyword, by path."""
    if not isinstance(node, dict):
        return []
    found = [path] if "title" in node else []
    for key in _SUBSCHEMA_KEYS:
        found += _titled_nodes(node.get(key), f"{path}.{key}")
    # ``items`` may also be a list: the tuple form of older drafts.
    for key in (*_SUBSCHEMA_LISTS, "items"):
        value = node.get(key)
        for i, sub in enumerate(value if isinstance(value, list) else ()):
            found += _titled_nodes(sub, f"{path}.{key}[{i}]")
    for key in _SUBSCHEMA_MAPS:
        for name, sub in (node.get(key) or {}).items():
            found += _titled_nodes(sub, f"{path}.{key}.{name}")
    return found


def _seeded() -> dict[str, Any]:
    return {
        "title": "toolArguments",
        "$defs": {"Mode": {"title": "Mode", "enum": ["a"]}},
        "properties": {
            "title": {"title": "Title", "description": "A parameter named title."},
            "items": {
                "title": "Items",
                "type": "array",
                "items": {
                    "title": "Item",
                    "properties": {"x": {"anyOf": [{"title": "Arm", "type": "string"}]}},
                    "additionalProperties": {"title": "Extra"},
                },
            },
            "mode": {"title": "Mode", "default": {"title": "a default value"}},
            "pair": {"type": "array", "items": [{"title": "First"}, {"type": "string"}]},
        },
    }


def test_walker_finds_every_seeded_title() -> None:
    assert sorted(_titled_nodes(_seeded())) == sorted(
        [
            "$",
            "$.$defs.Mode",
            "$.properties.title",
            "$.properties.items",
            "$.properties.items.items",
            "$.properties.items.items.properties.x.anyOf[0]",
            "$.properties.items.items.additionalProperties",
            "$.properties.mode",
            "$.properties.pair.items[0]",
        ]
    )


def test_strip_removes_the_keyword_and_keeps_names_and_values() -> None:
    stripped = strip_schema_titles(_seeded())

    assert _titled_nodes(stripped) == []
    assert "title" in stripped["properties"], "a parameter named title was unpublished"
    assert stripped["properties"]["title"] == {"description": "A parameter named title."}
    assert stripped["properties"]["mode"]["default"] == {"title": "a default value"}


def test_strip_does_not_mutate_its_input() -> None:
    seeded = _seeded()
    strip_schema_titles(seeded)
    assert seeded == _seeded()


@pytest.mark.parametrize("name", sorted(EXPECTED_SURFACE))
def test_no_published_schema_node_carries_a_title_keyword(name: str) -> None:
    titled = _titled_nodes(published_tools()[name].parameters)
    assert not titled, f"{name} publishes generated titles at {titled}"


def test_title_named_parameters_are_still_published() -> None:
    assert "title" in published_tools()["ingest_document"].parameters["properties"]


def test_unpartitioned_server_publishes_the_same_untitled_schemas() -> None:
    """The module-level server, which the tripwire tests read, is stripped too."""
    from sage.mcp_server import mcp

    for tool in mcp._tool_manager.list_tools():  # noqa: SLF001
        assert _titled_nodes(tool.parameters) == [], tool.name
        assert tool.parameters == published_tools()[tool.name].parameters, tool.name
