"""POST /sage_vaults/{vault_id}/parse-filename tests for CAS-ADR-021
(TEST-AD021-010 through TEST-AD021-012).

Validates the side-effect-free filename parser endpoint added in
implementation Chunk 3:

- Returns parsed fields for filenames matching a vault's configured
  pattern.
- Creates no documents and writes nothing to pending_metadata, even
  across repeated calls.
- Returns all-null fields when the vault has no filename_extraction
  pattern configured.

Schema-level coverage of ParseFilenameRequest/ParseFilenameResponse
lives in tests/sage/test_ad021_schemas.py.
"""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from sage.adapters.stubs import (
    StubAbstractionProvider,
    StubContentStore,
    StubEmbeddingProvider,
)
from sage.app import _initialize_services, create_app
from sage.config import VaultConfig
from tests.sage.test_ingestion_metadata_extraction import _pim_vault_config_dict

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


async def _build_app(config: VaultConfig):
    app = create_app(config=config)
    await _initialize_services(
        app,
        config,
        content_store=StubContentStore(),
        embedding_provider=StubEmbeddingProvider(),
        abstraction_provider=StubAbstractionProvider(),
    )
    return app


@pytest.fixture
async def pim_app(tmp_vault_dir):
    """SAGE app whose vault has PIM-style filename_extraction enabled."""
    config = VaultConfig.model_validate(_pim_vault_config_dict(tmp_vault_dir))
    app = await _build_app(config)
    yield app
    await app.state.graph_store.close()


@pytest.fixture
async def no_pattern_app(tmp_vault_dir):
    """SAGE app whose vault has NO filename_extraction block."""
    config_dict = _pim_vault_config_dict(tmp_vault_dir)
    config_dict["metadata_extraction"] = {}
    config = VaultConfig.model_validate(config_dict)
    app = await _build_app(config)
    yield app
    await app.state.graph_store.close()


@pytest.fixture
async def pim_client(pim_app):
    transport = ASGITransport(app=pim_app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.fixture
async def no_pattern_client(no_pattern_app):
    transport = ASGITransport(app=no_pattern_app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


# ---------------------------------------------------------------------------
# TEST-AD021-010: parse-filename returns parsed fields for a filename
# matching the vault's configured pattern.
# ---------------------------------------------------------------------------


async def test_ad021_010_parse_returns_parsed_fields(pim_client):
    resp = await pim_client.post(
        "/sage_vaults/test_metadata_vault/parse-filename",
        json={
            "filename": "2026-03-09_PIM_PV06_Claim-Set_v6.md",
            "source_type": "markdown",
        },
    )
    assert resp.status_code == 200
    body = resp.json()

    assert body["title"] == "Claim-Set"
    assert body["project"] == "PIM"
    assert body["version_label"] == "v6.0"
    assert body["document_date"] == "2026-03-09"
    assert body["doc_type"] == "patent_draft"
    assert body["codes"] == ["PV06"]


# ---------------------------------------------------------------------------
# TEST-AD021-011: parse-filename is side-effect-free. Repeated calls
# create no documents and add nothing to pending_metadata.
# ---------------------------------------------------------------------------


async def test_ad021_011_parse_is_side_effect_free(pim_client, pim_app):
    graph_store = pim_app.state.graph_store

    # Baseline: empty vault.
    documents_before = await graph_store.list_all_documents()
    pending_before = await graph_store.list_pending_metadata_documents()
    assert documents_before == []
    assert pending_before == []

    for _ in range(3):
        resp = await pim_client.post(
            "/sage_vaults/test_metadata_vault/parse-filename",
            json={
                "filename": "2026-03-09_PIM_PV06_Claim-Set_v6.md",
                "source_type": "markdown",
            },
        )
        assert resp.status_code == 200

    documents_after = await graph_store.list_all_documents()
    pending_after = await graph_store.list_pending_metadata_documents()

    assert documents_after == [], "parse-filename must not create document records"
    assert pending_after == [], "parse-filename must not enqueue pending_metadata entries"


# ---------------------------------------------------------------------------
# TEST-AD021-012: parse-filename on a vault without filename_extraction
# returns all parsed fields as null. No exception, no fabricated values.
# ---------------------------------------------------------------------------


async def test_ad021_012_parse_no_pattern_returns_nulls(no_pattern_client):
    resp = await no_pattern_client.post(
        "/sage_vaults/test_metadata_vault/parse-filename",
        json={
            "filename": "2026-03-09_PIM_PV06_Claim-Set_v6.md",
            "source_type": "markdown",
        },
    )
    assert resp.status_code == 200
    body = resp.json()

    assert body["title"] is None
    assert body["project"] is None
    assert body["version_label"] is None
    assert body["document_date"] is None
    assert body["doc_type"] is None
    assert body["codes"] is None
