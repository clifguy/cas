"""Parameter descriptions for the MCP tool schemas.

An MCP client forwards a tool's ``inputSchema`` to the model alongside its
description, and a parameter's ``description`` there travels with the
parameter: no client description cap reaches it. Parameter documentation is
therefore published on the parameter rather than in the tool docstring.

The text is taken from the request model the REST surface validates, whose
field descriptions are held equal to the published specification, so a
parameter is described once for both surfaces. ``mcp`` appends what only an
MCP caller needs -- an alias, an inline response budget, a delivery channel
the REST surface does not have.
"""

from __future__ import annotations

from typing import Annotated

from pydantic import BaseModel, BeforeValidator, Field, TypeAdapter

from sage.models.schemas import DocumentIdStr, VaultIdStr


def param_doc(model: type[BaseModel], field: str, *, mcp: str | None = None) -> str:
    """The published description of ``model.field``, plus any MCP-only text.

    Raises ``LookupError`` when the field is absent or undescribed, so a
    renamed or undocumented request field fails at import rather than
    publishing an empty parameter description.
    """
    info = model.model_fields.get(field)
    if info is None or not info.description:
        raise LookupError(f"{model.__name__}.{field} has no description to publish")
    return f"{info.description} {mcp}" if mcp else info.description


_SHAPE_KEYS = ("pattern", "format")


def published_shape(annotation: object) -> dict[str, object]:
    """The ``pattern`` and ``format`` that ``annotation`` publishes.

    Read from the schema the annotation renders, so a shape declared by a
    ``Field(pattern=...)`` and one a typed alias states through its marker
    are found alike, and an optional value's null arm is looked past. A list
    publishes its items' shape as its ``items`` schema.
    """
    schema = TypeAdapter(annotation).json_schema()
    arms = [a for a in schema.get("anyOf", ()) if a.get("type") != "null"]
    node = arms[0] if len(arms) == 1 else schema
    shape: dict[str, object] = {k: node[k] for k in _SHAPE_KEYS if k in node}
    items = node.get("items")
    if isinstance(items, dict) and any(k in items for k in _SHAPE_KEYS):
        shape["items"] = items
    return shape


def _described(annotation: object, description: str, shape: dict[str, object]) -> object:
    if not shape:
        return Annotated[annotation, Field(description=description)]
    return Annotated[annotation, Field(description=description, json_schema_extra=shape)]


def model_param(
    annotation: object, model: type[BaseModel], field: str, *, mcp: str | None = None
) -> object:
    """``annotation`` published with ``param_doc(model, field, mcp=mcp)``.

    A request field whose type states a shape publishes that shape on the
    parameter's schema too, so the MCP parameter states the rule the REST
    contract does. The schema annotation constrains nothing at the framework
    boundary: the tool body validates the value against the same request
    model, so a refusal reads the same on both surfaces.
    """
    description = param_doc(model, field, mcp=mcp)
    info = model.model_fields[field]
    # A typed alias's metadata is lifted off the annotation onto the field.
    typed = Annotated[(info.annotation, *info.metadata)] if info.metadata else info.annotation
    shape = published_shape(typed)
    return _described(annotation, description, shape)


def shaped_param(annotation: object, shaped_as: object, description: str) -> object:
    """``annotation`` published with ``description`` and the shape of ``shaped_as``.

    For a parameter with no request-body field to read: a path segment or the
    vault a tool addresses. ``shaped_as`` is the typed alias the REST route
    validates the same value with.
    """
    return _described(annotation, description, published_shape(shaped_as))


VaultIdParam = shaped_param(
    str, VaultIdStr, "Target vault identifier. `list_vaults` names the registered vaults."
)

DocIdAliasParam = shaped_param(
    str | None, DocumentIdStr, "Alias for `document_id`; supply exactly one of the two."
)

#: Appended to a ``document_id`` description that admits the ``doc_id`` alias.
DOC_ID_ALIAS_NOTE = "Alias: `doc_id`; supply exactly one of the two."


def _number_as_text(value: object) -> object:
    """An int or float as the digits it spells; any other value unchanged.

    ``bool`` is an ``int`` subclass and is excluded: ``True`` is not text a
    caller meant to send.
    """
    if isinstance(value, int | float) and not isinstance(value, bool):
        return str(value)
    return value


#: Marks a free-text parameter that reads a JSON number as its string form.
#:
#: A client that ignores the published schema can send an all-digit string as
#: a number, and a lax ``str`` refuses it. Only free text takes this: an
#: identifier does not. A document id always carries an underscore, so a
#: number can never be one; and ``vault_id`` is required and published with a
#: bare type, which no client loses. The published schema is unaffected.
NumericText = BeforeValidator(_number_as_text)
