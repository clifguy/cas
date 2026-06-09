"""Tests for the purge_batch maintenance script.

Exercises the service-level entry point
``sage.maintenance.purge_batch.purge`` against a real SQLite + LanceDB
vault. Covers selector accuracy (half-open window), dry-run shape,
typed-count confirmation gate, pre-flight batch rejection, per-document
cascade correctness, audit-log shape with shared ``batch_id``, and
halt-on-first-failure semantics.

Fixture layout (six documents around ``T_BASE``):

| doc_id | created_at | role |
|-------------------|---------------------------------|---------------------|
| doc_pre | T_BASE - 2min | before-window control |
| doc_at_since | T_BASE | inclusive lower edge |
| doc_mid_1 | T_BASE + 1min | in window |
| doc_mid_2 | T_BASE + 2min | in window |
| doc_at_until | T_BASE + 5min | exclusive upper edge |
| doc_post | now(utc) + 24h | after-window control |

Most tests invoke the batch with ``since=T_BASE`` and
``until=T_BASE + 5min``, producing the target set
``{doc_at_since, doc_mid_1, doc_mid_2}``.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import lancedb
import pytest
import yaml

from sage.adapters.content_store_lancedb import CHUNKS_TABLE, LanceDBContentStore
from sage.adapters.interfaces import Chunk

# Module under test.
from sage.maintenance import purge_batch
from sage.models.enums import PipelineStatus, SourceType
from sage.models.schemas import Document
from sage.storage.graph_store import SqliteGraphStore

VECTOR_DIMENSIONS = 768
VAULT_ID = "purge_batch_test"

DOC_PRE = "doc_pre"
DOC_AT_SINCE = "doc_at_since"
DOC_MID_1 = "doc_mid_1"
DOC_MID_2 = "doc_mid_2"
DOC_AT_UNTIL = "doc_at_until"
DOC_POST = "doc_post"

IN_WINDOW_IDS = (DOC_AT_SINCE, DOC_MID_1, DOC_MID_2)
OUT_OF_WINDOW_IDS = (DOC_PRE, DOC_AT_UNTIL, DOC_POST)


def _make_doc(
    doc_id: str,
    *,
    created_at: datetime,
    pipeline_status: PipelineStatus = PipelineStatus.ABSTRACTION_COMPLETE,
    tags: list[str] | None = None,
    doc_type: str = "note",
    document_date: str | None = None,
) -> Document:
    """Construct a Document with controllable ``created_at``."""
    return Document.model_construct(
        id=doc_id,
        title=f"Title for {doc_id}",
        source_type=SourceType.MARKDOWN,
        source_path=f"test/{doc_id}.md",
        lifecycle_status="active",
        version_label=None,
        project=None,
        tags=tags or [],
        authority_scope=None,
        doc_type=doc_type,
        source_content_hash=f"sha256:{(doc_id + 'a' * 64)[:64]}",
        adapter_version="0.1.0",
        created_by="testuser",
        created_at=created_at,
        last_modified_by="testuser",
        updated_at=created_at,
        projected_at=created_at,
        indexed_at=None,
        source_modified_at=None,
        document_date=document_date,
        semantic_abstract=None,
        pipeline_status=pipeline_status,
        pipeline_error=None,
        tier3_metadata=None,
        metadata_confirmed=True,
    )


def _make_chunk(doc_id: str, index: int) -> Chunk:
    return Chunk(
        document_id=doc_id,
        heading_path=f"Heading {index}",
        content=f"chunk content {index}",
        embedding=[0.1] * VECTOR_DIMENSIONS,
        chunk_index=index,
        doc_type="note",
        lifecycle_status="active",
        project="CAS",
    )


def _write_vault_config(vault_dir: Path, brain_root: Path, storage_root: Path) -> Path:
    config_dict = {
        "vault": {
            "id": VAULT_ID,
            "name": VAULT_ID,
            "owner": "testuser",
            "storage_root": str(storage_root),
            "brain_root": str(brain_root),
            "visibility": "personal",
        },
        "document_types": {
            "doc_types": [{"value": "note", "label": "Note"}],
        },
        "lifecycle": {
            "base_states_required": True,
            "states": [
                {"value": "active", "label": "Active"},
                {"value": "completed", "label": "Completed"},
                {"value": "archived", "label": "Archived", "is_terminal": True},
            ],
            "transitions": [
                {"from_state": "(new)", "action": "ingest", "to_state": "active"},
                {"from_state": "active", "action": "archive", "to_state": "archived"},
            ],
        },
        "source_adapters": {"adapters": [{"source_type": "markdown", "enabled": True}]},
        "metadata_extraction": {},
        "edge_inference": {
            "tier_assignments": [
                {
                    "edge_type": "supersedes",
                    "tier": 1,
                    "inference_rules": [{"method": "version_chain"}],
                },
            ],
        },
    }
    cfg_path = vault_dir / "vault_config.yaml"
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    cfg_path.write_text(yaml.dump(config_dict, sort_keys=False))
    return cfg_path


def _insert_edge(
    conn: sqlite3.Connection,
    edge_id: str,
    source: str,
    target: str,
    edge_type: str = "references",
) -> None:
    now_iso = datetime.now(timezone.utc).isoformat()
    conn.execute(
        """INSERT INTO edges (
            id, source_id, target_id, edge_type, resolution_policy,
            source_valid_from_version, target_valid_from_version,
            valid_until_version, retracted_edge_id, created_at,
            notes, rationale, rationale_kind
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            edge_id,
            source,
            target,
            edge_type,
            "transitive_both",
            source,
            target,
            None,
            None,
            now_iso,
            None,
            "test rationale",
            "manual",
        ),
    )


