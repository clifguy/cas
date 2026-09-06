"""Graph Store Foundation tests: BH-001 through BH-008.

Covers document ID generation, per-document concurrency, indexed_at nullable
semantics, tag filtering, edge enumeration with retraction state, and the
post-close dispatch barrier -- all through the ``graph_store`` port fixture.
"""

import asyncio
import hashlib
import re
import uuid
from datetime import datetime, timezone

import pytest

from sage.models.enums import (
    EdgeType,
    PipelineStatus,
    SourceType,
)
from sage.models.schemas import (
    Document,
    Edge,
)
from sage.services.identity import generate_document_id
from sage.storage.postgres.graph_store import _SORTABLE_COLUMNS, PostgresGraphStore

_DOC_ID_RE = re.compile(r"^[0-9a-f]{8}_[a-z0-9_]+$")


def _id(name: str) -> str:
    """Translate a short test name to a shape-conformant document ID.

    The ID validator in sage/models/schemas.py requires the pattern
    ^[0-9a-f]{8}_[a-z0-9_]+$. Test fixtures use short readable names
    like "a1" or "doc_a"; this helper wraps them so the values still
    construct valid Document / Edge instances. Idempotent: an
    already-canonical id passes through unchanged so wrapping is safe
    to apply at every call site.
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
# BH-001: Document ID format
# ---------------------------------------------------------------------------


def test_bh_001_document_id_format():
    """Doc ID matches pattern ^[0-9a-f]{8}_[a-z0-9_]+$"""
    doc_id = generate_document_id(
        source_path="reports/2026-03-09_EXAMPLE_PV06_Claim_Set_v6_12.docx",
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
    id_a = generate_document_id("reports/doc_a.docx", ts, "Doc A")
    id_b = generate_document_id("reports/doc_b.docx", ts, "Doc B")
    assert id_a != id_b


# ---------------------------------------------------------------------------
# BH-003: Document ID uniqueness -- same path, different timestamps
# ---------------------------------------------------------------------------


def test_bh_003_same_path_different_timestamps():
    path = "reports/doc_a.docx"
    id_1 = generate_document_id(path, "2026-03-09T10:00:00Z", "Doc A")
    id_2 = generate_document_id(path, "2026-03-09T11:00:00Z", "Doc A")
    assert id_1 != id_2


# ---------------------------------------------------------------------------
# BH-005: Concurrent writes to different documents succeed
# ---------------------------------------------------------------------------


async def test_bh_005_concurrent_writes_different_docs(graph_store, lock_manager):
    """Both concurrent update_metadata calls complete without blocking."""
    now = datetime.now(timezone.utc)
    docs = []
    for suffix in ("a", "b"):
        doc = Document(
            id=_id(f"doc_{suffix}"),
            title=f"Doc {suffix.upper()}",
            source_type=SourceType.MARKDOWN,
            source_path=f"test/doc_{suffix}.md",
            lifecycle_status="active",
            source_content_hash=_sha(suffix),
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
        update_doc(_id("doc_a"), "Updated A"),
        update_doc(_id("doc_b"), "Updated B"),
        return_exceptions=True,
    )
    assert all(r is None for r in results)

    doc_a = await graph_store.get_document(_id("doc_a"))
    doc_b = await graph_store.get_document(_id("doc_b"))
    assert doc_a.title == "Updated A"
    assert doc_b.title == "Updated B"


# ---------------------------------------------------------------------------
# BH-006: Concurrent writes to the same document serialize
# ---------------------------------------------------------------------------


async def test_bh_006_concurrent_writes_same_doc(graph_store, lock_manager):
    """Two concurrent writes to the same doc serialize; no SQLITE_BUSY."""
    now = datetime.now(timezone.utc)
    doc = Document(
        id=_id("doc_shared"),
        title="Shared",
        source_type=SourceType.MARKDOWN,
        source_path="test/shared.md",
        lifecycle_status="active",
        source_content_hash=_sha("shared"),
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
        async with lock_manager.lock(_id("doc_shared")):
            call_order.append(f"{label}_start")
            await graph_store.update_document(_id("doc_shared"), {"title": new_title})
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
        id=_id("doc_unindexed"),
        title="Unindexed",
        source_type=SourceType.MARKDOWN,
        source_path="test/unindexed.md",
        lifecycle_status="active",
        source_content_hash=_sha("unindexed"),
        adapter_version="0.1.0",
        created_by="testuser",
        created_at=now,
        last_modified_by="testuser",
        updated_at=now,
        projected_at=now,
        pipeline_status=PipelineStatus.PROJECTION_COMPLETE,
    )
    await graph_store.insert_document(doc)

    fetched = await graph_store.get_document(_id("doc_unindexed"))
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
        id=_id("doc_indexed"),
        title="Indexed",
        source_type=SourceType.MARKDOWN,
        source_path="test/indexed.md",
        lifecycle_status="active",
        source_content_hash=_sha("indexed"),
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

    fetched = await graph_store.get_document(_id("doc_indexed"))
    assert fetched.indexed_at is not None
    assert isinstance(fetched.indexed_at, datetime)

    # Advance to abstraction_complete; indexed_at should not change
    await graph_store.update_document(
        _id("doc_indexed"),
        {
            "pipeline_status": PipelineStatus.ABSTRACTION_COMPLETE.value,
        },
    )
    fetched2 = await graph_store.get_document(_id("doc_indexed"))
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


def _make_doc(doc_id: str) -> Document:
    now = datetime.now(timezone.utc)
    return Document(
        id=doc_id,
        title=f"Doc {doc_id}",
        source_type=SourceType.MARKDOWN,
        source_path=f"test/{doc_id}.md",
        lifecycle_status="active",
        source_content_hash=_sha(doc_id),
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
    await graph_store.insert_document(_make_doc(_id("doc_a")))
    await graph_store.insert_document(_make_doc(_id("doc_b")))
    await _make_supersedes_edge(graph_store, _id("doc_a"), _id("doc_b"))
    await _make_supersedes_edge(graph_store, _id("doc_b"), _id("doc_a"))

    result = await asyncio.wait_for(
        graph_store.get_supersedes_lineage(_id("doc_a")),
        timeout=2.0,
    )
    assert set(result) == {_id("doc_a"), _id("doc_b")}


async def test_get_supersedes_lineage_dedupes_diamond(graph_store):
    """Diamond (A->B, A->C, B->D, C->D) — result has each doc at most once.

    Under UNION ALL the walk generated both A->B->D and A->C->D paths,
    duplicating D (and multiplying combinatorially for wider diamonds).
    """
    for d in ("doc_a", "doc_b", "doc_c", "doc_d"):
        await graph_store.insert_document(_make_doc(_id(d)))
    await _make_supersedes_edge(graph_store, _id("doc_a"), _id("doc_b"))
    await _make_supersedes_edge(graph_store, _id("doc_a"), _id("doc_c"))
    await _make_supersedes_edge(graph_store, _id("doc_b"), _id("doc_d"))
    await _make_supersedes_edge(graph_store, _id("doc_c"), _id("doc_d"))

    result = await asyncio.wait_for(
        graph_store.get_supersedes_lineage(_id("doc_a")),
        timeout=2.0,
    )
    assert sorted(result) == sorted([_id("doc_a"), _id("doc_b"), _id("doc_c"), _id("doc_d")])
    assert len(result) == len(set(result)), f"lineage must not contain duplicates, got {result}"


async def test_get_supersedes_lineage_linear_chain(graph_store):
    """Linear chain: unchanged behavior — all ancestors returned."""
    for d in ("v1", "v2", "v3", "v4"):
        await graph_store.insert_document(_make_doc(_id(d)))
    # v4 supersedes v3 supersedes v2 supersedes v1
    await _make_supersedes_edge(graph_store, _id("v4"), _id("v3"))
    await _make_supersedes_edge(graph_store, _id("v3"), _id("v2"))
    await _make_supersedes_edge(graph_store, _id("v2"), _id("v1"))

    result = await graph_store.get_supersedes_lineage(_id("v4"))
    assert set(result) == {_id("v1"), _id("v2"), _id("v3"), _id("v4")}


# ---------------------------------------------------------------------------
# Tag-filter behavior through the public query_documents API
# ---------------------------------------------------------------------------


def _make_doc_with_tags(doc_id: str, tags: list[str]) -> Document:
    doc = _make_doc(doc_id)
    doc.tags = list(tags)
    return doc


async def test_tag_filter_reflects_insert_and_update(graph_store):
    """The tag filter tracks a document's CURRENT tags, not a stale copy.

    Two-phase guard. Phase 1: a freshly inserted document is findable by
    its tag. Phase 2: after update_document rewrites the tags, the old tag
    no longer matches and the new one does. Phase 2 is the
    anti-coincidental half -- a store that materializes tags at insert
    time but never refreshes the materialization on update passes phase 1
    and fails here.
    """
    doc = _make_doc_with_tags(_id("doc_tagflow"), ["before"])
    await graph_store.insert_document(doc)

    docs, total = await graph_store.query_documents(
        {"tags": ["before"]}, limit=100, offset=0, sort_by=None, sort_order=None
    )
    assert [d.id for d in docs] == [doc.id]
    assert total == 1

    await graph_store.update_document(doc.id, {"tags": ["after"]})

    docs, total = await graph_store.query_documents(
        {"tags": ["before"]}, limit=100, offset=0, sort_by=None, sort_order=None
    )
    assert docs == [] and total == 0, "stale tag must stop matching after the update"

    docs, total = await graph_store.query_documents(
        {"tags": ["after"]}, limit=100, offset=0, sort_by=None, sort_order=None
    )
    assert [d.id for d in docs] == [doc.id]
    assert total == 1


async def test_tag_filter_and_semantics(graph_store):
    """#8: filter for two tags returns only documents that carry both."""
    for suffix, tags in (
        ("a", ["alpha"]),
        ("b", ["beta"]),
        ("c", ["alpha", "beta"]),
        ("d", ["alpha", "beta", "gamma"]),
    ):
        d = _make_doc_with_tags(_id(f"doc_and_{suffix}"), tags)
        await graph_store.insert_document(d)

    docs, total = await graph_store.query_documents(
        {"tags": ["alpha", "beta"]}, limit=100, offset=0, sort_by=None, sort_order=None
    )
    ids = {d.id for d in docs}
    assert ids == {_id("doc_and_c"), _id("doc_and_d")}
    assert total == 2


