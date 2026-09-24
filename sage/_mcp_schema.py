"""Shape the published MCP input schemas for the clients that read them.

Pydantic gives every property of a tool's argument model a ``title`` derived
from its name (``vault_id`` becomes ``"Vault Id"``) and gives the model itself
one (``"searchArguments"``). A client that forwards the schema whole shows the
model a restatement of each parameter name, paid for on every listing, and a
client that simplifies the schema drops it; neither is told anything. So the
``title`` keyword is removed from every schema node before the tool is
published.

Pydantic also publishes every optional parameter as a union with null,
``anyOf: [<arm>, {"type": "null"}]``. Some clients discard ``anyOf`` when
they load a tool, and the parameter reaches the model with a description and
no type -- free, then, to send an all-digit string as a JSON number, which the
argument model refuses. So each top-level parameter of that shape is published
as its non-null arm alone. Its null default is kept: a description saying what
a null value does still reads true of the parameter left out, and a null sent
explicitly is still accepted. Each ``$ref`` within the arm is inlined, because
the same clients resolve no references and an enum reached only through one
reaches the model with no values. Item schemas nested inside
a parameter keep their null arms; a client that reads nested structure at all
reads the union.

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
#: Keys whose value is a list of subschemas. ``items`` also takes this form
#: (the tuple form of older drafts) and is walked as a list when it does.
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
        if key in _SUBSCHEMA_KEYS and not isinstance(value, list):
            out[key] = strip_schema_titles(value)
        elif key in (_SUBSCHEMA_LIST_KEYS | {"items"}) and isinstance(value, list):
            out[key] = [strip_schema_titles(sub) for sub in value]
        elif key in _SUBSCHEMA_MAP_KEYS and isinstance(value, dict):
            out[key] = {name: strip_schema_titles(sub) for name, sub in value.items()}
        else:
            out[key] = value
    return out


_NULL_ARM: Final[dict[str, str]] = {"type": "null"}
_DEFS_PREFIX: Final[str] = "#/$defs/"


def _refs_in(node: Any) -> set[str]:
    """The ``$defs`` names every local ``$ref`` under ``node`` points at."""
    if isinstance(node, dict):
        found = set()
        ref = node.get("$ref")
        if isinstance(ref, str) and ref.startswith(_DEFS_PREFIX):
            found.add(ref.removeprefix(_DEFS_PREFIX))
        for value in node.values():
            found |= _refs_in(value)
        return found
    if isinstance(node, list):
        return set().union(*(_refs_in(item) for item in node))
    return set()


def _reachable(schema: dict[str, Any], defs: dict[str, Any]) -> set[str]:
    """The definitions reachable from ``schema`` outside its own ``$defs``."""
    reached = _refs_in({k: v for k, v in schema.items() if k != "$defs"})
    frontier = set(reached)
    while frontier:
        frontier = _refs_in([defs[n] for n in frontier if n in defs]) - reached
        reached |= frontier
    return reached


def _inline_refs(node: Any, defs: dict[str, Any], expanding: frozenset[str]) -> Any:
    """``node`` with each local ``$ref`` replaced by the definition it names.

    A reference node's own keywords are kept over the definition's, and the
    definition's ``description`` is dropped: it describes the type, and the
    property that referenced it carries its own. A definition already being
    expanded on this path is left as its reference, so a recursive definition
    terminates.
    """
    if isinstance(node, list):
        return [_inline_refs(item, defs, expanding) for item in node]
    if not isinstance(node, dict):
        return node
    ref = node.get("$ref")
    if isinstance(ref, str) and ref.startswith(_DEFS_PREFIX):
        name = ref.removeprefix(_DEFS_PREFIX)
        target = defs.get(name)
        if isinstance(target, dict) and name not in expanding:
            body = {k: v for k, v in target.items() if k != "description"}
            rest = {k: v for k, v in node.items() if k != "$ref"}
            return _inline_refs({**body, **rest}, defs, expanding | {name})
    return {key: _inline_refs(value, defs, expanding) for key, value in node.items()}


def _non_null_arm(prop: dict[str, Any], defs: dict[str, Any]) -> dict[str, Any] | None:
    """The arm to publish in place of ``prop``'s union with null, or ``None``.

    Every reference within the arm is inlined: the clients that discard the
    union also resolve no references, and publish none of ``$defs``.
    """
    arms = prop.get("anyOf")
    if not isinstance(arms, list) or len(arms) != 2 or _NULL_ARM not in arms:
        return None
    arm = arms[0] if arms[1] == _NULL_ARM else arms[1]
    return _inline_refs(arm, defs, frozenset())


def flatten_optional_parameters(schema: Any) -> Any:
    """``schema`` with each top-level ``anyOf: [<arm>, null]`` published as ``<arm>``.

    The property keeps its other keywords, its default included. Each ``$ref``
    within the arm is replaced by the definition it names, and a definition
    that inlining leaves unreferenced is dropped. Unions of any other shape,
    and anything below the top level, are unchanged. Returns a new structure;
    ``schema`` is unchanged.
    """
    if not isinstance(schema, dict) or not isinstance(schema.get("properties"), dict):
        return schema
    defs = schema.get("$defs") if isinstance(schema.get("$defs"), dict) else {}
    properties: dict[str, Any] = {}
    for name, prop in schema["properties"].items():
        arm = _non_null_arm(prop, defs) if isinstance(prop, dict) else None
        if arm is None:
            properties[name] = prop
            continue
        rest = {k: v for k, v in prop.items() if k != "anyOf"}
        properties[name] = {**arm, **rest}
    out = {**schema, "properties": properties}
    if "$defs" in out:
        # Drop only the definitions inlining orphaned: ones reachable before
        # the flatten and not after. References between definitions are
        # followed to a fixed point on both sides.
        orphaned = _reachable(schema, defs) - _reachable(out, defs)
        pruned = {n: d for n, d in defs.items() if n not in orphaned}
        if pruned:
            out["$defs"] = pruned
        else:
            del out["$defs"]
    return out


def publish_input_schemas(server: Any) -> None:
    """Replace each registered tool's published input schema with its client-facing form."""
    for tool in server._tool_manager.list_tools():  # noqa: SLF001
        tool.parameters = flatten_optional_parameters(strip_schema_titles(tool.parameters))
