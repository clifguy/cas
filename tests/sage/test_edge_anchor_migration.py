"""Tests for scripts/migrate_edge_anchors.py (CAS-ADR-017, Chunk 3).

Operates on a bare SQLite database built from the real DDL, seeding
legacy-shaped edge rows (NULL policy, NULL anchors) via direct INSERT.
This is what a pre-Chunk-2 vault looks like before backfill.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest

from sage.models.enums import EdgeType, ResolutionPolicy
from sage.storage.migrations import INDEXES, MIGRATIONS, TABLES
from scripts.migrate_edge_anchors import (
    apply_backfill,
    apply_reverse,
    build_backfill_plan,
    build_reverse_plan,
    run_backfill,
    run_reverse,
)


# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------


def _make_db(tmp_path: Path) -> Path:
    """Create an empty SAGE-shaped SQLite DB at tmp_path/graph.db."""
    db_path = tmp_path / "graph.db"
    conn = sqlite3.connect(str(db_path))
    try:
        for ddl in TABLES:
            conn.execute(ddl)
        for mig in MIGRATIONS:
            try:
                conn.execute(mig)
            except sqlite3.OperationalError:
                pass  # column already exists on fresh schema
        for ddl in INDEXES:
            conn.execute(ddl)
        conn.commit()
    finally:
        conn.close()
    return db_path


def _insert_doc(conn: sqlite3.Connection, doc_id: str) -> None:
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        "INSERT INTO documents (id, title, source_type, source_path, "
        "source_content_hash, adapter_version, created_by, created_at, "
        "last_modified_by, updated_at) "
        "VALUES (?, ?, 'markdown', ?, ?, '0.1.0', 'testuser', ?, 'testuser', ?)",
        (doc_id, f"Doc {doc_id}", f"test/{doc_id}.md", f"hash_{doc_id}", now, now),
    )


def _insert_legacy_edge(
    conn: sqlite3.Connection,
    edge_id: str,
    source_id: str,
    target_id: str | None,
    edge_type: str,
) -> None:
    """Insert a legacy-shaped edge: NULL policy and NULL anchor columns."""
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        "INSERT INTO edges (id, source_id, target_id, edge_type, created_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (edge_id, source_id, target_id, edge_type, now),
    )


def _read_edge(conn: sqlite3.Connection, edge_id: str) -> dict:
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT resolution_policy, source_valid_from_version, "
        "target_valid_from_version, valid_until_version, retracted_edge_id "
        "FROM edges WHERE id = ?",
        (edge_id,),
    ).fetchone()
    return dict(row) if row else {}


def _seeded_db(tmp_path: Path) -> Path:
    """Create a DB with docs + one legacy edge per applicable policy."""
    db_path = _make_db(tmp_path)
    conn = sqlite3.connect(str(db_path))
    try:
        for doc_id in ("a1", "a2", "b1", "b2", "c1", "c2", "d1", "d2"):
            _insert_doc(conn, doc_id)
        # policy=none
        _insert_legacy_edge(
            conn, "edge-sup", "a2", "a1", EdgeType.SUPERSEDES.value
        )
        # policy=transitive_source
        _insert_legacy_edge(
            conn, "edge-der", "b1", "b2", EdgeType.DERIVED_FROM.value
        )
        # policy=transitive_both
        _insert_legacy_edge(
            conn, "edge-ref", "c1", "c2", EdgeType.REFERENCES.value
        )
        _insert_legacy_edge(
            conn, "edge-cov", "d1", "d2", EdgeType.COVERS.value
        )
        conn.commit()
    finally:
        conn.close()
    return db_path


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_backfill_populates_all_three_policies(tmp_path):
    db_path = _seeded_db(tmp_path)

    rc = run_backfill(db_path, execute=True)
    assert rc == 0

    conn = sqlite3.connect(str(db_path))
    try:
        sup = _read_edge(conn, "edge-sup")
        assert sup["resolution_policy"] == ResolutionPolicy.NONE.value
        assert sup["source_valid_from_version"] is None
        assert sup["target_valid_from_version"] is None
        assert sup["valid_until_version"] is None
        assert sup["retracted_edge_id"] is None

        der = _read_edge(conn, "edge-der")
        assert der["resolution_policy"] == ResolutionPolicy.TRANSITIVE_SOURCE.value
        assert der["source_valid_from_version"] == "b1"
        # transitive_source: target anchor is not applicable per CAS-ADR-017
        # (target is frozen at derivation; version specificity carried by
        # target_id). Backfill leaves target_valid_from_version null.
        assert der["target_valid_from_version"] is None

        ref = _read_edge(conn, "edge-ref")
        assert ref["resolution_policy"] == ResolutionPolicy.TRANSITIVE_BOTH.value
        assert ref["source_valid_from_version"] == "c1"
        assert ref["target_valid_from_version"] == "c2"

        cov = _read_edge(conn, "edge-cov")
        assert cov["resolution_policy"] == ResolutionPolicy.TRANSITIVE_BOTH.value
        assert cov["source_valid_from_version"] == "d1"
        assert cov["target_valid_from_version"] == "d2"
    finally:
        conn.close()


def test_backfill_is_idempotent(tmp_path):
    db_path = _seeded_db(tmp_path)

    assert run_backfill(db_path, execute=True) == 0

    # Second pass: plan should find nothing to backfill.
    conn = sqlite3.connect(str(db_path))
    try:
        from sage.models.edge_registry import EdgeTypeRegistry

        plan = build_backfill_plan(conn, EdgeTypeRegistry.default())
        assert plan.total_applicable == 0
        assert plan.tbd_edges == []
    finally:
        conn.close()

    # Re-running end-to-end is a no-op: exit 0, no mutations.
    assert run_backfill(db_path, execute=True) == 0


def test_tbd_edge_halts_run_without_mutation(tmp_path, capsys):
    db_path = _seeded_db(tmp_path)
    conn = sqlite3.connect(str(db_path))
    try:
        _insert_doc(conn, "e1")
        _insert_doc(conn, "e2")
        _insert_legacy_edge(
            conn, "edge-auth", "e1", "e2", EdgeType.AUTHORITATIVE_FOR.value
        )
        conn.commit()
    finally:
        conn.close()

    # Dry-run halts with exit 2, no mutations.
    rc = run_backfill(db_path, execute=False)
    assert rc == 2
    # Execute attempt also halts with exit 2 before any UPDATE runs.
    rc = run_backfill(db_path, execute=True)
    assert rc == 2

    conn = sqlite3.connect(str(db_path))
    try:
        # No edge has been mutated, including the otherwise-eligible ones.
        for edge_id in ("edge-sup", "edge-der", "edge-ref", "edge-cov", "edge-auth"):
            row = _read_edge(conn, edge_id)
            assert row["resolution_policy"] is None, f"{edge_id} was mutated"
            assert row["source_valid_from_version"] is None
            assert row["target_valid_from_version"] is None
    finally:
        conn.close()


def test_apply_backfill_refuses_tbd(tmp_path):
    """The apply function itself guards against TBD, independent of run_*."""
    db_path = _make_db(tmp_path)
    conn = sqlite3.connect(str(db_path))
    try:
        _insert_doc(conn, "x1")
        _insert_doc(conn, "x2")
        _insert_legacy_edge(
            conn, "edge-tbd", "x1", "x2", EdgeType.AUTHORITATIVE_FOR.value
        )
        conn.commit()

        from sage.models.edge_registry import EdgeTypeRegistry

        plan = build_backfill_plan(conn, EdgeTypeRegistry.default())
        assert plan.tbd_edges == [("edge-tbd", EdgeType.AUTHORITATIVE_FOR.value)]
        with pytest.raises(RuntimeError, match="TBD-policy"):
            apply_backfill(conn, plan)
    finally:
        conn.close()


def test_dry_run_performs_no_writes(tmp_path):
    db_path = _seeded_db(tmp_path)

    rc = run_backfill(db_path, execute=False)
    assert rc == 0

    conn = sqlite3.connect(str(db_path))
    try:
        for edge_id in ("edge-sup", "edge-der", "edge-ref", "edge-cov"):
            row = _read_edge(conn, edge_id)
            assert row["resolution_policy"] is None
            assert row["source_valid_from_version"] is None
            assert row["target_valid_from_version"] is None
    finally:
        conn.close()


def test_reverse_nulls_all_adr017_columns(tmp_path):
    db_path = _seeded_db(tmp_path)
    assert run_backfill(db_path, execute=True) == 0
    assert run_reverse(db_path, execute=True) == 0

    conn = sqlite3.connect(str(db_path))
    try:
        for edge_id in ("edge-sup", "edge-der", "edge-ref", "edge-cov"):
            row = _read_edge(conn, edge_id)
            assert row["resolution_policy"] is None
            assert row["source_valid_from_version"] is None
            assert row["target_valid_from_version"] is None
            assert row["valid_until_version"] is None
            assert row["retracted_edge_id"] is None
    finally:
        conn.close()


def test_reverse_dry_run_performs_no_writes(tmp_path):
    db_path = _seeded_db(tmp_path)
    assert run_backfill(db_path, execute=True) == 0

    rc = run_reverse(db_path, execute=False)
    assert rc == 0

    conn = sqlite3.connect(str(db_path))
    try:
        der = _read_edge(conn, "edge-der")
        assert der["resolution_policy"] == ResolutionPolicy.TRANSITIVE_SOURCE.value
        assert der["source_valid_from_version"] == "b1"
    finally:
        conn.close()


def test_mixed_prepopulated_and_legacy_rows(tmp_path):
    """Rows with non-NULL resolution_policy are left untouched; others backfill."""
    db_path = _make_db(tmp_path)
    conn = sqlite3.connect(str(db_path))
    try:
        _insert_doc(conn, "p1")
        _insert_doc(conn, "p2")
        _insert_doc(conn, "q1")
        _insert_doc(conn, "q2")
        # Legacy (NULL policy)
        _insert_legacy_edge(
            conn, "edge-legacy", "p1", "p2", EdgeType.REFERENCES.value
        )
        # Pre-populated (written by validator-like path). Intentionally
        # different anchors to prove backfill did not overwrite.
        now = datetime.now(timezone.utc).isoformat()
        conn.execute(
            "INSERT INTO edges (id, source_id, target_id, edge_type, "
            "resolution_policy, source_valid_from_version, "
            "target_valid_from_version, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "edge-prepop",
                "q1",
                "q2",
                EdgeType.REFERENCES.value,
                ResolutionPolicy.TRANSITIVE_BOTH.value,
                "custom-anchor-src",
                "custom-anchor-tgt",
                now,
            ),
        )
        conn.commit()
    finally:
        conn.close()

    assert run_backfill(db_path, execute=True) == 0

    conn = sqlite3.connect(str(db_path))
    try:
        legacy = _read_edge(conn, "edge-legacy")
        assert legacy["resolution_policy"] == ResolutionPolicy.TRANSITIVE_BOTH.value
        assert legacy["source_valid_from_version"] == "p1"
        assert legacy["target_valid_from_version"] == "p2"

        prepop = _read_edge(conn, "edge-prepop")
        assert prepop["resolution_policy"] == ResolutionPolicy.TRANSITIVE_BOTH.value
        assert prepop["source_valid_from_version"] == "custom-anchor-src"
        assert prepop["target_valid_from_version"] == "custom-anchor-tgt"
    finally:
        conn.close()


def test_plan_counts_reported_per_policy(tmp_path, capsys):
    db_path = _seeded_db(tmp_path)
    rc = run_backfill(db_path, execute=False)
    assert rc == 0
    out = capsys.readouterr().out
    assert "Edges needing backfill: 4" in out
    assert "policy=none:              1" in out
    assert "policy=transitive_source: 1" in out
    assert "policy=transitive_both:   2" in out
