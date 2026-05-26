"""Backfill script tests.

Exercises ``scripts.backfill_chunk_lifecycle_project.run`` against an
in-process LanceDB + SQLite vault. Verifies that the script:

* Populates ``lifecycle_status`` and ``project`` on chunks whose
  parent documents already carry those values, when those chunks are
  currently NULL (the post-schema-migration state).
* Is idempotent: a second run leaves the table unchanged.
* Honors ``--dry-run`` (the runner's ``execute=False`` mode).
* Skips documents that have no chunks (e.g. failed-pipeline records).
"""

from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone
from pathlib import Path

import pytest

try:
    import lancedb

    _HAS_LANCEDB = True
except ImportError:
    _HAS_LANCEDB = False

if _HAS_LANCEDB:
    from sage.adapters.content_store_lancedb import (
        CHUNKS_TABLE,
        VECTOR_DIMENSIONS,
        LanceDBContentStore,
    )

from sage.adapters.interfaces import Chunk
from sage.models.enums import PipelineStatus, SourceType
from sage.models.schemas import Document

requires_lancedb = pytest.mark.skipif(not _HAS_LANCEDB, reason="lancedb not available")


_DOC_ID_RE = re.compile(r"^[0-9a-f]{8}_[a-z0-9_]+$")
_SHA256_RE = re.compile(r"^sha256:[0-9a-f]{64}$")


def _did(short: str) -> str:
    if _DOC_ID_RE.fullmatch(short):
        return short
    return f"{hashlib.sha256(short.encode()).hexdigest()[:8]}_{short}"


def _sha(short: str) -> str:
    if _SHA256_RE.fullmatch(short):
        return short
    return "sha256:" + hashlib.sha256(f"sage-test-hash:{short}".encode()).hexdigest()


def _make_doc(
    short: str,
    *,
    lifecycle_status: str = "active",
    project: str | None = None,
    pipeline_status: PipelineStatus = PipelineStatus.ABSTRACTION_COMPLETE,
) -> Document:
    now = datetime.now(timezone.utc)
    return Document(
        id=_did(short),
        title=f"Test {short}",
        source_type=SourceType.MARKDOWN,
        source_path=f"test/{short}.md",
        lifecycle_status=lifecycle_status,
        source_content_hash=_sha(short),
        adapter_version="0.1.0",
        created_by="testuser",
        created_at=now,
        last_modified_by="testuser",
        updated_at=now,
        projected_at=now,
        pipeline_status=pipeline_status,
        project=project,
        doc_type="note",
    )


async def _seed_chunks_with_null_metadata(
    brain_root: Path, document_id: str, n_chunks: int = 2
) -> None:
    """Write chunks for a document with lifecycle_status=None and
    project=None. Mirrors the post-migration state where the columns
    exist but have not been backfilled yet.
    """
    store = LanceDBContentStore(brain_root)
    chunks = [
        Chunk(
            document_id=document_id,
            heading_path=f"H{i}",
            content=f"content {i}",
            chunk_index=i,
            embedding=[0.0] * VECTOR_DIMENSIONS,
            doc_type="note",
            lifecycle_status=None,
            project=None,
        )
        for i in range(n_chunks)
    ]
    await store.index_chunks(document_id, chunks)


def _chunk_row_metadata(brain_root: Path, document_id: str) -> list[dict]:
    db = lancedb.connect(str(brain_root / "lancedb"))
    table = db.open_table(CHUNKS_TABLE)
    rows = table.to_arrow().to_pylist()
    return [
        {"lifecycle_status": r["lifecycle_status"], "project": r["project"]}
        for r in rows
        if r["document_id"] == document_id
    ]


# ── Backfill T1: NULL columns populated from parent doc ──────────────


