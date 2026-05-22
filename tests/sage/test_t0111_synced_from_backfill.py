"""T-0111 rationale-prose backfill tests.

Drives ``_backfill_synced_from_version_from_rationale_detect`` and
``_backfill_synced_from_version_from_rationale_apply`` against a
freshly-migrated SQLite DB. Raw SQL inserts (not the ORM) so each
fixture isolates one branch of the regex/chain-membership logic.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest

from sage.storage.graph_store import GraphStore
from sage.storage.migrations import (
    _backfill_synced_from_version_from_rationale_apply,
    _backfill_synced_from_version_from_rationale_detect,
)


@pytest.fixture
async def db_path(tmp_path: Path) -> Path:
    """Freshly-initialized graph.db; backfill tests drive raw SQL after."""
    path = tmp_path / "graph.db"
    store = GraphStore(path)
    await store.initialize(migrate=True)
    await store.close()
    return path


def _insert_doc(
    conn: sqlite3.Connection,
    doc_id: str,
    *,
    version_label: str | None = None,
) -> None:
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        "INSERT INTO documents "
        "(id, title, source_type, source_path, source_content_hash, "
        "adapter_version, created_by, created_at, last_modified_by, "
        "updated_at, version_label) "
        "VALUES (?, ?, 'markdown', ?, ?, '0.1.0', 'tester', ?, 'tester', ?, ?)",
        (
            doc_id,
            f"Doc {doc_id}",
            f"test/{doc_id}.md",
            "sha256:" + ("a" * 64),
            now,
            now,
            version_label,
        ),
    )


def _insert_edge(
    conn: sqlite3.Connection,
    edge_id: str,
    *,
    source_id: str,
    target_id: str,
    edge_type: str = "derived_from",
    rationale: str | None = None,
    synced_from_version: str | None = None,
) -> None:
    conn.execute(
        "INSERT INTO edges "
        "(id, source_id, target_id, edge_type, created_at, rationale, synced_from_version) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            edge_id,
            source_id,
            target_id,
            edge_type,
            datetime.now(timezone.utc).isoformat(),
            rationale,
            synced_from_version,
        ),
    )


def _seed_chain(conn: sqlite3.Connection) -> tuple[str, str, str]:
    """Seed a 3-deep supersedes chain T1 → T2 → T3 with version labels.

    Returns (t1_id, t2_id, t3_id). Each version label is unique so the
    happy-path regex captures only one chain member.
    """
    t1, t2, t3 = "deadbeef_t1", "cafebabe_t2", "cafef00d_t3"
    _insert_doc(conn, t1, version_label="v1.0.0")
    _insert_doc(conn, t2, version_label="v1.2.3")
    _insert_doc(conn, t3, version_label="v2.0.0")
    # T2 supersedes T1, T3 supersedes T2 (source=newer convention).
    _insert_edge(
        conn,
        "11111111-1111-4111-8111-111111111111",
        source_id=t2,
        target_id=t1,
        edge_type="supersedes",
    )
    _insert_edge(
        conn,
        "11111111-1111-4111-8111-222222222222",
        source_id=t3,
        target_id=t2,
        edge_type="supersedes",
    )
    return t1, t2, t3


def _get_synced_from_version(conn: sqlite3.Connection, edge_id: str) -> str | None:
    row = conn.execute("SELECT synced_from_version FROM edges WHERE id = ?", (edge_id,)).fetchone()
    return row[0] if row is not None else None


async def test_t_bf_happy(db_path: Path) -> None:
    """T-BF-happy: chain of 3; edge rationale ``derived from v1.2.3``
    matches T2 (the middle version, version_label='v1.2.3'). After
    backfill, synced_from_version = T2.id (NOT T3, NOT T1)."""
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        t1, t2, t3 = _seed_chain(conn)
        _insert_doc(conn, "aaaaaaaa_consumer")
        _insert_edge(
            conn,
            "22222222-2222-4222-8222-222222222222",
            source_id="aaaaaaaa_consumer",
            target_id=t3,  # head
            rationale="derived from v1.2.3 of the upstream spec",
        )

        assert _backfill_synced_from_version_from_rationale_detect(conn) is True
        _backfill_synced_from_version_from_rationale_apply(conn)

        assigned = _get_synced_from_version(conn, "22222222-2222-4222-8222-222222222222")
        assert assigned == t2, f"expected resolution to T2 ({t2}), got {assigned!r}"
        assert assigned != t1
        assert assigned != t3
    finally:
        conn.close()


async def test_t_bf_ambiguous_and_mixed(db_path: Path) -> None:
    """T-BF-ambiguous-and-mixed: two edges, one ambiguous, one
    unambiguous. The ambiguous one stays NULL; the unambiguous one
    populates. Catches a "never writes" bug (Y would stay NULL) and an
    "always writes" bug (X would get a value)."""
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        # Chain where TWO entries share version_label='v1.2', plus one
        # unique entry with 'v1.2.3'.
        _insert_doc(conn, "deadbeef_a", version_label="v1.2")
        _insert_doc(conn, "cafebabe_b", version_label="v1.2")
        _insert_doc(conn, "cafef00d_c", version_label="v1.2.3")
        # Chain them all so they share a supersedes chain.
        _insert_edge(
            conn,
            "11111111-1111-4111-8111-aaaaaaaaaaaa",
            source_id="cafebabe_b",
            target_id="deadbeef_a",
            edge_type="supersedes",
        )
        _insert_edge(
            conn,
            "11111111-1111-4111-8111-bbbbbbbbbbbb",
            source_id="cafef00d_c",
            target_id="cafebabe_b",
            edge_type="supersedes",
        )
        head = "cafef00d_c"

        _insert_doc(conn, "feedface_consumer_x")
        _insert_doc(conn, "deadbeef_consumer_y")
        # X: rationale matches "v1.2", which two chain entries share -> NULL.
        _insert_edge(
            conn,
            "22222222-2222-4222-8222-aaaaaaaaaaaa",
            source_id="feedface_consumer_x",
            target_id=head,
            rationale="derived from v1.2 of the upstream",
        )
        # Y: rationale matches "v1.2.3", unique -> populated.
        _insert_edge(
            conn,
            "22222222-2222-4222-8222-bbbbbbbbbbbb",
            source_id="deadbeef_consumer_y",
            target_id=head,
            rationale="derived from v1.2.3 of the upstream",
        )

        assert _backfill_synced_from_version_from_rationale_detect(conn) is True
        _backfill_synced_from_version_from_rationale_apply(conn)

        x_assigned = _get_synced_from_version(conn, "22222222-2222-4222-8222-aaaaaaaaaaaa")
        y_assigned = _get_synced_from_version(conn, "22222222-2222-4222-8222-bbbbbbbbbbbb")
        assert x_assigned is None, f"ambiguous edge should stay NULL; got {x_assigned!r}"
        assert y_assigned == "cafef00d_c", (
            f"unambiguous edge should resolve to head; got {y_assigned!r}"
        )
    finally:
        conn.close()


async def test_t_bf_verb_gate(db_path: Path) -> None:
    """T-BF-verb-gate: rationale ``"this contradicts v1.0 of the spec"``
    does NOT trigger the backfill — no provenance verb. Even though the
    chain has exactly one entry with version_label='v1.0', the verb
    gate suppresses the false match."""
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        _insert_doc(conn, "deadbeef_only", version_label="v1.0.0")
        _insert_doc(conn, "feedface_src")
        _insert_edge(
            conn,
            "33333333-3333-4333-8333-333333333333",
            source_id="feedface_src",
            target_id="deadbeef_only",
            rationale="this contradicts v1.0.0 of the spec; unrelated to provenance",
        )

        assert _backfill_synced_from_version_from_rationale_detect(conn) is False
        _backfill_synced_from_version_from_rationale_apply(conn)

        assigned = _get_synced_from_version(conn, "33333333-3333-4333-8333-333333333333")
        assert assigned is None, f"non-provenance prose must not trigger backfill; got {assigned!r}"
    finally:
        conn.close()


async def test_t_bf_idempotent(db_path: Path) -> None:
    """T-BF-idempotent: apply once -> row set; reset to NULL; re-apply
    -> row restored; re-apply with no NULLs -> detect returns False,
    apply is a no-op. Catches both "detect hard-wired True" and "detect
    hard-wired False"."""
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        _t1, t2, t3 = _seed_chain(conn)
        _insert_doc(conn, "aaaaaaaa_consumer")
        edge_id = "44444444-4444-4444-8444-444444444444"
        _insert_edge(
            conn,
            edge_id,
            source_id="aaaaaaaa_consumer",
            target_id=t3,
            rationale="derived from v1.2.3 of upstream",
        )

        # Pass 1: detect → True, apply sets the row.
        assert _backfill_synced_from_version_from_rationale_detect(conn) is True
        _backfill_synced_from_version_from_rationale_apply(conn)
        assert _get_synced_from_version(conn, edge_id) == t2

        # Reset the row out-of-band and re-run; the backfill restores it.
        conn.execute("UPDATE edges SET synced_from_version = NULL WHERE id = ?", (edge_id,))
        assert _backfill_synced_from_version_from_rationale_detect(conn) is True
        _backfill_synced_from_version_from_rationale_apply(conn)
        assert _get_synced_from_version(conn, edge_id) == t2

        # Pass 3: no NULLs left to fill -> detect returns False, apply no-op.
        assert _backfill_synced_from_version_from_rationale_detect(conn) is False
        _backfill_synced_from_version_from_rationale_apply(conn)
        assert _get_synced_from_version(conn, edge_id) == t2
    finally:
        conn.close()
