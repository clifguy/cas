"""Utilities tests: BH-038 through BH-042.

Tests for export_projection (path containment security) and
eval_retrieval (retrieval health assertions from YAML).
"""

import asyncio
import hashlib
import re
from pathlib import Path

import pytest
import yaml

from sage.adapters.stubs import (
    SeededEmbeddingProvider,
    StubAbstractionProvider,
    StubContentStore,
)
from sage.config import VaultConfig
from sage.models.enums import SourceType
from sage.models.schemas import IngestRequest
from sage.services.ingestion import IngestionService
from sage.services.utilities import UtilitiesService
from sage.source_adapters.markdown_adapter import MarkdownAdapter

_DOC_ID_RE = re.compile(r"^[0-9a-f]{8}_[a-z0-9_]+$")


def _id(name: str) -> str:
    """Translate a short test name to a shape-conformant document ID.

    The ID validator in sage/models/schemas.py requires the pattern
    ^[0-9a-f]{8}_[a-z0-9_]+$. Test fixtures use short readable names
    like "doc_no_chunks"; this helper wraps them so the values still
    construct valid Document instances. Idempotent: an already-canonical
    id passes through unchanged so wrapping is safe to apply at every
    call site.
    """
    if _DOC_ID_RE.fullmatch(name):
        return name
    return f"{hashlib.sha256(name.encode()).hexdigest()[:8]}_{name}"


_SHA256_RE = re.compile(r"^sha256:[0-9a-f]{64}$")


def _sha(name: str) -> str:
    """Deterministic canonical Sha256 from a short test name.

    The Sha256Str validator requires `^sha256:[0-9a-f]{64}$`. Test
    fixtures historically used short readable strings like
    f"hash_{doc_id}" or "sha256:abc"; this helper maps any such
    name to a stable canonical Sha256. Idempotent.
    """
    if _SHA256_RE.fullmatch(name):
        return name
    return "sha256:" + hashlib.sha256(f"sage-test-hash:{name}".encode()).hexdigest()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def utilities_service(graph_store, stub_content_store, stub_embedding_provider, minimal_config):
    return UtilitiesService(
        graph_store=graph_store,
        content_store=stub_content_store,
        embedding_provider=stub_embedding_provider,
        config=minimal_config,
    )


@pytest.fixture
async def ingested_doc(
    graph_store,
    lock_manager,
    stub_content_store,
    stub_embedding_provider,
    stub_abstraction_provider,
    minimal_config,
    tmp_vault_dir,
):
    """Ingest a test document and wait for pipeline to complete."""
    # Create test source file
    sources = tmp_vault_dir / "sources"
    test_dir = sources / "test"
    test_dir.mkdir(parents=True, exist_ok=True)
    (test_dir / "sample.md").write_text("# Sample Document\n\nSample content for testing.")

    ingestion = IngestionService(
        graph_store=graph_store,
        lock_manager=lock_manager,
        content_store=stub_content_store,
        embedding_provider=stub_embedding_provider,
        abstraction_provider=stub_abstraction_provider,
        config=minimal_config,
        source_adapters={SourceType.MARKDOWN: MarkdownAdapter()},
    )

    result = await ingestion.ingest(
        IngestRequest(source="test/sample.md", adapter=SourceType.MARKDOWN),
    )

    # Wait for background pipeline to complete
    await asyncio.sleep(0.5)

    return result.document


# ---------------------------------------------------------------------------
# BH-038: export_projection enforces path containment
# ---------------------------------------------------------------------------


async def test_bh038_path_traversal_denied(utilities_service, ingested_doc):
    """Relative path with ../ that escapes storage_root is rejected."""
    from sage.api.errors import PathTraversalDeniedError

    with pytest.raises(PathTraversalDeniedError) as exc_info:
        await utilities_service.export_projection(ingested_doc.id, "../../etc/passwd")

    assert exc_info.value.code == "path_traversal_denied"
    assert exc_info.value.status_code == 400


# ---------------------------------------------------------------------------
# BH-039: export_projection allows valid relative paths
# ---------------------------------------------------------------------------


