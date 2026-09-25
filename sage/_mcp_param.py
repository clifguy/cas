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

from typing import Annotated, get_args

from pydantic import BaseModel, BeforeValidator, Field


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


def _field_pattern(model: type[BaseModel], field: str) -> str | None:
    """The ``pattern`` constraint on ``model.field``, if it carries one.

    Read from the field's own metadata and, for an optional field, from the
    typed alias inside its union, so the MCP parameter publishes the same
    rule the REST contract does.
    """
    info = model.model_fields[field]
    candidates = list(info.metadata)
    for arg in get_args(info.annotation):
        candidates.extend(getattr(arg, "__metadata__", ()))
    for item in candidates:
        for meta in getattr(item, "metadata", [item]):
            pattern = getattr(meta, "pattern", None)
            if pattern:
                return pattern
    return None


def model_param(
    annotation: object, model: type[BaseModel], field: str, *, mcp: str | None = None
) -> object:
    """``annotation`` published with ``param_doc(model, field, mcp=mcp)``.

    A request field constrained by a pattern publishes that pattern on the
    parameter's schema too. The schema annotation constrains nothing at the
    framework boundary: the tool body validates the value against the same
    request model, so a refusal reads the same on both surfaces.
    """
    description = param_doc(model, field, mcp=mcp)
    pattern = _field_pattern(model, field)
    if pattern is None:
        return Annotated[annotation, Field(description=description)]
    return Annotated[
        annotation, Field(description=description, json_schema_extra={"pattern": pattern})
    ]


VaultIdParam = Annotated[
    str,
    Field(description="Target vault identifier. `list_vaults` names the registered vaults."),
]

DocIdAliasParam = Annotated[
    str | None,
    Field(description="Alias for `document_id`; supply exactly one of the two."),
]

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