def _insert_staging_edge(
    conn: sqlite3.Connection,
    edge_id: str,
    source: str,
    target: str,
    edge_type: str = "references",
) -> None:
    now_iso = datetime.now(timezone.utc).isoformat()
    conn.execute(
        """INSERT INTO staging_edges (
            id, source_id, target_id, edge_type, inference_evidence,
            confidence_tier, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (edge_id, source, target, edge_type, "test evidence", 2, now_iso),
    )


def _set_pipeline_status(sqlite_path: Path, doc_id: str, status: str) -> None:
    conn = sqlite3.connect(sqlite_path)
    conn.execute(
        "UPDATE documents SET pipeline_status = ? WHERE id = ?",
        (status, doc_id),
    )
    conn.commit()
    conn.close()


def _vault_dir() -> Path:
    from sage import vault_management

    return vault_management._VAULTS_ROOT / VAULT_ID


def _audit_log_path() -> Path:
    return _vault_dir() / ".maintenance_log.jsonl"


def _count_rows(sqlite_path: Path, sql: str, params: tuple) -> int:
    conn = sqlite3.connect(sqlite_path)
    try:
        row = conn.execute(sql, params).fetchone()
        return row[0] if row else 0
    finally:
        conn.close()


def _count_lancedb_chunks(lancedb_dir: Path, doc_id: str) -> int:
    if not lancedb_dir.exists():
        return 0
    db = lancedb.connect(str(lancedb_dir))
    if CHUNKS_TABLE not in db.list_tables().tables:
        return 0
    return db.open_table(CHUNKS_TABLE).count_rows(filter=f"document_id = '{doc_id}'")


@pytest.fixture
async def populated_vault():
    """Vault with six documents distributed around T_BASE.

    ``T_BASE`` is two hours ago so the windowed docs are all in the
    past. ``doc_post`` is twenty-four hours in the future so the
    default-to-now upper bound (test A2) excludes it.

    Each in-window target gets a representative cascade footprint:
    - ``doc_mid_1``: 2 chunks, 1 tag, 1 outbound + 1 inbound edge.
    - The other two have lighter footprints; collectively they prove
      multi-doc cascade correctness.
    """
    vault_dir = _vault_dir()
    brain_root = vault_dir / "brain"
    storage_root = vault_dir / "sources"
    brain_root.mkdir(parents=True, exist_ok=True)
    storage_root.mkdir(parents=True, exist_ok=True)

    _write_vault_config(vault_dir, brain_root, storage_root)

    now = datetime.now(timezone.utc)
    t_base = now - timedelta(hours=2)
    offsets = {
        DOC_PRE: t_base - timedelta(minutes=2),
        DOC_AT_SINCE: t_base,
        DOC_MID_1: t_base + timedelta(minutes=1),
        DOC_MID_2: t_base + timedelta(minutes=2),
        DOC_AT_UNTIL: t_base + timedelta(minutes=5),
        DOC_POST: now + timedelta(hours=24),
    }

    graph = SqliteGraphStore(brain_root / "graph.db")
    await graph.initialize()
    docs = {
        DOC_PRE: _make_doc(DOC_PRE, created_at=offsets[DOC_PRE], tags=["control"]),
        DOC_AT_SINCE: _make_doc(
            DOC_AT_SINCE,
            created_at=offsets[DOC_AT_SINCE],
            tags=["alpha"],
            document_date="2026-05-20",
        ),
        DOC_MID_1: _make_doc(
            DOC_MID_1,
            created_at=offsets[DOC_MID_1],
            tags=["beta"],
            document_date="2026-05-20",
        ),
        DOC_MID_2: _make_doc(
            DOC_MID_2,
            created_at=offsets[DOC_MID_2],
            tags=["gamma", "delta"],
            document_date="2026-05-20",
        ),
        DOC_AT_UNTIL: _make_doc(DOC_AT_UNTIL, created_at=offsets[DOC_AT_UNTIL], tags=["control"]),
        DOC_POST: _make_doc(DOC_POST, created_at=offsets[DOC_POST], tags=["control"]),
    }
    for d in docs.values():
        await graph.insert_document(d)
    await graph.close()

    # Add an inbound edge from a control (doc_pre) into a target (doc_mid_1)
    # for E2, an outbound edge from doc_mid_2 to doc_post, and a
    # control-to-control edge that must survive any cascade.
    conn = sqlite3.connect(brain_root / "graph.db")
    _insert_edge(conn, "e_pre_into_mid1", DOC_PRE, DOC_MID_1, "references")
    _insert_edge(conn, "e_mid2_to_post", DOC_MID_2, DOC_POST, "references")
    _insert_edge(conn, "e_pre_to_until", DOC_PRE, DOC_AT_UNTIL, "references")
    conn.commit()
    conn.close()

    # LanceDB chunks for the windowed targets and one control.
    store = LanceDBContentStore(brain_root)
    await store.index_chunks(DOC_AT_SINCE, [_make_chunk(DOC_AT_SINCE, i) for i in range(1)])
    await store.index_chunks(DOC_MID_1, [_make_chunk(DOC_MID_1, i) for i in range(2)])
    await store.index_chunks(DOC_MID_2, [_make_chunk(DOC_MID_2, i) for i in range(3)])
    await store.index_chunks(DOC_AT_UNTIL, [_make_chunk(DOC_AT_UNTIL, i) for i in range(2)])

    # Drop a small on-disk source file for content_size assertions.
    (storage_root / "test").mkdir(parents=True, exist_ok=True)
    (storage_root / "test" / f"{DOC_MID_1}.md").write_bytes(b"hello\n")  # 6 bytes

    return {
        "vault_id": VAULT_ID,
        "vault_dir": vault_dir,
        "brain_root": brain_root,
        "storage_root": storage_root,
        "sqlite_path": brain_root / "graph.db",
        "lancedb_dir": brain_root / "lancedb",
        "t_base": t_base,
        "offsets": offsets,
    }


# ─── Group A: selector accuracy ─────────────────────────────────────


async def test_selector_window_is_half_open(populated_vault, capsys):
    """Window [since, since+5min) includes doc_at_since but excludes doc_at_until."""
    t_base = populated_vault["t_base"]
    until = t_base + timedelta(minutes=5)

    rc = purge_batch.purge(
        vault_id=VAULT_ID,
        since=t_base,
        until=until,
        reason="A1",
        apply=False,
    )
    out = capsys.readouterr().out

    assert rc == 0
    for in_id in IN_WINDOW_IDS:
        assert in_id in out, f"{in_id} should be in dry-run output"
    for out_id in OUT_OF_WINDOW_IDS:
        assert out_id not in out, f"{out_id} should NOT be in dry-run output (boundary violation)"
    assert "Target count: 3" in out


async def test_selector_with_only_since_uses_now_as_until(populated_vault, capsys):
    """until=None defaults to script-start now(); doc_post (24h ahead) is excluded."""
    t_base = populated_vault["t_base"]

    rc = purge_batch.purge(
        vault_id=VAULT_ID,
        since=t_base,
        until=None,
        reason="A2",
        apply=False,
    )
    out = capsys.readouterr().out

    assert rc == 0
    # All past-but-after-since docs are included.
    for in_id in (DOC_AT_SINCE, DOC_MID_1, DOC_MID_2, DOC_AT_UNTIL):
        assert in_id in out
    # doc_post is the trap: a bug defaulting to "+infinity" would include it.
    assert DOC_POST not in out, "doc_post (24h in future) escaped the default 'until = now' cutoff"
    # doc_pre is before since.
    assert DOC_PRE not in out


async def test_selector_empty_window_returns_zero_targets(populated_vault, capsys):
    """A window that contains no rows exits 0, never prompts, never writes audit."""
    t_base = populated_vault["t_base"]

    rc = purge_batch.purge(
        vault_id=VAULT_ID,
        since=t_base - timedelta(minutes=10),
        until=t_base - timedelta(minutes=5),
        reason="A3",
        apply=False,
    )
    out = capsys.readouterr().out

    assert rc == 0
    assert "Target count: 0" in out
    assert "(no documents in window" in out
    assert not _audit_log_path().exists(), "empty-window dry-run wrote an audit-log entry"


# ─── Group B: dry-run enumeration shape ─────────────────────────────


async def test_dry_run_enumerates_per_doc_fields(populated_vault, capsys):
    """Every windowed target's six required fields appear in the dry-run output."""
    t_base = populated_vault["t_base"]
    offsets = populated_vault["offsets"]

    rc = purge_batch.purge(
        vault_id=VAULT_ID,
        since=t_base,
        until=t_base + timedelta(minutes=5),
        reason="B1",
        apply=False,
    )
    out = capsys.readouterr().out

    assert rc == 0
    for doc_id in IN_WINDOW_IDS:
        assert doc_id in out
        assert f"Title for {doc_id}" in out
        assert f"test/{doc_id}.md" in out
        # doc_type
        assert "note" in out
        # created_at appears (ISO format)
        assert offsets[doc_id].isoformat() in out
    # document_date assertion (set for windowed docs)
    assert "2026-05-20" in out
    # Cumulative content_size summary line. doc_mid_1's source file is 6 bytes.
    assert "Total: 3 documents" in out
    assert "bytes cumulative content_size" in out


async def test_dry_run_writes_no_state(populated_vault):
    """Dry-run changes nothing in SQLite or LanceDB and writes no audit log."""
    sqlite_path = populated_vault["sqlite_path"]
    lancedb_dir = populated_vault["lancedb_dir"]
    t_base = populated_vault["t_base"]

    def counts():
        return {
            "documents": _count_rows(sqlite_path, "SELECT COUNT(*) FROM documents", ()),
            "edges": _count_rows(sqlite_path, "SELECT COUNT(*) FROM edges", ()),
            "staging_edges": _count_rows(sqlite_path, "SELECT COUNT(*) FROM staging_edges", ()),
            "document_tags": _count_rows(sqlite_path, "SELECT COUNT(*) FROM document_tags", ()),
            "chunks_mid_1": _count_lancedb_chunks(lancedb_dir, DOC_MID_1),
            "chunks_mid_2": _count_lancedb_chunks(lancedb_dir, DOC_MID_2),
        }

    before = counts()
    rc = purge_batch.purge(
        vault_id=VAULT_ID,
        since=t_base,
        until=t_base + timedelta(minutes=5),
        reason="B2",
        apply=False,
    )
    after = counts()

    assert rc == 0
    assert before == after, f"dry-run mutated state: {before} → {after}"
    assert not _audit_log_path().exists()


# ─── Group C: typed-count confirmation gate ─────────────────────────


async def test_apply_proceeds_on_matching_typed_count(populated_vault, monkeypatch):
    """Typed count matches → cascade proceeds."""
    t_base = populated_vault["t_base"]
    monkeypatch.setattr("builtins.input", lambda _prompt: "3")

    rc = purge_batch.purge(
        vault_id=VAULT_ID,
        since=t_base,
        until=t_base + timedelta(minutes=5),
        reason="C1",
        apply=True,
    )

    assert rc == 0
    for doc_id in IN_WINDOW_IDS:
        assert (
            _count_rows(
                populated_vault["sqlite_path"],
                "SELECT COUNT(*) FROM documents WHERE id = ?",
                (doc_id,),
            )
            == 0
        )
    for doc_id in OUT_OF_WINDOW_IDS:
        assert (
            _count_rows(
                populated_vault["sqlite_path"],
                "SELECT COUNT(*) FROM documents WHERE id = ?",
                (doc_id,),
            )
            == 1
        )


async def test_apply_refuses_when_typed_count_mismatches(populated_vault, monkeypatch):
    """Off-by-one typed count → refuse, no deletes, no audit."""
    t_base = populated_vault["t_base"]
    monkeypatch.setattr("builtins.input", lambda _prompt: "2")

    rc = purge_batch.purge(
        vault_id=VAULT_ID,
        since=t_base,
        until=t_base + timedelta(minutes=5),
        reason="C2",
        apply=True,
    )

    assert rc != 0
    for doc_id in IN_WINDOW_IDS:
        assert (
            _count_rows(
                populated_vault["sqlite_path"],
                "SELECT COUNT(*) FROM documents WHERE id = ?",
                (doc_id,),
            )
            == 1
        )
    assert not _audit_log_path().exists()


@pytest.mark.parametrize("bad_input", ["", "yes", "3 ", "03"])
async def test_apply_refuses_on_non_string_equal_inputs(populated_vault, monkeypatch, bad_input):
    """The check is strict string-equality: "03" parses to int 3 but is rejected."""
    t_base = populated_vault["t_base"]
    monkeypatch.setattr("builtins.input", lambda _prompt: bad_input)

    rc = purge_batch.purge(
        vault_id=VAULT_ID,
        since=t_base,
        until=t_base + timedelta(minutes=5),
        reason=f"C3-{bad_input!r}",
        apply=True,
    )

    assert rc != 0, f"input {bad_input!r} should have refused"
    for doc_id in IN_WINDOW_IDS:
        assert (
            _count_rows(
                populated_vault["sqlite_path"],
                "SELECT COUNT(*) FROM documents WHERE id = ?",
                (doc_id,),
            )
            == 1
        )
    assert not _audit_log_path().exists()


# ─── Group D: pre-flight batch rejection ────────────────────────────


async def test_batch_refuses_if_any_target_has_pending_staging_edges(
    populated_vault, monkeypatch, capsys
):
    """One target with staging edges → whole batch refused, no deletes."""
    conn = sqlite3.connect(populated_vault["sqlite_path"])
    _insert_staging_edge(conn, "se_dirty", DOC_MID_2, DOC_AT_UNTIL)
    conn.commit()
    conn.close()

    t_base = populated_vault["t_base"]
    monkeypatch.setattr("builtins.input", lambda _prompt: "3")

    rc = purge_batch.purge(
        vault_id=VAULT_ID,
        since=t_base,
        until=t_base + timedelta(minutes=5),
        reason="D1",
        apply=True,
    )
    err = capsys.readouterr().err

    assert rc != 0
    assert DOC_MID_2 in err
    assert "staging" in err.lower()
    for doc_id in IN_WINDOW_IDS:
        assert (
            _count_rows(
                populated_vault["sqlite_path"],
                "SELECT COUNT(*) FROM documents WHERE id = ?",
                (doc_id,),
            )
            == 1
        ), f"{doc_id} should still exist (whole-batch refusal)"
    assert not _audit_log_path().exists()


async def test_batch_refuses_if_any_target_pipeline_status_non_terminal(
    populated_vault, monkeypatch, capsys
):
    """One target with non-terminal pipeline_status → whole batch refused."""
    _set_pipeline_status(populated_vault["sqlite_path"], DOC_MID_1, "indexing_in_progress")

    t_base = populated_vault["t_base"]
    monkeypatch.setattr("builtins.input", lambda _prompt: "3")

    rc = purge_batch.purge(
        vault_id=VAULT_ID,
        since=t_base,
        until=t_base + timedelta(minutes=5),
        reason="D2",
        apply=True,
    )
    err = capsys.readouterr().err

    assert rc != 0
    assert DOC_MID_1 in err
    assert "indexing_in_progress" in err
    for doc_id in IN_WINDOW_IDS:
        assert (
            _count_rows(
                populated_vault["sqlite_path"],
                "SELECT COUNT(*) FROM documents WHERE id = ?",
                (doc_id,),
            )
            == 1
        )
    assert not _audit_log_path().exists()


# ─── Group E: per-document cascade correctness ─────────────────────


async def test_apply_deletes_every_in_window_target(populated_vault, monkeypatch):
    """All three windowed targets and their dependents deleted; controls preserved."""
    t_base = populated_vault["t_base"]
    sqlite_path = populated_vault["sqlite_path"]
    lancedb_dir = populated_vault["lancedb_dir"]
    monkeypatch.setattr("builtins.input", lambda _prompt: "3")

    rc = purge_batch.purge(
        vault_id=VAULT_ID,
        since=t_base,
        until=t_base + timedelta(minutes=5),
        reason="E1",
        apply=True,
    )
    assert rc == 0

    for doc_id in IN_WINDOW_IDS:
        assert (
            _count_rows(
                sqlite_path,
                "SELECT COUNT(*) FROM documents WHERE id = ?",
                (doc_id,),
            )
            == 0
        )
        assert (
            _count_rows(
                sqlite_path,
                "SELECT COUNT(*) FROM edges WHERE source_id = ? OR target_id = ?",
                (doc_id, doc_id),
            )
            == 0
        )
        assert (
            _count_rows(
                sqlite_path,
                "SELECT COUNT(*) FROM document_tags WHERE document_id = ?",
                (doc_id,),
            )
            == 0
        )
        assert _count_lancedb_chunks(lancedb_dir, doc_id) == 0

    # Controls untouched.
    for doc_id in OUT_OF_WINDOW_IDS:
        assert (
            _count_rows(
                sqlite_path,
                "SELECT COUNT(*) FROM documents WHERE id = ?",
                (doc_id,),
            )
            == 1
        )
    # The control-to-control edge (doc_pre → doc_at_until) survives.
    assert (
        _count_rows(
            sqlite_path,
            "SELECT COUNT(*) FROM edges WHERE id = ?",
            ("e_pre_to_until",),
        )
        == 1
    )
    # The control's chunks survive.
    assert _count_lancedb_chunks(lancedb_dir, DOC_AT_UNTIL) == 2


async def test_apply_deletes_inbound_edge_into_target(populated_vault, monkeypatch):
    """An edge from a control INTO a target is removed; the control survives."""
    sqlite_path = populated_vault["sqlite_path"]
    t_base = populated_vault["t_base"]

    # Sanity: the inbound edge exists pre-cascade.
    assert (
        _count_rows(
            sqlite_path,
            "SELECT COUNT(*) FROM edges WHERE id = ?",
            ("e_pre_into_mid1",),
        )
        == 1
    )

    monkeypatch.setattr("builtins.input", lambda _prompt: "3")
    rc = purge_batch.purge(
        vault_id=VAULT_ID,
        since=t_base,
        until=t_base + timedelta(minutes=5),
        reason="E2",
        apply=True,
    )
    assert rc == 0

    # The edge is gone (cascaded with doc_mid_1).
    assert (
        _count_rows(
            sqlite_path,
            "SELECT COUNT(*) FROM edges WHERE id = ?",
            ("e_pre_into_mid1",),
        )
        == 0
    )
    # The source control survives.
    assert (
        _count_rows(
            sqlite_path,
            "SELECT COUNT(*) FROM documents WHERE id = ?",
            (DOC_PRE,),
        )
        == 1
    )


# ─── Group F: audit log shape and batch_id linkage ─────────────────


async def test_audit_log_one_entry_per_target_with_shared_batch_id(populated_vault, monkeypatch):
    """Three target deletes → three audit lines, all sharing one batch_id."""
    t_base = populated_vault["t_base"]
    monkeypatch.setattr("builtins.input", lambda _prompt: "3")

    rc = purge_batch.purge(
        vault_id=VAULT_ID,
        since=t_base,
        until=t_base + timedelta(minutes=5),
        reason="F1",
        apply=True,
    )
    assert rc == 0

    lines = _audit_log_path().read_text().strip().splitlines()
    assert len(lines) == 3, f"expected 3 audit entries, got {len(lines)}"
    records = [json.loads(line) for line in lines]
    batch_ids = {r["batch_id"] for r in records}
    assert len(batch_ids) == 1, f"all entries must share one batch_id, found {batch_ids}"
    # Validate UUID format.
    uuid.UUID(records[0]["batch_id"])


async def test_audit_log_each_entry_carries_t0105_field_shape(populated_vault, monkeypatch):
    """Each entry has seven fields plus batch_id; values from rows, not constants."""
    sqlite_path = populated_vault["sqlite_path"]
    t_base = populated_vault["t_base"]

    # Capture pre-cascade row values for the three targets.
    conn = sqlite3.connect(sqlite_path)
    conn.row_factory = sqlite3.Row
    pre_rows = {}
    for doc_id in IN_WINDOW_IDS:
        row = conn.execute(
            "SELECT id, title, source_path, source_content_hash, doc_type "
            "FROM documents WHERE id = ?",
            (doc_id,),
        ).fetchone()
        pre_rows[doc_id] = dict(row)
    conn.close()

    monkeypatch.setattr("builtins.input", lambda _prompt: "3")
    rc = purge_batch.purge(
        vault_id=VAULT_ID,
        since=t_base,
        until=t_base + timedelta(minutes=5),
        reason="F2",
        apply=True,
    )
    assert rc == 0

    lines = _audit_log_path().read_text().strip().splitlines()
    records_by_id = {json.loads(line)["document_id"]: json.loads(line) for line in lines}
    assert set(records_by_id) == set(IN_WINDOW_IDS)

    required_fields = {
        "timestamp",
        "document_id",
        "title",
        "source_path",
        "source_content_hash",
        "doc_type",
        "reason",
        "batch_id",
    }
    for doc_id in IN_WINDOW_IDS:
        record = records_by_id[doc_id]
        assert set(record) >= required_fields
        # Values match the row, not constants.
        pre = pre_rows[doc_id]
        assert record["title"] == pre["title"]
        assert record["source_path"] == pre["source_path"]
        assert record["source_content_hash"] == pre["source_content_hash"]
        assert record["doc_type"] == pre["doc_type"]
        assert record["reason"] == "F2"
        datetime.fromisoformat(record["timestamp"])


async def test_two_batches_have_distinct_batch_ids(populated_vault, monkeypatch):
    """Consecutive --apply invocations produce different batch_ids."""
    t_base = populated_vault["t_base"]
    monkeypatch.setattr("builtins.input", lambda _prompt: "3")

    rc1 = purge_batch.purge(
        vault_id=VAULT_ID,
        since=t_base,
        until=t_base + timedelta(minutes=5),
        reason="F3a",
        apply=True,
    )
    assert rc1 == 0

    # Second batch: empty window (target doc_at_until isn't included by
    # the first batch's window). Use a one-doc window around doc_at_until.
    monkeypatch.setattr("builtins.input", lambda _prompt: "1")
    rc2 = purge_batch.purge(
        vault_id=VAULT_ID,
        since=t_base + timedelta(minutes=5),
        until=t_base + timedelta(minutes=6),
        reason="F3b",
        apply=True,
    )
    assert rc2 == 0

    lines = _audit_log_path().read_text().strip().splitlines()
    records = [json.loads(line) for line in lines]
    batch_ids = {r["batch_id"] for r in records}
    assert len(batch_ids) == 2, (
        f"two batches should produce two distinct batch_ids, got {batch_ids}"
    )


async def test_audit_log_appends_does_not_overwrite_t0105_entries(populated_vault, monkeypatch):
    """A pre-existing-shaped line (no batch_id) survives a batch run."""
    audit_path = _audit_log_path()
    audit_path.parent.mkdir(parents=True, exist_ok=True)
    seed = json.dumps(
        {
            "timestamp": "2026-01-01T00:00:00+00:00",
            "document_id": "doc_t0105_legacy",
            "title": "Legacy",
            "source_path": "test/legacy.md",
            "source_content_hash": "sha256:" + "0" * 64,
            "doc_type": "note",
            "reason": "pre-existing",
        }
    )
    audit_path.write_text(seed + "\n")

    t_base = populated_vault["t_base"]
    monkeypatch.setattr("builtins.input", lambda _prompt: "3")
    rc = purge_batch.purge(
        vault_id=VAULT_ID,
        since=t_base,
        until=t_base + timedelta(minutes=5),
        reason="F4",
        apply=True,
    )
    assert rc == 0

    lines = audit_path.read_text().strip().splitlines()
    assert len(lines) == 4, "seed + 3 batch entries"
    seed_record = json.loads(lines[0])
    assert seed_record["document_id"] == "doc_t0105_legacy"
    assert "batch_id" not in seed_record


# ─── Group G: halt-on-failure semantics ─────────────────────────────


class _FailOnMidOneDocDeleteConn:
    """Wrapper that fails the documents-row DELETE for doc_mid_1 only.

    Python 3.14 made ``sqlite3.Connection.execute`` read-only, so the
    failure must be injected via a wrapper rather than rebinding the
    method on the connection itself.
    """

    def __init__(self, real: sqlite3.Connection) -> None:
        object.__setattr__(self, "_real", real)

    def execute(self, sql, *params, **kwargs):
        if (
            isinstance(sql, str)
            and "DELETE FROM documents" in sql
            and params
            and params[0]
            and isinstance(params[0], tuple)
            and params[0][0] == DOC_MID_1
        ):
            raise sqlite3.OperationalError("simulated cascade failure on doc_mid_1")
        return self._real.execute(sql, *params, **kwargs)

    def __getattr__(self, name):
        return getattr(self._real, name)

    def __setattr__(self, name, value):
        if name == "_real":
            object.__setattr__(self, name, value)
        else:
            setattr(self._real, name, value)


async def test_per_doc_failure_halts_batch_and_reports_failing_doc(
    populated_vault, monkeypatch, capsys
):
    """doc_mid_1's cascade fails → batch halts; doc_at_since gone, doc_mid_2 untouched."""
    sqlite_path = populated_vault["sqlite_path"]
    lancedb_dir = populated_vault["lancedb_dir"]
    t_base = populated_vault["t_base"]

    real_connect = sqlite3.connect

    def failing_connect(*args, **kwargs):
        return _FailOnMidOneDocDeleteConn(real_connect(*args, **kwargs))

    monkeypatch.setattr("sage.maintenance.purge_batch._sqlite_connect", failing_connect)
    monkeypatch.setattr("builtins.input", lambda _prompt: "3")

    rc = purge_batch.purge(
        vault_id=VAULT_ID,
        since=t_base,
        until=t_base + timedelta(minutes=5),
        reason="G1",
        apply=True,
    )
    err = capsys.readouterr().err

    assert rc != 0
    assert DOC_MID_1 in err
    assert "simulated cascade failure" in err

    # doc_at_since (processed first) is gone.
    assert (
        _count_rows(
            sqlite_path,
            "SELECT COUNT(*) FROM documents WHERE id = ?",
            (DOC_AT_SINCE,),
        )
        == 0
    )
    # doc_mid_1 (failed) rolled back — row still present.
    assert (
        _count_rows(
            sqlite_path,
            "SELECT COUNT(*) FROM documents WHERE id = ?",
            (DOC_MID_1,),
        )
        == 1
    )
    # doc_mid_2 (after the failure) is untouched. The decisive signal is
    # its LanceDB chunks: a non-halting loop would have called the chunk
    # delete for it.
    assert (
        _count_rows(
            sqlite_path,
            "SELECT COUNT(*) FROM documents WHERE id = ?",
            (DOC_MID_2,),
        )
        == 1
    )
    assert _count_lancedb_chunks(lancedb_dir, DOC_MID_2) == 3
    # Audit log has exactly TWO entries: doc_at_since (success) and
    # doc_mid_1 (audit-before-delete). Three would prove the loop didn't halt.
    lines = _audit_log_path().read_text().strip().splitlines()
    assert len(lines) == 2, (
        f"halt-on-failure should produce 2 audit entries, got {len(lines)}: {lines}"
    )
    audited_ids = {json.loads(line)["document_id"] for line in lines}
    assert audited_ids == {DOC_AT_SINCE, DOC_MID_1}


async def test_halt_on_failure_preserves_batch_id_on_partial_entries(populated_vault, monkeypatch):
    """Partial batch's audit entries share one batch_id for later reconstruction."""
    t_base = populated_vault["t_base"]
    real_connect = sqlite3.connect

    def failing_connect(*args, **kwargs):
        return _FailOnMidOneDocDeleteConn(real_connect(*args, **kwargs))

    monkeypatch.setattr("sage.maintenance.purge_batch._sqlite_connect", failing_connect)
    monkeypatch.setattr("builtins.input", lambda _prompt: "3")

    rc = purge_batch.purge(
        vault_id=VAULT_ID,
        since=t_base,
        until=t_base + timedelta(minutes=5),
        reason="G2",
        apply=True,
    )
    assert rc != 0

    records = [json.loads(line) for line in _audit_log_path().read_text().strip().splitlines()]
    batch_ids = {r["batch_id"] for r in records}
    assert len(batch_ids) == 1, "halted batch's partial audit entries should share one batch_id"


# ─── Refusal on bad vault (sanity, parallel to) ──────────────


def test_refuses_unknown_vault(capsys):
    """Vault config absent → non-zero, clear error."""
    rc = purge_batch.purge(
        vault_id="this_vault_does_not_exist",
        since=datetime.now(timezone.utc) - timedelta(days=1),
        until=None,
        reason="refusal-test",
        apply=False,
    )
    err = capsys.readouterr().err
    assert rc != 0
    assert "this_vault_does_not_exist" in err or "vault" in err.lower()
