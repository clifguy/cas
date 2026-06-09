"""Tests for the purge_chain maintenance script.

Exercises the service-level entry point ``sage.maintenance.purge_chain.purge_chain``
against a real SQLite + LanceDB vault. The script enforces the
SAGE-Architecture v2.1 No-Delete Invariant carve-out (ADR-029 v1.1) at
chain granularity: operator-invoked only, dry-run by default, named-only
head, refuses non-linear chains without ``--allow-branched``, typed
confirmation of both head id and chain length, per-member cascade via
``sage.maintenance._internal._purge_one``, audit-log entries carry a
shared ``chain_id`` UUID per invocation.
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

# Module under test. Import will FAIL until sage/maintenance/purge_chain.py exists.
from sage.maintenance import purge_chain
from sage.models.enums import PipelineStatus, SourceType
from sage.models.schemas import Document
from sage.storage.graph_store import SqliteGraphStore

VECTOR_DIMENSIONS = 768
VAULT_ID = "purge_chain_test"

# Linear chain: v3 supersedes v2 supersedes v1.
# Edge convention (per sage/services/graph_ops.py): source supersedes target,
# so newer is source, older is target. Head (newest) = v3, tail = v1.
DOC_V1 = "doc_v1"
DOC_V2 = "doc_v2"
DOC_V3 = "doc_v3"

# Branched: v3 supersedes both v2a and v2b (two siblings).
DOC_V2A = "doc_v2a"
DOC_V2B = "doc_v2b"

# Unrelated control document.
CONTROL_DOC = "doc_control"
CONTROL_ENDPOINT = "doc_control_endpoint"


def _make_doc(
    doc_id: str,
    *,
    pipeline_status: PipelineStatus = PipelineStatus.ABSTRACTION_COMPLETE,
    tags: list[str] | None = None,
    doc_type: str = "note",
    version_label: str | None = None,
) -> Document:
    """Construct a Document with safe defaults, bypassing validators."""
    now = datetime.now(timezone.utc)
    return Document.model_construct(
        id=doc_id,
        title=f"Title for {doc_id}",
        source_type=SourceType.MARKDOWN,
        source_path=f"test/{doc_id}.md",
        lifecycle_status="active",
        version_label=version_label,
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
    edge_type: str = "supersedes",
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


async def _build_base_vault() -> dict:
    """Create vault config + initialized graph + LanceDB store. Returns paths."""
    vault_dir = _vault_dir()
    brain_root = vault_dir / "brain"
    storage_root = vault_dir / "sources"
    brain_root.mkdir(parents=True, exist_ok=True)
    storage_root.mkdir(parents=True, exist_ok=True)
    _write_vault_config(vault_dir, brain_root, storage_root)

    graph = SqliteGraphStore(brain_root / "graph.db")
    await graph.initialize()
    await graph.close()

    return {
        "vault_id": VAULT_ID,
        "vault_dir": vault_dir,
        "brain_root": brain_root,
        "sqlite_path": brain_root / "graph.db",
        "lancedb_dir": brain_root / "lancedb",
    }


@pytest.fixture
async def populated_chain_vault():
    """Linear 3-doc supersedes chain (v3 -> v2 -> v1) + unrelated control doc.

    Edge direction (per graph_ops convention): source supersedes target,
    so v3 -> v2 and v2 -> v1. Head (newest) = v3. Tail (oldest) = v1.

    Each chain member has 2 LanceDB chunks and 1 tag. Control doc has
    1 chunk, 1 tag, and one outbound edge to an off-chain endpoint.
    """
    paths = await _build_base_vault()
    brain_root = paths["brain_root"]

    graph = SqliteGraphStore(brain_root / "graph.db")
    await graph.initialize()
    for doc_id, version in [(DOC_V1, "v1"), (DOC_V2, "v2"), (DOC_V3, "v3")]:
        await graph.insert_document(_make_doc(doc_id, tags=[version], version_label=version))
    await graph.insert_document(_make_doc(CONTROL_DOC, tags=["control"]))
    await graph.insert_document(_make_doc(CONTROL_ENDPOINT))
    await graph.close()

    conn = sqlite3.connect(brain_root / "graph.db")
    # Supersedes chain: v3 -> v2 -> v1.
    _insert_edge(conn, "e_v3_v2", DOC_V3, DOC_V2, "supersedes")
    _insert_edge(conn, "e_v2_v1", DOC_V2, DOC_V1, "supersedes")
    # Control doc has an outbound references edge to an off-chain endpoint.
    _insert_edge(conn, "e_control_out", CONTROL_DOC, CONTROL_ENDPOINT, "references")
    conn.commit()
    conn.close()

    store = LanceDBContentStore(brain_root)
    for doc_id in (DOC_V1, DOC_V2, DOC_V3):
        await store.index_chunks(doc_id, [_make_chunk(doc_id, i) for i in range(2)])
    await store.index_chunks(CONTROL_DOC, [_make_chunk(CONTROL_DOC, 0)])

    paths["head_id"] = DOC_V3
    paths["tail_id"] = DOC_V1
    paths["middle_id"] = DOC_V2
    paths["chain_ids"] = [DOC_V3, DOC_V2, DOC_V1]  # head -> tail order
    paths["control_doc_id"] = CONTROL_DOC
    return paths


@pytest.fixture
async def branched_chain_vault():
    """Branched chain: v3 supersedes both v2a and v2b (two siblings)."""
    paths = await _build_base_vault()
    brain_root = paths["brain_root"]

    graph = SqliteGraphStore(brain_root / "graph.db")
    await graph.initialize()
    for doc_id, version in [(DOC_V2A, "v2a"), (DOC_V2B, "v2b"), (DOC_V3, "v3")]:
        await graph.insert_document(_make_doc(doc_id, tags=[version], version_label=version))
    await graph.close()

    conn = sqlite3.connect(brain_root / "graph.db")
    _insert_edge(conn, "e_v3_v2a", DOC_V3, DOC_V2A, "supersedes")
    _insert_edge(conn, "e_v3_v2b", DOC_V3, DOC_V2B, "supersedes")
    conn.commit()
    conn.close()

    store = LanceDBContentStore(brain_root)
    for doc_id in (DOC_V2A, DOC_V2B, DOC_V3):
        await store.index_chunks(doc_id, [_make_chunk(doc_id, 0)])

    paths["head_id"] = DOC_V3
    paths["chain_ids"] = [DOC_V3, DOC_V2A, DOC_V2B]
    return paths


# ─── T1–T2: dry-run behaviour ───────────────────────────────────────


async def test_dry_run_enumerates_linear_chain(populated_chain_vault, capsys):
    """Dry run lists all 3 chain members, no state change."""
    rc = purge_chain.purge_chain(
        vault_id=VAULT_ID,
        head_id=DOC_V3,
        reason="dry-run-test",
        apply=False,
    )
    out = capsys.readouterr().out

    assert rc == 0, "dry run should exit 0"
    # All three chain member ids appear in the output.
    assert DOC_V3 in out
    assert DOC_V2 in out
    assert DOC_V1 in out
    # Summary line carries chain length 3 and cumulative chunk count 6 (2 per member).
    assert "3" in out
    assert "6" in out

    # Nothing actually changed.
    for doc_id in (DOC_V1, DOC_V2, DOC_V3):
        assert (
            _count_rows(
                populated_chain_vault["sqlite_path"],
                "SELECT COUNT(*) FROM documents WHERE id = ?",
                (doc_id,),
            )
            == 1
        )
        assert _count_lancedb_chunks(populated_chain_vault["lancedb_dir"], doc_id) == 2


async def test_dry_run_does_not_write_audit_log(populated_chain_vault):
    """A dry run must not create the maintenance audit log."""
    audit_path = _audit_log_path()
    assert not audit_path.exists()

    purge_chain.purge_chain(
        vault_id=VAULT_ID,
        head_id=DOC_V3,
        reason="dry-run-test",
        apply=False,
    )

    assert not audit_path.exists(), (
        "dry run wrote the audit log; audit append must be gated behind --apply"
    )


# ─── T3–T4: missing vault / missing head id ─────────────────────────


def test_refuses_unknown_vault(capsys):
    """Vault config does not exist → non-zero exit, clear error to stderr."""
    rc = purge_chain.purge_chain(
        vault_id="this_vault_does_not_exist",
        head_id="any_doc_id",
        reason="refusal-test",
        apply=False,
    )
    err = capsys.readouterr().err

    assert rc != 0
    assert "this_vault_does_not_exist" in err or "vault" in err.lower()


async def test_refuses_unknown_head_id(populated_chain_vault, capsys):
    """Head id does not exist in the vault → non-zero exit, named id in stderr."""
    rc = purge_chain.purge_chain(
        vault_id=VAULT_ID,
        head_id="doc_does_not_exist",
        reason="refusal-test",
        apply=False,
    )
    err = capsys.readouterr().err

    assert rc != 0
    assert "doc_does_not_exist" in err
    assert not _audit_log_path().exists()


# ─── T5: head-id validation ─────────────────────────────────────────


async def test_refuses_when_head_id_is_not_actual_head(populated_chain_vault, capsys):
    """Operator names the middle member as --head-id → refuse and surface the actual head.

    For supersedes (source supersedes target), the head is the newest version:
    no INBOUND edges. The middle doc (v2) has an inbound edge from v3, so it
    is not a head — the script must refuse and name v3 as the actual head.
    """
    rc = purge_chain.purge_chain(
        vault_id=VAULT_ID,
        head_id=DOC_V2,  # middle, not head
        reason="head-validation-test",
        apply=False,
    )
    out_err = capsys.readouterr()
    combined = out_err.err + out_err.out

    assert rc != 0
    assert DOC_V3 in combined, "must name the actual head (v3) in the refusal message"
    assert DOC_V2 in combined, "must reference the supplied (incorrect) head"
    assert not _audit_log_path().exists()


# ─── T6–T7: branched chain refusal and --allow-branched escape ──────


async def test_refuses_branched_chain_without_flag(branched_chain_vault, capsys):
    """Branched chain without --allow-branched → refuse with linearity diagnostic."""
    rc = purge_chain.purge_chain(
        vault_id=VAULT_ID,
        head_id=DOC_V3,
        reason="branched-refuse-test",
        apply=False,
    )
    out_err = capsys.readouterr()
    combined = (out_err.err + out_err.out).lower()

    assert rc != 0
    assert "branched" in combined or "linear" in combined


async def test_allow_branched_permits_branched_chain(branched_chain_vault, capsys):
    """--allow-branched permits enumeration of a branched chain."""
    rc = purge_chain.purge_chain(
        vault_id=VAULT_ID,
        head_id=DOC_V3,
        reason="branched-allow-test",
        apply=False,
        allow_branched=True,
    )
    out = capsys.readouterr().out

    assert rc == 0, "--allow-branched should let the dry-run proceed"
    assert DOC_V3 in out
    assert DOC_V2A in out
    assert DOC_V2B in out


# ─── T8–T9: precondition refusals across whole chain ────────────────


async def test_refuses_when_any_member_has_pending_staging_edges(
    populated_chain_vault, monkeypatch, capsys
):
    """Staging edge attached to a NON-head member must refuse the whole chain.

    Placing the violation on v2 (middle) rather than v3 (head) is load-bearing:
    it forces the script to scan every chain member, not just the head.
    The monkeypatched input supplies *correct* head id and chain length so
    that any rc != 0 must come from the precondition refusal, not a prompt
    mismatch — otherwise the test would pass coincidentally on a head-only
    iteration regression.
    """
    conn = sqlite3.connect(populated_chain_vault["sqlite_path"])
    _insert_staging_edge(conn, "se_middle", DOC_V2, CONTROL_ENDPOINT)
    conn.commit()
    conn.close()

    inputs = iter([DOC_V3, "3"])
    monkeypatch.setattr("builtins.input", lambda _prompt: next(inputs))
    rc = purge_chain.purge_chain(
        vault_id=VAULT_ID,
        head_id=DOC_V3,
        reason="staging-refuse",
        apply=True,
    )
    out_err = capsys.readouterr()
    combined = (out_err.err + out_err.out).lower()

    assert rc != 0
    assert "staging" in combined
    # No chain member was deleted.
    for doc_id in (DOC_V1, DOC_V2, DOC_V3):
        assert (
            _count_rows(
                populated_chain_vault["sqlite_path"],
                "SELECT COUNT(*) FROM documents WHERE id = ?",
                (doc_id,),
            )
            == 1
        )
    assert not _audit_log_path().exists()


async def test_refuses_when_any_member_has_non_terminal_pipeline_status(
    populated_chain_vault, monkeypatch, capsys
):
    """Non-terminal pipeline_status on a NON-head member must refuse the whole chain.

    Correct typed confirmations are supplied so the rc != 0 must come from
    the precondition refusal, not a prompt mismatch (same coincidental-pass
    guard as T8).
    """
    _set_pipeline_status(
        populated_chain_vault["sqlite_path"],
        DOC_V2,
        PipelineStatus.INDEXING_IN_PROGRESS.value,
    )

    inputs = iter([DOC_V3, "3"])
    monkeypatch.setattr("builtins.input", lambda _prompt: next(inputs))
    rc = purge_chain.purge_chain(
        vault_id=VAULT_ID,
        head_id=DOC_V3,
        reason="pipeline-refuse",
        apply=True,
    )
    out_err = capsys.readouterr()
    combined = out_err.err + out_err.out

    assert rc != 0
    # The offending status or member id appears in the message.
    assert PipelineStatus.INDEXING_IN_PROGRESS.value in combined or DOC_V2 in combined
    for doc_id in (DOC_V1, DOC_V2, DOC_V3):
        assert (
            _count_rows(
                populated_chain_vault["sqlite_path"],
                "SELECT COUNT(*) FROM documents WHERE id = ?",
                (doc_id,),
            )
            == 1
        )


# ─── T10–T11: typed confirmation gate (head id + chain length) ──────


async def test_apply_requires_typed_confirmation_of_head_id(populated_chain_vault, monkeypatch):
    """Wrong head id at first prompt → refuse, no delete, no audit."""
    monkeypatch.setattr("builtins.input", lambda _prompt: "wrong-head-id")
    rc = purge_chain.purge_chain(
        vault_id=VAULT_ID,
        head_id=DOC_V3,
        reason="confirm-head-test",
        apply=True,
    )

    assert rc != 0
    for doc_id in (DOC_V1, DOC_V2, DOC_V3):
        assert (
            _count_rows(
                populated_chain_vault["sqlite_path"],
                "SELECT COUNT(*) FROM documents WHERE id = ?",
                (doc_id,),
            )
            == 1
        )
    assert not _audit_log_path().exists()


async def test_apply_requires_typed_confirmation_of_chain_length(
    populated_chain_vault, monkeypatch
):
    """Correct head id but wrong length → refuse, no delete, no audit."""
    inputs = iter([DOC_V3, "2"])  # length is 3, type "2"
    monkeypatch.setattr("builtins.input", lambda _prompt: next(inputs))
    rc = purge_chain.purge_chain(
        vault_id=VAULT_ID,
        head_id=DOC_V3,
        reason="confirm-length-test",
        apply=True,
    )

    assert rc != 0
    for doc_id in (DOC_V1, DOC_V2, DOC_V3):
        assert (
            _count_rows(
                populated_chain_vault["sqlite_path"],
                "SELECT COUNT(*) FROM documents WHERE id = ?",
                (doc_id,),
            )
            == 1
        )
    assert not _audit_log_path().exists()


# ─── T12: full cascade across the chain ─────────────────────────────


async def test_apply_purges_all_chain_members(populated_chain_vault, monkeypatch):
    """All chain members are removed; control doc and its dependents survive."""
    inputs = iter([DOC_V3, "3"])
    monkeypatch.setattr("builtins.input", lambda _prompt: next(inputs))

    rc = purge_chain.purge_chain(
        vault_id=VAULT_ID,
        head_id=DOC_V3,
        reason="full-cascade-test",
        apply=True,
    )

    assert rc == 0, "full apply with correct confirmations should succeed"
    # Each chain member: documents row, tags, edges, chunks all gone.
    for doc_id in (DOC_V1, DOC_V2, DOC_V3):
        assert (
            _count_rows(
                populated_chain_vault["sqlite_path"],
                "SELECT COUNT(*) FROM documents WHERE id = ?",
                (doc_id,),
            )
            == 0
        ), f"document {doc_id} should be gone"
        assert (
            _count_rows(
                populated_chain_vault["sqlite_path"],
                "SELECT COUNT(*) FROM document_tags WHERE document_id = ?",
                (doc_id,),
            )
            == 0
        )
        assert (
            _count_rows(
                populated_chain_vault["sqlite_path"],
                "SELECT COUNT(*) FROM edges WHERE source_id = ? OR target_id = ?",
                (doc_id, doc_id),
            )
            == 0
        )
        assert _count_lancedb_chunks(populated_chain_vault["lancedb_dir"], doc_id) == 0


# ─── T13–T14: audit log shape ───────────────────────────────────────


async def test_audit_log_carries_shared_chain_id_per_member(populated_chain_vault, monkeypatch):
    """Three audit entries; each carries chain_id; all three ids equal; no batch_id."""
    inputs = iter([DOC_V3, "3"])
    monkeypatch.setattr("builtins.input", lambda _prompt: next(inputs))
    rc = purge_chain.purge_chain(
        vault_id=VAULT_ID,
        head_id=DOC_V3,
        reason="chain-id-test",
        apply=True,
    )
    assert rc == 0

    audit_path = _audit_log_path()
    assert audit_path.exists()
    lines = audit_path.read_text().strip().splitlines()
    assert len(lines) == 3, f"expected 3 audit entries, got {len(lines)}"

    records = [json.loads(line) for line in lines]
    chain_ids = {r["chain_id"] for r in records}
    assert len(chain_ids) == 1, (
        "chain_id must be shared across all members of one invocation; "
        f"got {len(chain_ids)} distinct values: {chain_ids}"
    )
    for r in records:
        assert "batch_id" not in r, (
            "chain-purge entries must not carry batch_id (that field is for T-0106 batch purges)"
        )


async def test_audit_entry_fields_match_pre_purge_document_state(
    populated_chain_vault, monkeypatch
):
    """Each audit line carries the document's pre-purge metadata."""
    # Snapshot live state per doc.
    conn = sqlite3.connect(populated_chain_vault["sqlite_path"])
    conn.row_factory = sqlite3.Row
    pre_state: dict[str, dict] = {}
    for doc_id in (DOC_V1, DOC_V2, DOC_V3):
        row = conn.execute(
            "SELECT id, title, source_path, source_content_hash, doc_type "
            "FROM documents WHERE id = ?",
            (doc_id,),
        ).fetchone()
        pre_state[doc_id] = dict(row)
    conn.close()

    inputs = iter([DOC_V3, "3"])
    monkeypatch.setattr("builtins.input", lambda _prompt: next(inputs))
    rc = purge_chain.purge_chain(
        vault_id=VAULT_ID,
        head_id=DOC_V3,
        reason="audit-fields-test",
        apply=True,
    )
    assert rc == 0

    lines = _audit_log_path().read_text().strip().splitlines()
    by_id = {json.loads(line)["document_id"]: json.loads(line) for line in lines}
    for doc_id in (DOC_V1, DOC_V2, DOC_V3):
        record = by_id[doc_id]
        pre = pre_state[doc_id]
        assert record["title"] == pre["title"]
        assert record["source_path"] == pre["source_path"]
        assert record["source_content_hash"] == pre["source_content_hash"]
        assert record["doc_type"] == pre["doc_type"]
        assert record["reason"] == "audit-fields-test"
        assert "chain_id" in record
        datetime.fromisoformat(record["timestamp"])


