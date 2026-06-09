"""— SQLite expression-index additions for tier3_metadata fields.

Verifies that:

1. Fresh-init creates the three expression indexes for the canonical
   high-frequency tier3 fields: ``ticket_id``, ``failure_id``,
   ``tool_name``.
2. SQLite's query planner picks each expression index for the
   corresponding ``json_extract(tier3_metadata, '$.<field>') = ?``
   predicate. This is the O(log D) gate: an index that exists but
   never gets selected is a silent regression.
3. Re-initialization against an existing DB is idempotent.
"""

from __future__ import annotations

import sqlite3

from sage.storage.graph_store import SqliteGraphStore


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


async def test_t0075_expression_indexes_present_after_init(tmp_path):
    db_path = tmp_path / "graph.db"
    store = SqliteGraphStore(db_path)
    await store.initialize()
    await store.close()

    indexes = _index_names(db_path)
    assert "idx_tier3_ticket_id" in indexes
    assert "idx_tier3_failure_id" in indexes
    assert "idx_tier3_tool_name" in indexes


async def test_t0075_query_planner_picks_ticket_id_expression_index(tmp_path):
    db_path = tmp_path / "graph.db"
    store = SqliteGraphStore(db_path)
    await store.initialize()
    await store.close()

    plan = _explain(
        db_path,
        "SELECT id FROM documents WHERE json_extract(tier3_metadata, '$.ticket_id') = ?",
        ("T-0001",),
    )
    assert "idx_tier3_ticket_id" in plan, plan


async def test_t0075_query_planner_picks_failure_id_expression_index(tmp_path):
    db_path = tmp_path / "graph.db"
    store = SqliteGraphStore(db_path)
    await store.initialize()
    await store.close()

    plan = _explain(
        db_path,
        "SELECT id FROM documents WHERE json_extract(tier3_metadata, '$.failure_id') = ?",
        ("F1",),
    )
    assert "idx_tier3_failure_id" in plan, plan


async def test_t0075_query_planner_picks_tool_name_expression_index(tmp_path):
    db_path = tmp_path / "graph.db"
    store = SqliteGraphStore(db_path)
    await store.initialize()
    await store.close()

    plan = _explain(
        db_path,
        "SELECT id FROM documents WHERE json_extract(tier3_metadata, '$.tool_name') = ?",
        ("ruff",),
    )
    assert "idx_tier3_tool_name" in plan, plan


async def test_t0075_init_is_idempotent_on_existing_db(tmp_path):
    db_path = tmp_path / "graph.db"

    store1 = SqliteGraphStore(db_path)
    await store1.initialize()
    await store1.close()
    before = _index_names(db_path)

    store2 = SqliteGraphStore(db_path)
    await store2.initialize()
    await store2.close()
    after = _index_names(db_path)

    assert before == after
