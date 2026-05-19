"""Tests for scripts/dedup_edges.py — the T-0079 backfill script.

The script removes duplicate (source_id, target_id, edge_type) rows
from ``edges`` and ``staging_edges`` so the T-0079 ``CREATE UNIQUE
INDEX`` migration can be applied cleanly. These tests build a
legacy-shaped DB (no unique index) seeded with duplicate edges, drive
the ``run`` entry point, and verify both the JSON audit trail and the
post-run row state under dry-run, ``--apply``, and the
``--force-divergent-rationale`` gate.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from scripts.dedup_edges import run

# Legacy-shaped tables (no unique index). Mirrors the production CREATE
# TABLE definitions minus the T-0079 index migration so the test can
# seed duplicate rows that would otherwise be rejected.
_DOCS_DDL = """\
CREATE TABLE documents (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    source_type TEXT NOT NULL,
    source_path TEXT NOT NULL,
    source_content_hash TEXT NOT NULL,
    adapter_version TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    last_modified_by TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""

_EDGES_DDL = """\
CREATE TABLE edges (
    id TEXT PRIMARY KEY,
    source_id TEXT NOT NULL,
    target_id TEXT,
    edge_type TEXT NOT NULL,
    created_at TEXT NOT NULL,
    rationale TEXT
);
"""

_STAGING_DDL = """\
CREATE TABLE staging_edges (
    id TEXT PRIMARY KEY,
    source_id TEXT NOT NULL,
    target_id TEXT NOT NULL,
    edge_type TEXT NOT NULL,
    inference_evidence TEXT NOT NULL,
    confidence_tier INTEGER NOT NULL DEFAULT 2,
    created_at TEXT NOT NULL
);
"""


def _build_db(db_path: Path) -> None:
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(_DOCS_DDL)
        conn.execute(_EDGES_DDL)
        conn.execute(_STAGING_DDL)
        # Seed two documents for FK-shaped completeness (no FK constraint
        # is set on the legacy DDL above, but it keeps the schema
        # readable).
        now = datetime.now(timezone.utc).isoformat()
        for doc_id in ("aaaa1111_a", "bbbb2222_b"):
            conn.execute(
                "INSERT INTO documents (id, title, source_type, source_path, "
                "source_content_hash, adapter_version, created_by, created_at, "
                "last_modified_by, updated_at) VALUES (?, 'T', 'markdown', ?, "
                "'sha256:abc', '0.1.0', 'tester', ?, 'tester', ?)",
                (doc_id, f"src/{doc_id}.md", now, now),
            )
        conn.commit()
    finally:
        conn.close()


def _seed_edges(db_path: Path, rows: list[tuple]) -> None:
    """rows: (id, source_id, target_id, edge_type, created_at, rationale)."""
    conn = sqlite3.connect(str(db_path))
    try:
        conn.executemany(
            "INSERT INTO edges (id, source_id, target_id, edge_type, created_at, "
            "rationale) VALUES (?, ?, ?, ?, ?, ?)",
            rows,
        )
        conn.commit()
    finally:
        conn.close()


def _seed_staging_edges(db_path: Path, rows: list[tuple]) -> None:
    """rows: (id, source_id, target_id, edge_type, evidence, tier, created_at)."""
    conn = sqlite3.connect(str(db_path))
    try:
        conn.executemany(
            "INSERT INTO staging_edges (id, source_id, target_id, edge_type, "
            "inference_evidence, confidence_tier, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            rows,
        )
        conn.commit()
    finally:
        conn.close()


def _count(db_path: Path, table: str) -> int:
    conn = sqlite3.connect(str(db_path))
    try:
        return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]  # noqa: S608 -- table value is a test-internal literal ("edges" or "staging_edges")
    finally:
        conn.close()


def _capture_stdout(capsys) -> list[dict]:
    captured = capsys.readouterr()
    return [
        json.loads(line)
        for line in captured.out.strip().splitlines()
        if line and not line.startswith("#")
    ]


# ---------------------------------------------------------------------------
# Dry run: emits audit trail, leaves data alone
# ---------------------------------------------------------------------------


def test_dedup_dry_run_reports_without_writing(tmp_path, capsys):
    db_path = tmp_path / "graph.db"
    _build_db(db_path)
    base = datetime.now(timezone.utc) - timedelta(hours=3)
    _seed_edges(
        db_path,
        [
            ("edge-1", "aaaa1111_a", "bbbb2222_b", "references", base.isoformat(), "r"),
            (
                "edge-2",
                "aaaa1111_a",
                "bbbb2222_b",
                "references",
                (base + timedelta(hours=1)).isoformat(),
                "r",
            ),
            (
                "edge-3",
                "aaaa1111_a",
                "bbbb2222_b",
                "references",
                (base + timedelta(hours=2)).isoformat(),
                "r",
            ),
        ],
    )

    pre_count = _count(db_path, "edges")
    rc = run(db_path, apply=False, force_divergent_rationale=False)
    assert rc == 0
    records = _capture_stdout(capsys)
    assert len(records) == 1
    assert records[0]["kept"] == "edge-1"  # oldest
    assert set(records[0]["dropped"]) == {"edge-2", "edge-3"}
    assert records[0]["divergent_rationale"] is False
    # No mutation in dry-run.
    assert _count(db_path, "edges") == pre_count


# ---------------------------------------------------------------------------
# Apply: deletes the dropped rows, audit trail records the action
# ---------------------------------------------------------------------------


