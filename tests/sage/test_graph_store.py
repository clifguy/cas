"""Graph Store Foundation tests: BH-001 through BH-008.

Covers document ID generation, SQLite WAL mode, per-document concurrency,
and indexed_at nullable semantics.
"""

import asyncio
import re
import uuid
from datetime import datetime, timezone

from sage.models.enums import PipelineStatus, SourceType
from sage.models.schemas import Document
from sage.services.identity import generate_document_id

# ---------------------------------------------------------------------------
# BH-001: Document ID format
# ---------------------------------------------------------------------------


def test_bh_001_document_id_format():
    """Doc ID matches pattern ^[0-9a-f]{8}_[a-z0-9_]+$"""
    doc_id = generate_document_id(
        source_path="patents/2026-03-09_PIM_PV06_Claim_Set_v6_12.docx",
        created_at="2026-03-09T10:00:00Z",
        title="Claim Set",
    )
    assert re.match(r"^[0-9a-f]{8}_[a-z0-9_]+$", doc_id)
    # Title fragment is derived from title, not full filename
    assert "claim_set" in doc_id


# ---------------------------------------------------------------------------
# BH-002: Document ID uniqueness -- different paths, same timestamp
# ---------------------------------------------------------------------------


def test_bh_002_different_paths_different_ids():
    ts = "2026-03-09T10:00:00Z"
    id_a = generate_document_id("patents/doc_a.docx", ts, "Doc A")
    id_b = generate_document_id("patents/doc_b.docx", ts, "Doc B")
    assert id_a != id_b


# ---------------------------------------------------------------------------
# BH-003: Document ID uniqueness -- same path, different timestamps
# ---------------------------------------------------------------------------


def test_bh_003_same_path_different_timestamps():
    path = "patents/doc_a.docx"
    id_1 = generate_document_id(path, "2026-03-09T10:00:00Z", "Doc A")
    id_2 = generate_document_id(path, "2026-03-09T11:00:00Z", "Doc A")
    assert id_1 != id_2


# ---------------------------------------------------------------------------
# BH-004: SQLite WAL mode enabled at startup
# ---------------------------------------------------------------------------


async def test_bh_004_wal_mode_enabled(graph_store):
    mode = await graph_store.get_journal_mode()
    assert mode == "wal"


# ---------------------------------------------------------------------------
# BH-005: Concurrent writes to different documents succeed
# ---------------------------------------------------------------------------


async def test_bh_005_concurrent_writes_different_docs(graph_store, lock_manager):
    """Both concurrent update_metadata calls complete without blocking."""
    now = datetime.now(timezone.utc)
    docs = []
    for suffix in ("a", "b"):
        doc = Document(
            id=f"doc_{suffix}",
            title=f"Doc {suffix.upper()}",
            source_type=SourceType.MARKDOWN,
            source_path=f"test/doc_{suffix}.md",
            source_content_hash=f"hash_{suffix}",
            adapter_version="0.1.0",
            created_by="testuser",
            created_at=now,
            last_modified_by="testuser",
            updated_at=now,
            projected_at=now,
            pipeline_status=PipelineStatus.ABSTRACTION_COMPLETE,
        )
        await graph_store.insert_document(doc)
        docs.append(doc)

    async def update_doc(doc_id: str, new_title: str):
        async with lock_manager.lock(doc_id):
            await graph_store.update_document(doc_id, {"title": new_title})

    # Both should complete concurrently
    results = await asyncio.gather(
        update_doc("doc_a", "Updated A"),
        update_doc("doc_b", "Updated B"),
        return_exceptions=True,
    )
    assert all(r is None for r in results)

    doc_a = await graph_store.get_document("doc_a")
    doc_b = await graph_store.get_document("doc_b")
    assert doc_a.title == "Updated A"
    assert doc_b.title == "Updated B"


# ---------------------------------------------------------------------------
# BH-006: Concurrent writes to the same document serialize
# ---------------------------------------------------------------------------


async def test_bh_006_concurrent_writes_same_doc(graph_store, lock_manager):
    """Two concurrent writes to the same doc serialize; no SQLITE_BUSY."""
    now = datetime.now(timezone.utc)
    doc = Document(
        id="doc_shared",
        title="Shared",
        source_type=SourceType.MARKDOWN,
        source_path="test/shared.md",
        source_content_hash="hash_shared",
        adapter_version="0.1.0",
        created_by="testuser",
        created_at=now,
        last_modified_by="testuser",
        updated_at=now,
        projected_at=now,
        pipeline_status=PipelineStatus.ABSTRACTION_COMPLETE,
    )
    await graph_store.insert_document(doc)

    call_order = []

    async def update_with_tracking(label: str, new_title: str):
        async with lock_manager.lock("doc_shared"):
            call_order.append(f"{label}_start")
            await graph_store.update_document("doc_shared", {"title": new_title})
            call_order.append(f"{label}_end")

    results = await asyncio.gather(
        update_with_tracking("first", "Title 1"),
        update_with_tracking("second", "Title 2"),
        return_exceptions=True,
    )
    # Neither call should raise
    assert all(r is None for r in results)

    # Verify serialization: one completes before the other starts
    assert call_order == [
        "first_start",
        "first_end",
        "second_start",
        "second_end",
    ] or call_order == ["second_start", "second_end", "first_start", "first_end"]


# ---------------------------------------------------------------------------
# BH-007: indexed_at is null before indexing completes
# ---------------------------------------------------------------------------