# ---------------------------------------------------------------------------
# Query_edges enumeration with retraction JOIN
# ---------------------------------------------------------------------------


async def _seed_query_edges_fixture(graph_store) -> dict[str, list[str]]:
    """Seed a small mixed-edge fixture and return ids by category.

    Layout (12 total edges): 3 references from A, 4 references from B,
    2 depends_on from B, 2 references into Z (from C, D), 1 references
    from E. Plus 1 supersedes edge to give a non-references baseline
    and exercise the edge_type filter discrimination. Plus 1 retracts
    edge disclaiming one of A's references — for the JOIN trap.

    Returns a dict carrying canonical doc ids and the seeded
    references edge ids so tests can assert on retraction state.
    """
    # Documents
    docs = ["doc_a", "doc_b", "doc_c", "doc_d", "doc_e", "doc_z", "doc_y"]
    for d in docs:
        await graph_store.insert_document(_make_doc(_id(d)))

    edges_by_kind: dict[str, list[str]] = {
        "a_refs": [],
        "b_refs": [],
        "b_depends": [],
        "into_z": [],
        "e_refs": [],
        "supersedes": [],
    }

    async def _ins(source, target, etype, kind, when=None):
        eid = str(uuid.uuid4())
        await graph_store.insert_edge(
            Edge(
                id=eid,
                source_id=_id(source),
                target_id=_id(target) if target else None,
                edge_type=EdgeType(etype),
                created_at=when or datetime.now(timezone.utc),
            )
        )
        edges_by_kind.setdefault(kind, []).append(eid)
        return eid

    # 3 refs from A
    await _ins("doc_a", "doc_b", "references", "a_refs")
    await _ins("doc_a", "doc_c", "references", "a_refs")
    await _ins("doc_a", "doc_d", "references", "a_refs")
    # 4 refs from B
    await _ins("doc_b", "doc_c", "references", "b_refs")
    await _ins("doc_b", "doc_d", "references", "b_refs")
    await _ins("doc_b", "doc_e", "references", "b_refs")
    await _ins("doc_b", "doc_y", "references", "b_refs")
    # 2 depends_on from B
    await _ins("doc_b", "doc_z", "depends_on", "b_depends")
    await _ins("doc_b", "doc_y", "depends_on", "b_depends")
    # 2 refs into Z
    await _ins("doc_c", "doc_z", "references", "into_z")
    await _ins("doc_d", "doc_z", "references", "into_z")
    # 1 ref from E
    await _ins("doc_e", "doc_y", "references", "e_refs")
    # 1 supersedes (non-references discrimination)
    await _ins("doc_y", "doc_z", "supersedes", "supersedes")

    return edges_by_kind


async def test_query_edges_unfiltered_returns_all_paginated(graph_store):
    """1. Empty filter returns every edge, paginated. total = unpaginated count."""
    fixture = await _seed_query_edges_fixture(graph_store)
    total_seeded = sum(len(v) for v in fixture.values())  # 13

    rows, total = await graph_store.query_edges(limit=10, offset=0)
    assert total == total_seeded, (
        f"total_available must equal unpaginated count, got {total} vs seeded {total_seeded}"
    )
    assert len(rows) == 10, f"page-1 must return min(limit, total), got {len(rows)}"
    # Anti-coincidental: assert shape — these are edges, not documents.
    for r in rows:
        assert hasattr(r.edge, "edge_type"), "row must hydrate an Edge, not a Document"
        assert r.edge.edge_type in EdgeType, "edge_type must be a valid EdgeType"


async def test_query_edges_filter_by_source_id(graph_store):
    """2. source_id filter selects only edges sourced from that document."""
    await _seed_query_edges_fixture(graph_store)

    rows, total = await graph_store.query_edges(filters={"source_id": _id("doc_a")})
    assert total == 3, "doc_a sources exactly 3 references in the fixture"
    assert len(rows) == 3
    assert all(r.edge.source_id == _id("doc_a") for r in rows)


