"""Tests for read_section: section-level text retrieval.

read_section returns clean readable text for a heading subtree,
bridging the gap between read_projection (full document) and
discover deterministic mode (search-formatted chunks).

Test cases:
  - Happy path: returns joined text for a top-level section.
  - Nested heading: returns parent + child chunks.
  - Leaf heading: returns only the matched heading's chunks.
  - Metadata fields: response includes document_id, heading_path, chunk_count.
  - DocumentNotFoundError: nonexistent document_id.
  - HeadingNotFoundError: valid document, nonexistent heading.
  - NoProjectionError: document exists but has no chunks.
"""

import asyncio
from datetime import datetime, timezone

import pytest

from sage.adapters.interfaces import Chunk
from sage.adapters.stubs import StubAbstractionProvider, StubContentStore, StubEmbeddingProvider
from sage.api.errors import DocumentNotFoundError, HeadingNotFoundError, NoProjectionError
from sage.config import VaultConfig
from sage.models.enums import SourceType
from sage.models.schemas import Document, IngestRequest
from sage.services.ingestion import IngestionService
from sage.services.utilities import UtilitiesService
from sage.source_adapters.markdown_adapter import MarkdownAdapter


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

MULTI_SECTION_DOC = """\
# Overview

This is the overview section.

## Background

Background context goes here.

# Technical Description

## Composite Claim Binding

Binding logic for composite claims.

### Implementation Details

The implementation uses a recursive approach.

## Data Flow

Data flows from ingestion to retrieval.

# Conclusion

Final remarks.
"""


@pytest.fixture
def multi_section_service(
    graph_store, lock_manager, stub_content_store, stub_embedding_provider,
    stub_abstraction_provider, minimal_config, tmp_vault_dir,
):
    """Return (utilities_service, ingestion_service) for multi-section tests."""
    ingestion = IngestionService(
        graph_store=graph_store,
        lock_manager=lock_manager,
        content_store=stub_content_store,
        embedding_provider=stub_embedding_provider,
        abstraction_provider=stub_abstraction_provider,
        config=minimal_config,
        source_adapters={SourceType.MARKDOWN: MarkdownAdapter()},
    )
    utilities = UtilitiesService(
        graph_store=graph_store,
        content_store=stub_content_store,
        embedding_provider=stub_embedding_provider,
        config=minimal_config,
    )
    return utilities, ingestion


@pytest.fixture
async def multi_section_doc(multi_section_service, tmp_vault_dir):
    """Ingest a multi-section markdown document."""
    utilities, ingestion = multi_section_service

    sources = tmp_vault_dir / "sources"
    test_dir = sources / "test"
    test_dir.mkdir(parents=True, exist_ok=True)
    (test_dir / "multi.md").write_text(MULTI_SECTION_DOC)

    result = await ingestion.ingest(
        IngestRequest(source="test/multi.md", adapter=SourceType.MARKDOWN),
    )
    await asyncio.sleep(0.5)
    return result.document


# ---------------------------------------------------------------------------
# Happy path: top-level section
# ---------------------------------------------------------------------------

async def test_read_section_top_level(multi_section_service, multi_section_doc):
    """Reading a top-level heading returns its content and children."""
    utilities, _ = multi_section_service
    result = await utilities.read_section(multi_section_doc.id, "Conclusion")

    assert result.document_id == multi_section_doc.id
    assert result.heading_path == "Conclusion"
    assert result.chunk_count >= 1
    assert "Final remarks" in result.section_text


# ---------------------------------------------------------------------------
# Nested heading: parent returns entire subtree
# ---------------------------------------------------------------------------

async def test_read_section_subtree(multi_section_service, multi_section_doc):
    """Reading a parent heading returns all descendant chunks."""
    utilities, _ = multi_section_service
    result = await utilities.read_section(
        multi_section_doc.id, "Technical Description"
    )

    assert result.heading_path == "Technical Description"
    # Should include content from child headings
    assert "Composite Claim Binding" in result.section_text or "Binding logic" in result.section_text
    assert "Data Flow" in result.section_text or "Data flows" in result.section_text
    assert result.chunk_count >= 3


# ---------------------------------------------------------------------------
# Deep heading path: exact path returns only that subtree
# ---------------------------------------------------------------------------

async def test_read_section_deep_path(multi_section_service, multi_section_doc):
    """Reading a deep heading path returns only that subtree."""
    utilities, _ = multi_section_service
    result = await utilities.read_section(
        multi_section_doc.id,
        "Technical Description > Composite Claim Binding",
    )

    assert "Binding logic" in result.section_text or "recursive approach" in result.section_text
    # Should NOT include Data Flow content
    assert "Data flows" not in result.section_text