@requires_lancedb
async def test_t0077_backfill_populates_null_columns(tmp_vault_dir, graph_store):
    """When chunks have NULL lifecycle_status/project but the parent
    document carries real values, the backfill must write those values
    to every chunk row.
    """
    from scripts.backfill_chunk_lifecycle_project import run

    brain_root = tmp_vault_dir / "brain"
    doc = _make_doc("doc_backfill_a", lifecycle_status="active", project="CAS")
    await graph_store.insert_document(doc)
    await _seed_chunks_with_null_metadata(brain_root, doc.id, n_chunks=2)

    # Sanity: chunks start with NULL metadata.
    pre_rows = _chunk_row_metadata(brain_root, doc.id)
    assert pre_rows, "fixture must seed chunks"
    assert all(r["lifecycle_status"] is None for r in pre_rows)
    assert all(r["project"] is None for r in pre_rows)

    # Run the backfill (execute mode).
    rc = await run(
        graph_store=graph_store,
        brain_root=brain_root,
        execute=True,
    )
    assert rc == 0

    post_rows = _chunk_row_metadata(brain_root, doc.id)
    assert all(r["lifecycle_status"] == "active" for r in post_rows), (
        "backfill must populate lifecycle_status from the parent document."
    )
    assert all(r["project"] == "CAS" for r in post_rows), (
        "backfill must populate project from the parent document."
    )


# ── Backfill T2: Idempotent ──────────────────────────────────────────


@requires_lancedb
async def test_t0077_backfill_idempotent(tmp_vault_dir, graph_store):
    """Running the backfill twice must not change row counts or raise.
    Real-world operators may run the script as part of a routine
    deployment script that runs the migration unconditionally.
    """
    from scripts.backfill_chunk_lifecycle_project import run

    brain_root = tmp_vault_dir / "brain"
    doc = _make_doc("doc_backfill_idem", lifecycle_status="active", project="alpha")
    await graph_store.insert_document(doc)
    await _seed_chunks_with_null_metadata(brain_root, doc.id, n_chunks=3)

    rc1 = await run(graph_store=graph_store, brain_root=brain_root, execute=True)
    rc2 = await run(graph_store=graph_store, brain_root=brain_root, execute=True)
    assert rc1 == 0
    assert rc2 == 0

    rows = _chunk_row_metadata(brain_root, doc.id)
    assert len(rows) == 3, "row count must be preserved across runs"
    assert all(r["lifecycle_status"] == "active" for r in rows)
    assert all(r["project"] == "alpha" for r in rows)


# ── Backfill T3: --dry-run leaves table unchanged ─────────────────────


@requires_lancedb
async def test_t0077_backfill_dry_run_writes_nothing(tmp_vault_dir, graph_store):
    """Dry-run mode (execute=False) must not mutate the chunk table.
    Used by operators to inspect the planned change before applying it.
    """
    from scripts.backfill_chunk_lifecycle_project import run

    brain_root = tmp_vault_dir / "brain"
    doc = _make_doc("doc_dry_run", lifecycle_status="active", project="CAS")
    await graph_store.insert_document(doc)
    await _seed_chunks_with_null_metadata(brain_root, doc.id, n_chunks=2)

    rc = await run(graph_store=graph_store, brain_root=brain_root, execute=False)
    assert rc == 0

    rows = _chunk_row_metadata(brain_root, doc.id)
    assert all(r["lifecycle_status"] is None for r in rows), "dry-run must not mutate chunk rows."
    assert all(r["project"] is None for r in rows)


# ── Backfill T4: Document with no chunks is skipped silently ─────────


@requires_lancedb
async def test_t0077_backfill_skips_documents_with_no_chunks(tmp_vault_dir, graph_store):
    """A failed-pipeline document has no chunk rows. The backfill must
    skip it without error (no-op update is fine; what matters is the
    script doesn't crash).
    """
    from scripts.backfill_chunk_lifecycle_project import run

    brain_root = tmp_vault_dir / "brain"

    # Doc with chunks (control)
    doc_a = _make_doc("doc_with_chunks", lifecycle_status="active", project="CAS")
    await graph_store.insert_document(doc_a)
    await _seed_chunks_with_null_metadata(brain_root, doc_a.id, n_chunks=1)

    # Doc with no chunks
    doc_b = _make_doc(
        "doc_no_chunks",
        lifecycle_status="active",
        project="CAS",
        pipeline_status=PipelineStatus.FAILED,
    )
    await graph_store.insert_document(doc_b)

    rc = await run(graph_store=graph_store, brain_root=brain_root, execute=True)
    assert rc == 0

    rows_a = _chunk_row_metadata(brain_root, doc_a.id)
    assert rows_a[0]["lifecycle_status"] == "active"