async def test_query_edges_filter_by_target_id(graph_store):
    """3. target_id filter selects only edges pointing at that document."""
    await _seed_query_edges_fixture(graph_store)

    rows, total = await graph_store.query_edges(filters={"target_id": _id("doc_z")})
    # 2 refs into Z + 1 depends_on into Z + 1 supersedes into Z = 4
    assert total == 4, f"doc_z is the target of 4 edges in the fixture, got {total}"
    assert all(r.edge.target_id == _id("doc_z") for r in rows)


async def test_query_edges_filter_by_edge_type(graph_store):
    """4. edge_type filter selects only edges of that type."""
    await _seed_query_edges_fixture(graph_store)

    rows, total = await graph_store.query_edges(filters={"edge_type": "depends_on"})
    assert total == 2, f"fixture seeds 2 depends_on edges, got {total}"
    assert all(r.edge.edge_type.value == "depends_on" for r in rows)


async def test_query_edges_combined_filters_AND(graph_store):
    """5. Combined source_id + edge_type filters AND together."""
    await _seed_query_edges_fixture(graph_store)

    rows, total = await graph_store.query_edges(
        filters={"source_id": _id("doc_b"), "edge_type": "depends_on"}
    )
    # B has 4 references + 2 depends_on; intersection with depends_on = 2.
    assert total == 2
    assert all(
        r.edge.source_id == _id("doc_b") and r.edge.edge_type.value == "depends_on" for r in rows
    )


async def test_query_edges_pagination_correctness(graph_store):
    """6. Pagination: offset slices correctly; total reports unpaginated count.

    Anti-coincidental: total > limit when the underlying set is larger than
    the page — a buggy implementation that derived total from len(results)
    would compute total <= limit and fail this assertion.
    """
    await _seed_query_edges_fixture(graph_store)  # 13 edges

    page1, total1 = await graph_store.query_edges(limit=5, offset=0)
    page2, total2 = await graph_store.query_edges(limit=5, offset=5)
    assert total1 == total2 == 13, "total_available must be independent of pagination"
    assert total1 > 5, "anti-coincidental: total must exceed limit when underlying set is larger"
    assert len(page1) == 5 and len(page2) == 5
    ids_p1 = {r.edge.id for r in page1}
    ids_p2 = {r.edge.id for r in page2}
    assert ids_p1.isdisjoint(ids_p2), "pages must not overlap"


async def test_query_edges_retraction_state_via_left_join(graph_store):
    """8. Retraction JOIN: a disclaimed edge surfaces retracted_at and
    retracted_by_edge_id IN THE SAME RESULT SET as a sibling non-retracted
    edge that surfaces null for both. The same-result-set assertion is the
    anti-coincidental guard: a test that only asserts the retracted edge
    could pass by always returning the same timestamp.
    """
    await graph_store.insert_document(_make_doc(_id("doc_src")))
    await graph_store.insert_document(_make_doc(_id("doc_tgt")))
    await graph_store.insert_document(_make_doc(_id("doc_sibling_tgt")))

    # E1 is the edge that will be retracted.
    e1_id = str(uuid.uuid4())
    e1_created = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    await graph_store.insert_edge(
        Edge(
            id=e1_id,
            source_id=_id("doc_src"),
            target_id=_id("doc_tgt"),
            edge_type=EdgeType.REFERENCES,
            created_at=e1_created,
        )
    )
    # E2 is a sibling edge from the same source that is NOT retracted.
    e2_id = str(uuid.uuid4())
    await graph_store.insert_edge(
        Edge(
            id=e2_id,
            source_id=_id("doc_src"),
            target_id=_id("doc_sibling_tgt"),
            edge_type=EdgeType.REFERENCES,
            created_at=datetime(2026, 1, 1, 12, 5, 0, tzinfo=timezone.utc),
        )
    )
    # R1 is the retracts edge that disclaims E1.
    r1_id = str(uuid.uuid4())
    r1_created = datetime(2026, 2, 1, 0, 0, 0, tzinfo=timezone.utc)
    await graph_store.insert_edge(
        Edge(
            id=r1_id,
            source_id=_id("doc_src"),
            target_id=None,
            edge_type=EdgeType.RETRACTS,
            source_valid_from_version=_id("doc_src"),
            retracted_edge_id=e1_id,
            created_at=r1_created,
        )
    )

    rows, _ = await graph_store.query_edges(filters={"source_id": _id("doc_src")})
    by_id = {r.edge.id: r for r in rows}
    assert e1_id in by_id and e2_id in by_id and r1_id in by_id

    # Disclaimed edge: retracted_at + retracted_by_edge_id populated.
    disclaimed = by_id[e1_id]
    assert disclaimed.retracted_at == r1_created, (
        f"retracted_at must equal disclaiming edge's created_at, got {disclaimed.retracted_at}"
    )
    assert disclaimed.retracted_by_edge_id == r1_id

    # Sibling live edge: both null.
    sibling = by_id[e2_id]
    assert sibling.retracted_at is None
    assert sibling.retracted_by_edge_id is None

    # Retracts edge itself: not subject to retraction; both null.
    retracts_row = by_id[r1_id]
    assert retracts_row.retracted_at is None, (
        "a retracts-type edge is not itself retracted by the JOIN"
    )
    assert retracts_row.retracted_by_edge_id is None
    # But its NATIVE column carries the id of the edge it disclaims.
    assert retracts_row.edge.retracted_edge_id == e1_id


async def test_query_edges_multiple_retracts_earliest_wins(graph_store):
    """9. Multiple retracts targeting the same edge: earliest by created_at wins.

    The window function ORDER BY created_at ASC + rn=1 picks the earliest.
    """
    await graph_store.insert_document(_make_doc(_id("doc_src")))
    await graph_store.insert_document(_make_doc(_id("doc_tgt")))

    e1_id = str(uuid.uuid4())
    await graph_store.insert_edge(
        Edge(
            id=e1_id,
            source_id=_id("doc_src"),
            target_id=_id("doc_tgt"),
            edge_type=EdgeType.REFERENCES,
            created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        )
    )

    earliest = datetime(2026, 2, 1, 0, 0, 0, tzinfo=timezone.utc)
    latest = datetime(2026, 3, 1, 0, 0, 0, tzinfo=timezone.utc)
    r_earliest_id = str(uuid.uuid4())
    r_latest_id = str(uuid.uuid4())
    # Insert latest FIRST so any "natural order" implementation would
    # pick the wrong one. The window must order by created_at, not by
    # insertion order.
    await graph_store.insert_edge(
        Edge(
            id=r_latest_id,
            source_id=_id("doc_src"),
            target_id=None,
            edge_type=EdgeType.RETRACTS,
            source_valid_from_version=_id("doc_src"),
            retracted_edge_id=e1_id,
            created_at=latest,
        )
    )
    await graph_store.insert_edge(
        Edge(
            id=r_earliest_id,
            source_id=_id("doc_src"),
            target_id=None,
            edge_type=EdgeType.RETRACTS,
            source_valid_from_version=_id("doc_src"),
            retracted_edge_id=e1_id,
            created_at=earliest,
        )
    )

    rows, _ = await graph_store.query_edges(filters={"source_id": _id("doc_src")})
    disclaimed = next(r for r in rows if r.edge.id == e1_id)
    assert disclaimed.retracted_at == earliest, "earliest retracts edge must win"
    assert disclaimed.retracted_by_edge_id == r_earliest_id


