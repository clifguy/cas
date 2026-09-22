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

from pydantic import BaseModel, Field


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


def model_param(
    annotation: object, model: type[BaseModel], field: str, *, mcp: str | None = None
) -> object:
    """``annotation`` published with ``param_doc(model, field, mcp=mcp)``."""
    return Annotated[annotation, Field(description=param_doc(model, field, mcp=mcp))]


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
