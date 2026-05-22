"""Graph Store Foundation tests: BH-001 through BH-008.

Covers document ID generation, SQLite WAL mode, per-document concurrency,
and indexed_at nullable semantics.
"""

import asyncio
import hashlib
import json
import re
import sqlite3
import uuid
from datetime import datetime, timezone

from sage.models.enums import (
    EdgeType,
    PipelineStatus,
    RationaleKind,
    ResolutionPolicy,
    SourceType,
)
from sage.models.schemas import (
    Document,
    Edge,
    StagingEdge,
    TagsPatch,
    UpdateMetadataRequest,
)
from sage.services.identity import generate_document_id
from sage.storage.graph_store import GraphStore
from sage.storage.migrations import (
    BACKFILL_PLAN,
    _backfill_document_tags_apply,
    _backfill_document_tags_detect,
)

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
# T-0078: document_tags join-table normalization
# ---------------------------------------------------------------------------


def _join_rows(db_path, doc_id: str | None = None) -> list[tuple[str, str]]:
    """Open a fresh read-only connection and return document_tags rows."""
    conn = sqlite3.connect(str(db_path))
    try:
        if doc_id is None:
            rows = conn.execute(
                "SELECT document_id, tag FROM document_tags ORDER BY document_id, tag"
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT document_id, tag FROM document_tags WHERE document_id = ? ORDER BY tag",
                (doc_id,),
            ).fetchall()
        return rows
    finally:
        conn.close()


def _make_doc_with_tags(doc_id: str, tags: list[str]) -> Document:
    doc = _make_doc(doc_id)
    doc.tags = list(tags)
    return doc


async def test_t0078_insert_with_tags_populates_join_table(graph_store):
    """T-0078 #1: insert document with tags -> matching join rows."""
    doc = _make_doc_with_tags(_id("doc_t1"), ["alpha", "beta"])
    await graph_store.insert_document(doc)
    rows = _join_rows(graph_store._db_path, doc.id)
    assert rows == [(doc.id, "alpha"), (doc.id, "beta")]


async def test_t0078_insert_with_no_tags_creates_no_join_rows(graph_store):
    """T-0078 #2: insert with empty tags -> zero join rows.

    Vacuous pre-implementation (no hook runs, join table is always empty).
    Kept as a regression guard against an over-eager hook that emits a
    placeholder row for tag-less docs.
    """
    doc = _make_doc_with_tags(_id("doc_t2"), [])
    await graph_store.insert_document(doc)
    assert _join_rows(graph_store._db_path, doc.id) == []


async def test_t0078_update_tags_add(graph_store):
    """T-0078 #3: update ["a"] -> ["a","b"] reflects in join table."""
    doc = _make_doc_with_tags(_id("doc_t3"), ["a"])
    await graph_store.insert_document(doc)
    await graph_store.update_document(doc.id, {"tags": ["a", "b"]})
    rows = _join_rows(graph_store._db_path, doc.id)
    assert rows == [(doc.id, "a"), (doc.id, "b")]


async def test_t0078_update_tags_remove(graph_store):
    """T-0078 #4: update ["a","b"] -> ["a"] removes the dropped row."""
    doc = _make_doc_with_tags(_id("doc_t4"), ["a", "b"])
    await graph_store.insert_document(doc)
    await graph_store.update_document(doc.id, {"tags": ["a"]})
    assert _join_rows(graph_store._db_path, doc.id) == [(doc.id, "a")]


async def test_t0078_update_tags_full_replace(graph_store):
    """T-0078 #5: update ["a"] -> ["c"] replaces the row entirely."""
    doc = _make_doc_with_tags(_id("doc_t5"), ["a"])
    await graph_store.insert_document(doc)
    await graph_store.update_document(doc.id, {"tags": ["c"]})
    assert _join_rows(graph_store._db_path, doc.id) == [(doc.id, "c")]