# ─── T15: per-member failure halt + retain-completed ────────────────


class _FailOnDocDeleteForId:
    """sqlite3.Connection facade that raises on DELETE FROM documents for one id.

    Lets us simulate a per-document cascade failure mid-chain. Earlier
    members purge cleanly; the target member's cascade fails; later
    members are never attempted.
    """

    def __init__(self, real: sqlite3.Connection, fail_for_id: str) -> None:
        object.__setattr__(self, "_real", real)
        object.__setattr__(self, "_fail_for_id", fail_for_id)

    def execute(self, sql, *params, **kwargs):
        if (
            isinstance(sql, str)
            and "DELETE FROM documents" in sql
            and params
            and self._fail_for_id in tuple(params[0])
        ):
            raise sqlite3.OperationalError(f"simulated cascade failure on {self._fail_for_id}")
        return self._real.execute(sql, *params, **kwargs)

    def __getattr__(self, name):
        return getattr(self._real, name)

    def __setattr__(self, name, value):
        if name in ("_real", "_fail_for_id"):
            object.__setattr__(self, name, value)
        else:
            setattr(self._real, name, value)


async def test_per_member_failure_halts_chain_and_retains_completed(
    populated_chain_vault, monkeypatch
):
    """Failure on the second member halts the loop. First member stays deleted
    with audit entry. Failing member stays present, with audit entry (audit
    before delete). Third member untouched, no audit entry. chain_id shared
    across the two written audit entries.
    """
    real_connect = sqlite3.connect

    def failing_connect(*args, **kwargs):
        return _FailOnDocDeleteForId(real_connect(*args, **kwargs), DOC_V2)

    monkeypatch.setattr("sage.maintenance.purge_chain._sqlite_connect", failing_connect)
    inputs = iter([DOC_V3, "3"])
    monkeypatch.setattr("builtins.input", lambda _prompt: next(inputs))

    rc = purge_chain.purge_chain(
        vault_id=VAULT_ID,
        head_id=DOC_V3,
        reason="partial-failure-test",
        apply=True,
    )

    assert rc != 0, "per-member failure should surface non-zero"

    # Head (first in head-first iteration) was successfully purged.
    assert (
        _count_rows(
            populated_chain_vault["sqlite_path"],
            "SELECT COUNT(*) FROM documents WHERE id = ?",
            (DOC_V3,),
        )
        == 0
    )
    # Failing middle member still present (rollback).
    assert (
        _count_rows(
            populated_chain_vault["sqlite_path"],
            "SELECT COUNT(*) FROM documents WHERE id = ?",
            (DOC_V2,),
        )
        == 1
    )
    # Third member never attempted.
    assert (
        _count_rows(
            populated_chain_vault["sqlite_path"],
            "SELECT COUNT(*) FROM documents WHERE id = ?",
            (DOC_V1,),
        )
        == 1
    )

    # Audit log: two entries (head + failing middle), both with chain_id matching.
    lines = _audit_log_path().read_text().strip().splitlines()
    assert len(lines) == 2, (
        f"expected audit entries for head (succeeded) and middle (audit-before-delete), "
        f"got {len(lines)} lines"
    )
    records = [json.loads(line) for line in lines]
    audit_ids = {r["document_id"] for r in records}
    assert audit_ids == {DOC_V3, DOC_V2}
    chain_ids = {r["chain_id"] for r in records}
    assert len(chain_ids) == 1, "chain_id must match across both partial-failure entries"


