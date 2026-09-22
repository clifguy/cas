"""``param_doc`` publishes a request field's description, once, for both surfaces."""

from __future__ import annotations

import pytest
from pydantic import BaseModel, Field

from sage._mcp_param import param_doc


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