# ---------------------------------------------------------------------------
# Metadata fields present
# ---------------------------------------------------------------------------

async def test_read_section_metadata(multi_section_service, multi_section_doc):
    """Response includes all expected metadata fields."""
    utilities, _ = multi_section_service
    result = await utilities.read_section(multi_section_doc.id, "Overview")

    assert result.document_id == multi_section_doc.id
    assert result.heading_path == "Overview"
    assert result.title == multi_section_doc.title
    assert isinstance(result.chunk_count, int)
    assert result.chunk_count > 0
    assert isinstance(result.section_text, str)
    assert len(result.section_text) > 0


# ---------------------------------------------------------------------------
# Error: document not found
# ---------------------------------------------------------------------------

async def test_read_section_document_not_found(multi_section_service):
    """Nonexistent document_id raises DocumentNotFoundError."""
    utilities, _ = multi_section_service

    with pytest.raises(DocumentNotFoundError) as exc_info:
        await utilities.read_section("nonexistent_doc_id", "Overview")

    assert exc_info.value.code == "document_not_found"


# ---------------------------------------------------------------------------
# Error: heading not found
# ---------------------------------------------------------------------------

async def test_read_section_heading_not_found(multi_section_service, multi_section_doc):
    """Valid document but nonexistent heading raises HeadingNotFoundError."""
    utilities, _ = multi_section_service

    with pytest.raises(HeadingNotFoundError) as exc_info:
        await utilities.read_section(
            multi_section_doc.id, "Nonexistent Section"
        )

    assert exc_info.value.code == "heading_not_found"


async def test_read_section_heading_not_found_lists_available(
    multi_section_service, multi_section_doc
):
    """HeadingNotFoundError detail includes available_headings for the document."""
    utilities, _ = multi_section_service

    with pytest.raises(HeadingNotFoundError) as exc_info:
        await utilities.read_section(
            multi_section_doc.id, "CLAIMS"
        )

    detail = exc_info.value.detail
    assert "available_headings" in detail
    headings = detail["available_headings"]
    # The multi-section doc has these headings (Technical Description has
    # no standalone chunk -- its content lives in child headings)
    assert "Overview" in headings
    assert "Conclusion" in headings
    assert any("Technical Description" in h for h in headings)


async def test_read_section_heading_not_found_surfaces_substring_candidates(
    multi_section_service, multi_section_doc
):
    """When the query is a tail/middle of a stored path, the error response
    surfaces matching candidate paths so the caller can retry with the
    exact stored path in one extra round-trip. Common case: query
    'Composite' against stored 'Technical Description > Composite Claim Binding'.
    """
    utilities, _ = multi_section_service

    with pytest.raises(HeadingNotFoundError) as exc_info:
        await utilities.read_section(multi_section_doc.id, "Composite")

    detail = exc_info.value.detail
    # The substring "Composite" doesn't equal or left-prefix any stored path
    # (each stored path begins with a top-level heading), so exact match fails.
    # But several stored paths contain "Composite" as a tail/middle segment.
    assert "candidate_matches" in detail, (
        "Expected candidate_matches in heading_not_found detail; got: "
        + str(detail)
    )
    candidates = detail["candidate_matches"]
    assert candidates, "candidate_matches should not be empty when substring hits exist"
    assert all("composite" in c.lower() for c in candidates)


async def test_read_section_heading_not_found_no_candidates_when_no_substring_match(
    multi_section_service, multi_section_doc
):
    """If the query has no substring matches in any stored heading_path,
    candidate_matches is omitted from the error detail entirely (rather
    than being an empty list)."""
    utilities, _ = multi_section_service

    with pytest.raises(HeadingNotFoundError) as exc_info:
        await utilities.read_section(
            multi_section_doc.id, "ZZZ_NoSuchPhrase_QQQ"
        )

    detail = exc_info.value.detail
    assert "available_headings" in detail
    assert "candidate_matches" not in detail


# ---------------------------------------------------------------------------
# Error: document exists but has no chunks
# ---------------------------------------------------------------------------

async def test_read_section_no_projection(multi_section_service, graph_store):
    """Document with no chunks raises NoProjectionError."""
    utilities, _ = multi_section_service

    # Insert a document directly without ingesting chunks
    doc = Document(
        id="doc_empty",
        title="Empty Doc",
        source_type="markdown",
        source_path="test/empty.md",
        source_content_hash="sha256:fake",
        adapter_version="1.0",
        created_by="test",
        created_at=datetime.now(timezone.utc),
        last_modified_by="test",
        updated_at=datetime.now(timezone.utc),
    )
    await graph_store.insert_document(doc)

    with pytest.raises(NoProjectionError) as exc_info:
        await utilities.read_section("doc_empty", "Overview")

    assert exc_info.value.code == "no_projection"
