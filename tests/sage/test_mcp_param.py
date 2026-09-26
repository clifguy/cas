"""``param_doc`` publishes a request field's description, once, for both surfaces.

``model_param`` and ``shaped_param`` publish the shape a typed alias states
beside it.
"""

from __future__ import annotations

from typing import get_args

import pytest
from pydantic import BaseModel, Field
from pydantic.fields import FieldInfo

from sage._mcp_param import model_param, param_doc, shaped_param
from sage.models.schemas import (
    DOCUMENT_ID_PATTERN,
    SHA256_INPUT_PATTERN,
    DocumentDateStr,
    DocumentIdStr,
    EdgeIdStr,
    Sha256Str,
)


class _Request(BaseModel):
    described: str = Field(description="What the field means.")
    bare: str = ""


def test_returns_the_field_description_verbatim() -> None:
    assert param_doc(_Request, "described") == "What the field means."


def test_appends_mcp_only_text_after_the_shared_description() -> None:
    assert param_doc(_Request, "described", mcp="MCP only.") == "What the field means. MCP only."


@pytest.mark.parametrize("field", ["bare", "absent"])
def test_refuses_a_field_with_nothing_to_publish(field: str) -> None:
    with pytest.raises(LookupError, match=f"_Request.{field}"):
        param_doc(_Request, field)


class _Shaped(BaseModel):
    document_id: DocumentIdStr = Field(description="A document.")
    date: DocumentDateStr = Field(default=None, description="A date.")
    hashes: list[Sha256Str] = Field(description="Some digests.")
    plain: str = Field(description="Free text.")


def _extra(published: object) -> object:
    (info,) = [meta for meta in get_args(published)[1:] if isinstance(meta, FieldInfo)]
    return info.json_schema_extra


@pytest.mark.parametrize(
    ("field", "expected"),
    [
        ("document_id", {"pattern": DOCUMENT_ID_PATTERN}),
        ("date", {"format": "date"}),
        ("hashes", {"items": {"type": "string", "pattern": SHA256_INPUT_PATTERN}}),
        ("plain", None),
    ],
)
def test_model_param_publishes_the_shape_the_field_type_states(field: str, expected) -> None:
    assert _extra(model_param(str, _Shaped, field)) == expected


def test_shaped_param_publishes_the_alias_shape_and_description() -> None:
    published = shaped_param(str, EdgeIdStr, "An edge.")
    (info,) = [meta for meta in get_args(published)[1:] if isinstance(meta, FieldInfo)]
    assert (info.description, info.json_schema_extra) == ("An edge.", {"format": "uuid"})
