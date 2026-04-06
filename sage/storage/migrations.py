"""SQLite DDL for the SAGE graph store.

Executed at vault initialization. All tables use IF NOT EXISTS for
idempotent re-initialization.
"""

SCHEMA_VERSION = 1

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
    semantic_abstract TEXT,           -- nullable
    pipeline_status TEXT NOT NULL DEFAULT 'projection_complete',
    pipeline_error TEXT,              -- nullable (BH-022, BH-024)
    tier3_metadata TEXT               -- JSON, nullable
);
"""

EDGES_TABLE = """\
CREATE TABLE IF NOT EXISTS edges (
    id TEXT PRIMARY KEY,              -- UUID (BH-032)
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

USERS_TABLE = """\
CREATE TABLE IF NOT EXISTS users (
    id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    type TEXT NOT NULL CHECK(type IN ('human', 'agent')),
    created_at TEXT NOT NULL
);
"""

INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_documents_source_path ON documents(source_path);",
    "CREATE INDEX IF NOT EXISTS idx_documents_source_hash ON documents(source_content_hash);",
    "CREATE INDEX IF NOT EXISTS idx_documents_lifecycle ON documents(lifecycle_status);",
    "CREATE INDEX IF NOT EXISTS idx_documents_pipeline ON documents(pipeline_status);",
    "CREATE INDEX IF NOT EXISTS idx_edges_source ON edges(source_id);",
    "CREATE INDEX IF NOT EXISTS idx_edges_target ON edges(target_id);",
    "CREATE INDEX IF NOT EXISTS idx_edges_type ON edges(edge_type);",
]

MIGRATIONS = [
    # v1 -> v2: source file provenance (BH-049)
    "ALTER TABLE documents ADD COLUMN source_modified_at TEXT;",
]

ALL_DDL = [DOCUMENTS_TABLE, EDGES_TABLE, USERS_TABLE, *INDEXES]
