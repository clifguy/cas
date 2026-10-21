"""Semantic tokenizer work limits remain actionable across actual transports."""

import json
from pathlib import Path

import jsonschema
import numpy as np
import pytest
from httpx import ASGITransport, AsyncClient
from mcp.types import TextContent

import sage.mcp_server as mcp_module
from sage.adapters.embedding_nomic import NomicEmbeddingProvider
from sage.adapters.stubs import StubAbstractionProvider, StubContentStore
from sage.adapters.tokenization import MAX_TOKENIZATION_CHARACTERS
from sage.app import _initialize_services, create_app
from sage.config import RetrievalHealthConfig, VaultConfig
from tests.helpers.pipeline_wait import drain_vaults


@pytest.fixture
async def query_vault(minimal_vault_config_dict):
    calls = []

    class Model:
        error = None

        def encode(self, texts, **kwargs):
            calls.extend(texts)
            if self.error:
                raise self.error
            return np.zeros((len(texts), 768))

    provider = object.__new__(NomicEmbeddingProvider)
    provider._model = Model()
    provider._executor = None
    config = VaultConfig.model_validate(minimal_vault_config_dict)
    app = create_app(config=config)
    await _initialize_services(
        app,
        config,
        content_store=StubContentStore(),
        embedding_provider=provider,
        abstraction_provider=StubAbstractionProvider(),
    )
    try:
        yield app, provider, calls
    finally:
        await drain_vaults(app.state.vault_registry)
        for services in app.state.vault_registry.values():
            services.close_timing()
            await services.close_storage()
        mcp_module._vaults.pop("test_vault", None)
        if provider._executor:
            provider._executor.shutdown(wait=True)


async def call_surface(query_vault, surface, *, mode=None, query=None, health=False):
    app, _provider, _calls = query_vault
    arguments = {} if health else {"mode": mode, "query": query}
    operation = "eval-retrieval" if health else "discover"
    if surface == "rest":
        async with AsyncClient(
            transport=ASGITransport(app=app, raise_app_exceptions=False), base_url="http://test"
        ) as client:
            response = await client.post(f"/sage_vaults/test_vault/{operation}", json=arguments)
        return (
            response.status_code,
            response.json() if response.status_code != 500 else response.text,
        )
    blocks = await mcp_module.mcp.call_tool(
        "verify_vault_retrieval" if health else "search", {"vault_id": "test_vault", **arguments}
    )
    assert len(blocks) == 1 and isinstance(blocks[0], TextContent)
    return None, json.loads(blocks[0].text)


async def error_schema(query_vault, surface, tool_name):
    if surface == "rest":
        spec = query_vault[0].openapi()
        return {"$ref": "#/components/schemas/ErrorResponse", "components": spec["components"]}
    tools = {tool.name: tool for tool in await mcp_module.mcp.list_tools()}
    return tools[tool_name].meta["org.sage/errorSchema"]


@pytest.mark.parametrize("surface", ["rest", "mcp"])
async def test_over_limit_semantic_query_has_typed_actionable_refusal(query_vault, surface):
    query = "x" * (MAX_TOKENIZATION_CHARACTERS + 1)
    status, envelope = await call_surface(query_vault, surface, mode="semantic", query=query)
    if surface == "rest":
        assert status == 400
    assert isinstance(envelope, dict)
    assert envelope["code" if surface == "rest" else "error"] == "semantic_query_too_long"
    assert envelope["detail"] == {
        "max_characters": MAX_TOKENIZATION_CHARACTERS,
        "actual_characters": len(query),
    }
    assert "shorten" in envelope["message"].lower()
    assert query not in json.dumps(envelope)
    assert query_vault[2] == [], "refused query must never reach model inference"
    check = jsonschema.Draft202012Validator(await error_schema(query_vault, surface, "search"))
    check.validate(envelope)
    with pytest.raises(jsonschema.ValidationError):
        check.validate({**envelope, "detail": {**envelope["detail"], "actual_characters": "wrong"}})


@pytest.mark.parametrize("surface", ["rest", "mcp"])
@pytest.mark.parametrize("mode,extra", [("semantic", 0), ("keyword", 1)])
async def test_size_controls_preserve_exact_limit_semantic_and_long_keyword(
    query_vault, surface, mode, extra
):
    query = "x" * (MAX_TOKENIZATION_CHARACTERS + extra)
    status, envelope = await call_surface(query_vault, surface, mode=mode, query=query)
    if surface == "rest":
        assert status == 200
    assert "error" not in envelope and "code" not in envelope
    assert envelope["mode"] == mode
    assert query_vault[2] == ([query] if mode == "semantic" else [])


@pytest.mark.parametrize("surface", ["rest", "mcp"])
async def test_unrelated_provider_value_error_remains_internal(query_vault, surface):
    marker = "unrelated model defect"
    query_vault[1]._model.error = ValueError(marker)
    status, envelope = await call_surface(
        query_vault, surface, mode="semantic", query="small query"
    )
    if surface == "rest":
        assert status == 500
        assert envelope == "Internal Server Error"
    else:
        assert envelope["error"] == "internal_error"
    assert marker not in json.dumps(envelope)
    assert query_vault[2] == ["small query"]


@pytest.mark.parametrize("surface", ["rest", "mcp"])
async def test_health_assertion_query_refusal_is_typed_and_published(query_vault, surface):
    config = query_vault[0].state.vault_registry["test_vault"].utilities_service._config
    config.retrieval_health = RetrievalHealthConfig(assertions_file="assertions.yaml")
    query = "x" * (MAX_TOKENIZATION_CHARACTERS + 1)
    source = Path(config.vault.storage_root) / "assertions.yaml"
    source.write_text(
        json.dumps({"assertions": [{"query": query, "expected_document_id": "aaaaaaaa_expected"}]})
    )
    status, envelope = await call_surface(query_vault, surface, health=True)
    if surface == "rest":
        assert status == 400
    assert envelope["code" if surface == "rest" else "error"] == "semantic_query_too_long"
    assert envelope["detail"] == {
        "max_characters": MAX_TOKENIZATION_CHARACTERS,
        "actual_characters": len(query),
    }
    assert query_vault[2] == []
    check = jsonschema.Draft202012Validator(
        await error_schema(query_vault, surface, "verify_vault_retrieval")
    )
    check.validate(envelope)
