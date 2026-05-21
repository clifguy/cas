"""Tests for the purge_document maintenance script (T-0105).

Exercises the service-level entry point ``sage.maintenance.purge_document.purge``
against a real SQLite + LanceDB vault. The script enforces the
SAGE-Architecture v2.1 No-Delete Invariant carve-out (ADR-029 v1.1):
operator-only document removal, dry-run by default, named-only target,
typed confirmation, transaction-spanning cascade, append-only audit
record per vault.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import lancedb
import pytest
import yaml

from sage.adapters.content_store_lancedb import CHUNKS_TABLE, LanceDBContentStore
from sage.adapters.interfaces import Chunk

# Module under test. Import will FAIL until sage/maintenance/ exists.
from sage.maintenance import purge_document
from sage.models.enums import PipelineStatus, SourceType
from sage.models.schemas import Document
from sage.storage.graph_store import GraphStore

VECTOR_DIMENSIONS = 768
VAULT_ID = "purge_test"

TARGET_DOC_ID = "doc_target_001"
CONTROL_DOC_ID = "doc_control_001"
INBOUND_DOC_ID = "doc_inbound_001"
OUTBOUND_A_DOC_ID = "doc_outbound_a_001"
OUTBOUND_B_DOC_ID = "doc_outbound_b_001"


def _make_doc(
    doc_id: str,
    *,
    pipeline_status: PipelineStatus = PipelineStatus.ABSTRACTION_COMPLETE,
    tags: list[str] | None = None,
    doc_type: str = "note",
) -> Document:
    """Construct a Document with safe defaults, bypassing validators."""
    now = datetime.now(timezone.utc)
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
        created_at=now,
        last_modified_by="testuser",
        updated_at=now,
        projected_at=now,
        indexed_at=None,
        source_modified_at=None,
        document_date=None,
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
    """Return the patched vault root from the autouse fixture in tests/conftest.py."""
    from sage import vault_management

    return vault_management._VAULTS_ROOT / VAULT_ID


def _audit_log_path() -> Path:
    return _vault_dir() / ".maintenance_log.jsonl"


@pytest.fixture
async def populated_vault():
    """Vault with target (3 chunks, 2 outbound + 1 inbound edges, 2 tags) and
    control (2 chunks, 1 outbound edge, 1 tag) documents, plus the inbound
    and outbound endpoint docs. The control document and its edges must
    survive any cascade test for the anti-coincidental-pass guard.
    """
    vault_dir = _vault_dir()
    brain_root = vault_dir / "brain"
    storage_root = vault_dir / "sources"
    brain_root.mkdir(parents=True, exist_ok=True)
    storage_root.mkdir(parents=True, exist_ok=True)

    _write_vault_config(vault_dir, brain_root, storage_root)

    graph = GraphStore(brain_root / "graph.db")
    await graph.initialize()
    target = _make_doc(TARGET_DOC_ID, tags=["alpha", "beta"])
    control = _make_doc(CONTROL_DOC_ID, tags=["gamma"])
    inbound = _make_doc(INBOUND_DOC_ID)
    outbound_a = _make_doc(OUTBOUND_A_DOC_ID)
    outbound_b = _make_doc(OUTBOUND_B_DOC_ID)
    for d in (target, control, inbound, outbound_a, outbound_b):
        await graph.insert_document(d)
    await graph.close()

    conn = sqlite3.connect(brain_root / "graph.db")
    _insert_edge(conn, "e_target_out_a", TARGET_DOC_ID, OUTBOUND_A_DOC_ID, "references")
    _insert_edge(conn, "e_target_out_b", TARGET_DOC_ID, OUTBOUND_B_DOC_ID, "depends_on")
    _insert_edge(conn, "e_target_in", INBOUND_DOC_ID, TARGET_DOC_ID, "references")
    _insert_edge(conn, "e_control_out", CONTROL_DOC_ID, OUTBOUND_A_DOC_ID, "references")
    conn.commit()
    conn.close()

    store = LanceDBContentStore(brain_root)
    await store.index_chunks(
        TARGET_DOC_ID,
        [_make_chunk(TARGET_DOC_ID, i) for i in range(3)],
    )
    await store.index_chunks(
        CONTROL_DOC_ID,
        [_make_chunk(CONTROL_DOC_ID, i) for i in range(2)],
    )

    return {
        "vault_id": VAULT_ID,
        "vault_dir": vault_dir,
        "brain_root": brain_root,
        "sqlite_path": brain_root / "graph.db",
        "lancedb_dir": brain_root / "lancedb",
        "target_doc_id": TARGET_DOC_ID,
        "control_doc_id": CONTROL_DOC_ID,
    }


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


# ─── Tests 1–2: dry-run behaviour ────────────────────────────────────


async def test_dry_run_enumerates_all_dependents(populated_vault, capsys):
    """Dry run names target row, 3 chunks, 2 outbound + 1 inbound edge,
    0 staging edges. No state changes."""
    rc = purge_document.purge(
        vault_id=VAULT_ID,
        document_id=TARGET_DOC_ID,
        reason="dry-run-test",
        apply=False,
    )
    out = capsys.readouterr().out

    assert rc == 0, "dry run should exit 0"
    # Independent count assertions catch any single under-count in the script.
    assert "3" in out and "chunk" in out.lower()
    assert "2" in out and "outbound" in out.lower()
    assert "1" in out and "inbound" in out.lower()
    assert "staging" in out.lower()

    # Nothing actually changed.
    assert (
        _count_rows(
            populated_vault["sqlite_path"],
            "SELECT COUNT(*) FROM documents WHERE id = ?",
            (TARGET_DOC_ID,),
        )
        == 1
    )
    assert _count_lancedb_chunks(populated_vault["lancedb_dir"], TARGET_DOC_ID) == 3


async def test_dry_run_does_not_write_audit_log(populated_vault):
    """A dry run must not create the maintenance audit log."""
    audit_path = _audit_log_path()
    assert not audit_path.exists()

    purge_document.purge(
        vault_id=VAULT_ID,
        document_id=TARGET_DOC_ID,
        reason="dry-run-test",
        apply=False,
    )

    assert not audit_path.exists(), (
        "dry run wrote the audit log; the audit append must be gated behind --apply"
    )


# ─── Tests 3–4: refusal on missing vault / missing document ──────────


def test_refuses_unknown_vault(capsys):
    """Vault config does not exist → non-zero exit, clear error to stderr."""
    rc = purge_document.purge(
        vault_id="this_vault_does_not_exist",
        document_id="any_doc_id",
        reason="refusal-test",
        apply=False,
    )
    err = capsys.readouterr().err

    assert rc != 0
    assert "this_vault_does_not_exist" in err or "vault" in err.lower()


async def test_refuses_unknown_document(populated_vault, capsys):
    """Document id does not exist in the vault → non-zero exit, clear error."""
    rc = purge_document.purge(
        vault_id=VAULT_ID,
        document_id="doc_does_not_exist",
        reason="refusal-test",
        apply=False,
    )
    err = capsys.readouterr().err

    assert rc != 0
    assert "doc_does_not_exist" in err
    # No audit log on a missing-doc refusal.
    assert not _audit_log_path().exists()


# ─── Tests 5–6: refusal on pending staging edges ─────────────────────


async def test_refuses_when_staging_edge_references_target_as_source(
    populated_vault, monkeypatch, capsys
):
    """Staging edge with source_id == target_doc_id → --apply refuses, no
    cascade, original state intact."""
    conn = sqlite3.connect(populated_vault["sqlite_path"])
    _insert_staging_edge(conn, "se_a", TARGET_DOC_ID, OUTBOUND_A_DOC_ID)
    conn.commit()
    conn.close()

    monkeypatch.setattr("builtins.input", lambda _prompt: TARGET_DOC_ID)
    rc = purge_document.purge(
        vault_id=VAULT_ID,
        document_id=TARGET_DOC_ID,
        reason="should-refuse",
        apply=True,
    )
    err_and_out = capsys.readouterr()

    assert rc != 0
    assert "staging" in (err_and_out.err + err_and_out.out).lower()

    # Everything still present.
    assert (
        _count_rows(
            populated_vault["sqlite_path"],
            "SELECT COUNT(*) FROM documents WHERE id = ?",
            (TARGET_DOC_ID,),
        )
        == 1
    )
    assert (
        _count_rows(
            populated_vault["sqlite_path"],
            "SELECT COUNT(*) FROM edges WHERE source_id = ? OR target_id = ?",
            (TARGET_DOC_ID, TARGET_DOC_ID),
        )
        == 3
    )
    assert _count_lancedb_chunks(populated_vault["lancedb_dir"], TARGET_DOC_ID) == 3
    assert not _audit_log_path().exists()


async def test_refuses_when_staging_edge_references_target_as_target(
    populated_vault, monkeypatch, capsys
):
    """Staging edge with target_id == target_doc_id → --apply refuses."""
    conn = sqlite3.connect(populated_vault["sqlite_path"])
    _insert_staging_edge(conn, "se_b", INBOUND_DOC_ID, TARGET_DOC_ID)
    conn.commit()
    conn.close()

    monkeypatch.setattr("builtins.input", lambda _prompt: TARGET_DOC_ID)
    rc = purge_document.purge(
        vault_id=VAULT_ID,
        document_id=TARGET_DOC_ID,
        reason="should-refuse",
        apply=True,
    )

    assert rc != 0
    assert (
        _count_rows(
            populated_vault["sqlite_path"],
            "SELECT COUNT(*) FROM documents WHERE id = ?",
            (TARGET_DOC_ID,),
        )
        == 1
    )


# ─── Tests 7–8: refusal on non-terminal pipeline_status ──────────────


_NON_TERMINAL_STATUSES = [
    PipelineStatus.PROJECTION_COMPLETE.value,
    PipelineStatus.INDEXING_IN_PROGRESS.value,
    PipelineStatus.INDEXING_COMPLETE.value,
    PipelineStatus.ABSTRACTION_IN_PROGRESS.value,
]

_TERMINAL_STATUSES = [
    PipelineStatus.ABSTRACTION_COMPLETE.value,
    PipelineStatus.ABSTRACTION_SKIPPED.value,
    PipelineStatus.FAILED.value,
]


@pytest.mark.parametrize("status", _NON_TERMINAL_STATUSES)
async def test_refuses_when_pipeline_status_non_terminal(
    populated_vault, monkeypatch, capsys, status
):
    """For each non-terminal pipeline_status, --apply refuses."""
    _set_pipeline_status(populated_vault["sqlite_path"], TARGET_DOC_ID, status)

    monkeypatch.setattr("builtins.input", lambda _prompt: TARGET_DOC_ID)
    rc = purge_document.purge(
        vault_id=VAULT_ID,
        document_id=TARGET_DOC_ID,
        reason="pipeline-refuse",
        apply=True,
    )
    combined = capsys.readouterr()

    assert rc != 0, f"should refuse when pipeline_status = {status}"
    assert status in (combined.err + combined.out)
    assert (
        _count_rows(
            populated_vault["sqlite_path"],
            "SELECT COUNT(*) FROM documents WHERE id = ?",
            (TARGET_DOC_ID,),
        )
        == 1
    )


@pytest.mark.parametrize("status", _TERMINAL_STATUSES)
async def test_proceeds_when_pipeline_status_terminal(populated_vault, monkeypatch, status):
    """For each terminal pipeline_status, the script reaches the cascade
    (does not refuse on pipeline-status grounds)."""
    _set_pipeline_status(populated_vault["sqlite_path"], TARGET_DOC_ID, status)

    monkeypatch.setattr("builtins.input", lambda _prompt: TARGET_DOC_ID)
    rc = purge_document.purge(
        vault_id=VAULT_ID,
        document_id=TARGET_DOC_ID,
        reason=f"terminal-{status}",
        apply=True,
    )

    assert rc == 0, f"should not refuse on terminal status {status}"
    assert (
        _count_rows(
            populated_vault["sqlite_path"],
            "SELECT COUNT(*) FROM documents WHERE id = ?",
            (TARGET_DOC_ID,),
        )
        == 0
    )


# ─── Tests 9–10: typed confirmation gate ─────────────────────────────


async def test_apply_requires_typed_confirmation(populated_vault, monkeypatch, capsys):
    """Confirmation input does not match doc_id → refuse, no delete, no audit."""
    monkeypatch.setattr("builtins.input", lambda _prompt: "wrong-id")
    rc = purge_document.purge(
        vault_id=VAULT_ID,
        document_id=TARGET_DOC_ID,
        reason="confirm-test",
        apply=True,
    )

    assert rc != 0
    assert (
        _count_rows(
            populated_vault["sqlite_path"],
            "SELECT COUNT(*) FROM documents WHERE id = ?",
            (TARGET_DOC_ID,),
        )
        == 1
    )
    assert _count_lancedb_chunks(populated_vault["lancedb_dir"], TARGET_DOC_ID) == 3
    assert not _audit_log_path().exists()


async def test_apply_proceeds_on_matching_confirmation(populated_vault, monkeypatch):
    """Confirmation input matches doc_id → cascade executes."""
    monkeypatch.setattr("builtins.input", lambda _prompt: TARGET_DOC_ID)
    rc = purge_document.purge(
        vault_id=VAULT_ID,
        document_id=TARGET_DOC_ID,
        reason="confirm-match",
        apply=True,
    )

    assert rc == 0
    assert (
        _count_rows(
            populated_vault["sqlite_path"],
            "SELECT COUNT(*) FROM documents WHERE id = ?",
            (TARGET_DOC_ID,),
        )
        == 0
    )


# ─── Tests 11–15: cascade correctness ────────────────────────────────


async def test_apply_removes_document_row(populated_vault, monkeypatch):
    monkeypatch.setattr("builtins.input", lambda _prompt: TARGET_DOC_ID)
    rc = purge_document.purge(
        vault_id=VAULT_ID,
        document_id=TARGET_DOC_ID,
        reason="cascade-doc",
        apply=True,
    )
    assert rc == 0
    assert (
        _count_rows(
            populated_vault["sqlite_path"],
            "SELECT COUNT(*) FROM documents WHERE id = ?",
            (TARGET_DOC_ID,),
        )
        == 0
    )


async def test_apply_removes_inbound_and_outbound_edges(populated_vault, monkeypatch):
    monkeypatch.setattr("builtins.input", lambda _prompt: TARGET_DOC_ID)
    purge_document.purge(
        vault_id=VAULT_ID,
        document_id=TARGET_DOC_ID,
        reason="cascade-edges",
        apply=True,
    )
    assert (
        _count_rows(
            populated_vault["sqlite_path"],
            "SELECT COUNT(*) FROM edges WHERE source_id = ?",
            (TARGET_DOC_ID,),
        )
        == 0
    )
    assert (
        _count_rows(
            populated_vault["sqlite_path"],
            "SELECT COUNT(*) FROM edges WHERE target_id = ?",
            (TARGET_DOC_ID,),
        )
        == 0
    )


async def test_apply_removes_document_tags(populated_vault, monkeypatch):
    monkeypatch.setattr("builtins.input", lambda _prompt: TARGET_DOC_ID)
    # Sanity: target had two tags pre-purge.
    assert (
        _count_rows(
            populated_vault["sqlite_path"],
            "SELECT COUNT(*) FROM document_tags WHERE document_id = ?",
            (TARGET_DOC_ID,),
        )
        == 2
    )

    purge_document.purge(
        vault_id=VAULT_ID,
        document_id=TARGET_DOC_ID,
        reason="cascade-tags",
        apply=True,
    )

    assert (
        _count_rows(
            populated_vault["sqlite_path"],
            "SELECT COUNT(*) FROM document_tags WHERE document_id = ?",
            (TARGET_DOC_ID,),
        )
        == 0
    )


async def test_apply_removes_lancedb_chunks(populated_vault, monkeypatch):
    monkeypatch.setattr("builtins.input", lambda _prompt: TARGET_DOC_ID)
    purge_document.purge(
        vault_id=VAULT_ID,
        document_id=TARGET_DOC_ID,
        reason="cascade-chunks",
        apply=True,
    )
    assert _count_lancedb_chunks(populated_vault["lancedb_dir"], TARGET_DOC_ID) == 0


async def test_apply_does_not_touch_unrelated_documents(populated_vault, monkeypatch):
    """Control doc row, edges, tags, chunks all survive a target purge."""
    monkeypatch.setattr("builtins.input", lambda _prompt: TARGET_DOC_ID)
    purge_document.purge(
        vault_id=VAULT_ID,
        document_id=TARGET_DOC_ID,
        reason="cascade-control",
        apply=True,
    )

    assert (
        _count_rows(
            populated_vault["sqlite_path"],
            "SELECT COUNT(*) FROM documents WHERE id = ?",
            (CONTROL_DOC_ID,),
        )
        == 1
    )
    assert (
        _count_rows(
            populated_vault["sqlite_path"],
            "SELECT COUNT(*) FROM edges WHERE source_id = ? OR target_id = ?",
            (CONTROL_DOC_ID, CONTROL_DOC_ID),
        )
        == 1
    )
    assert (
        _count_rows(
            populated_vault["sqlite_path"],
            "SELECT COUNT(*) FROM document_tags WHERE document_id = ?",
            (CONTROL_DOC_ID,),
        )
        == 1
    )
    assert _count_lancedb_chunks(populated_vault["lancedb_dir"], CONTROL_DOC_ID) == 2


# ─── Tests 16–18: audit log ──────────────────────────────────────────


async def test_apply_writes_audit_record_with_required_fields(populated_vault, monkeypatch):
    """Audit JSONL contains a single line with all required fields,
    each value sourced from the live document row."""
    # Capture pre-purge field values.
    conn = sqlite3.connect(populated_vault["sqlite_path"])
    conn.row_factory = sqlite3.Row
    pre = conn.execute(
        "SELECT id, title, source_path, source_content_hash, doc_type FROM documents WHERE id = ?",
        (TARGET_DOC_ID,),
    ).fetchone()
    conn.close()

    monkeypatch.setattr("builtins.input", lambda _prompt: TARGET_DOC_ID)
    rc = purge_document.purge(
        vault_id=VAULT_ID,
        document_id=TARGET_DOC_ID,
        reason="audit-fields-test",
        apply=True,
    )
    assert rc == 0

    audit_path = _audit_log_path()
    assert audit_path.exists()
    lines = audit_path.read_text().strip().splitlines()
    assert len(lines) == 1, f"expected exactly one audit line, got {len(lines)}"
    record = json.loads(lines[0])

    assert record["document_id"] == TARGET_DOC_ID
    assert record["title"] == pre["title"]
    assert record["source_path"] == pre["source_path"]
    assert record["source_content_hash"] == pre["source_content_hash"]
    assert record["doc_type"] == pre["doc_type"]
    assert record["reason"] == "audit-fields-test"
    # timestamp parses as ISO-8601.
    datetime.fromisoformat(record["timestamp"])


async def test_apply_appends_does_not_overwrite(populated_vault, monkeypatch):
    """Pre-existing line in the audit log is preserved; a new line is appended."""
    audit_path = _audit_log_path()
    audit_path.parent.mkdir(parents=True, exist_ok=True)
    pre_existing = json.dumps(
        {
            "timestamp": "2026-01-01T00:00:00+00:00",
            "document_id": "doc_legacy",
            "title": "Legacy",
            "source_path": "test/legacy.md",
            "source_content_hash": "sha256:" + "0" * 64,
            "doc_type": "note",
            "reason": "pre-existing",
        }
    )
    audit_path.write_text(pre_existing + "\n")

    monkeypatch.setattr("builtins.input", lambda _prompt: TARGET_DOC_ID)
    purge_document.purge(
        vault_id=VAULT_ID,
        document_id=TARGET_DOC_ID,
        reason="append-test",
        apply=True,
    )

    lines = audit_path.read_text().strip().splitlines()
    assert len(lines) == 2, "audit log must be append-only"
    assert json.loads(lines[0])["document_id"] == "doc_legacy"
    assert json.loads(lines[1])["document_id"] == TARGET_DOC_ID


class _FailOnDocDeleteConn:
    """sqlite3.Connection facade that raises on ``DELETE FROM documents``.

    Python 3.14 made ``sqlite3.Connection.execute`` read-only, so the
    failure must be injected via a wrapper rather than rebinding the
    method on the connection itself.
    """

    def __init__(self, real: sqlite3.Connection) -> None:
        object.__setattr__(self, "_real", real)

    def execute(self, sql, *params, **kwargs):
        if isinstance(sql, str) and "DELETE FROM documents" in sql:
            raise sqlite3.OperationalError("simulated cascade failure")
        return self._real.execute(sql, *params, **kwargs)

    def __getattr__(self, name):
        return getattr(self._real, name)

    def __setattr__(self, name, value):
        if name == "_real":
            object.__setattr__(self, name, value)
        else:
            setattr(self._real, name, value)


async def test_audit_record_is_written_before_sqlite_delete(populated_vault, monkeypatch):
    """If the SQLite delete fails, an audit record still exists and the
    document row is left in place (transactional rollback). The worst-case
    outcome is audit-with-no-delete, never delete-with-no-audit.
    """
    real_connect = sqlite3.connect

    def failing_connect(*args, **kwargs):
        return _FailOnDocDeleteConn(real_connect(*args, **kwargs))

    monkeypatch.setattr("sage.maintenance.purge_document._sqlite_connect", failing_connect)
    monkeypatch.setattr("builtins.input", lambda _prompt: TARGET_DOC_ID)

    rc = purge_document.purge(
        vault_id=VAULT_ID,
        document_id=TARGET_DOC_ID,
        reason="audit-before-delete",
        apply=True,
    )

    assert rc != 0, "simulated failure should surface as a non-zero exit"
    audit_path = _audit_log_path()
    assert audit_path.exists(), (
        "audit record must be written before the SQLite cascade so a delete "
        "failure cannot leave a deleted document with no audit trail"
    )
    record = json.loads(audit_path.read_text().strip().splitlines()[-1])
    assert record["document_id"] == TARGET_DOC_ID
    # Document row still present because the DELETE rolled back.
    assert (
        _count_rows(
            populated_vault["sqlite_path"],
            "SELECT COUNT(*) FROM documents WHERE id = ?",
            (TARGET_DOC_ID,),
        )
        == 1
    )
