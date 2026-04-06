"""MCP adapter tests.

Verifies that each MCP tool correctly translates to the underlying SAGE
service calls, returns well-formed JSON, and propagates errors as
structured error responses rather than exceptions.

Tests call the tool functions directly (bypassing MCP transport) with
a pre-initialized vault registry, matching how the existing test suite
tests services directly rather than through HTTP.
"""

import asyncio
import json
from pathlib import Path

import pytest

from sage.config import VaultConfig
from sage.mcp_init import initialize_services
from sage.mcp_server import (
    _vaults,
    sage_check_preconditions,
    sage_discover,
    sage_export_projection,
    sage_get_document,
    sage_ingest,
    sage_link,
    sage_refresh_views,
    sage_register_user,
    sage_set_lifecycle,
    sage_traverse,
    sage_update_metadata,
)


@pytest.fixture
async def vault_services(minimal_vault_config_dict, tmp_vault_dir):
    """Initialize SAGE services and register them in the MCP vault registry."""
    config = VaultConfig.model_validate(minimal_vault_config_dict)
    services = await initialize_services(config)
    _vaults["test_vault"] = services

    # Create a test source file
    sources = tmp_vault_dir / "sources"
    test_dir = sources / "test"
    test_dir.mkdir(parents=True, exist_ok=True)
    (test_dir / "sample.md").write_text("# Sample Document\n\nSample content.")
    (test_dir / "second.md").write_text("# Second Document\n\nDifferent content.")

    yield services

    await asyncio.sleep(0.5)
    await services.graph_store.close()
    _vaults.pop("test_vault", None)


def _parse(result: str) -> dict:
    """Parse a tool's JSON string result."""
    return json.loads(result)


# ---------------------------------------------------------------------------
# Vault routing
# ---------------------------------------------------------------------------


async def test_unknown_vault_returns_error(vault_services):
    result = _parse(await sage_get_document("nonexistent_vault", "doc1"))
    assert result["error"] == "unknown_vault"
    assert "nonexistent_vault" in result["message"]


async def test_unknown_vault_lists_available(vault_services):
    result = _parse(await sage_get_document("nonexistent_vault", "doc1"))
    assert "test_vault" in result["message"]


# ---------------------------------------------------------------------------
# Ingestion
# ---------------------------------------------------------------------------


async def test_ingest_returns_document(vault_services):
    result = _parse(await sage_ingest("test_vault", "test/sample.md", "markdown"))
    assert "id" in result
    assert result["source_path"] == "test/sample.md"
    assert result["source_type"] == "markdown"


async def test_ingest_duplicate_returns_error(vault_services):
    await sage_ingest("test_vault", "test/sample.md", "markdown")
    await asyncio.sleep(0.1)
    result = _parse(await sage_ingest("test_vault", "test/sample.md", "markdown"))
    assert result["error"] == "duplicate_content"


async def test_ingest_force_bypasses_duplicate(vault_services):
    await sage_ingest("test_vault", "test/sample.md", "markdown")
    await asyncio.sleep(0.1)
    result = _parse(
        await sage_ingest("test_vault", "test/sample.md", "markdown", force=True)
    )
    assert "id" in result
    assert "error" not in result


async def test_ingest_missing_file_returns_error(vault_services):
    result = _parse(await sage_ingest("test_vault", "no/such/file.md", "markdown"))
    assert result["error"] == "source_file_not_found"


# ---------------------------------------------------------------------------
# Get document
# ---------------------------------------------------------------------------


async def test_get_document_returns_full_record(vault_services):
    ingest_result = _parse(
        await sage_ingest("test_vault", "test/sample.md", "markdown")
    )
    doc_id = ingest_result["id"]

    result = _parse(await sage_get_document("test_vault", doc_id))
    assert result["id"] == doc_id
    assert result["title"] == "Sample Document"


async def test_get_document_not_found(vault_services):
    result = _parse(await sage_get_document("test_vault", "nonexistent"))
    assert result["error"] == "document_not_found"


# ---------------------------------------------------------------------------
# Update metadata
# ---------------------------------------------------------------------------


async def test_update_metadata_partial(vault_services):
    ingest_result = _parse(
        await sage_ingest("test_vault", "test/sample.md", "markdown")
    )
    doc_id = ingest_result["id"]

    result = _parse(
        await sage_update_metadata(
            "test_vault",
            doc_id,
            title="Renamed Document",
            tags=["alpha", "beta"],
            doc_type="note",
        )
    )
    assert result["title"] == "Renamed Document"
    assert result["tags"] == ["alpha", "beta"]
    assert result["doc_type"] == "note"


async def test_update_metadata_invalid_doc_type(vault_services):
    ingest_result = _parse(
        await sage_ingest("test_vault", "test/sample.md", "markdown")
    )
    doc_id = ingest_result["id"]

    result = _parse(
        await sage_update_metadata("test_vault", doc_id, doc_type="invalid_type")
    )
    assert result["error"] == "invalid_doc_type"


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------


async def test_set_lifecycle_archive(vault_services):
    ingest_result = _parse(
        await sage_ingest("test_vault", "test/sample.md", "markdown")
    )
    doc_id = ingest_result["id"]

    result = _parse(await sage_set_lifecycle("test_vault", doc_id, "archive"))
    assert result["document"]["lifecycle_status"] == "archived"


async def test_set_lifecycle_invalid_transition(vault_services):
    ingest_result = _parse(
        await sage_ingest("test_vault", "test/sample.md", "markdown")
    )
    doc_id = ingest_result["id"]

    result = _parse(await sage_set_lifecycle("test_vault", doc_id, "reactivate"))
    assert result["error"] == "invalid_lifecycle_transition"
    assert "valid_actions" in result["detail"]