async def test_query_edges_retracts_tie_on_created_at_resolves_by_id(graph_store):
    """Two retracts edges written together: the pick is by id, not by arrival.

    Its sibling above pins that the *earliest* retraction wins, which leaves
    the equal-timestamp case open, and nothing forbids that case: a retracts
    edge carries a null target, so the natural-key index on
    (source_id, target_id, edge_type) does not fire across two of them, and the
    link path checks only that the retracted edge exists. Two written in one
    batch share a created_at, and the row-number window then has a tie to break.

    The two retractions are inserted in descending id order, so insertion order
    and id order disagree and a window ranking on created_at alone reports
    whichever the scan reaches first. Fixed ids rather than uuid4 because the
    assertion is about *which* id wins, and a random pair cannot express that.
    """
    await graph_store.insert_document(_make_doc(_id("doc_tiesrc")))
    await graph_store.insert_document(_make_doc(_id("doc_tietgt")))

    e1_id = str(uuid.uuid4())
    await graph_store.insert_edge(
        Edge(
            id=e1_id,
            source_id=_id("doc_tiesrc"),
            target_id=_id("doc_tietgt"),
            edge_type=EdgeType.REFERENCES,
            created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        )
    )

    lower_id = "00000000-0000-4000-8000-000000000001"
    higher_id = "00000000-0000-4000-8000-000000000002"
    tied_at = datetime(2026, 2, 1, tzinfo=timezone.utc)
    for retracts_id in (higher_id, lower_id):
        await graph_store.insert_edge(
            Edge(
                id=retracts_id,
                source_id=_id("doc_tiesrc"),
                target_id=None,
                edge_type=EdgeType.RETRACTS,
                source_valid_from_version=_id("doc_tiesrc"),
                retracted_edge_id=e1_id,
                created_at=tied_at,
            )
        )

    rows, _ = await graph_store.query_edges(filters={"source_id": _id("doc_tiesrc")})
    disclaimed = next(r for r in rows if r.edge.id == e1_id)

    assert disclaimed.retracted_at == tied_at
    assert disclaimed.retracted_by_edge_id == lower_id, (
        "a created_at tie between two retractions must resolve on the edge id, "
        f"not on which arrived first; got {disclaimed.retracted_by_edge_id}"
    )


async def test_query_edges_null_target_on_retracts_preserved(graph_store):
    """10. Per CAS-ADR-017 retracts edges have target_id=NULL.
    query_edges must preserve the null (not coerce or drop the row).
    """
    await graph_store.insert_document(_make_doc(_id("doc_src")))
    await graph_store.insert_document(_make_doc(_id("doc_tgt")))

    e1_id = str(uuid.uuid4())
    await graph_store.insert_edge(
        Edge(
            id=e1_id,
            source_id=_id("doc_src"),
            target_id=_id("doc_tgt"),
            edge_type=EdgeType.REFERENCES,
            created_at=datetime.now(timezone.utc),
        )
    )
    r_id = str(uuid.uuid4())
    await graph_store.insert_edge(
        Edge(
            id=r_id,
            source_id=_id("doc_src"),
            target_id=None,
            edge_type=EdgeType.RETRACTS,
            source_valid_from_version=_id("doc_src"),
            retracted_edge_id=e1_id,
            created_at=datetime.now(timezone.utc),
        )
    )

    rows, _ = await graph_store.query_edges(filters={"edge_type": "retracts"})
    assert len(rows) == 1
    assert rows[0].edge.target_id is None
    assert rows[0].edge.id == r_id


# ---------------------------------------------------------------------------
# Close() barrier semantics per CAS-ADR-036
# ---------------------------------------------------------------------------


async def test_run_raises_on_closed_store(graph_store):
    """Post-close dispatch through the _run boundary raises
    RuntimeError naming the closed state. Simplest expression of the
    barrier contract.
    """
    await graph_store.close()
    with pytest.raises(RuntimeError, match="closed"):
        await graph_store.list_all_documents()


async def test_run_raises_after_close_with_warmed_store(graph_store):
    """Post-close dispatch raises even after a successful prior dispatch.

    Complements the cold-store case above: a store that has already served
    a call may hold warmed per-connection state (cached handles, pooled
    workers) that a buggy close barrier could keep silently serving from.
    The warm-up call stages exactly that state before close().
    """
    await graph_store.list_all_documents()
    await graph_store.close()
    with pytest.raises(RuntimeError, match="closed"):
        await graph_store.list_all_documents()


# ---------------------------------------------------------------------------
# Bulk source-path lookup
# ---------------------------------------------------------------------------


def _make_doc_at(doc_id: str, source_path: str, **overrides) -> Document:
    """A document pinned to an explicit source_path.

    ``_make_doc`` derives source_path from the id, so it cannot express the
    several-documents-one-path case the bulk lookup has to collapse.
    """
    doc = _make_doc(doc_id)
    return doc.model_copy(update={"source_path": source_path, **overrides})


async def test_find_documents_by_source_paths_maps_each_present_path_to_its_hash(graph_store):
    """Each present path maps to that document's own content hash; an absent
    path is simply not a key.

    Trap: an implementation that echoes its input (``{p: p for p in paths}``)
    or that returns every path as present satisfies a keys-only assertion.
    Comparing values against the seeded hashes, and including a path no
    document carries, closes both.
    """
    docs = [_make_doc_at(_id(f"bulk_path_{n}"), f"test/bulk/{n}.md") for n in ("a", "b", "c")]
    for doc in docs:
        await graph_store.insert_document(doc)

    result = await graph_store.find_documents_by_source_paths(
        [d.source_path for d in docs] + ["test/bulk/absent.md"]
    )

    assert result == {d.source_path: d.source_content_hash for d in docs}


async def test_find_documents_by_source_paths_empty_list_returns_empty_dict(graph_store):
    """An empty path list returns an empty mapping, not the whole table.

    Trap: a query built as ``source_path = ANY('{}')`` is harmless, but a
    predicate that collapses to a no-op WHERE clause returns every row.
    Asserting against a store that is *not* empty distinguishes them.
    """
    await graph_store.insert_document(_make_doc_at(_id("bulk_empty_probe"), "test/bulk/probe.md"))

    assert await graph_store.find_documents_by_source_paths([]) == {}


