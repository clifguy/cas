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
import hashlib
import re
from datetime import datetime, timezone

import pytest

from sage.api.errors import DocumentNotFoundError, HeadingNotFoundError, NoProjectionError
from sage.models.enums import SourceType
from sage.models.schemas import Document, IngestRequest
from sage.services.ingestion import IngestionService
from sage.services.utilities import UtilitiesService
from sage.source_adapters.markdown_adapter import MarkdownAdapter

_DOC_ID_RE = re.compile(r"^[0-9a-f]{8}_[a-z0-9_]+$")


def _id(name: str) -> str:
    """Translate a short test name to a shape-conformant document ID.

    The ID validator in sage/models/schemas.py requires the pattern
    ^[0-9a-f]{8}_[a-z0-9_]+$. Test fixtures use short readable names
    like "doc_empty"; this helper wraps them so the values still
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
    graph_store,
    lock_manager,
    stub_content_store,
    stub_embedding_provider,
    stub_abstraction_provider,
    minimal_config,
    tmp_vault_dir,
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
        IngestRequest(source="test/multi.md", source_type=SourceType.MARKDOWN),
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
    result = await utilities.read_section(multi_section_doc.id, "Technical Description")

    assert result.heading_path == "Technical Description"
    # Should include content from child headings
    assert (
        "Composite Claim Binding" in result.section_text or "Binding logic" in result.section_text
    )
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
        await utilities.read_section(multi_section_doc.id, "Nonexistent Section")

    assert exc_info.value.code == "heading_not_found"


async def test_read_section_heading_not_found_lists_available(
    multi_section_service, multi_section_doc
):
    """HeadingNotFoundError detail includes available_headings for the document."""
    utilities, _ = multi_section_service

    with pytest.raises(HeadingNotFoundError) as exc_info:
        await utilities.read_section(multi_section_doc.id, "CLAIMS")

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
        "Expected candidate_matches in heading_not_found detail; got: " + str(detail)
    )
    candidates = detail["candidate_matches"]
    assert candidates, "candidate_matches should not be empty when substring hits exist"
    assert all("composite" in c.lower() for c in candidates)


def test_rank_candidate_matches_prefers_leaf_match_over_content_mention():
    """Leaf-prefix matches outrank parent-segment or buried-substring matches.

    PV13-style data: 'CLAIMS' as a query should surface
    'CLAIMS -- Remove Before Filing' (leaf starts with CLAIMS) before
    'Verification Result: A claims pre-adjudication outcome...' (claims
    appears mid-leaf in a definition entry).
    """
    from sage.services.utilities import _rank_candidate_matches

    available = [
        "Verification Result: A claims pre-adjudication outcome that maps to a code.",
        "CLAIMS -- Remove Before Filing",
        "Definitions > Claim Element: Used in claims for granular billing analysis.",
        "Appendix: Internal Revision History",
    ]
    candidates = _rank_candidate_matches("CLAIMS", available)

    # The exact-leaf-prefix candidate must come first.
    assert candidates[0] == "CLAIMS -- Remove Before Filing"
    # All returned candidates must contain "claims" (case-insensitive).
    assert all("claims" in c.lower() for c in candidates)


def test_rank_candidate_matches_caps_at_max_candidates():
    """When many headings substring-match, return at most the cap (10)."""
    from sage.services.utilities import _MAX_CANDIDATE_MATCHES, _rank_candidate_matches

    available = [f"Definitions > Term {i}: This entry mentions claims." for i in range(50)]
    candidates = _rank_candidate_matches("claims", available)
    assert len(candidates) <= _MAX_CANDIDATE_MATCHES


def test_rank_candidate_matches_returns_empty_for_no_match():
    from sage.services.utilities import _rank_candidate_matches

    available = ["Introduction", "Methods", "Conclusion"]
    assert _rank_candidate_matches("nonexistent", available) == []


def test_rank_candidate_matches_handles_empty_inputs():
    from sage.services.utilities import _rank_candidate_matches

    assert _rank_candidate_matches("", ["heading"]) == []
    assert _rank_candidate_matches("heading", None) == []
    assert _rank_candidate_matches("heading", []) == []


def test_rank_candidate_matches_orders_by_tier_then_length():
    """Within a tier, shorter paths come first (more navigation-like)."""
    from sage.services.utilities import _rank_candidate_matches

    available = [
        "DETAILED DESCRIPTION > Interpretive Conventions > Subsection About Claims",
        "DETAILED DESCRIPTION > Claims",  # tier 0 (leaf eq)
        "Foo > Bar > Baz Claims Long Path Ending Here",  # tier 2 (leaf substring)
        "CLAIMS",  # tier 0 (leaf eq, shorter path)
    ]
    candidates = _rank_candidate_matches("claims", available)
    # Tier 0 entries (exact leaf match) come first; among them the shorter
    # path "CLAIMS" beats "DETAILED DESCRIPTION > Claims".
    assert candidates[0] == "CLAIMS"
    assert candidates[1] == "DETAILED DESCRIPTION > Claims"


async def test_read_section_heading_not_found_no_candidates_when_no_substring_match(
    multi_section_service, multi_section_doc
):
    """If the query has no substring matches in any stored heading_path,
    candidate_matches is omitted from the error detail entirely (rather
    than being an empty list)."""
    utilities, _ = multi_section_service

    with pytest.raises(HeadingNotFoundError) as exc_info:
        await utilities.read_section(multi_section_doc.id, "ZZZ_NoSuchPhrase_QQQ")

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
        id=_id("doc_empty"),
        title="Empty Doc",
        source_type="markdown",
        source_path="test/empty.md",
        source_content_hash=_sha("fake"),
        adapter_version="1.0",
        created_by="test",
        created_at=datetime.now(timezone.utc),
        last_modified_by="test",
        updated_at=datetime.now(timezone.utc),
    )
    await graph_store.insert_document(doc)

    with pytest.raises(NoProjectionError) as exc_info:
        await utilities.read_section(_id("doc_empty"), "Overview")

    assert exc_info.value.code == "no_projection"
