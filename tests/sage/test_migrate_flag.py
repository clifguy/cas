"""Tests for the --migrate CLI gating (TEST-SAGE-MIG-001..013).

Spec: tests/sage/migrate_flag_tests.md.

These tests build deliberately legacy-shaped storage (a SQLite database
that omits a column added by MIGRATION_PLAN, or a LanceDB chunks table
missing a column from CHUNKS_SCHEMA) and verify that:

* The default startup path refuses to start when a migration is needed.
* The --migrate path applies the migration and preserves data.
* Owner bootstrap remains always-on (data init, not schema).
"""
from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest

from sage.adapters.stubs import (
    StubAbstractionProvider,
    StubContentStore,
    StubEmbeddingProvider,
)
from sage.config import VaultConfig
from sage.mcp_init import initialize_services
from sage.storage.graph_store import GraphStore
from sage.storage.migrations import (
    MIGRATION_PLAN,
    POST_MIGRATION_DDL,
    SchemaMigrationRequired,
    pending_migrations,
)


# ── LanceDB skip guard ───────────────────────────────────────────────

try:
    import lancedb  # noqa: F401
    import pyarrow as pa
    from sage.adapters.content_store_lancedb import (
        CHUNKS_TABLE,
        VECTOR_DIMENSIONS,
        LanceDBContentStore,
    )
    _HAS_LANCEDB = True
except ImportError:
    _HAS_LANCEDB = False

requires_lancedb = pytest.mark.skipif(
    not _HAS_LANCEDB, reason="lancedb not available"
)


# ── Legacy-shape helpers ────────────────────────────────────────────

# A documents table that omits every column added by MIGRATION_PLAN.
# This is the shape SAGE used before any of those migrations existed.
_LEGACY_DOCUMENTS_TABLE = """\
CREATE TABLE documents (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    source_type TEXT NOT NULL,
    source_path TEXT NOT NULL,
    lifecycle_status TEXT NOT NULL DEFAULT 'active',
    version_label TEXT,
    project TEXT,
    tags TEXT,
    authority_scope TEXT,
    doc_type TEXT,
    source_content_hash TEXT NOT NULL,
    adapter_version TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    last_modified_by TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    projected_at TEXT,
    indexed_at TEXT,
    semantic_abstract TEXT,
    pipeline_status TEXT NOT NULL DEFAULT 'projection_complete',
    pipeline_error TEXT,
    tier3_metadata TEXT
);
"""

# An edges table missing all CAS-ADR-017 anchor columns.
_LEGACY_EDGES_TABLE = """\
CREATE TABLE edges (
    id TEXT PRIMARY KEY,
    source_id TEXT NOT NULL,
    target_id TEXT NOT NULL,
    edge_type TEXT NOT NULL,
    created_at TEXT NOT NULL,
    notes TEXT,
    rationale TEXT,
    FOREIGN KEY (source_id) REFERENCES documents(id),
    FOREIGN KEY (target_id) REFERENCES documents(id)
);
"""

_USERS_TABLE = """\
CREATE TABLE users (
    id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    type TEXT NOT NULL CHECK(type IN ('human', 'agent')),
    created_at TEXT NOT NULL
);
"""

_STAGING_EDGES_TABLE = """\
CREATE TABLE staging_edges (
    id TEXT PRIMARY KEY,
    source_id TEXT NOT NULL,
    target_id TEXT NOT NULL,
    edge_type TEXT NOT NULL,
    inference_evidence TEXT NOT NULL,
    confidence_tier INTEGER NOT NULL DEFAULT 2,
    created_at TEXT NOT NULL,
    FOREIGN KEY (source_id) REFERENCES documents(id),
    FOREIGN KEY (target_id) REFERENCES documents(id)
);
"""