async def test_find_documents_by_source_paths_returns_one_deterministic_row_per_path(graph_store):
    """Several documents on one path collapse to a single, stable entry.

    The per-file form consulted ``docs[0]`` of an unordered SELECT, so the
    winner was arbitrary. Trap: drop the row collapse and both rows come back,
    leaving the caller-side mapping to keep whichever arrived last -- the
    opposite document, which a membership-only assertion would not notice.
    Pinning the value to the lower-ordering id's hash catches that.

    The higher-id document is inserted first on purpose: with the two orders
    coincident, "lowest id wins" and "first inserted wins" are different rules
    that no assertion here could separate -- and both are implemented, the
    former by the durable store and the latter by the in-memory stub.

    What this pins is which document represents the path, not the row count:
    a collapse-free query that happened to yield the same winner is
    indistinguishable at this boundary, and behaves identically for callers.
    """
    shared = "test/bulk/shared.md"
    lowest_id = _make_doc_at("00000001_bulk_dup_first", shared)
    higher_id = _make_doc_at("00000002_bulk_dup_second", shared)
    await graph_store.insert_document(higher_id)
    await graph_store.insert_document(lowest_id)

    one = await graph_store.find_documents_by_source_paths([shared])
    two = await graph_store.find_documents_by_source_paths([shared])

    assert one == {shared: lowest_id.source_content_hash}
    assert two == one


async def test_find_documents_by_source_paths_ignores_lifecycle_status(graph_store):
    """A non-active document is still found by its path.

    Parity with the per-file form, which applied no lifecycle filter. Trap:
    the other tests in this group seed only active documents, so a copied-in
    ``AND lifecycle_status = 'active'`` clause would leave them all green.
    """
    archived = _make_doc_at(
        _id("bulk_archived"), "test/bulk/archived.md", lifecycle_status="archived"
    )
    await graph_store.insert_document(archived)

    result = await graph_store.find_documents_by_source_paths([archived.source_path])

    assert result == {archived.source_path: archived.source_content_hash}


# ---------------------------------------------------------------------------
# Document facet aggregation via query_document_facets
# ---------------------------------------------------------------------------

# Deliberately a literal rather than an import of the implementation's
# field constant: the tests assert the contract vocabulary independently,
# so a drifted constant cannot re-shape the expectations it is tested by.
_FACET_FIELDS = (
    "doc_type",
    "lifecycle_status",
    "source_type",
    "pipeline_status",
    "tags",
)


async def _seed_facet_fixture(graph_store) -> None:
    """Six documents with a skewed facet distribution.

    doc_type: 3x adr, 2x ticket, one NULL. lifecycle_status: 5x active,
    1x archived. source_type: 5x markdown, 1x docx. pipeline_status:
    5x abstraction_complete, 1x failed (on the NULL-doc_type doc).
    tags: "alpha" on fac_a1 + fac_a2, "beta" on fac_a1 + fac_t1
    (fac_a1 is multi-tag; three documents carry no tags). The skew
    means no two facet fields share a count vector, and the multi-tag
    doc keeps the tag-count sum from coinciding with the total.
    """
    seeds = [
        ("fac_a1", {"doc_type": "adr", "tags": ["alpha", "beta"]}),
        ("fac_a2", {"doc_type": "adr", "tags": ["alpha"]}),
        (
            "fac_a3",
            {
                "doc_type": "adr",
                "lifecycle_status": "archived",
                "source_type": SourceType.DOCX,
            },
        ),
        (
            "fac_t1",
            {
                "doc_type": "ticket",
                "tags": ["beta"],
                "tier3_metadata": {"ticket_id": "tk_1"},
            },
        ),
        ("fac_t2", {"doc_type": "ticket"}),
        ("fac_n1", {"pipeline_status": PipelineStatus.FAILED}),
    ]
    for name, overrides in seeds:
        doc = _make_doc(_id(name)).model_copy(update=overrides)
        await graph_store.insert_document(doc)


async def test_query_document_facets_full_vault_counts(graph_store):
    """Unfiltered facets: exact per-value counts for every field.

    Trap coverage: the skewed distribution defeats a COUNT(*)-everywhere
    implementation; the NULL-doc_type document must be excluded from the
    doc_type values but included in the total; the multi-tag document
    keeps the tag sum from matching the denominator.
    """
    await _seed_facet_fixture(graph_store)

    facets, total = await graph_store.query_document_facets()

    assert set(facets.keys()) == set(_FACET_FIELDS)
    assert total == 6
    assert facets["doc_type"].values == {"adr": 3, "ticket": 2}
    assert facets["lifecycle_status"].values == {"active": 5, "archived": 1}
    assert facets["source_type"].values == {"markdown": 5, "docx": 1}
    assert facets["pipeline_status"].values == {"abstraction_complete": 5, "failed": 1}
    assert facets["tags"].values == {"alpha": 2, "beta": 2}
    assert sum(facets["tags"].values.values()) != total
    # Uncapped, the distinct total always equals the returned value count.
    assert all(f.total_distinct == len(f.values) for f in facets.values())
    # Deterministic value ordering: count DESC, then value ASC. The
    # source_type facet is the discriminating case -- its higher-count
    # value ("markdown") sorts alphabetically AFTER "docx", so a pure
    # value-ASC ordering fails here while passing the other two. The
    # tags facet (a 2/2 count tie) pins the value-ASC tiebreak.
    assert list(facets["source_type"].values.keys()) == ["markdown", "docx"]
    assert list(facets["doc_type"].values.keys()) == ["adr", "ticket"]
    assert list(facets["tags"].values.keys()) == ["alpha", "beta"]


async def test_query_document_facets_respects_filters(graph_store):
    """Facets computed within a filter slice, not vault-wide."""
    await _seed_facet_fixture(graph_store)

    facets, total = await graph_store.query_document_facets({"doc_type": "adr"})

    assert total == 3
    assert facets["doc_type"].values == {"adr": 3}
    assert facets["lifecycle_status"].values == {"active": 2, "archived": 1}
    assert facets["source_type"].values == {"markdown": 2, "docx": 1}
    assert facets["tags"].values == {"alpha": 2, "beta": 1}


async def test_query_document_facets_composes_with_tags_filter(graph_store):
    """The tag-filter EXISTS predicate composes under the tags-facet join.

    Trap coverage: the tags facet joins document_tags while the tags
    filter is a correlated EXISTS against the same table; an alias on
    the outer ``documents`` table would break the correlation silently.
    """
    await _seed_facet_fixture(graph_store)

    facets, total = await graph_store.query_document_facets({"tags": ["alpha"]})

    assert total == 2
    assert facets["tags"].values == {"alpha": 2, "beta": 1}
    assert facets["doc_type"].values == {"adr": 2}


async def test_query_document_facets_composes_with_tier3_filter(graph_store):
    """tier3_metadata predicates restrict the facet slice."""
    await _seed_facet_fixture(graph_store)

    facets, total = await graph_store.query_document_facets(
        {"tier3_metadata": {"ticket_id": "tk_1"}}
    )

    assert total == 1
    assert facets["doc_type"].values == {"ticket": 1}
    assert facets["tags"].values == {"beta": 1}


async def test_query_document_facets_includes_failed_pipeline_documents(graph_store):
    """Failed-pipeline documents are facet-visible.

    Catalog parity: enumeration surfaces apply no default failed-pipeline
    exclusion. The fixture seeds exactly one failed document so an
    implementation that quietly excluded it could not pass by accident.
    """
    await _seed_facet_fixture(graph_store)

    facets, total = await graph_store.query_document_facets()

    assert facets["pipeline_status"].values.get("failed") == 1
    assert total == 6


