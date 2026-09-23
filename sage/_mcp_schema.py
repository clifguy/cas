"""Remove framework-generated titles from the published MCP input schemas.

Pydantic gives every property of a tool's argument model a ``title`` derived
from its name (``vault_id`` becomes ``"Vault Id"``) and gives the model itself
one (``"searchArguments"``). A client that forwards the schema whole shows the
model a restatement of each parameter name, paid for on every listing, and a
client that simplifies the schema drops it; neither is told anything. So the
``title`` keyword is removed from every schema node before the tool is
published.

Only the published schema changes. A tool's arguments are validated by the
argument model FastMCP builds from the function signature, which never reads
the published copy, so no call is accepted or refused differently.
"""

from __future__ import annotations

from typing import Any, Final

#: Keys whose value is a single subschema.
_SUBSCHEMA_KEYS: Final[frozenset[str]] = frozenset(
    {"items", "additionalProperties", "not", "contains", "propertyNames"}
)
#: Keys whose value is a list of subschemas.
_SUBSCHEMA_LIST_KEYS: Final[frozenset[str]] = frozenset({"anyOf", "oneOf", "allOf", "prefixItems"})
#: Keys whose value maps names to subschemas; the names are not keywords.
_SUBSCHEMA_MAP_KEYS: Final[frozenset[str]] = frozenset(
    {"properties", "patternProperties", "$defs", "definitions"}
)


def strip_schema_titles(schema: Any) -> Any:
    """``schema`` with the ``title`` keyword removed from every schema node.

    Walks only the keywords whose values are schemas, so a parameter *named*
    ``title`` survives, as does a ``title`` key inside a ``default``, ``enum``
    or ``examples`` value. Returns a new structure; ``schema`` is unchanged.
    """
    if not isinstance(schema, dict):
        return schema
    out: dict[str, Any] = {}
    for key, value in schema.items():
        if key == "title":
            continue
        if key in _SUBSCHEMA_KEYS:
            out[key] = strip_schema_titles(value)
        elif key in _SUBSCHEMA_LIST_KEYS and isinstance(value, list):
            out[key] = [strip_schema_titles(sub) for sub in value]
        elif key in _SUBSCHEMA_MAP_KEYS and isinstance(value, dict):
            out[key] = {name: strip_schema_titles(sub) for name, sub in value.items()}
        else:
            out[key] = value
    return out


def publish_without_titles(server: Any) -> None:
    """Replace each registered tool's published input schema with its untitled form."""
    for tool in server._tool_manager.list_tools():  # noqa: SLF001
        tool.parameters = strip_schema_titles(tool.parameters)
