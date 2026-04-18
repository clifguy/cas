"""SQLite DDL for the SAGE graph store.

Executed at vault initialization. All tables use IF NOT EXISTS for
idempotent re-initialization.
"""

SCHEMA_VERSION = 2

DOCUMENTS_TABLE = """\
CREATE TABLE IF NOT EXISTS documents (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    source_type TEXT NOT NULL,
    source_path TEXT NOT NULL,
    lifecycle_status TEXT NOT NULL DEFAULT 'active',
    version_label TEXT,
    project TEXT,
    tags TEXT,                        -- JSON array stored as text
    authority_scope TEXT,
    doc_type TEXT,
    source_content_hash TEXT NOT NULL,
    adapter_version TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,         -- ISO 8601
    last_modified_by TEXT NOT NULL,
    updated_at TEXT NOT NULL,         -- ISO 8601
    projected_at TEXT,                -- ISO 8601, nullable
    indexed_at TEXT,                  -- ISO 8601, nullable (BH-007)
    source_modified_at TEXT,          -- ISO 8601, nullable (BH-049)
    document_date TEXT,               -- YYYY-MM-DD, nullable (BH-062)
    semantic_abstract TEXT,           -- nullable
    pipeline_status TEXT NOT NULL DEFAULT 'projection_complete',
    pipeline_error TEXT,              -- nullable (BH-022, BH-024)
    tier3_metadata TEXT,              -- JSON, nullable
    metadata_confirmed INTEGER NOT NULL DEFAULT 0  -- boolean (BE-014)
);
"""

EDGES_TABLE = """\
CREATE TABLE IF NOT EXISTS edges (
    id TEXT PRIMARY KEY,              -- UUID (BH-032)
    source_id TEXT NOT NULL,
    target_id TEXT,                   -- nullable on `retracts` edges (CAS-ADR-017)
    edge_type TEXT NOT NULL,
    resolution_policy TEXT,           -- frozen policy (CAS-ADR-017), populated by write-time validator
    source_valid_from_version TEXT,   -- source-chain anchor (CAS-ADR-017)
    target_valid_from_version TEXT,   -- target-chain anchor (CAS-ADR-017)
    valid_until_version TEXT,         -- tombstone from merged_from termination (CAS-ADR-017)
    retracted_edge_id TEXT,           -- edge instance being retracted (CAS-ADR-017)
    created_at TEXT NOT NULL,
    notes TEXT,
    rationale TEXT,
    FOREIGN KEY (source_id) REFERENCES documents(id),
    FOREIGN KEY (target_id) REFERENCES documents(id)
);
"""

USERS_TABLE = """\
CREATE TABLE IF NOT EXISTS users (
    id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    type TEXT NOT NULL CHECK(type IN ('human', 'agent')),
    created_at TEXT NOT NULL
);
"""

STAGING_EDGES_TABLE = """\
CREATE TABLE IF NOT EXISTS staging_edges (
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

INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_documents_source_path ON documents(source_path);",
    "CREATE INDEX IF NOT EXISTS idx_documents_source_hash ON documents(source_content_hash);",
    "CREATE INDEX IF NOT EXISTS idx_documents_lifecycle ON documents(lifecycle_status);",
    "CREATE INDEX IF NOT EXISTS idx_documents_pipeline ON documents(pipeline_status);",
    "CREATE INDEX IF NOT EXISTS idx_documents_metadata_confirmed ON documents(metadata_confirmed);",
    "CREATE INDEX IF NOT EXISTS idx_edges_source ON edges(source_id);",
    "CREATE INDEX IF NOT EXISTS idx_edges_target ON edges(target_id);",
    "CREATE INDEX IF NOT EXISTS idx_edges_type ON edges(edge_type);",
    "CREATE INDEX IF NOT EXISTS idx_staging_edges_source ON staging_edges(source_id);",
    "CREATE INDEX IF NOT EXISTS idx_staging_edges_target ON staging_edges(target_id);",
]

MIGRATIONS = [
    # v1 -> v2: source file provenance (BH-049)
    "ALTER TABLE documents ADD COLUMN source_modified_at TEXT;",
    # v2 -> v3: metadata confirmation tracking (BE-014)
    "ALTER TABLE documents ADD COLUMN metadata_confirmed INTEGER NOT NULL DEFAULT 0;",
    # v3 -> v4: document date metadata (BH-062)
    "ALTER TABLE documents ADD COLUMN document_date TEXT;",
    # Chain-scoped edge resolution anchors and retracts target (CAS-ADR-017).
    # All nullable; FKs enforced at the application layer since SQLite
    # ALTER TABLE cannot add FK constraints.
    "ALTER TABLE edges ADD COLUMN resolution_policy TEXT;",
    "ALTER TABLE edges ADD COLUMN source_valid_from_version TEXT;",
    "ALTER TABLE edges ADD COLUMN target_valid_from_version TEXT;",
    "ALTER TABLE edges ADD COLUMN valid_until_version TEXT;",
    "ALTER TABLE edges ADD COLUMN retracted_edge_id TEXT;",
]

TABLES = [DOCUMENTS_TABLE, EDGES_TABLE, USERS_TABLE, STAGING_EDGES_TABLE]

# Indexes and other DDL that depend on columns added by MIGRATIONS.
# Must run AFTER migrations so that indexes on new columns (e.g.,
# metadata_confirmed) succeed against existing databases where
# CREATE TABLE IF NOT EXISTS was a no-op.
POST_MIGRATION_DDL = INDEXES

# Legacy alias used by tests and graph_store prior to ordering fix.
ALL_DDL = [*TABLES, *INDEXES]