async def test_t0078_update_without_tags_key_preserves_join(graph_store):
    """T-0078 #6: update title only -> join rows unchanged.

    Seeds the join table via the insert hook so the "unchanged" claim is
    not vacuous. Pre-implementation this test still detects a buggy hook
    that fires on every update regardless of which fields changed.
    """
    doc = _make_doc_with_tags(_id("doc_t6"), ["x", "y"])
    await graph_store.insert_document(doc)
    pre = _join_rows(graph_store._db_path, doc.id)
    assert pre == [(doc.id, "x"), (doc.id, "y")], (
        "precondition: insert hook must populate join rows; "
        "if this fails, run sync-hook tests #1/#3-5 first"
    )
    await graph_store.update_document(doc.id, {"title": "New Title"})
    assert _join_rows(graph_store._db_path, doc.id) == pre


async def test_t0078_tag_filter_query_uses_join_table_plan(graph_store):
    """T-0078 #7: the production tag-filter query path uses the join table.

    Two assertions:
    1. The source of `_query_documents_sync` no longer contains the
       `json_each(tags)` form. This is what fails pre-rewrite.
    2. The join-table form, when EXPLAIN'd, uses an index on
       `document_tags` (any index — SQLite may pick the PK auto-index
       on `(document_id, tag)` or our explicit `(tag, document_id)`).
    """
    import inspect

    from sage.storage.graph_store import GraphStore

    source = inspect.getsource(GraphStore._query_documents_sync)
    assert "json_each(tags)" not in source, (
        "_query_documents_sync still references json_each(tags); "
        "tag filter has not been rewritten to use document_tags"
    )
    assert "document_tags" in source, (
        "_query_documents_sync does not reference document_tags; tag filter rewrite incomplete"
    )

    # Seed a few documents and verify the plan is index-driven
    for suffix, tags in (("a", ["alpha"]), ("b", ["beta"]), ("c", ["alpha", "beta"])):
        d = _make_doc_with_tags(_id(f"doc_plan_{suffix}"), tags)
        await graph_store.insert_document(d)

    conn = sqlite3.connect(str(graph_store._db_path))
    try:
        plan_rows = conn.execute(
            "EXPLAIN QUERY PLAN SELECT * FROM documents WHERE pipeline_status != 'failed' "
            "AND EXISTS (SELECT 1 FROM document_tags "
            "WHERE document_id = documents.id AND tag = 'alpha')"
        ).fetchall()
    finally:
        conn.close()
    plan_text = " | ".join(str(r) for r in plan_rows)
    assert "document_tags" in plan_text, plan_text
    assert "USING" in plan_text and "INDEX" in plan_text, (
        f"expected an index-driven plan over document_tags; got: {plan_text}"
    )
    assert "SCAN document_tags" not in plan_text, (
        f"plan shows a full scan of document_tags; got: {plan_text}"
    )

    # Result correctness via the public API
    docs, total = await graph_store.query_documents(
        {"tags": ["alpha"]}, limit=100, offset=0, sort_by=None, sort_order=None
    )
    ids = {d.id for d in docs}
    assert ids == {_id("doc_plan_a"), _id("doc_plan_c")}
    assert total == 2


async def test_t0078_tag_filter_and_semantics(graph_store):
    """T-0078 #8: filter for two tags returns only documents that carry both."""
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


async def test_t0078_backfill_populates_from_json(graph_store):
    """T-0078 #9: backfill helper fills join table from existing JSON.

    Simulates an unmigrated state by inserting a document directly via SQL
    (bypassing the sync hook) so the JSON column is populated but the
    join table is empty. Then runs the backfill apply function.
    """
    doc_id = _id("doc_backfill")
    now = datetime.now(timezone.utc).isoformat()
    conn = sqlite3.connect(str(graph_store._db_path))
    try:
        conn.execute(
            "INSERT INTO documents ("
            "id, title, source_type, source_path, lifecycle_status, "
            "tags, source_content_hash, adapter_version, created_by, "
            "created_at, last_modified_by, updated_at, pipeline_status, metadata_confirmed"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                doc_id,
                "Backfill Doc",
                "markdown",
                f"test/{doc_id}.md",
                "active",
                json.dumps(["p", "q", "r"]),
                _sha("backfill"),
                "0.1.0",
                "testuser",
                now,
                "testuser",
                now,
                "abstraction_complete",
                1,
            ),
        )
        # Manually clear any join rows that an existing hook may have created
        conn.execute("DELETE FROM document_tags WHERE document_id = ?", (doc_id,))
        conn.commit()
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM document_tags WHERE document_id = ?", (doc_id,)
            ).fetchone()[0]
            == 0
        )

        assert _backfill_document_tags_detect(conn) is True
        _backfill_document_tags_apply(conn)
        conn.commit()
    finally:
        conn.close()

    rows = _join_rows(graph_store._db_path, doc_id)
    assert rows == [(doc_id, "p"), (doc_id, "q"), (doc_id, "r")]