# ─── T16: control documents survive ─────────────────────────────────


async def test_chain_purge_does_not_touch_unrelated_documents(populated_chain_vault, monkeypatch):
    """Control doc, its tag, its outbound edge, its chunk all intact after chain purge."""
    inputs = iter([DOC_V3, "3"])
    monkeypatch.setattr("builtins.input", lambda _prompt: next(inputs))
    rc = purge_chain.purge_chain(
        vault_id=VAULT_ID,
        head_id=DOC_V3,
        reason="control-survival-test",
        apply=True,
    )
    assert rc == 0

    assert (
        _count_rows(
            populated_chain_vault["sqlite_path"],
            "SELECT COUNT(*) FROM documents WHERE id = ?",
            (CONTROL_DOC,),
        )
        == 1
    )
    assert (
        _count_rows(
            populated_chain_vault["sqlite_path"],
            "SELECT COUNT(*) FROM document_tags WHERE document_id = ?",
            (CONTROL_DOC,),
        )
        == 1
    )
    assert (
        _count_rows(
            populated_chain_vault["sqlite_path"],
            "SELECT COUNT(*) FROM edges WHERE source_id = ? OR target_id = ?",
            (CONTROL_DOC, CONTROL_DOC),
        )
        == 1
    )
    assert _count_lancedb_chunks(populated_chain_vault["lancedb_dir"], CONTROL_DOC) == 1
    # The control endpoint document is also untouched.
    assert (
        _count_rows(
            populated_chain_vault["sqlite_path"],
            "SELECT COUNT(*) FROM documents WHERE id = ?",
            (CONTROL_ENDPOINT,),
        )
        == 1
    )
