"""An undeclared_key refusal redirects a misfiled key and marks aliases.

A key refused as undeclared in one batch item may be one a sibling operation on
the same surface accepts. The refusal names that operation and where the key
goes in it (``see_also``), derived from the item models' declared fields and
their declared ownership markers. The accepted set lists canonical names only;
an alias is reported beside it (``aliases``), from the alias marker the item
model declares on the field.

Every MCP test drives ``mcp.call_tool()`` so the refusal is the one a real
caller receives, and the REST arm asserts the two surfaces agree.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient
from mcp.types import TextContent

from sage._tool_naming import SERVER_ASSIGNMENT
from sage.adapters.stubs import (
    StubAbstractionProvider,
    StubContentStore,
    StubEmbeddingProvider,
)
from sage.api.errors import key_owners
from sage.app import create_app
from sage.config import VaultConfig
from sage.mcp_server import _vaults as _mcp_vaults
from sage.mcp_server import mcp
from sage.models.schemas import (
    BATCH_ITEM_MODELS,
    BulkLinkItem,
    BulkMetadataItem,
    canonical_fields,
)
from tests.sage.conftest import initialize_services_for_test

VAULT_ID = "test_vault"
ROOT = Path(__file__).resolve().parents[2]
_DOC = "00000000_absent_document"


@pytest.fixture
async def vault_services(minimal_vault_config_dict, tmp_vault_dir):
    config = VaultConfig.model_validate(minimal_vault_config_dict)
    async with initialize_services_for_test(
        config,
        content_store=StubContentStore(),
        embedding_provider=StubEmbeddingProvider(),
        abstraction_provider=StubAbstractionProvider(),
    ) as services:
        _mcp_vaults[VAULT_ID] = services
        try:
            yield services
        finally:
            _mcp_vaults.pop(VAULT_ID, None)


@pytest.fixture
async def http_client(minimal_vault_config_dict):
    config = VaultConfig.model_validate(minimal_vault_config_dict)
    app = create_app(config=config)
    async with initialize_services_for_test(
        config,
        content_store=StubContentStore(),
        embedding_provider=StubEmbeddingProvider(),
        abstraction_provider=StubAbstractionProvider(),
    ) as services:
        app.state.vault_registry = {VAULT_ID: services}
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            yield client


async def _mcp_refusal(tool: str, item: dict) -> dict:
    result = await mcp.call_tool(tool, {"vault_id": VAULT_ID, "items": [item]})
    assert isinstance(result, list) and isinstance(result[0], TextContent)
    envelope = json.loads(result[0].text)
    assert envelope["error"] == "undeclared_key", envelope
    return envelope


# ---------------------------------------------------------------------------
# see_also
# ---------------------------------------------------------------------------


async def test_lifecycle_status_in_metadata_item_names_update_lifecycles(vault_services):
    envelope = await _mcp_refusal(
        "update_metadata", {"document_id": _DOC, "lifecycle_status": "completed"}
    )
    assert envelope["detail"]["see_also"] == [
        {"key": "lifecycle_status", "operation": "update_lifecycles", "location": "items[].action"}
    ]
    assert "update_lifecycles" in envelope["message"]
    assert "items[].action" in envelope["message"]


async def test_field_name_match_redirects(vault_services):
    """The plain arm: a key that is a declared field of a sibling item model."""
    envelope = await _mcp_refusal("update_metadata", {"document_id": _DOC, "edge_type": "cites"})
    assert envelope["detail"]["see_also"] == [
        {"key": "edge_type", "operation": "create_edges", "location": "items[].edge_type"}
    ]
    assert "create_edges" in envelope["message"]


async def test_key_no_tool_accepts_has_no_see_also(vault_services):
    envelope = await _mcp_refusal("update_metadata", {"document_id": _DOC, "bogus_key": 1})
    assert "see_also" not in envelope["detail"]
    assert "belongs to" not in envelope["message"]


# ---------------------------------------------------------------------------
# aliases
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("tool", ["update_metadata", "update_lifecycles"])
async def test_recognized_marks_doc_id_as_alias(vault_services, tool):
    item = {"document_id": _DOC, "bogus_key": 1}
    if tool == "update_lifecycles":
        item["action"] = "archive"
    detail = (await _mcp_refusal(tool, item))["detail"]
    assert detail["aliases"] == {"doc_id": "document_id"}
    assert "doc_id" not in detail["recognized"]
    assert "document_id" in detail["recognized"]
    assert detail["recognized"] == sorted(canonical_fields(BATCH_ITEM_MODELS[tool]))


async def test_alias_is_named_in_the_message(vault_services):
    envelope = await _mcp_refusal("update_metadata", {"document_id": _DOC, "bogus_key": 1})
    assert "doc_id -> document_id" in envelope["message"]


async def test_model_without_aliases_carries_no_aliases_key(vault_services):
    item = {"source_id": _DOC, "target_id": _DOC, "edge_type": "references", "bogus_key": 1}
    detail = (await _mcp_refusal("create_edges", item))["detail"]
    assert "aliases" not in detail
    assert detail["recognized"] == sorted(BulkLinkItem.model_fields)


# ---------------------------------------------------------------------------
# Both surfaces
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "item",
    [
        {"document_id": _DOC, "lifecycle_status": "completed"},
        {"document_id": _DOC, "bogus_key": 1},
    ],
    ids=["see_also", "aliases"],
)
async def test_rest_and_mcp_agree(vault_services, http_client, item):
    mcp_envelope = await _mcp_refusal("update_metadata", item)
    resp = await http_client.post(f"/sage_vaults/{VAULT_ID}/metadata", json={"items": [item]})
    body = resp.json()
    assert resp.status_code == 400, resp.text
    assert body["code"] == "undeclared_key"
    assert body["detail"] == mcp_envelope["detail"]
    assert body["message"] == mcp_envelope["message"]
    assert "aliases" in body["detail"]
    if "lifecycle_status" in item:
        assert body["detail"]["see_also"]


# ---------------------------------------------------------------------------
# The derivation
# ---------------------------------------------------------------------------


def test_redirect_never_names_the_refusing_operation():
    """Keys each refusing model declares resolve to siblings only, never back to it."""
    for operation, model in BATCH_ITEM_MODELS.items():
        for key in model.model_fields:
            owners = key_owners(key, refusing=model)
            assert operation not in {o["operation"] for o in owners}, (operation, key)
    # document_id is declared by two of the three models: each names the other.
    assert key_owners("document_id", refusing=BulkMetadataItem) == [
        {"key": "document_id", "operation": "update_lifecycles", "location": "items[].document_id"}
    ]


def test_registry_binds_every_items_tool():
    """Every batch-item validation in the tool module reads its model from the registry."""
    tree = ast.parse((ROOT / "sage" / "sage_api_tools.py").read_text())
    bound = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "_validated_items"
        ):
            model = node.args[0]
            assert (
                isinstance(model, ast.Subscript)
                and isinstance(model.value, ast.Name)
                and model.value.id == "BATCH_ITEM_MODELS"
                and isinstance(model.slice, ast.Constant)
            ), ast.unparse(model)
            bound.append(model.slice.value)
    assert sorted(bound) == sorted(BATCH_ITEM_MODELS)
    for operation in BATCH_ITEM_MODELS:
        assert operation in SERVER_ASSIGNMENT


def test_an_alias_redirects_to_its_canonical_location():
    """``doc_id`` refused where no item declares it points where ``document_id`` would."""
    assert key_owners("doc_id", refusing=BulkLinkItem) == [
        {"key": "doc_id", "operation": "update_lifecycles", "location": "items[].document_id"},
        {"key": "doc_id", "operation": "update_metadata", "location": "items[].document_id"},
    ]


def test_a_sibling_on_another_surface_is_not_named(monkeypatch):
    """Only operations on the refusing operation's surface are redirect targets."""
    from sage.api import errors

    monkeypatch.setitem(errors.SERVER_ASSIGNMENT, "update_lifecycles", "sage_maint")
    assert key_owners("lifecycle_status", refusing=BulkMetadataItem) == []
    assert key_owners("edge_type", refusing=BulkMetadataItem) == [
        {"key": "edge_type", "operation": "create_edges", "location": "items[].edge_type"}
    ]