async def test_query_document_facets_empty_vault(graph_store):
    """Empty vault: every field present with empty values, zero totals.

    Tuple equality pins the per-field shape (values, total_distinct)
    without importing the implementation's named type: a total_distinct
    that goes missing or nonzero on the empty vault fails here.
    """
    facets, total = await graph_store.query_document_facets()

    assert facets == {f: ({}, 0) for f in _FACET_FIELDS}
    assert total == 0


async def _seed_wide_tag_fixture(graph_store) -> None:
    """Four documents whose tag vocabulary (8) exceeds a small value cap.

    Tag counts: w0 on all four documents, w1 on three, w2 and w3 tied at
    two, w4-w7 on one each. The w2/w3 tie sits exactly at the cut line
    for value_limit=3, so the value-ASC tiebreak is exercised *under*
    the cap. Each document also carries a distinct doc_type (dta-dtd,
    all count 1) so the scalar branch has more distinct values than the
    cap and its own all-tied value-ASC ordering.
    """
    seeds = [
        ("wt_1", {"doc_type": "dta", "tags": ["w0", "w1", "w2", "w3", "w4"]}),
        ("wt_2", {"doc_type": "dtb", "tags": ["w0", "w1", "w2", "w3", "w5"]}),
        ("wt_3", {"doc_type": "dtc", "tags": ["w0", "w1", "w6"]}),
        ("wt_4", {"doc_type": "dtd", "tags": ["w0", "w7"]}),
    ]
    for name, overrides in seeds:
        doc = _make_doc(_id(name)).model_copy(update=overrides)
        await graph_store.insert_document(doc)


async def test_query_document_facets_value_limit_caps_every_field(graph_store):
    """value_limit caps values uniformly while totals stay true.

    Trap coverage: a tags-only cap fails the doc_type assertions; a
    LIMIT applied without the count-DESC/value-ASC ordering returns an
    arbitrary subset and fails the exact-prefix assertion (the w2/w3
    tie pins the ASC tiebreak at the cut line); a total_distinct
    computed from the capped rows fails the == 8 / == 4 assertions.
    """
    await _seed_wide_tag_fixture(graph_store)

    facets, total = await graph_store.query_document_facets(value_limit=3)

    assert total == 4
    assert facets["tags"].values == {"w0": 4, "w1": 3, "w2": 2}
    assert list(facets["tags"].values.keys()) == ["w0", "w1", "w2"]
    assert facets["tags"].total_distinct == 8
    assert facets["doc_type"].values == {"dta": 1, "dtb": 1, "dtc": 1}
    assert facets["doc_type"].total_distinct == 4


async def test_query_document_facets_total_distinct_uncapped_and_boundary(graph_store):
    """No cap and cap == distinct count both return the full vocabulary.

    Trap coverage: an off-by-one LIMIT drops a value at the boundary; a
    distinct total computed after the LIMIT reads 3 in the capped test
    and masks here; a cap defaulting on at this layer (policy leaking
    into storage) fails the uncapped assertion.
    """
    await _seed_wide_tag_fixture(graph_store)

    uncapped, _ = await graph_store.query_document_facets()
    at_boundary, _ = await graph_store.query_document_facets(value_limit=8)

    assert len(uncapped["tags"].values) == 8
    assert uncapped["tags"].total_distinct == 8
    assert at_boundary["tags"].values == uncapped["tags"].values
    assert at_boundary["tags"].total_distinct == 8


async def test_query_document_facets_fields_subset_skips_unrequested(graph_store):
    """A fields subset returns exactly the requested facet keys, and
    only the requested aggregations run.

    Trap coverage: an implementation that always returns all five keys
    fails the exact-key-set assertion; one that aggregates all five and
    filters post hoc fails the query count -- exactly two per-field
    aggregation queries may run for a two-field subset.
    """
    await _seed_wide_tag_fixture(graph_store)

    aggregation_queries = []
    original_fetch = graph_store._fetch_tuples

    async def counting_fetch(sql, params):
        aggregation_queries.append(sql)
        return await original_fetch(sql, params)

    graph_store._fetch_tuples = counting_fetch
    try:
        facets, total = await graph_store.query_document_facets(fields=["doc_type", "tags"])
    finally:
        graph_store._fetch_tuples = original_fetch

    assert set(facets.keys()) == {"doc_type", "tags"}
    assert total == 4
    assert facets["tags"].total_distinct == 8
    assert len(aggregation_queries) == 2, "unrequested facet fields must not be aggregated"


async def test_query_document_facets_value_limit_composes_with_filters(graph_store):
    """The correlated tag-filter EXISTS survives the capped query shape.

    Trap coverage: re-arms the alias regression for the new subquery --
    wrapping the tags aggregation while aliasing the outer ``documents``
    table breaks the EXISTS correlation (wrong counts or a SQL error).
    """
    await _seed_wide_tag_fixture(graph_store)

    facets, total = await graph_store.query_document_facets({"tags": ["w1"]}, value_limit=2)

    assert total == 3
    assert facets["tags"].values == {"w0": 3, "w1": 3}
    assert facets["tags"].total_distinct == 7


async def test_stub_graph_store_facets_unsupported():
    """The in-memory stub declares the method but refuses to answer."""
    from sage.adapters.stubs import StubGraphStore

    store = StubGraphStore()
    with pytest.raises(NotImplementedError):
        await store.query_document_facets()


# ---------------------------------------------------------------------------
# list_non_canonical_source_paths
# ---------------------------------------------------------------------------

# Every stored spelling worth asking about. Whether each one is expected back is
# *derived* below rather than written down here, so the expectation cannot be
# quietly edited into agreement with a defective predicate.
#
# The controls matter as much as the candidates. Four plain paths carry a dot in
# the *filename* -- including a hidden file and a trailing-dot name, the two
# shapes a segment pattern is most likely to over-match -- and three carry a
# ``..``, which pathlib preserves rather than resolving, so their plain form is
# the string they already are and there is nothing here to repair.
_STORED_SPELLINGS: tuple[str, ...] = (
    "test/dot/plain.md",
    "test/dot/two.dots.in.name.md",
    "test/dot/.hidden.md",
    "test/dot/trailing.dot.",
    "../test/dot/up.md",
    "test/dot/../dot/rel.md",
    "test/dot/..",
    "./test/dot/lead.md",
    "test/dot/./interior.md",
    "test/dot//doubled.md",
    "test/dot/trailing_sep.md/",
    ".//test/dot//both.md",
    "test/dot/sub/.",
    "/test/dot/./absolute.md",
)

# The spellings that match the filter and still reduce to themselves: the two
# degenerate whole-path forms, and anything under a ``//`` root, which POSIX
# leaves implementation-defined and the reducer preserves. The filter is
# syntactic, so it offers all of them and the caller drops them. They are held
# apart from the corpus above rather than folded into it because they are
# exactly where the filter is deliberately a superset of "the plain form
# differs", and separating them is what keeps the corpus assertion an exact one.
_DEGENERATE_SPELLINGS: tuple[str, ...] = (".", "/", "//test/dot/rooted.md")


def _respelling_differs(source_path: str) -> bool:
    """Whether the plain POSIX form of ``source_path`` is a different string.

    Computed with ``PurePosixPath`` -- the same reduction the caller normalizes
    with -- rather than restated as a pattern, so the store's predicate and this
    expectation are provably answering one question.
    """
    from pathlib import PurePosixPath

    return str(PurePosixPath(source_path)) != source_path