async def test_set_lifecycle_unknown_action(vault_services):
    ingest_result = _parse(
        await sage_ingest("test_vault", "test/sample.md", "markdown")
    )
    doc_id = ingest_result["id"]

    result = _parse(await sage_set_lifecycle("test_vault", doc_id, "explode"))
    assert result["error"] == "invalid_action"


# ---------------------------------------------------------------------------
# User registration
# ---------------------------------------------------------------------------


async def test_register_user(vault_services):
    result = _parse(
        await sage_register_user("test_vault", "test_agent", "agent")
    )
    assert result["display_name"] == "test_agent"
    assert result["type"] == "agent"
    assert "id" in result


# ---------------------------------------------------------------------------
# Graph operations: link
# ---------------------------------------------------------------------------


async def test_link_creates_edge(vault_services):
    doc_a = _parse(await sage_ingest("test_vault", "test/sample.md", "markdown"))
    doc_b = _parse(await sage_ingest("test_vault", "test/second.md", "markdown"))
    await asyncio.sleep(0.3)

    result = _parse(
        await sage_link(
            "test_vault",
            doc_a["id"],
            doc_b["id"],
            "references",
            rationale="test link",
        )
    )
    assert result["source_id"] == doc_a["id"]
    assert result["target_id"] == doc_b["id"]
    assert result["edge_type"] == "references"
    assert "id" in result


async def test_link_self_referential_error(vault_services):
    doc = _parse(await sage_ingest("test_vault", "test/sample.md", "markdown"))
    await asyncio.sleep(0.3)

    result = _parse(
        await sage_link("test_vault", doc["id"], doc["id"], "references")
    )
    assert result["error"] == "self_referential_edge"


# ---------------------------------------------------------------------------
# Graph operations: check_preconditions
# ---------------------------------------------------------------------------


async def test_check_preconditions_no_deps(vault_services):
    doc = _parse(await sage_ingest("test_vault", "test/sample.md", "markdown"))
    await asyncio.sleep(0.3)

    result = _parse(await sage_check_preconditions("test_vault", doc["id"]))
    assert result["function_id"] == doc["id"]
    assert result["satisfied"] is True
    assert result["checks"] == []


# ---------------------------------------------------------------------------
# Graph operations: traverse
# ---------------------------------------------------------------------------


async def test_traverse_returns_nodes(vault_services):
    doc_a = _parse(await sage_ingest("test_vault", "test/sample.md", "markdown"))
    doc_b = _parse(await sage_ingest("test_vault", "test/second.md", "markdown"))
    await asyncio.sleep(0.3)
    await sage_link("test_vault", doc_a["id"], doc_b["id"], "references")

    result = _parse(await sage_traverse("test_vault", doc_a["id"]))
    assert result["start_id"] == doc_a["id"]
    assert len(result["nodes"]) == 1
    assert result["nodes"][0]["document"]["id"] == doc_b["id"]


async def test_traverse_no_edges(vault_services):
    doc = _parse(await sage_ingest("test_vault", "test/sample.md", "markdown"))
    await asyncio.sleep(0.3)

    result = _parse(await sage_traverse("test_vault", doc["id"]))
    assert result["start_id"] == doc["id"]
    assert result["nodes"] == []


# ---------------------------------------------------------------------------
# Retrieval: discover
# ---------------------------------------------------------------------------


async def test_discover_semantic(vault_services):
    await sage_ingest("test_vault", "test/sample.md", "markdown")
    await asyncio.sleep(0.5)

    result = _parse(
        await sage_discover("test_vault", "semantic", query="sample content")
    )
    assert result["mode"] == "semantic"
    assert isinstance(result["results"], list)


async def test_discover_deterministic(vault_services):
    doc = _parse(await sage_ingest("test_vault", "test/sample.md", "markdown"))
    await asyncio.sleep(0.5)

    result = _parse(
        await sage_discover(
            "test_vault",
            "deterministic",
            document_id=doc["id"],
            heading_path="Sample Document",
        )
    )
    assert result["mode"] == "deterministic"
    assert len(result["results"]) > 0


async def test_discover_semantic_missing_query(vault_services):
    result = _parse(await sage_discover("test_vault", "semantic"))
    assert result["error"] == "missing_query"


# ---------------------------------------------------------------------------
# Utilities: export_projection
# ---------------------------------------------------------------------------


async def test_export_projection(vault_services):
    doc = _parse(await sage_ingest("test_vault", "test/sample.md", "markdown"))
    await asyncio.sleep(0.5)

    result = _parse(
        await sage_export_projection("test_vault", doc["id"], "exports/out.md")
    )
    assert result["document_id"] == doc["id"]
    assert "exports/out.md" in result["output_path"]


async def test_export_projection_path_traversal(vault_services):
    doc = _parse(await sage_ingest("test_vault", "test/sample.md", "markdown"))
    await asyncio.sleep(0.5)

    result = _parse(
        await sage_export_projection("test_vault", doc["id"], "../../etc/passwd")
    )
    assert result["error"] == "path_traversal_denied"


# ---------------------------------------------------------------------------
# Utilities: refresh_views
# ---------------------------------------------------------------------------


async def test_refresh_views(vault_services):
    await sage_ingest("test_vault", "test/sample.md", "markdown")

    result = _parse(await sage_refresh_views("test_vault"))
    assert result["vault_id"] == "test_vault"
    assert isinstance(result["views_generated"], int)
    assert result["views_generated"] >= 1