async def test_bh039_valid_relative_path(utilities_service, ingested_doc, tmp_vault_dir):
    """Relative path within storage_root succeeds and writes file."""
    result = await utilities_service.export_projection(ingested_doc.id, "exports/doc_a.md")

    assert result.document_id == ingested_doc.id
    expected_path = tmp_vault_dir / "sources" / "exports" / "doc_a.md"
    assert result.output_path == str(expected_path)
    assert expected_path.exists()

    content = expected_path.read_text(encoding="utf-8")
    assert len(content) > 0


# ---------------------------------------------------------------------------
# BH-040: export_projection rejects absolute paths outside vault
# ---------------------------------------------------------------------------


async def test_bh040_absolute_path_outside_vault(utilities_service, ingested_doc):
    """Absolute path that doesn't start with storage_root is rejected."""
    from sage.api.errors import PathTraversalDeniedError

    with pytest.raises(PathTraversalDeniedError) as exc_info:
        await utilities_service.export_projection(ingested_doc.id, "/home/user/outside.md")

    assert exc_info.value.code == "path_traversal_denied"
    assert exc_info.value.status_code == 400


# ---------------------------------------------------------------------------
# read_projection: returns full document text with metadata
# ---------------------------------------------------------------------------


async def test_read_projection_returns_text(utilities_service, ingested_doc):
    """read_projection returns projection text and metadata fields."""
    result = await utilities_service.read_projection(ingested_doc.id)

    assert result.document_id == ingested_doc.id
    assert result.title == ingested_doc.title
    assert result.lifecycle_status == ingested_doc.lifecycle_status
    assert result.source_path == ingested_doc.source_path
    assert len(result.projection_text) > 0
    assert "Sample" in result.projection_text


async def test_read_projection_excludes_synthetic_header(utilities_service, ingested_doc):
    """The synthetic header chunk (T-0038) carries title/source/tags/
    abstract for retrieval and must not leak into the exported/read
    projection text."""
    result = await utilities_service.read_projection(ingested_doc.id)

    assert "Identifier tokens:" not in result.projection_text
    # The synthetic header always starts with "Title: " on its first line;
    # the body's first chunk does not, so this is a discriminating check.
    assert not result.projection_text.startswith("Title:")


async def test_export_projection_excludes_synthetic_header(
    utilities_service, ingested_doc, tmp_vault_dir
):
    """export_projection writes the body-only projection — the synthetic
    header chunk content does not appear in the exported file (T-0038)."""
    result = await utilities_service.export_projection(ingested_doc.id, "exports/no_synthetic.md")

    written = Path(result.output_path).read_text(encoding="utf-8")
    assert "Identifier tokens:" not in written
    assert not written.startswith("Title:")


async def test_read_projection_document_not_found(utilities_service):
    """read_projection raises DocumentNotFoundError for nonexistent id."""
    from sage.api.errors import DocumentNotFoundError

    with pytest.raises(DocumentNotFoundError) as exc_info:
        await utilities_service.read_projection("nonexistent_doc_id")

    assert exc_info.value.code == "document_not_found"


async def test_read_projection_no_projection(utilities_service, graph_store):
    """read_projection raises NoProjectionError when no chunks exist."""
    from datetime import datetime, timezone

    from sage.api.errors import NoProjectionError
    from sage.models.schemas import Document

    # Insert a document directly into the graph store without indexing chunks
    doc = Document(
        id=_id("doc_no_chunks"),
        title="No Chunks",
        source_type="markdown",
        source_path="test/no_chunks.md",
        source_content_hash=_sha("fake"),
        adapter_version="1.0",
        created_by="test",
        created_at=datetime.now(timezone.utc),
        last_modified_by="test",
        updated_at=datetime.now(timezone.utc),
    )
    await graph_store.insert_document(doc)

    with pytest.raises(NoProjectionError) as exc_info:
        await utilities_service.read_projection(_id("doc_no_chunks"))

    assert exc_info.value.code == "no_projection"


