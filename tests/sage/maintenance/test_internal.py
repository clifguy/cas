"""Direct unit tests for the shared per-document purge helper.

and both call ``_purge_one``. The two call sites diverge
in whether they pass a ``batch_id``: passes ``None`` (single-doc,
no batch concept) and passes a UUID shared across every
document in the batch. The helper must honour that distinction in the
audit-log shape, otherwise the single-doc audit format leaks a
spurious ``batch_id`` field.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest
import yaml

from sage.adapters.content_store_lancedb import LanceDBContentStore
from sage.adapters.interfaces import Chunk
from sage.maintenance._internal import _purge_one, _PurgeOneResult
from sage.models.enums import PipelineStatus, SourceType
from sage.models.schemas import Document
from sage.storage.graph_store import GraphStore

VECTOR_DIMENSIONS = 768
VAULT_ID = "purge_one_helper_test"
TARGET_DOC_ID = "doc_target_helper_001"


def _make_doc(doc_id: str) -> Document:
    now = datetime.now(timezone.utc)
    return Document.model_construct(
        id=doc_id,
        title=f"Title for {doc_id}",
        source_type=SourceType.MARKDOWN,
        source_path=f"test/{doc_id}.md",
        lifecycle_status="active",
        version_label=None,
        project=None,
        tags=[],
        authority_scope=None,
        doc_type="note",
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
        pipeline_status=PipelineStatus.ABSTRACTION_COMPLETE,
        pipeline_error=None,
        tier3_metadata=None,
        metadata_confirmed=True,
    )


def _make_chunk(doc_id: str) -> Chunk:
    return Chunk(
        document_id=doc_id,
        heading_path="Heading",
        content="chunk content",
        embedding=[0.1] * VECTOR_DIMENSIONS,
        chunk_index=0,
        doc_type="note",
        lifecycle_status="active",
        project="CAS",
    )


def _write_vault_config(vault_dir: Path, brain_root: Path, storage_root: Path) -> None:
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


def _vault_dir() -> Path:
    from sage import vault_management

    return vault_management._VAULTS_ROOT / VAULT_ID


def _audit_log_path() -> Path:
    return _vault_dir() / ".maintenance_log.jsonl"


@pytest.fixture
async def helper_vault():
    vault_dir = _vault_dir()
    brain_root = vault_dir / "brain"
    storage_root = vault_dir / "sources"
    brain_root.mkdir(parents=True, exist_ok=True)
    storage_root.mkdir(parents=True, exist_ok=True)
    _write_vault_config(vault_dir, brain_root, storage_root)

    graph = GraphStore(brain_root / "graph.db")
    await graph.initialize()
    await graph.insert_document(_make_doc(TARGET_DOC_ID))
    await graph.close()

    store = LanceDBContentStore(brain_root)
    await store.index_chunks(TARGET_DOC_ID, [_make_chunk(TARGET_DOC_ID)])

    return {
        "vault_dir": vault_dir,
        "sqlite_path": brain_root / "graph.db",
        "lancedb_dir": brain_root / "lancedb",
    }


async def test_purge_one_writes_audit_with_batch_id_when_provided(helper_vault):
    """When batch_id is set, the audit record carries that exact value."""
    conn = sqlite3.connect(helper_vault["sqlite_path"])
    try:
        result = _purge_one(
            document_id=TARGET_DOC_ID,
            conn=conn,
            lancedb_dir=helper_vault["lancedb_dir"],
            vault_dir=helper_vault["vault_dir"],
            reason="helper-test-with-bid",
            batch_id="bid-explicit-test-value",
        )
    finally:
        conn.close()

    assert isinstance(result, _PurgeOneResult)
    assert result.succeeded
    assert result.audit_written
    assert result.sqlite_committed
    assert result.lancedb_deleted
    assert result.error is None

    lines = _audit_log_path().read_text().strip().splitlines()
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["batch_id"] == "bid-explicit-test-value"
    assert record["document_id"] == TARGET_DOC_ID
    assert record["reason"] == "helper-test-with-bid"


async def test_purge_one_omits_batch_id_when_none(helper_vault):
    """When batch_id is None, the field is absent from the audit record.

    Anti-coincidental-pass guard for the H2 trap: a helper that always
    injects a batch_id (defaulting to a fresh UUID) would silently break
    the single-doc audit format. This test catches that.
    """
    conn = sqlite3.connect(helper_vault["sqlite_path"])
    try:
        result = _purge_one(
            document_id=TARGET_DOC_ID,
            conn=conn,
            lancedb_dir=helper_vault["lancedb_dir"],
            vault_dir=helper_vault["vault_dir"],
            reason="helper-test-no-bid",
            batch_id=None,
        )
    finally:
        conn.close()

    assert result.succeeded

    record = json.loads(_audit_log_path().read_text().strip().splitlines()[0])
    assert "batch_id" not in record, (
        "single-doc callers pass batch_id=None; the field must be absent"
    )
    assert record["document_id"] == TARGET_DOC_ID


async def test_purge_one_returns_failure_for_missing_document(helper_vault):
    """An unknown document id surfaces as a structured failure result."""
    conn = sqlite3.connect(helper_vault["sqlite_path"])
    try:
        result = _purge_one(
            document_id="doc_does_not_exist",
            conn=conn,
            lancedb_dir=helper_vault["lancedb_dir"],
            vault_dir=helper_vault["vault_dir"],
            reason="missing-doc",
            batch_id=None,
        )
    finally:
        conn.close()

    assert not result.succeeded
    assert "not found" in (result.error or "")
    assert not result.audit_written
    assert not _audit_log_path().exists()
