"""A null value inside ``ingest_document``'s ``metadata`` means the key was omitted.

An optional request property's null means the same as leaving the property
out, and the same holds for the values inside ``metadata``: a caller that
serializes an absent field as null is accepted, and the ingest behaves exactly
as if the key had not been sent. The case that separates the two readings is a
supersession, where an omitted ``doc_type``, ``project`` or ``authority_scope``
inherits the predecessor's value (CAS-ADR-021); a null that counted as supplied
would decline the inheritance and land on the derived default instead.

Every behavioural test drives a request boundary -- ``mcp.call_tool`` or
``POST /documents`` -- because the refusal this replaces sat at the boundary
and a test against the service alone would never meet it.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml
from httpx import ASGITransport, AsyncClient
from mcp.types import TextContent

from sage import mcp_server as _mcp
from sage.adapters.stubs import (
    StubAbstractionProvider,
    StubContentStore,
    StubEmbeddingProvider,
)
from sage.app import _initialize_services, create_app
from sage.config import VaultConfig
from sage.mcp_server import _vaults as _mcp_vaults
from sage.mcp_server import get_document, mcp
from sage.models.schemas import IngestRequest
from tests.helpers.pipeline_wait import await_tool_idle, drain_vaults
from tests.sage.conftest import initialize_services_for_test

REPO_ROOT = Path(__file__).resolve().parents[2]
OPENAPI_PATH = REPO_ROOT / "docs" / "fs" / "sage" / "sage_core_api.openapi.yaml"

# Predecessor values, each distinct from what the successor would land on
# without inheritance: ``misc`` for doc_type, nothing for the other two.
PREDECESSOR_TRIO = {"doc_type": "memo", "project": "P-pred", "authority_scope": "S-pred"}


# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------


@pytest.fixture
async def vault_services(minimal_vault_config_dict, tmp_vault_dir):
    """Stub-backed services registered as ``test_vault`` on the MCP registry."""
    config = VaultConfig.model_validate(minimal_vault_config_dict)
    async with initialize_services_for_test(
        config,
        content_store=StubContentStore(),
        embedding_provider=StubEmbeddingProvider(),
        abstraction_provider=StubAbstractionProvider(),
    ) as services:
        _mcp_vaults["test_vault"] = services
        test_dir = tmp_vault_dir / "sources" / "test"
        test_dir.mkdir(parents=True, exist_ok=True)
        (test_dir / "pred.md").write_text("# Predecessor\n\nFirst version.")
        (test_dir / "succ.md").write_text("# Successor\n\nSecond version.")
        (test_dir / "first.md").write_text("# First Title\n\nFirst body.")
        (test_dir / "second.md").write_text("# First Title\n\nSecond body.")
        try:
            yield services
        finally:
            _mcp_vaults.pop("test_vault", None)


@pytest.fixture
async def http_app(minimal_vault_config_dict, monkeypatch, tmp_vault_dir):
    """The ASGI app with one stub-backed vault, and a sources directory."""
    monkeypatch.setenv("SAGE_TEST_STUB_PROVIDERS", "1")
    config = VaultConfig.model_validate(minimal_vault_config_dict)
    app = create_app(config=config)
    await _initialize_services(
        app,
        config,
        content_store_factory=lambda _brain: StubContentStore(),
    )
    vault_id = config.vault.id
    test_dir = tmp_vault_dir / "sources" / "test"
    test_dir.mkdir(parents=True, exist_ok=True)

    yield app, vault_id, test_dir

    try:
        await drain_vaults(app.state.vault_registry, [vault_id])
    finally:
        if vault_id in app.state.vault_registry:
            app.state.vault_registry[vault_id].close_timing()
            await app.state.vault_registry[vault_id].close_storage()
        _mcp._vaults.clear()


def _decode_envelope(result) -> dict:
    """The JSON payload of a ``[TextContent]`` ``mcp.call_tool`` return."""
    assert isinstance(result, list) and len(result) == 1, result
    block = result[0]
    assert isinstance(block, TextContent), block
    return json.loads(block.text)


async def _call_ingest(arguments: dict) -> dict:
    return _decode_envelope(
        await mcp.call_tool("ingest_document", {"vault_id": "test_vault", **arguments})
    )


async def _settled(services, doc_id: str) -> dict:
    """The document once its pipeline has finished and released its claim."""

    async def fetch():
        payload = await get_document("test_vault", doc_id)
        return payload if isinstance(payload, dict) else json.loads(payload)

    return await await_tool_idle(
        fetch, doc_id, service=services.ingestion_service, attempts=150, delay=0.02
    )


# ---------------------------------------------------------------------------
# Chain inheritance
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("field", sorted(PREDECESSOR_TRIO))
async def test_null_trio_field_on_supersede_inherits_via_mcp(vault_services, field):
    """A null trio field on a supersession inherits, exactly as an omitted one does."""
    predecessor = await _call_ingest(
        {"source": "test/pred.md", "source_type": "markdown", "metadata": PREDECESSOR_TRIO}
    )
    assert "error" not in predecessor, predecessor
    await _settled(vault_services, predecessor["id"])

    successor = await _call_ingest(
        {
            "source": "test/succ.md",
            "source_type": "markdown",
            "predecessor_id": predecessor["id"],
            "metadata": {field: None},
        }
    )

    assert "error" not in successor, successor
    stored = await _settled(vault_services, successor["id"])
    # MCP omits a null field from the payload, so read with ``get``.
    assert stored.get(field) == PREDECESSOR_TRIO[field]


async def test_null_trio_fields_on_supersede_inherit_via_rest(http_app):
    """The same rule holds on ``POST /documents``, all three fields null at once."""
    app, vault_id, test_dir = http_app
    (test_dir / "pred.md").write_text("# Predecessor\n\nFirst version over HTTP.")
    (test_dir / "succ.md").write_text("# Successor\n\nSecond version over HTTP.")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        seeded = await client.post(
            f"/sage_vaults/{vault_id}/documents",
            json={
                "source": "test/pred.md",
                "source_type": "markdown",
                "metadata": PREDECESSOR_TRIO,
            },
        )
        assert seeded.status_code == 201, seeded.text
        pred_id = seeded.json()["document"]["id"]

        async def fetch_pred():
            return (await client.get(f"/sage_vaults/{vault_id}/documents/{pred_id}")).json()

        await await_tool_idle(
            fetch_pred,
            pred_id,
            service=app.state.vault_registry[vault_id].ingestion_service,
            attempts=150,
            delay=0.02,
        )

        superseded = await client.post(
            f"/sage_vaults/{vault_id}/documents",
            json={
                "source": "test/succ.md",
                "source_type": "markdown",
                "predecessor_id": pred_id,
                "metadata": dict.fromkeys(PREDECESSOR_TRIO),
            },
        )

    assert superseded.status_code == 201, superseded.text
    document = superseded.json()["document"]
    assert {k: document.get(k) for k in PREDECESSOR_TRIO} == PREDECESSOR_TRIO


# ---------------------------------------------------------------------------
# Null equals omission on a fresh ingest
# ---------------------------------------------------------------------------


async def test_null_values_equal_omission_on_fresh_ingest(vault_services):
    """Nulls for the descriptive fields leave the same record omission does."""
    with_nulls = await _call_ingest(
        {
            "source": "test/first.md",
            "source_type": "markdown",
            "metadata": {"title": None, "tags": None, "document_date": None, "doc_type": None},
        }
    )
    omitted = await _call_ingest({"source": "test/second.md", "source_type": "markdown"})

    assert "error" not in with_nulls, with_nulls
    assert "error" not in omitted, omitted
    a = await _settled(vault_services, with_nulls["id"])
    b = await _settled(vault_services, omitted["id"])
    for field in ("title", "doc_type", "tags"):
        assert a[field] == b[field], field
    assert a["title"] == "First Title"
    assert a["doc_type"] == "misc"
    assert a["document_date"]


async def test_null_codes_beside_tags_is_not_a_conflict(vault_services):
    """``codes`` null beside ``tags`` supplies one of the pair, not both."""
    result = await _call_ingest(
        {
            "source": "test/first.md",
            "source_type": "markdown",
            "metadata": {"codes": None, "tags": ["a"]},
        }
    )

    assert "error" not in result, result
    stored = await _settled(vault_services, result["id"])
    assert stored["tags"] == ["a"]


async def test_misplaced_top_level_name_with_null_value_is_still_refused(vault_services):
    """A top-level argument spelled inside ``metadata`` is refused whatever its value."""
    result = await _call_ingest(
        {
            "source": "test/first.md",
            "source_type": "markdown",
            "metadata": {"predecessor_id": None},
        }
    )

    assert result["error"] == "misplaced_top_level_field", result
    assert result["detail"]["fields"] == ["predecessor_id"]


async def test_unknown_metadata_key_is_ignored(vault_services):
    """A key outside the recognized set is accepted and stored nowhere on the record."""
    result = await _call_ingest(
        {
            "source": "test/first.md",
            "source_type": "markdown",
            "metadata": {"custom_key": "custom_value"},
        }
    )

    assert "error" not in result, result
    stored = json.dumps(await _settled(vault_services, result["id"]))
    assert "custom_key" not in stored
    assert "custom_value" not in stored


# ---------------------------------------------------------------------------
# Published contract
# ---------------------------------------------------------------------------


def test_published_metadata_schema_admits_null_values():
    """The spec and the model both admit a null value inside ``metadata``."""
    spec = yaml.safe_load(OPENAPI_PATH.read_text())
    prop = spec["components"]["schemas"]["IngestRequest"]["properties"]["metadata"]
    value_arms = prop["additionalProperties"]["oneOf"]
    assert {"type": "null"} in value_arms, value_arms

    request = IngestRequest.model_validate({"source": "x.md", "metadata": {"title": None}})
    assert request.metadata == {"title": None}
