"""Publish the item shape of an MCP batch argument without validating against it.

A batch tool takes its items as plain mappings and validates each against an
item model in the tool body, where a refusal can name the item's position and
the fields it accepts. Typing the argument with the model instead would move
validation into the argument model FastMCP builds, ahead of that refusal. So
the argument keeps its ``list[dict]`` type and only its published JSON Schema
is replaced, with one derived from the item model.

Three things distinguish the published shape from the model's own schema:

* Every reference is resolved in place. The shape is embedded in a tool's
  input schema, where the model's ``$defs`` would not resolve, and a client
  that cannot follow a reference sees the whole shape either way.
* Every object is published open (``additionalProperties: true``) although
  the models forbid undeclared fields. A client that coerces arguments to the
  published schema drops properties a closed object does not declare before
  the call leaves it, which would turn the server's refusal of a misspelled
  key into a silent omission. Left open, the key reaches the server and is
  refused there by name (CAS-ADR-037).
* A definition's own title and description -- a model's or an enum's
  docstring, written for a maintainer -- are left out; each field's
  description is kept, since it is written for the caller.
"""

from __future__ import annotations

from typing import Annotated, Any

from pydantic import BaseModel, WithJsonSchema

#: The keys a definition's docstring and class name populate.
_DEFINITION_PROSE = frozenset({"title", "description"})


def _inline(node: Any, definitions: dict[str, Any]) -> Any:
    """Resolve every ``$ref`` in ``node`` and open every closed object."""
    if isinstance(node, list):
        return [_inline(value, definitions) for value in node]
    if not isinstance(node, dict):
        return node
    if "$ref" in node:
        definition = definitions[node["$ref"].rsplit("/", 1)[-1]]
        target = {key: value for key, value in definition.items() if key not in _DEFINITION_PROSE}
        siblings = {key: value for key, value in node.items() if key != "$ref"}
        return _inline({**target, **siblings}, definitions)
    resolved = {key: _inline(value, definitions) for key, value in node.items()}
    if resolved.get("additionalProperties") is False:
        resolved["additionalProperties"] = True
    return resolved


def item_shape(model: type[BaseModel]) -> dict[str, Any]:
    """The published JSON Schema of one item validated against ``model``."""
    schema = model.model_json_schema(by_alias=True)
    definitions = schema.pop("$defs", {})
    for key in _DEFINITION_PROSE:
        schema.pop(key, None)
    return _inline(schema, definitions)


def published_item_list(model: type[BaseModel]) -> Any:
    """A ``list[dict]`` annotation that publishes ``model`` as its item shape."""
    return Annotated[list[dict], WithJsonSchema({"type": "array", "items": item_shape(model)})]
