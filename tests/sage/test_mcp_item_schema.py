"""A batch argument publishes its own description beside its item shape."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

from sage._mcp_item_schema import published_item_list
from tests.helpers.published_tool import published_tool


class _Item(BaseModel):
    """Maintainer prose that must not publish."""

    model_config = ConfigDict(extra="forbid")

    key: str = Field(description="What the key means.")


def _schema(annotation: object) -> dict:
    return TypeAdapter(annotation).json_schema()


def test_description_lands_on_the_array() -> None:
    schema = _schema(published_item_list(_Item, description="The batch."))
    assert schema["type"] == "array"
    assert schema["description"] == "The batch."
    assert "description" not in schema["items"]
    assert "title" not in schema["items"]
    assert schema["items"]["properties"]["key"]["description"] == "What the key means."


def test_no_description_publishes_none() -> None:
    assert "description" not in _schema(published_item_list(_Item))


def test_batch_tools_publish_the_items_description() -> None:
    """On the registered tool, not only from the helper.

    Guards the case where a ``WithJsonSchema`` override and a ``Field``
    description disagree about which one survives into ``inputSchema``.
    """
    for name in ("create_edges", "update_lifecycles", "update_metadata", "bulk_ingest_document"):
        schema = published_tool(name).parameters
        batch = "files" if name == "bulk_ingest_document" else "items"
        assert schema["properties"][batch].get("description", "").strip(), name
