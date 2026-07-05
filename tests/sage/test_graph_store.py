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


async def test_t0078_tag_filter_and_semantics(graph_store):
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


async def test_t0157_query_edges_unfiltered_returns_all_paginated(graph_store):
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


async def test_t0157_query_edges_filter_by_source_id(graph_store):
    """2. source_id filter selects only edges sourced from that document."""
    await _seed_query_edges_fixture(graph_store)

    rows, total = await graph_store.query_edges(filters={"source_id": _id("doc_a")})
    assert total == 3, "doc_a sources exactly 3 references in the fixture"
    assert len(rows) == 3
    assert all(r.edge.source_id == _id("doc_a") for r in rows)


async def test_t0157_query_edges_filter_by_target_id(graph_store):
    """3. target_id filter selects only edges pointing at that document."""
    await _seed_query_edges_fixture(graph_store)

    rows, total = await graph_store.query_edges(filters={"target_id": _id("doc_z")})
    # 2 refs into Z + 1 depends_on into Z + 1 supersedes into Z = 4
    assert total == 4, f"doc_z is the target of 4 edges in the fixture, got {total}"
    assert all(r.edge.target_id == _id("doc_z") for r in rows)


async def test_t0157_query_edges_filter_by_edge_type(graph_store):
    """4. edge_type filter selects only edges of that type."""
    await _seed_query_edges_fixture(graph_store)

    rows, total = await graph_store.query_edges(filters={"edge_type": "depends_on"})
    assert total == 2, f"fixture seeds 2 depends_on edges, got {total}"
    assert all(r.edge.edge_type.value == "depends_on" for r in rows)


async def test_t0157_query_edges_combined_filters_AND(graph_store):
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


async def test_t0157_query_edges_pagination_correctness(graph_store):
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


async def test_t0157_query_edges_retraction_state_via_left_join(graph_store):
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


async def test_t0157_query_edges_multiple_retracts_earliest_wins(graph_store):
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


async def test_t0157_query_edges_null_target_on_retracts_preserved(graph_store):
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