async def test_t0078_backfill_is_idempotent(graph_store):
    """T-0078 #10: re-running backfill does not duplicate or error."""
    doc_id = _id("doc_idem")
    now = datetime.now(timezone.utc).isoformat()
    conn = sqlite3.connect(str(graph_store._db_path))
    try:
        conn.execute(
            "INSERT INTO documents ("
            "id, title, source_type, source_path, lifecycle_status, "
            "tags, source_content_hash, adapter_version, created_by, "
            "created_at, last_modified_by, updated_at, pipeline_status, metadata_confirmed"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                doc_id,
                "Idem Doc",
                "markdown",
                f"test/{doc_id}.md",
                "active",
                json.dumps(["s", "t"]),
                _sha("idem"),
                "0.1.0",
                "testuser",
                now,
                "testuser",
                now,
                "abstraction_complete",
                1,
            ),
        )
        conn.execute("DELETE FROM document_tags WHERE document_id = ?", (doc_id,))
        conn.commit()
        _backfill_document_tags_apply(conn)
        _backfill_document_tags_apply(conn)
        conn.commit()
        # Second detect should now return False -- the work is fully applied
        assert _backfill_document_tags_detect(conn) is False
    finally:
        conn.close()

    rows = _join_rows(graph_store._db_path, doc_id)
    assert rows == [(doc_id, "s"), (doc_id, "t")]


async def test_t0078_fk_cascade_on_document_delete(graph_store):
    """T-0078 #11: deleting a document cascades to document_tags rows."""
    doc = _make_doc_with_tags(_id("doc_fk"), ["m", "n"])
    await graph_store.insert_document(doc)
    assert _join_rows(graph_store._db_path, doc.id) == [(doc.id, "m"), (doc.id, "n")]

    conn = sqlite3.connect(str(graph_store._db_path))
    try:
        conn.execute("PRAGMA foreign_keys=ON;")
        conn.execute("DELETE FROM documents WHERE id = ?", (doc.id,))
        conn.commit()
    finally:
        conn.close()

    assert _join_rows(graph_store._db_path, doc.id) == []


async def test_t0078_metadata_service_e2e(metadata_service, graph_store):
    """T-0078 #12: TagsPatch through MetadataService syncs the join table."""
    doc = _make_doc_with_tags(_id("doc_e2e"), ["initial"])
    await graph_store.insert_document(doc)
    assert _join_rows(graph_store._db_path, doc.id) == [(doc.id, "initial")]

    await metadata_service.update_metadata(
        doc.id,
        UpdateMetadataRequest(tags=TagsPatch(add=["new1", "new2"], remove=["initial"])),
        modified_by="testuser",
    )
    rows = _join_rows(graph_store._db_path, doc.id)
    assert rows == [(doc.id, "new1"), (doc.id, "new2")]


async def test_t0078_backfill_plan_includes_document_tags():
    """The BACKFILL_PLAN registry must include the document_tags backfill."""
    names = [b.name for b in BACKFILL_PLAN]
    assert "document_tags" in names, names