def test_dedup_apply_deletes_drops_keeps_oldest(tmp_path, capsys):
    db_path = tmp_path / "graph.db"
    _build_db(db_path)
    base = datetime.now(timezone.utc) - timedelta(hours=3)
    _seed_edges(
        db_path,
        [
            ("edge-A", "aaaa1111_a", "bbbb2222_b", "references", base.isoformat(), "r"),
            (
                "edge-B",
                "aaaa1111_a",
                "bbbb2222_b",
                "references",
                (base + timedelta(hours=1)).isoformat(),
                "r",
            ),
        ],
    )

    rc = run(db_path, apply=True, force_divergent_rationale=False)
    assert rc == 0

    # Audit trail captured the action.
    records = _capture_stdout(capsys)
    assert records[0]["kept"] == "edge-A"
    assert records[0]["dropped"] == ["edge-B"]

    # DB state: only the kept row survives.
    conn = sqlite3.connect(str(db_path))
    try:
        ids = {r[0] for r in conn.execute("SELECT id FROM edges").fetchall()}
    finally:
        conn.close()
    assert ids == {"edge-A"}


# ---------------------------------------------------------------------------
# Tiebreaker: equal created_at falls back to lexicographic id
# ---------------------------------------------------------------------------


def test_dedup_tiebreaker_lexicographic_id(tmp_path, capsys):
    db_path = tmp_path / "graph.db"
    _build_db(db_path)
    now = datetime.now(timezone.utc).isoformat()
    _seed_edges(
        db_path,
        [
            ("edge-z", "aaaa1111_a", "bbbb2222_b", "references", now, "r"),
            ("edge-a", "aaaa1111_a", "bbbb2222_b", "references", now, "r"),
            ("edge-m", "aaaa1111_a", "bbbb2222_b", "references", now, "r"),
        ],
    )

    rc = run(db_path, apply=True, force_divergent_rationale=False)
    assert rc == 0
    records = _capture_stdout(capsys)
    assert records[0]["kept"] == "edge-a"  # lex-min wins the tiebreak


# ---------------------------------------------------------------------------
# Divergent rationale: --apply is blocked without --force-divergent-rationale
# ---------------------------------------------------------------------------


def test_dedup_divergent_rationale_blocks_apply(tmp_path, capsys):
    db_path = tmp_path / "graph.db"
    _build_db(db_path)
    base = datetime.now(timezone.utc) - timedelta(hours=2)
    _seed_edges(
        db_path,
        [
            (
                "edge-1",
                "aaaa1111_a",
                "bbbb2222_b",
                "references",
                base.isoformat(),
                "rationale-one",
            ),
            (
                "edge-2",
                "aaaa1111_a",
                "bbbb2222_b",
                "references",
                (base + timedelta(hours=1)).isoformat(),
                "rationale-DIFFERENT",
            ),
        ],
    )
    pre_count = _count(db_path, "edges")

    rc = run(db_path, apply=True, force_divergent_rationale=False)
    assert rc == 1  # refused

    records = _capture_stdout(capsys)
    assert records[0]["divergent_rationale"] is True

    # No mutation: rows still present.
    assert _count(db_path, "edges") == pre_count


def test_dedup_divergent_rationale_force_proceeds(tmp_path, capsys):
    db_path = tmp_path / "graph.db"
    _build_db(db_path)
    base = datetime.now(timezone.utc) - timedelta(hours=2)
    _seed_edges(
        db_path,
        [
            (
                "edge-1",
                "aaaa1111_a",
                "bbbb2222_b",
                "references",
                base.isoformat(),
                "rationale-one",
            ),
            (
                "edge-2",
                "aaaa1111_a",
                "bbbb2222_b",
                "references",
                (base + timedelta(hours=1)).isoformat(),
                "rationale-DIFFERENT",
            ),
        ],
    )

    rc = run(db_path, apply=True, force_divergent_rationale=True)
    assert rc == 0
    # Older row survives even with divergent rationale; the operator
    # owns the call.
    conn = sqlite3.connect(str(db_path))
    try:
        ids = {r[0] for r in conn.execute("SELECT id FROM edges").fetchall()}
    finally:
        conn.close()
    assert ids == {"edge-1"}


# ---------------------------------------------------------------------------
# Staging edges are deduped too
# ---------------------------------------------------------------------------


def test_dedup_processes_staging_edges(tmp_path, capsys):
    db_path = tmp_path / "graph.db"
    _build_db(db_path)
    base = datetime.now(timezone.utc) - timedelta(hours=2)
    _seed_staging_edges(
        db_path,
        [
            (
                "stage-1",
                "aaaa1111_a",
                "bbbb2222_b",
                "references",
                "filename_match",
                2,
                base.isoformat(),
            ),
            (
                "stage-2",
                "aaaa1111_a",
                "bbbb2222_b",
                "references",
                "filename_match",
                2,
                (base + timedelta(hours=1)).isoformat(),
            ),
        ],
    )

    rc = run(db_path, apply=True, force_divergent_rationale=False)
    assert rc == 0
    records = _capture_stdout(capsys)
    assert len(records) == 1
    assert records[0]["table"] == "staging_edges"
    assert records[0]["kept"] == "stage-1"

    # Staging edges remaining row count: 1.
    assert _count(db_path, "staging_edges") == 1


# ---------------------------------------------------------------------------
# No duplicates: clean exit, no audit records
# ---------------------------------------------------------------------------


def test_dedup_no_duplicates_clean_exit(tmp_path, capsys):
    db_path = tmp_path / "graph.db"
    _build_db(db_path)
    base = datetime.now(timezone.utc).isoformat()
    _seed_edges(
        db_path,
        [
            ("edge-only", "aaaa1111_a", "bbbb2222_b", "references", base, "r"),
        ],
    )

    rc = run(db_path, apply=True, force_divergent_rationale=False)
    assert rc == 0
    records = _capture_stdout(capsys)
    assert records == []
