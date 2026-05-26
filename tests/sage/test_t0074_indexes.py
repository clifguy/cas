"""— SQLite index additions: presence and query-plan tests.

Verifies that:

1. Fresh-init creates the three new ``documents`` indexes (``doc_type``,
   ``project``, and the composite ``(doc_type, lifecycle_status)``) plus
   the two composite edge indexes ``(source_id, edge_type)`` and
   ``(target_id, edge_type)``.
2. The obsolete single-column ``idx_edges_source`` and
   ``idx_edges_target`` are dropped (replaced by the composites, which
   serve single-column lookups via the left-prefix rule).
3. The retained ``idx_edges_type`` survives (still valid for
   type-only scans).
4. Re-initialization against an existing DB is idempotent.
5. Pre-existing legacy DBs are migrated forward: a DB pre-populated
   with the old single-column edge indexes loses them on init.
6. SQLite's query planner picks the new indexes for the canonical
   filter shapes that targets.
"""

from __future__ import annotations

import sqlite3

from sage.storage.graph_store import GraphStore


def _index_names(db_path) -> set[str]:
    conn = sqlite3.connect(str(db_path))
    try:
        return {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index' AND name NOT LIKE 'sqlite_%'"
            ).fetchall()
        }
    finally:
        conn.close()


def _explain(db_path, sql: str, params: tuple = ()) -> str:
    conn = sqlite3.connect(str(db_path))
    try:
        rows = conn.execute(f"EXPLAIN QUERY PLAN {sql}", params).fetchall()
        return "\n".join(str(r) for r in rows)
    finally:
        conn.close()


async def test_t0074_new_doc_indexes_present_after_init(tmp_path):
    db_path = tmp_path / "graph.db"
    store = GraphStore(db_path)
    await store.initialize()
    await store.close()

    indexes = _index_names(db_path)
    assert "idx_documents_doc_type" in indexes
    assert "idx_documents_project" in indexes
    assert "idx_documents_doc_type_lifecycle" in indexes


async def test_t0074_composite_edge_indexes_replace_single_column(tmp_path):
    db_path = tmp_path / "graph.db"
    store = GraphStore(db_path)
    await store.initialize()
    await store.close()

    indexes = _index_names(db_path)
    assert "idx_edges_source_type" in indexes
    assert "idx_edges_target_type" in indexes
    # Single-column edge indexes superseded.
    assert "idx_edges_source" not in indexes
    assert "idx_edges_target" not in indexes
    # Type-only index retained.
    assert "idx_edges_type" in indexes


async def test_t0074_init_is_idempotent_on_existing_db(tmp_path):
    db_path = tmp_path / "graph.db"

    store1 = GraphStore(db_path)
    await store1.initialize()
    await store1.close()
    before = _index_names(db_path)

    # Second initialize must not fail (the DROP and CREATE statements
    # are both idempotent forms) and must leave the index set unchanged.
    store2 = GraphStore(db_path)
    await store2.initialize()
    await store2.close()
    after = _index_names(db_path)

    assert before == after


async def test_t0074_legacy_single_column_edge_indexes_are_migrated_away(tmp_path):
    """Simulate a pre-DB that has idx_edges_source / idx_edges_target.

    After init, the legacy single-column edge indexes must be gone and
    the composites must be in place. This mirrors what happens on a
    real existing vault when the new code first runs.
    """
    db_path = tmp_path / "graph.db"

    # Seed the DB with just the edges table + the legacy single-column
    # indexes. No need to recreate the entire schema; the migration's
    # responsibility is bounded to its own DROP statements.
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            "CREATE TABLE edges ("
            "  id TEXT PRIMARY KEY, "
            "  source_id TEXT NOT NULL, "
            "  target_id TEXT, "
            "  edge_type TEXT NOT NULL, "
            "  created_at TEXT NOT NULL"
            ");"
        )
        conn.execute("CREATE INDEX idx_edges_source ON edges(source_id);")
        conn.execute("CREATE INDEX idx_edges_target ON edges(target_id);")
        conn.commit()
    finally:
        conn.close()

    pre_indexes = _index_names(db_path)
    assert "idx_edges_source" in pre_indexes
    assert "idx_edges_target" in pre_indexes

    store = GraphStore(db_path)
    await store.initialize(migrate=True)
    await store.close()

    post_indexes = _index_names(db_path)
    assert "idx_edges_source" not in post_indexes
    assert "idx_edges_target" not in post_indexes
    assert "idx_edges_source_type" in post_indexes
    assert "idx_edges_target_type" in post_indexes


async def test_t0074_query_planner_picks_new_doc_type_index(tmp_path):
    db_path = tmp_path / "graph.db"
    store = GraphStore(db_path)
    await store.initialize()
    await store.close()

    plan = _explain(db_path, "SELECT id FROM documents WHERE doc_type = ?", ("ticket",))
    assert "idx_documents_doc_type" in plan, plan


async def test_t0074_query_planner_picks_composite_doc_type_lifecycle(tmp_path):
    db_path = tmp_path / "graph.db"
    store = GraphStore(db_path)
    await store.initialize()
    await store.close()

    plan = _explain(
        db_path,
        "SELECT id FROM documents WHERE doc_type = ? AND lifecycle_status = ?",
        ("ticket", "active"),
    )
    assert "idx_documents_doc_type_lifecycle" in plan, plan


async def test_t0074_query_planner_picks_composite_edge_index_outbound(tmp_path):
    db_path = tmp_path / "graph.db"
    store = GraphStore(db_path)
    await store.initialize()
    await store.close()

    plan = _explain(
        db_path,
        "SELECT id FROM edges WHERE source_id = ? AND edge_type = ?",
        ("anysrc", "references"),
    )
    assert "idx_edges_source_type" in plan, plan


async def test_t0074_query_planner_picks_composite_edge_index_inbound(tmp_path):
    db_path = tmp_path / "graph.db"
    store = GraphStore(db_path)
    await store.initialize()
    await store.close()

    plan = _explain(
        db_path,
        "SELECT id FROM edges WHERE target_id = ? AND edge_type = ?",
        ("anytgt", "references"),
    )
    assert "idx_edges_target_type" in plan, plan


async def test_t0074_source_only_edge_query_uses_composite_via_left_prefix(tmp_path):
    """The composite (source_id, edge_type) must serve source-only queries
    via SQLite's left-prefix rule — that's what allows the old
    idx_edges_source to be dropped without regressing single-column
    traversal.
    """
    db_path = tmp_path / "graph.db"
    store = GraphStore(db_path)
    await store.initialize()
    await store.close()

    plan = _explain(db_path, "SELECT id FROM edges WHERE source_id = ?", ("anysrc",))
    # Either composite name is acceptable as long as some edge index is
    # selected (not a full scan).
    assert "idx_edges_source_type" in plan or "USING INDEX" in plan, plan
    assert "SCAN edges" not in plan, plan


async def test_t0111_query_planner_picks_synced_from_content_hash_index(tmp_path):
    """detector index: hash-equality scans over the edges table
    must use ``idx_edges_synced_from_content_hash`` so the per-vault
    drift sweep is index-driven, not a full scan.
    """
    db_path = tmp_path / "graph.db"
    store = GraphStore(db_path)
    await store.initialize()
    await store.close()

    plan = _explain(
        db_path,
        "SELECT id FROM edges WHERE synced_from_content_hash = ?",
        ("sha256:" + "a" * 64,),
    )
    assert "idx_edges_synced_from_content_hash" in plan, plan
    assert "SCAN edges" not in plan, plan
