"""Tests for list_headings: structural-only heading enumeration.

list_headings returns the distinct heading paths of a document in
document order without reading body content. It replaces the
antipattern of calling read_section with a deliberately wrong heading
path to harvest available_headings from the resulting
HeadingNotFoundError detail.

Test cases:
  - Happy path: returns ordered, deduped heading paths.
  - Synthetic header excluded.
  - DocumentNotFoundError on unknown document_id.
  - NoProjectionError when the document exists but has no chunks
    (mirrors read_section's preflight).
  - Trick parity: output equals HeadingNotFoundError.available_headings
    for the same document. This is the load-bearing assertion that lets
    callers retire the wrong-path trick.
  - MCP surface: tool registered in _sage_tools with the right name,
    argument names, and intent-carrying docstring.
"""

import asyncio
import hashlib
import inspect
import re

import pytest

from sage.api.errors import (
    DocumentNotFoundError,
    HeadingNotFoundError,
    NoProjectionError,
)
from sage.models.enums import SourceType
from sage.models.schemas import IngestRequest
from sage.services.ingestion import IngestionService
from sage.services.utilities import UtilitiesService
from sage.source_adapters.markdown_adapter import MarkdownAdapter

_DOC_ID_RE = re.compile(r"^[0-9a-f]{8}_[a-z0-9_]+$")


def _id(name: str) -> str:
    if _DOC_ID_RE.fullmatch(name):
        return name
    return f"{hashlib.sha256(name.encode()).hexdigest()[:8]}_{name}"


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
def listing_services(
    graph_store,
    lock_manager,
    stub_content_store,
    stub_embedding_provider,
    stub_abstraction_provider,
    minimal_config,
    tmp_vault_dir,
):
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
async def multi_section_doc(listing_services, tmp_vault_dir):
    _, ingestion = listing_services

    sources = tmp_vault_dir / "sources"
    test_dir = sources / "test"
    test_dir.mkdir(parents=True, exist_ok=True)
    (test_dir / "multi.md").write_text(MULTI_SECTION_DOC)

    result = await ingestion.ingest(
        IngestRequest(source="test/multi.md", source_type=SourceType.MARKDOWN),
    )
    await asyncio.sleep(0.5)
    return result.document


# ---------------------------------------------------------------------------
# Service: happy path
# ---------------------------------------------------------------------------


async def test_list_headings_returns_ordered_paths(listing_services, multi_section_doc):
    utilities, _ = listing_services

    result = await utilities.list_headings(multi_section_doc.id)

    assert result.document_id == multi_section_doc.id
    assert result.title == multi_section_doc.title
    assert isinstance(result.headings, list)
    assert len(result.headings) > 0

    # Document order: Overview ahead of Technical Description ahead of Conclusion.
    def first_match(needle: str) -> int:
        for i, h in enumerate(result.headings):
            if needle in h:
                return i
        raise AssertionError(f"{needle!r} not in {result.headings!r}")

    assert first_match("Overview") < first_match("Technical Description")
    assert first_match("Technical Description") < first_match("Conclusion")


async def test_list_headings_dedupes(listing_services, multi_section_doc):
    utilities, _ = listing_services

    result = await utilities.list_headings(multi_section_doc.id)

    # No duplicates: each heading path appears at most once.
    assert len(result.headings) == len(set(result.headings))


async def test_list_headings_excludes_synthetic_header(listing_services, multi_section_doc):
    """The synthetic header chunk must not appear in the listing."""
    from sage.adapters.interfaces import SYNTHETIC_HEADER_HEADING_PATH

    utilities, _ = listing_services
    result = await utilities.list_headings(multi_section_doc.id)

    assert SYNTHETIC_HEADER_HEADING_PATH not in result.headings


# ---------------------------------------------------------------------------
# Service: error paths
# ---------------------------------------------------------------------------


async def test_list_headings_document_not_found(listing_services):
    utilities, _ = listing_services

    with pytest.raises(DocumentNotFoundError) as exc_info:
        await utilities.list_headings(_id("unknown_doc"))

    assert exc_info.value.code == "document_not_found"