async def test_bh_007_indexed_at_null_before_indexing(graph_store):
    now = datetime.now(timezone.utc)
    doc = Document(
        id="doc_unindexed",
        title="Unindexed",
        source_type=SourceType.MARKDOWN,
        source_path="test/unindexed.md",
        source_content_hash="hash_unindexed",
        adapter_version="0.1.0",
        created_by="testuser",
        created_at=now,
        last_modified_by="testuser",
        updated_at=now,
        projected_at=now,
        pipeline_status=PipelineStatus.PROJECTION_COMPLETE,
    )
    await graph_store.insert_document(doc)

    fetched = await graph_store.get_document("doc_unindexed")
    assert fetched.indexed_at is None
    assert fetched.pipeline_status in (
        PipelineStatus.PROJECTION_COMPLETE,
        PipelineStatus.INDEXING_IN_PROGRESS,
    )


# ---------------------------------------------------------------------------
# BH-008: indexed_at populated after indexing completes
# ---------------------------------------------------------------------------


async def test_bh_008_indexed_at_populated_after_indexing(graph_store):
    now = datetime.now(timezone.utc)
    doc = Document(
        id="doc_indexed",
        title="Indexed",
        source_type=SourceType.MARKDOWN,
        source_path="test/indexed.md",
        source_content_hash="hash_indexed",
        adapter_version="0.1.0",
        created_by="testuser",
        created_at=now,
        last_modified_by="testuser",
        updated_at=now,
        projected_at=now,
        pipeline_status=PipelineStatus.INDEXING_COMPLETE,
        indexed_at=now,
    )
    await graph_store.insert_document(doc)

    fetched = await graph_store.get_document("doc_indexed")
    assert fetched.indexed_at is not None
    assert isinstance(fetched.indexed_at, datetime)

    # Advance to abstraction_complete; indexed_at should not change
    await graph_store.update_document(
        "doc_indexed",
        {
            "pipeline_status": PipelineStatus.ABSTRACTION_COMPLETE.value,
        },
    )
    fetched2 = await graph_store.get_document("doc_indexed")
    assert fetched2.indexed_at == fetched.indexed_at


# ---------------------------------------------------------------------------
# get_supersedes_lineage: recursive CTE must terminate on pathological graphs
# ---------------------------------------------------------------------------
#
# Real vault data can develop integrity issues — pairs of documents where
# each claims to supersede the other (2-cycle), or wide diamond patterns
# from multi-predecessor / multi-successor edges. The lineage walk must
# terminate cleanly in both cases; otherwise a single edge write whose
# source or target happens to touch such a shape loops forever in the
# recursive CTE, consuming the executor thread past any client timeout.

from sage.models.enums import (  # noqa: E402 -- grouped with the lineage-recursion test section below
    EdgeType,
)
from sage.models.schemas import (  # noqa: E402 -- grouped with the lineage-recursion test section below
    Edge,
)


def _make_doc(doc_id: str) -> Document:
    now = datetime.now(timezone.utc)
    return Document(
        id=doc_id,
        title=f"Doc {doc_id}",
        source_type=SourceType.MARKDOWN,
        source_path=f"test/{doc_id}.md",
        source_content_hash=f"hash_{doc_id}",
        adapter_version="0.1.0",
        created_by="testuser",
        created_at=now,
        last_modified_by="testuser",
        updated_at=now,
        projected_at=now,
        pipeline_status=PipelineStatus.ABSTRACTION_COMPLETE,
    )


async def _make_supersedes_edge(graph_store, newer, older):
    await graph_store.insert_edge(
        Edge(
            id=str(uuid.uuid4()),
            source_id=newer,
            target_id=older,
            edge_type=EdgeType.SUPERSEDES,
            created_at=datetime.now(timezone.utc),
        )
    )


async def test_get_supersedes_lineage_terminates_on_two_cycle(graph_store):
    """A supersedes B and B supersedes A — lineage walk must terminate."""
    await graph_store.insert_document(_make_doc("A"))
    await graph_store.insert_document(_make_doc("B"))
    await _make_supersedes_edge(graph_store, "A", "B")
    await _make_supersedes_edge(graph_store, "B", "A")

    result = await asyncio.wait_for(
        graph_store.get_supersedes_lineage("A"),
        timeout=2.0,
    )
    assert set(result) == {"A", "B"}


async def test_get_supersedes_lineage_dedupes_diamond(graph_store):
    """Diamond (A->B, A->C, B->D, C->D) — result has each doc at most once.

    Under UNION ALL the walk generated both A->B->D and A->C->D paths,
    duplicating D (and multiplying combinatorially for wider diamonds).
    """
    for d in ("A", "B", "C", "D"):
        await graph_store.insert_document(_make_doc(d))
    await _make_supersedes_edge(graph_store, "A", "B")
    await _make_supersedes_edge(graph_store, "A", "C")
    await _make_supersedes_edge(graph_store, "B", "D")
    await _make_supersedes_edge(graph_store, "C", "D")

    result = await asyncio.wait_for(
        graph_store.get_supersedes_lineage("A"),
        timeout=2.0,
    )
    assert sorted(result) == ["A", "B", "C", "D"]
    assert len(result) == len(set(result)), f"lineage must not contain duplicates, got {result}"


async def test_get_supersedes_lineage_linear_chain(graph_store):
    """Linear chain: unchanged behavior — all ancestors returned."""
    for d in ("v1", "v2", "v3", "v4"):
        await graph_store.insert_document(_make_doc(d))
    # v4 supersedes v3 supersedes v2 supersedes v1
    await _make_supersedes_edge(graph_store, "v4", "v3")
    await _make_supersedes_edge(graph_store, "v3", "v2")
    await _make_supersedes_edge(graph_store, "v2", "v1")

    result = await graph_store.get_supersedes_lineage("v4")
    assert set(result) == {"v1", "v2", "v3", "v4"}