# ---------------------------------------------------------------------------
# BH-041: Retrieval assertions loaded from separate YAML file
# ---------------------------------------------------------------------------


async def test_bh041_retrieval_assertions_from_yaml(
    graph_store,
    lock_manager,
    tmp_vault_dir,
):
    """Assertions are loaded from YAML, results contain pass/fail per assertion."""
    # Use seeded embedding provider for meaningful search results
    content_store = StubContentStore()
    embedding_provider = SeededEmbeddingProvider()
    abstraction_provider = StubAbstractionProvider()

    # Create assertions file
    sources = tmp_vault_dir / "sources"
    sources.mkdir(parents=True, exist_ok=True)

    # Create test source files
    test_dir = sources / "test"
    test_dir.mkdir(parents=True, exist_ok=True)
    (test_dir / "alpha.md").write_text("# Alpha Document\n\nAlpha content about patents.")
    (test_dir / "beta.md").write_text("# Beta Document\n\nBeta content about trademarks.")

    config_dict = {
        "vault": {
            "id": "test_vault",
            "name": "Test Vault",
            "owner": "testuser",
            "storage_root": str(sources),
            "brain_root": str(tmp_vault_dir / "brain"),
            "visibility": "personal",
        },
        "document_types": {"doc_types": [{"value": "note", "label": "Note"}]},
        "lifecycle": {
            "base_states_required": True,
            "states": [
                {"value": "active", "label": "Active"},
                {"value": "archived", "label": "Archived", "is_terminal": True},
            ],
            "transitions": [
                {"from_state": "(new)", "action": "ingest", "to_state": "active"},
                {"from_state": "active", "action": "archive", "to_state": "archived"},
            ],
        },
        "source_adapters": {"adapters": [{"source_type": "markdown", "enabled": True}]},
        "metadata_extraction": {},
        "edge_inference": {"tier_assignments": []},
        "retrieval_health": {
            "assertions_file": "retrieval_assertions.yaml",
        },
    }
    config = VaultConfig.model_validate(config_dict)

    # Ingest documents
    ingestion = IngestionService(
        graph_store=graph_store,
        lock_manager=lock_manager,
        content_store=content_store,
        embedding_provider=embedding_provider,
        abstraction_provider=abstraction_provider,
        config=config,
        source_adapters={SourceType.MARKDOWN: MarkdownAdapter()},
    )

    result_alpha = await ingestion.ingest(
        IngestRequest(source="test/alpha.md", adapter=SourceType.MARKDOWN),
    )
    doc_alpha = result_alpha.document
    result_beta = await ingestion.ingest(
        IngestRequest(source="test/beta.md", adapter=SourceType.MARKDOWN),
    )
    doc_beta = result_beta.document

    await asyncio.sleep(0.5)

    # Create assertions YAML referencing the actual document IDs
    assertions = {
        "assertions": [
            {
                "query": "Alpha content about patents",
                "expected_document_id": doc_alpha.id,
                "top_k": 10,
            },
            {
                "query": "Beta content about trademarks",
                "expected_document_id": doc_beta.id,
                "top_k": 10,
            },
        ]
    }
    assertions_file = sources / "retrieval_assertions.yaml"
    assertions_file.write_text(yaml.dump(assertions), encoding="utf-8")

    # Run eval_retrieval
    service = UtilitiesService(
        graph_store=graph_store,
        content_store=content_store,
        embedding_provider=embedding_provider,
        config=config,
    )

    result = await service.eval_retrieval()

    assert result.vault_id == "test_vault"
    assert result.assertion_count == 2
    assert isinstance(result.passed, bool)
    assert isinstance(result.failures, list)
    assert result.failure_count == len(result.failures)


# ---------------------------------------------------------------------------
# BH-042: Missing assertions file returns error
# ---------------------------------------------------------------------------