async def test_list_headings_no_projection(
    graph_store,
    stub_content_store,
    stub_embedding_provider,
    minimal_config,
):
    """A document record with no chunks raises NoProjectionError.

    Mirrors read_section's preflight so the two utility surfaces have the
    same failure shape on a half-ingested document.
    """
    from datetime import datetime, timezone

    from sage.models.enums import PipelineStatus
    from sage.models.enums import SourceType as ST
    from sage.models.schemas import Document

    utilities = UtilitiesService(
        graph_store=graph_store,
        content_store=stub_content_store,
        embedding_provider=stub_embedding_provider,
        config=minimal_config,
    )

    sha = "sha256:" + hashlib.sha256(b"empty").hexdigest()
    now = datetime.now(timezone.utc)
    doc_id = _id("empty_doc")
    await graph_store.insert_document(
        Document(
            id=doc_id,
            title="Empty Doc",
            source_type=ST.MARKDOWN,
            source_path="empty.md",
            source_content_hash=sha,
            adapter_version="0.1.0",
            created_by="test",
            last_modified_by="test",
            lifecycle_status="active",
            pipeline_status=PipelineStatus.ABSTRACTION_COMPLETE.value,
            created_at=now,
            updated_at=now,
        )
    )

    with pytest.raises(NoProjectionError) as exc_info:
        await utilities.list_headings(doc_id)

    assert exc_info.value.code == "no_projection"


# ---------------------------------------------------------------------------
# Trick parity: load-bearing assertion
# ---------------------------------------------------------------------------


async def test_list_headings_matches_heading_not_found_available_headings(
    listing_services, multi_section_doc
):
    """The load-bearing assertion: list_headings replaces the wrong-path trick.

    For the same document, the list_headings response must equal the
    available_headings field returned in the HeadingNotFoundError detail
    from read_section with a deliberately wrong heading path. Future
    readers can retire the trick by pointing at this assertion.
    """
    utilities, _ = listing_services

    listing = await utilities.list_headings(multi_section_doc.id)

    with pytest.raises(HeadingNotFoundError) as exc_info:
        await utilities.read_section(multi_section_doc.id, "__no_such_heading__")

    trick_headings = exc_info.value.detail["available_headings"]

    assert listing.headings == trick_headings


# ---------------------------------------------------------------------------
# MCP surface
# ---------------------------------------------------------------------------


def test_mcp_tool_registered_with_expected_name():
    """list_headings is exposed in the MCP tool registry."""
    from sage import mcp_server

    assert "list_headings" in mcp_server._sage_tools


def test_mcp_tool_top_level_alias():
    """list_headings is re-exported as a module-level attribute on mcp_server,
    matching the convention used for every other tool."""
    from sage import mcp_server

    assert hasattr(mcp_server, "list_headings")
    assert mcp_server.list_headings is mcp_server._sage_tools["list_headings"]


def test_mcp_tool_signature_has_vault_id_document_id_and_doc_id_alias():
    """The tool exposes exactly vault_id, document_id, and the ``doc_id``
    inbound alias for document_id — no other drift."""
    from sage import mcp_server

    tool = mcp_server._sage_tools["list_headings"]
    params = list(inspect.signature(tool).parameters)

    assert params == ["vault_id", "document_id", "doc_id"]


def test_mcp_tool_docstring_retires_wrong_path_trick():
    """The docstring must name the antipattern it replaces, so future
    readers do not re-invent it."""
    from sage import mcp_server

    tool = mcp_server._sage_tools["list_headings"]
    doc = (tool.__doc__ or "").lower()

    # Reference to the antipattern: either 'wrong' or 'deliberately wrong' or
    # 'available_headings' from the error response.
    assert "wrong" in doc or "available_headings" in doc, (
        f"docstring should mention the wrong-path trick it retires; got: {tool.__doc__!r}"
    )