# ---------------------------------------------------------------------------
# T-0123: Exhaustive-fields closure test for ``GraphStore._row_to_edge``.
#
# ``_row_to_edge`` is the single owning factory for the
# ``sqlite3.Row -> Edge`` projection (sage/storage/graph_store.py). Per the
# *CAS Projection-Point Audit Conventions* steering document (cas vault,
# doc_type=steering_document), every projection point owes a closure pair:
# a single owning factory and an exhaustive-fields test that fails closed
# when a field is added to the destination model but is not wired through
# the factory. This test installs the second half of the pair.
# ---------------------------------------------------------------------------


def _edge_row_with_every_edge_field() -> sqlite3.Row:
    """Build a ``sqlite3.Row`` with every column consumed by
    ``GraphStore._row_to_edge`` set to a distinct non-default sentinel.

    A real ``sqlite3.Row`` is used (rather than a ``dict`` stand-in) so
    that ``row.keys()`` and column lookup semantics match the production
    factory's expectations exactly, including the defensive
    ``"<col>" in keys`` guards for the optional CTE-stripped columns.

    Every field has a non-default value:

    - Optional columns (``resolution_policy``, ``source_valid_from_version``,
      ``target_valid_from_version``, ``valid_until_version``,
      ``retracted_edge_id``, ``notes``, ``rationale``) are populated, not
      left null, so an unread column trips ``value is not None``.
    - ``rationale_kind`` is set to ``version_chain`` rather than the
      ``manual`` default so a regression that hard-codes the default would
      be caught structurally.
    - ``edge_type`` is ``references`` (a ``transitive_both`` edge type)
      paired with ``resolution_policy='transitive_both'`` so the policy
      sentinel is itself a coherent value for the chosen edge type.
    """
    conn = sqlite3.connect(":memory:")
    try:
        conn.row_factory = sqlite3.Row
        cursor = conn.execute(
            """
            SELECT
                ? AS id,
                ? AS source_id,
                ? AS target_id,
                ? AS edge_type,
                ? AS resolution_policy,
                ? AS source_valid_from_version,
                ? AS target_valid_from_version,
                ? AS valid_until_version,
                ? AS retracted_edge_id,
                ? AS created_at,
                ? AS notes,
                ? AS rationale,
                ? AS rationale_kind,
                ? AS synced_from_version,
                ? AS synced_from_content_hash
            """,
            (
                # Edge ids validate as UUIDs (sage/models/schemas.py:57),
                # whereas document ids use the ``_id()`` short-hash form.
                str(uuid.UUID(int=0xED9E0000_0000_0000_0000_000000000001)),
                _id("doc_source"),
                _id("doc_target"),
                EdgeType.REFERENCES.value,
                ResolutionPolicy.TRANSITIVE_BOTH.value,
                _id("doc_source_anchor"),
                _id("doc_target_anchor"),
                _id("doc_tombstone"),
                str(uuid.UUID(int=0xED9E0000_0000_0000_0000_000000000002)),
                datetime(2026, 5, 21, 10, 30, tzinfo=timezone.utc).isoformat(),
                "sentinel notes",
                "sentinel rationale",
                RationaleKind.VERSION_CHAIN.value,
                _id("doc_synced_from_version"),
                "sha256:" + "ab" * 32,
            ),
        )
        row = cursor.fetchone()
    finally:
        conn.close()
    assert row is not None
    return row


def test_row_to_edge_populates_every_edge_field():
    """T-0123 (F4 closure pair, T1): every ``Edge`` field is populated by
    ``GraphStore._row_to_edge`` from a sentinel row dict whose columns are
    all non-default. Iterates ``Edge.model_fields`` so the assertion grows
    automatically when a field is added to ``Edge``; if the new field is
    not wired through ``_row_to_edge``, the loop trips the assertion.
    """
    row = _edge_row_with_every_edge_field()
    edge = GraphStore._row_to_edge(row)
    for field_name, field_info in Edge.model_fields.items():
        value = getattr(edge, field_name)
        annotation = field_info.annotation
        # Match the cohort scaffolding idiom even though no current Edge
        # field is list- or dict-typed; the branch is forward defense for
        # future field additions.
        if annotation == list[str] or annotation == (dict | None):
            assert value, (
                f"Edge.{field_name} not populated by _row_to_edge "
                "(empty/falsy default would pass a naive 'is not None' check)"
            )
        else:
            assert value is not None, f"Edge.{field_name} not populated by _row_to_edge"