async def test_bh042_missing_assertions_file(
    graph_store,
    stub_content_store,
    stub_embedding_provider,
    tmp_vault_dir,
):
    """Missing assertions file produces clear error."""
    from sage.api.errors import AssertionsFileNotFoundError

    sources = tmp_vault_dir / "sources"
    sources.mkdir(parents=True, exist_ok=True)

    config_dict = {
        "vault": {
            "id": "test_vault",
            "name": "Test Vault",
            "owner": "testuser",
            "storage_root": str(sources),
            "brain_root": str(tmp_vault_dir / "brain"),
            "visibility": "personal",
        },
        "document_types": {"doc_types": [{"value": "note", "label": "Note"}]},
        "lifecycle": {
            "base_states_required": True,
            "states": [{"value": "active", "label": "Active"}],
            "transitions": [
                {"from_state": "(new)", "action": "ingest", "to_state": "active"},
            ],
        },
        "source_adapters": {"adapters": [{"source_type": "markdown", "enabled": True}]},
        "metadata_extraction": {},
        "edge_inference": {"tier_assignments": []},
        "retrieval_health": {
            "assertions_file": "nonexistent.yaml",
        },
    }
    config = VaultConfig.model_validate(config_dict)

    service = UtilitiesService(
        graph_store=graph_store,
        content_store=stub_content_store,
        embedding_provider=stub_embedding_provider,
        config=config,
    )

    with pytest.raises(AssertionsFileNotFoundError) as exc_info:
        await service.eval_retrieval()

    assert exc_info.value.code == "assertions_file_not_found"
    assert exc_info.value.status_code == 400


async def test_bh042_malformed_assertions_file(
    graph_store,
    stub_content_store,
    stub_embedding_provider,
    tmp_vault_dir,
):
    """Malformed assertions file produces clear error."""
    from sage.api.errors import AssertionsFileInvalidError

    sources = tmp_vault_dir / "sources"
    sources.mkdir(parents=True, exist_ok=True)

    # Write a YAML file without the expected structure
    bad_file = sources / "bad_assertions.yaml"
    bad_file.write_text("just_a_string: true\n", encoding="utf-8")

    config_dict = {
        "vault": {
            "id": "test_vault",
            "name": "Test Vault",
            "owner": "testuser",
            "storage_root": str(sources),
            "brain_root": str(tmp_vault_dir / "brain"),
            "visibility": "personal",
        },
        "document_types": {"doc_types": [{"value": "note", "label": "Note"}]},
        "lifecycle": {
            "base_states_required": True,
            "states": [{"value": "active", "label": "Active"}],
            "transitions": [
                {"from_state": "(new)", "action": "ingest", "to_state": "active"},
            ],
        },
        "source_adapters": {"adapters": [{"source_type": "markdown", "enabled": True}]},
        "metadata_extraction": {},
        "edge_inference": {"tier_assignments": []},
        "retrieval_health": {
            "assertions_file": "bad_assertions.yaml",
        },
    }
    config = VaultConfig.model_validate(config_dict)

    service = UtilitiesService(
        graph_store=graph_store,
        content_store=stub_content_store,
        embedding_provider=stub_embedding_provider,
        config=config,
    )

    with pytest.raises(AssertionsFileInvalidError) as exc_info:
        await service.eval_retrieval()

    assert exc_info.value.code == "assertions_file_invalid"
    assert exc_info.value.status_code == 400


async def test_no_assertions_configured(
    graph_store,
    stub_content_store,
    stub_embedding_provider,
    minimal_config,
):
    """No assertions_file in vault config returns error."""
    from sage.api.errors import AssertionsNotConfiguredError

    service = UtilitiesService(
        graph_store=graph_store,
        content_store=stub_content_store,
        embedding_provider=stub_embedding_provider,
        config=minimal_config,
    )

    with pytest.raises(AssertionsNotConfiguredError) as exc_info:
        await service.eval_retrieval()

    assert exc_info.value.code == "assertions_not_configured"


# ---------------------------------------------------------------------------
# Additional export_projection edge cases
# ---------------------------------------------------------------------------


async def test_export_nonexistent_document(utilities_service):
    """Export for nonexistent document returns 404."""
    from sage.api.errors import DocumentNotFoundError

    with pytest.raises(DocumentNotFoundError):
        await utilities_service.export_projection("nonexistent", "output.md")