async def _seed_spellings(graph_store, tag: str) -> dict[str, str]:
    """Insert one document per stored spelling; return {source_path: doc_id}."""
    seeded = {}
    for n, path in enumerate(_STORED_SPELLINGS):
        doc = _make_doc_at(_id(f"noncanon_{tag}_{n}"), path)
        await graph_store.insert_document(doc)
        seeded[path] = doc.id
    return seeded


async def test_list_non_canonical_source_paths_returns_exactly_the_respellable(graph_store):
    """Over the seeded corpus, the store selects a record if and only if the
    plain form of its stored path is a different string -- and maps it to the
    value the record actually holds.

    Trap: a predicate keyed on the substring ``'.'`` selects every ``.md`` file,
    which a presence-only assertion cannot tell from a correct one. Set equality
    against a corpus whose controls include a hidden file, a trailing-dot name
    and three ``..`` forms closes that from both sides at once: over-matching
    fails on the controls, under-matching on the candidates. Asserting the
    returned *value* catches an implementation handing back the repaired path
    instead of the stored one, which would leave the caller nothing to repair.
    """
    seeded = await _seed_spellings(graph_store, "corpus")

    result = await graph_store.list_non_canonical_source_paths()

    selected = {path for path, doc_id in seeded.items() if doc_id in result}
    assert selected == {p for p in _STORED_SPELLINGS if _respelling_differs(p)}
    for path in selected:
        assert result[seeded[path]] == path


async def test_list_non_canonical_source_paths_offers_the_degenerate_spellings(graph_store):
    """``.`` and ``/`` are offered even though reducing them changes nothing.

    The filter is syntactic and these two are the whole of where that makes it
    a superset of "the plain form differs". Pinning it means the port's promise
    matches what the predicate does, rather than a stricter one that happens to
    hold everywhere a reviewer looked.

    Trap: leaving this unstated is how the caller's own "reduced to the same
    string, nothing to do" branch ends up with no test behind it -- the branch
    is unreachable in every other case, so removing it would look harmless.
    """
    seeded = {}
    for n, path in enumerate(_DEGENERATE_SPELLINGS):
        doc = _make_doc_at(_id(f"noncanon_degenerate_{n}"), path)
        await graph_store.insert_document(doc)
        seeded[path] = doc.id

    result = await graph_store.list_non_canonical_source_paths()

    assert {path: result.get(doc_id) for path, doc_id in seeded.items()} == {
        path: path for path in _DEGENERATE_SPELLINGS
    }
    assert not any(_respelling_differs(p) for p in _DEGENERATE_SPELLINGS)


async def test_list_non_canonical_source_paths_omits_a_store_of_plain_paths(graph_store):
    """Records whose paths are all already plain contribute nothing.

    Trap: this is the half the corpus test cannot carry. The migration that
    consumes this reads "not returned" as "nothing to repair", so a predicate
    degenerating to a no-op WHERE clause turns every call into a full-table read
    and every row into a self-rewrite. Seeding only ordinary paths and asserting
    none of them come back is what distinguishes a real no-op from that.

    The positive control is what makes the absence mean anything: an empty
    result set is also what a store that never received the documents would
    produce, so the same paths are looked up through a query that *does* return
    them before the absence is asserted.
    """
    paths = [
        "test/dot/clean/alpha.md",
        "test/dot/clean/beta.tar.gz",
        "test/dot/clean/gamma",
    ]
    seeded_ids = set()
    for n, path in enumerate(paths):
        doc = _make_doc_at(_id(f"noncanon_clean_{n}"), path)
        await graph_store.insert_document(doc)
        seeded_ids.add(doc.id)

    result = await graph_store.list_non_canonical_source_paths()

    assert set(await graph_store.find_documents_by_source_paths(paths)) == set(paths)
    assert seeded_ids & set(result) == set()


async def test_find_document_ids_by_source_paths_returns_every_id_per_path(graph_store):
    """Each present path maps to every document id carrying it, in id order.

    Trap: the neighbouring ``find_documents_by_source_paths`` collapses the
    several-documents-one-path case to a single representative, which is the
    right answer for provenance and the wrong one here -- a caller asking who
    else holds a path needs all of them. Seeding two documents on one path is
    what separates this method from a copy of that one; a single-document
    corpus cannot tell them apart. The absent path closes the other side: an
    implementation echoing its input reports a holder for a path nobody holds.
    """
    shared = "test/ids/shared.md"
    second = _make_doc_at("00000002_ids_shared_hi", shared)
    first = _make_doc_at("00000001_ids_shared_lo", shared)
    lone = _make_doc_at(_id("ids_lone"), "test/ids/lone.md")
    for doc in (second, first, lone):
        await graph_store.insert_document(doc)

    result = await graph_store.find_document_ids_by_source_paths(
        [shared, lone.source_path, "test/ids/absent.md"]
    )

    assert result == {shared: [first.id, second.id], lone.source_path: [lone.id]}


async def test_find_document_ids_by_source_paths_empty_list_returns_empty_dict(graph_store):
    """An empty path list returns an empty mapping, not the whole table.

    Trap: a predicate that collapses to a no-op WHERE clause returns every row.
    Asserting against a store that is *not* empty distinguishes them.
    """
    await graph_store.insert_document(_make_doc_at(_id("ids_probe"), "test/ids/probe.md"))

    assert await graph_store.find_document_ids_by_source_paths([]) == {}


# ---------------------------------------------------------------------------
# Catalog ordering totality
# ---------------------------------------------------------------------------

#: The tiebreak every catalog ORDER BY must end in, pinned here rather than
#: imported so this module states the requirement instead of restating whatever
#: the store happens to do. Changing the tiebreak means changing this line.
_EXPECTED_DOCUMENT_TIEBREAK = ", id ASC"


def _order_clause_cases() -> list[tuple[str | None, str | None]]:
    """Every (sort_by, sort_order) pair ``_build_order_clause`` distinguishes.

    Three branches: the default, a recognized column in either direction, and
    the fallback an unrecognized column takes. Built from
    ``_SORTABLE_COLUMNS`` rather than a hand-written list so a column added to
    the allowlist is gated the day it lands instead of the day someone
    remembers this test.
    """
    cases: list[tuple[str | None, str | None]] = [(None, None), (None, "desc")]
    for column in sorted(_SORTABLE_COLUMNS):
        cases.append((column, "asc"))
        cases.append((column, "desc"))
    cases.append(("no_such_column", None))
    return cases


@pytest.mark.parametrize(("sort_by", "sort_order"), _order_clause_cases())
def test_order_clause_is_total_in_every_branch(sort_by, sort_order):
    """Every ORDER BY the catalog document query can produce ends in the primary key.

    The property is *total order*, and the only way to have it is for the last
    sort term to be unique per row. ``endswith`` on the exact suffix is what
    makes this a real check: a containment test ("id" appears somewhere) passes
    against a clause that names the primary key first, or inside another
    column's name, and a total order needs it last. The case list is derived
    from ``_SORTABLE_COLUMNS``, so a fourth branch or a new sortable column
    cannot slip past by being absent from a hand-maintained list.
    """
    clause = PostgresGraphStore._build_order_clause(sort_by, sort_order)

    assert clause.endswith(_EXPECTED_DOCUMENT_TIEBREAK), (
        f"ORDER BY for sort_by={sort_by!r} sort_order={sort_order!r} does not end "
        f"in a unique-column tiebreak, so it admits ties: {clause!r}"
    )