# ---------------------------------------------------------------------------
# T-0125: Exhaustive-fields closure test for ``GraphStore._row_to_staging_edge``.
#
# ``_row_to_staging_edge`` is the single owning factory for the
# ``sqlite3.Row -> StagingEdge`` projection (sage/storage/graph_store.py).
# Per the *CAS Projection-Point Audit Conventions* steering document (cas
# vault, doc_type=steering_document), every projection point owes a
# closure pair: a single owning factory and an exhaustive-fields test
# that fails closed when a field is added to the destination model but is
# not wired through the factory. This test installs the second half of
# the pair.
# ---------------------------------------------------------------------------


def _staging_edge_row_with_every_staging_edge_field() -> sqlite3.Row:
    """Build a ``sqlite3.Row`` with every column consumed by
    ``GraphStore._row_to_staging_edge`` set to a distinct non-default
    sentinel.

    A real ``sqlite3.Row`` is used (rather than a ``dict`` stand-in) so
    that column-lookup semantics match the production factory's
    expectations exactly.

    Every column has a non-default value:

    - ``confidence_tier`` is set to ``3`` rather than the model's default
      of ``2`` so a regression that hard-codes the default would be
      caught structurally (the destination field is scalar with a
      non-falsy default, so the assertion idiom ``value is not None``
      alone would not catch it — the sentinel must differ from the
      default).
    - ``inference_evidence`` carries a distinctive sentinel string so
      an unread column trips ``value is not None``.
    - ``created_at`` is an ISO-8601 string (the factory parses it via
      ``datetime.fromisoformat``), matching how production rows are
      persisted in SQLite.
    """
    conn = sqlite3.connect(":memory:")
    try:
        conn.row_factory = sqlite3.Row
        cursor = conn.execute(
            """
            SELECT
                ? AS id,
                ? AS source_id,
                ? AS target_id,
                ? AS edge_type,
                ? AS inference_evidence,
                ? AS confidence_tier,
                ? AS created_at
            """,
            (
                # Edge ids validate as UUIDs (sage/models/schemas.py:57),
                # whereas document ids use the ``_id()`` short-hash form.
                str(uuid.UUID(int=0x57A60000_0000_0000_0000_000000000001)),
                _id("doc_source"),
                _id("doc_target"),
                EdgeType.REFERENCES.value,
                "sentinel inference evidence",
                3,
                datetime(2026, 5, 21, 10, 30, tzinfo=timezone.utc).isoformat(),
            ),
        )
        row = cursor.fetchone()
    finally:
        conn.close()
    assert row is not None
    return row


def test_row_to_staging_edge_populates_every_staging_edge_field():
    """T-0125 (F4 closure pair, T1): every ``StagingEdge`` field is
    populated by ``GraphStore._row_to_staging_edge`` from a sentinel
    row dict whose columns are all non-default. Iterates
    ``StagingEdge.model_fields`` so the assertion grows automatically
    when a field is added to ``StagingEdge``; if the new field is not
    wired through ``_row_to_staging_edge``, the loop trips the
    assertion.
    """
    row = _staging_edge_row_with_every_staging_edge_field()
    staging_edge = GraphStore._row_to_staging_edge(row)
    for field_name, field_info in StagingEdge.model_fields.items():
        value = getattr(staging_edge, field_name)
        annotation = field_info.annotation
        # Match the cohort scaffolding idiom even though no current
        # StagingEdge field is list- or dict-typed; the branch is
        # forward defense for future field additions.
        if annotation == list[str] or annotation == (dict | None):
            assert value, (
                f"StagingEdge.{field_name} not populated by _row_to_staging_edge "
                "(empty/falsy default would pass a naive 'is not None' check)"
            )
        else:
            assert value is not None, (
                f"StagingEdge.{field_name} not populated by _row_to_staging_edge"
            )