def _build_legacy_db(db_path: Path) -> None:
    """Create a SQLite DB with the pre-migration schema."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(_LEGACY_DOCUMENTS_TABLE)
        conn.execute(_LEGACY_EDGES_TABLE)
        conn.execute(_USERS_TABLE)
        conn.execute(_STAGING_EDGES_TABLE)
        conn.commit()
    finally:
        conn.close()


def _seed_legacy_row(db_path: Path, doc_id: str = "abc12345_seed") -> None:
    """Insert a documents row into the legacy DB to verify preservation."""
    now = datetime.now(timezone.utc).isoformat()
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            "INSERT INTO documents (id, title, source_type, source_path, "
            "source_content_hash, adapter_version, created_by, created_at, "
            "last_modified_by, updated_at) "
            "VALUES (?, 'Seed Doc', 'markdown', 'src/seed.md', 'h1', '0.1.0',"
            " 'tester', ?, 'tester', ?)",
            (doc_id, now, now),
        )
        conn.commit()
    finally:
        conn.close()


def _columns(db_path: Path, table: str) -> set[str]:
    conn = sqlite3.connect(str(db_path))
    try:
        rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
        return {r[1] for r in rows}
    finally:
        conn.close()


# ── MIG-001: detection helper is read-only ─────────────────────────


def test_mig_001_pending_migrations_is_readonly(tmp_path):
    db_path = tmp_path / "graph.db"
    _build_legacy_db(db_path)
    pre = _columns(db_path, "documents") | _columns(db_path, "edges")

    conn = sqlite3.connect(str(db_path))
    try:
        pending = pending_migrations(conn)
    finally:
        conn.close()

    assert len(pending) == len(MIGRATION_PLAN), \
        "every migration column should be detected as pending"
    detected = {(m.table, m.column) for m in pending}
    expected = {(m.table, m.column) for m in MIGRATION_PLAN}
    assert detected == expected

    post = _columns(db_path, "documents") | _columns(db_path, "edges")
    assert pre == post, "detection must not mutate the database"


# ── MIG-002: GraphStore.initialize(migrate=False) raises on legacy ─


async def test_mig_002_graphstore_refuses_without_flag(tmp_path):
    db_path = tmp_path / "graph.db"
    _build_legacy_db(db_path)

    store = GraphStore(db_path)
    with pytest.raises(SchemaMigrationRequired) as exc_info:
        await store.initialize(migrate=False)

    msg = str(exc_info.value)
    assert "--migrate" in msg
    assert "documents.metadata_confirmed" in msg or "edges.resolution_policy" in msg

    # Database left as-is.
    assert "metadata_confirmed" not in _columns(db_path, "documents")

    await store.close()


# ── MIG-003: GraphStore.initialize(migrate=True) applies, preserves data ─


async def test_mig_003_graphstore_applies_with_flag(tmp_path):
    db_path = tmp_path / "graph.db"
    _build_legacy_db(db_path)
    _seed_legacy_row(db_path, doc_id="abc12345_seed")

    store = GraphStore(db_path)
    await store.initialize(migrate=True)
    try:
        cols = _columns(db_path, "documents")
        for m in MIGRATION_PLAN:
            if m.table == "documents":
                assert m.column in cols, f"column {m.column} should have been added"
        edge_cols = _columns(db_path, "edges")
        for m in MIGRATION_PLAN:
            if m.table == "edges":
                assert m.column in edge_cols

        # Pre-existing row preserved.
        conn = sqlite3.connect(str(db_path))
        try:
            row = conn.execute(
                "SELECT id, title FROM documents WHERE id = ?",
                ("abc12345_seed",),
            ).fetchone()
        finally:
            conn.close()
        assert row is not None
        assert row[1] == "Seed Doc"

        # POST_MIGRATION_DDL indexes were created.
        conn = sqlite3.connect(str(db_path))
        try:
            idx = {
                r[0] for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='index'"
                ).fetchall()
            }
        finally:
            conn.close()
        # Sanity: at least one of the documented post-migration indexes exists.
        assert any("idx_documents" in name for name in idx)
        # And POST_MIGRATION_DDL is non-empty (sanity guard against drift).
        assert POST_MIGRATION_DDL
    finally:
        await store.close()


# ── MIG-004: current schema, migrate=False, no-op ──────────────────


async def test_mig_004_graphstore_current_schema_no_op(tmp_path):
    """Fresh DB created from current TABLES has no pending migrations,
    so initialize(migrate=False) succeeds without raising."""
    db_path = tmp_path / "graph.db"

    store = GraphStore(db_path)
    await store.initialize(migrate=False)  # creates fresh, current schema
    await store.close()

    # Re-open without migrate; should still succeed.
    store2 = GraphStore(db_path)
    await store2.initialize(migrate=False)
    await store2.close()


# ── LanceDB legacy-table builder ───────────────────────────────────


if _HAS_LANCEDB:
    _LEGACY_CHUNKS_SCHEMA = pa.schema([
        pa.field("document_id", pa.utf8()),
        pa.field("heading_path", pa.utf8()),
        pa.field("content", pa.utf8()),
        pa.field("chunk_index", pa.int32()),
        pa.field("vector", pa.list_(pa.float32(), VECTOR_DIMENSIONS)),
    ])

    def _build_legacy_lancedb(brain_root: Path, *, n_rows: int = 1) -> None:
        """Create a LanceDB chunks table with the pre-doc_type schema."""
        brain_root.mkdir(parents=True, exist_ok=True)
        db = lancedb.connect(str(brain_root / "lancedb"))
        rows = [
            {
                "document_id": f"doc_{i}",
                "heading_path": f"H{i}",
                "content": f"content {i}",
                "chunk_index": i,
                "vector": [0.0] * VECTOR_DIMENSIONS,
            }
            for i in range(n_rows)
        ]
        db.create_table(CHUNKS_TABLE, data=rows, schema=_LEGACY_CHUNKS_SCHEMA)


# ── MIG-005: LanceDB detection is read-only ────────────────────────


@requires_lancedb
def test_mig_005_lancedb_detection_readonly(tmp_path):
    brain = tmp_path / "brain"
    _build_legacy_lancedb(brain, n_rows=2)

    # Open without migrating; this should raise (gating), but the
    # pending_schema_columns helper itself can be used by an operator
    # to inspect state. We exercise the gating path here and check
    # that no backup file is written even on the failure path.
    with pytest.raises(SchemaMigrationRequired):
        LanceDBContentStore(brain, migrate=False)

    backup = brain / "chunks_migration_backup.parquet"
    assert not backup.exists(), "no backup should be written on detection"

    # The chunks table should still exist with the legacy schema.
    db = lancedb.connect(str(brain / "lancedb"))
    table = db.open_table(CHUNKS_TABLE)
    assert "doc_type" not in set(table.schema.names)


# ── MIG-006: LanceDB(migrate=False) on legacy raises ───────────────


@requires_lancedb
def test_mig_006_lancedb_refuses_without_flag(tmp_path):
    brain = tmp_path / "brain"
    _build_legacy_lancedb(brain, n_rows=1)

    with pytest.raises(SchemaMigrationRequired) as exc_info:
        LanceDBContentStore(brain, migrate=False)

    msg = str(exc_info.value)
    assert "--migrate" in msg
    assert "doc_type" in msg

    # Untouched.
    db = lancedb.connect(str(brain / "lancedb"))
    table = db.open_table(CHUNKS_TABLE)
    assert table.count_rows() == 1
    assert "doc_type" not in set(table.schema.names)


# ── MIG-007: LanceDB(migrate=True) rebuilds and preserves rows ─────


@requires_lancedb
def test_mig_007_lancedb_applies_with_flag(tmp_path):
    brain = tmp_path / "brain"
    _build_legacy_lancedb(brain, n_rows=3)

    store = LanceDBContentStore(brain, migrate=True)
    assert store.pending_schema_columns() == set()

    db = lancedb.connect(str(brain / "lancedb"))
    table = db.open_table(CHUNKS_TABLE)
    assert "doc_type" in set(table.schema.names)
    assert table.count_rows() == 3

    rows = table.to_arrow().to_pylist()
    docs = sorted(r["document_id"] for r in rows)
    assert docs == ["doc_0", "doc_1", "doc_2"]
    assert all(r["doc_type"] is None for r in rows)

    backup = brain / "chunks_migration_backup.parquet"
    assert not backup.exists(), "backup must be deleted on success"


# ── MIG-008: LanceDB refuses to overwrite existing backup ─────────


@requires_lancedb
def test_mig_008_lancedb_refuses_to_clobber_backup(tmp_path):
    brain = tmp_path / "brain"
    _build_legacy_lancedb(brain, n_rows=1)

    backup = brain / "chunks_migration_backup.parquet"
    backup.write_bytes(b"prior failed migration backup")
    original_bytes = backup.read_bytes()

    with pytest.raises(RuntimeError) as exc_info:
        LanceDBContentStore(brain, migrate=True)

    assert "chunks_migration_backup.parquet" in str(exc_info.value)
    assert backup.read_bytes() == original_bytes, "backup must not be overwritten"

    # Table still legacy-shaped.
    db = lancedb.connect(str(brain / "lancedb"))
    table = db.open_table(CHUNKS_TABLE)
    assert "doc_type" not in set(table.schema.names)


# ── MIG-009: Row count is logged before destructive rebuild ───────


@requires_lancedb
def test_mig_009_lancedb_logs_row_count(tmp_path, caplog):
    brain = tmp_path / "brain"
    _build_legacy_lancedb(brain, n_rows=7)

    with caplog.at_level(logging.INFO, logger="sage.adapters.content_store_lancedb"):
        LanceDBContentStore(brain, migrate=True)

    msgs = [rec.getMessage() for rec in caplog.records]
    assert any("7" in m and "rows" in m.lower() for m in msgs), (
        f"expected an INFO log mentioning '7 rows' to be migrated, got: {msgs}"
    )


# ── MIG-010: initialize_services threads the flag ─────────────────


def _minimal_config(tmp_path: Path) -> VaultConfig:
    brain = tmp_path / "brain"
    sources = tmp_path / "sources"
    brain.mkdir()
    sources.mkdir()
    return VaultConfig.model_validate({
        "vault": {
            "id": "test_vault",
            "name": "Test Vault",
            "owner": "tester",
            "storage_root": str(sources),
            "brain_root": str(brain),
            "visibility": "personal",
        },
        "document_types": {
            "doc_types": [{"value": "note", "label": "Note"}],
        },
        "lifecycle": {
            "base_states_required": True,
            "states": [
                {"value": "active", "label": "Active"},
                {"value": "archived", "label": "Archived", "is_terminal": True},
            ],
            "transitions": [
                {"from_state": "(new)", "action": "ingest", "to_state": "active"},
                {"from_state": "active", "action": "archive", "to_state": "archived"},
            ],
        },
        "source_adapters": {
            "adapters": [{"source_type": "markdown", "enabled": True}],
        },
        "metadata_extraction": {},
        "edge_inference": {"tier_assignments": []},
    })


async def test_mig_010_initialize_services_propagates_flag(tmp_path):
    config = _minimal_config(tmp_path)

    # Pre-create a legacy graph.db where SAGE expects it.
    brain = Path(config.vault.brain_root)
    _build_legacy_db(brain / "graph.db")

    with pytest.raises(SchemaMigrationRequired):
        await initialize_services(
            config,
            migrate=False,
            content_store=StubContentStore(),
            embedding_provider=StubEmbeddingProvider(),
            abstraction_provider=StubAbstractionProvider(),
        )

    # graph.db unchanged.
    assert "metadata_confirmed" not in _columns(brain / "graph.db", "documents")

    # With migrate=True it succeeds.
    services = await initialize_services(
        config,
        migrate=True,
        content_store=StubContentStore(),
        embedding_provider=StubEmbeddingProvider(),
        abstraction_provider=StubAbstractionProvider(),
    )
    try:
        assert "metadata_confirmed" in _columns(brain / "graph.db", "documents")
    finally:
        await services.graph_store.close()


# ── MIG-011, MIG-012: __main__ argparse defaults ──────────────────


def test_mig_011_main_default_no_migrate():
    from sage.__main__ import _build_parser
    args = _build_parser().parse_args(["some_config.yaml"])
    assert args.migrate is False


def test_mig_012_main_with_migrate_flag():
    from sage.__main__ import _build_parser
    args = _build_parser().parse_args(["some_config.yaml", "--migrate"])
    assert args.migrate is True


# ── MIG-013: Owner bootstrap remains always-on ────────────────────


async def test_mig_013_owner_bootstrap_always_runs(tmp_path):
    config = _minimal_config(tmp_path)

    services = await initialize_services(
        config,
        migrate=False,  # no schema migration needed on a fresh vault
        content_store=StubContentStore(),
        embedding_provider=StubEmbeddingProvider(),
        abstraction_provider=StubAbstractionProvider(),
    )
    try:
        owner = await services.graph_store.get_user_by_display_name(
            config.vault.owner
        )
        assert owner is not None
        assert owner.display_name == config.vault.owner
    finally:
        await services.graph_store.close()