def _make_tied_doc(doc_id: str) -> Document:
    """A document that ties with its siblings on every sortable column.

    ``_make_doc`` derives a distinct title from the id, which is enough to
    break the fallback branch's tie by accident. Every field the three ORDER BY
    branches read -- title, doc_type, document_date, lifecycle_status -- is
    held constant here, so the ordering has nothing but the tiebreak to work
    with and a non-total clause is free to answer differently each call.
    """
    doc = _make_doc(doc_id)
    doc.title = "Tied"
    doc.doc_type = "ticket"
    doc.document_date = "2026-05-01"
    doc.lifecycle_status = "active"
    return doc


async def _seed_tied_documents(graph_store, count: int, prefix: str) -> set[str]:
    ids: set[str] = set()
    for n in range(count):
        doc = _make_tied_doc(_id(f"{prefix}_{n:03d}"))
        await graph_store.insert_document(doc)
        ids.add(doc.id)
    return ids


async def _perturb_scan_order(graph_store, doc_id: str) -> None:
    """Move a row's physical position without touching anything the query reads.

    Rewriting a column to the value it already holds is a semantic no-op, but
    Postgres answers it with a new tuple version at a new position, so a
    sequential scan hands the sorter a different input order. Under a total
    order that cannot change the result; under a clause with ties it is exactly
    what lets two identical calls disagree. ``last_modified_by`` is in neither
    ``_SORTABLE_COLUMNS`` nor the default clause, and the write goes through
    the store's own update path rather than raw SQL.
    """
    doc = await graph_store.get_document(doc_id)
    await graph_store.update_document(doc_id, {"last_modified_by": doc.last_modified_by})


async def _perturb_edge_scan_order(graph_store, edge_id: str) -> None:
    """Move an edge's physical position while leaving the row identical.

    The edge store exposes no update, so the round trip is delete-then-reinsert
    with the same id and the same fields: what comes back out of a query is
    byte-for-byte what was there before, at a new scan position. Same purpose
    as the document-side ``_perturb_scan_order``.
    """
    edge = await graph_store.get_edge(edge_id)
    await graph_store.delete_edge(edge_id)
    await graph_store.insert_edge(edge)


@pytest.mark.parametrize(
    ("branch", "sort_by", "sort_order"),
    [
        ("default", None, None),
        ("sorted", "document_date", "desc"),
        ("fallback", "no_such_column", None),
    ],
    ids=["default", "sorted", "fallback"],
)
async def test_paging_a_tied_set_returns_each_document_exactly_once(
    graph_store, branch, sort_by, sort_order
):
    """limit/offset paging partitions the filtered set: no skips, no duplicates.

    The fixture ties on every sortable column, and the scan order is perturbed
    between pages. Both halves are load-bearing. Without the ties there is
    nothing for a non-total clause to reorder; without the perturbation
    Postgres happens to answer a small unperturbed table the same way twice, so
    the test passes against the defect and proves nothing. What is perturbed is
    the page just read, so a clause that lets those rows drift to the end of
    the tied block returns them again in a later page and drops whatever they
    displaced -- the skip and the duplicate a non-total order produces, in one shot.

    Run against all three ORDER BY branches -- a tiebreak on the default alone
    leaves the other two admitting ties.
    """
    seeded = await _seed_tied_documents(graph_store, 24, f"tiedpage_{branch}")
    ordered = sorted(seeded)

    seen: list[str] = []
    for offset in (0, 8, 16):
        page, total = await graph_store.query_documents(
            {"doc_type": "ticket"},
            limit=8,
            offset=offset,
            sort_by=sort_by,
            sort_order=sort_order,
        )
        assert total == 24
        seen.extend(d.id for d in page)
        for doc in page:
            await _perturb_scan_order(graph_store, doc.id)

    assert len(seen) == 24, f"paging returned {len(seen)} rows for a 24-row set"
    assert sorted(seen) == ordered, (
        "paging a tied set skipped or duplicated documents; "
        f"missing={sorted(seeded - set(seen))} duplicated="
        f"{sorted({i for i in seen if seen.count(i) > 1})}"
    )


async def test_two_identical_catalog_calls_agree_on_row_order(graph_store):
    """Two calls with the same filters return the same rows in the same order.

    The premise the budget hint's prefix simulation rests on, asserted directly.
    Perturbing between the calls is what separates a total order from one that
    merely looked stable: the second call sees a different scan order, and only
    a unique final sort term can undo that.
    """
    await _seed_tied_documents(graph_store, 24, "tiedstable")

    first, _ = await graph_store.query_documents({"doc_type": "ticket"}, limit=24, offset=0)
    assert len(first) == 24, (
        "two empty results also agree on order; the comparison below says "
        "nothing unless the query returned the seeded set"
    )
    await _perturb_scan_order(graph_store, sorted(d.id for d in first)[12])
    second, _ = await graph_store.query_documents({"doc_type": "ticket"}, limit=24, offset=0)

    assert [d.id for d in first] == [d.id for d in second]


async def test_paging_tied_edges_returns_each_edge_exactly_once(graph_store):
    """The edge enumeration arm partitions its set too.

    ``query_edges`` orders by ``created_at DESC`` alone, and
    ``search(mode=catalog, target="edges")`` pages it with the same
    limit/offset. Same defect as the document query, same fixture shape: every
    edge shares a ``created_at``, and the page just read is rewritten to move
    those rows' scan position. Twelve distinct targets rather than twelve edges
    between one pair -- the edges table carries a unique natural key on
    (source, target, type), so the one-pair form cannot be seeded at all.
    """
    await _seed_tied_documents(graph_store, 13, "tiededge_doc")
    doc_ids = sorted(
        d.id for d in (await graph_store.query_documents({"doc_type": "ticket"}, limit=13))[0]
    )
    source, targets = doc_ids[0], doc_ids[1:]
    created = datetime(2026, 5, 1, tzinfo=timezone.utc)
    edge_ids = set()
    for target in targets:
        edge = Edge(
            id=str(uuid.uuid4()),
            source_id=source,
            target_id=target,
            edge_type=EdgeType.REFERENCES,
            created_at=created,
        )
        await graph_store.insert_edge(edge)
        edge_ids.add(edge.id)

    seen: list[str] = []
    for offset in (0, 4, 8):
        page, total = await graph_store.query_edges(
            filters={"edge_type": EdgeType.REFERENCES.value}, limit=4, offset=offset
        )
        assert total == 12
        seen.extend(row.edge.id for row in page)
        for row in page:
            await _perturb_edge_scan_order(graph_store, row.edge.id)

    assert sorted(seen) == sorted(edge_ids), (
        "paging tied edges skipped or duplicated rows; "
        f"missing={sorted(edge_ids - set(seen))} duplicated="
        f"{sorted({i for i in seen if seen.count(i) > 1})}"
    )
