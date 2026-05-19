"""SQLite DDL for the SAGE graph store.

Executed at vault initialization. All tables use IF NOT EXISTS for
idempotent re-initialization.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass

SCHEMA_VERSION = 3


class SchemaMigrationRequired(RuntimeError):
    """Raised when SAGE detects a pending schema migration but the
    server was launched without the ``--migrate`` flag.
    """


class DuplicateEdgesPresentError(RuntimeError):
    """Raised when the T-0079 unique-index migration cannot be applied
    because the edges or staging_edges table contains duplicate rows on
    the natural-key triple (source_id, target_id, edge_type).

    Operator must run ``python -m scripts.dedup_edges --vault <id> --apply``
    to backfill the duplicates before re-running the migration.
    """


@dataclass(frozen=True)
class Migration:
    """A single ALTER-style schema migration.

    ``table`` and ``column`` are used by ``pending_migrations`` to detect
    whether the migration has already been applied (column-presence check
    via ``PRAGMA table_info``). ``ddl`` is the statement to execute when
    ``--migrate`` is set.
    """

    table: str
    column: str
    ddl: str


def pending_migrations(
    conn: sqlite3.Connection,
    migrations: list["Migration"] | None = None,
) -> list["Migration"]:
    """Return migrations whose column is not yet present on its table.

    Read-only: issues only ``PRAGMA table_info`` queries. Does not apply
    any DDL. If a referenced table does not yet exist, the migration is
    treated as not-pending (the table will be created from ``TABLES``
    with the column already in its CREATE definition).
    """
    if migrations is None:
        migrations = MIGRATION_PLAN
    pending: list[Migration] = []
    table_columns: dict[str, set[str]] = {}
    for m in migrations:
        if m.table not in table_columns:
            rows = conn.execute(f"PRAGMA table_info({m.table})").fetchall()
            if not rows:
                # Table absent: TABLES will create it with the column already present.
                table_columns[m.table] = {m.column}
            else:
                table_columns[m.table] = {row[1] for row in rows}
        if m.column not in table_columns[m.table]:
            pending.append(m)
    return pending


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
    resolution_policy TEXT,           -- frozen policy, set at write time (CAS-ADR-017)
    source_valid_from_version TEXT,   -- source-chain anchor (CAS-ADR-017)
    target_valid_from_version TEXT,   -- target-chain anchor (CAS-ADR-017)
    valid_until_version TEXT,         -- tombstone from merged_from termination (CAS-ADR-017)
    retracted_edge_id TEXT,           -- edge instance being retracted (CAS-ADR-017)
    created_at TEXT NOT NULL,
    notes TEXT,
    rationale TEXT,
    rationale_kind TEXT NOT NULL DEFAULT 'manual',  -- typed provenance (T-0080)
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

# T-0078: derived join table for tag-filter queries (F-1 remediation).
# Kept in sync from the SAGE layer; documents.tags JSON remains the
# authoritative serialization.
DOCUMENT_TAGS_TABLE = """\
CREATE TABLE IF NOT EXISTS document_tags (
    document_id TEXT NOT NULL,
    tag TEXT NOT NULL,
    PRIMARY KEY (document_id, tag),
    FOREIGN KEY (document_id) REFERENCES documents(id) ON DELETE CASCADE
);
"""

INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_documents_source_path ON documents(source_path);",
    "CREATE INDEX IF NOT EXISTS idx_documents_source_hash ON documents(source_content_hash);",
    "CREATE INDEX IF NOT EXISTS idx_documents_lifecycle ON documents(lifecycle_status);",
    "CREATE INDEX IF NOT EXISTS idx_documents_pipeline ON documents(pipeline_status);",
    "CREATE INDEX IF NOT EXISTS idx_documents_metadata_confirmed ON documents(metadata_confirmed);",
    # T-0074: doc_type and project are the two most-filtered columns in
    # the entire codebase. The composite (doc_type, lifecycle_status) index
    # serves the dominant "active tickets / ADRs / failures" query shape.
    "CREATE INDEX IF NOT EXISTS idx_documents_doc_type ON documents(doc_type);",
    "CREATE INDEX IF NOT EXISTS idx_documents_project ON documents(project);",
    (
        "CREATE INDEX IF NOT EXISTS idx_documents_doc_type_lifecycle "
        "ON documents(doc_type, lifecycle_status);"
    ),
    # T-0074: composite edge indexes for sage_traverse(edge_type=X). The
    # left-prefix rule lets these cover the source-only / target-only
    # scans previously served by idx_edges_source / idx_edges_target,
    # which are dropped below.
    "CREATE INDEX IF NOT EXISTS idx_edges_source_type ON edges(source_id, edge_type);",
    "CREATE INDEX IF NOT EXISTS idx_edges_target_type ON edges(target_id, edge_type);",
    "CREATE INDEX IF NOT EXISTS idx_edges_type ON edges(edge_type);",
    # T-0080: typed provenance discriminator, indexed for chain-repair
    # and future per-inference-rule telemetry.
    "CREATE INDEX IF NOT EXISTS idx_edges_rationale_kind ON edges(rationale_kind);",
    "CREATE INDEX IF NOT EXISTS idx_staging_edges_source ON staging_edges(source_id);",
    "CREATE INDEX IF NOT EXISTS idx_staging_edges_target ON staging_edges(target_id);",
    # T-0075: expression indexes on the three canonical high-frequency
    # tier3_metadata fields. The SQL builder in graph_store emits
    # json_extract(tier3_metadata, '$.<field>') = ? predicates with the
    # path string interpolated character-for-character so the planner
    # can match these expression indexes (parameterized paths cannot
    # hit an expression index).
    (
        "CREATE INDEX IF NOT EXISTS idx_tier3_ticket_id "
        "ON documents(json_extract(tier3_metadata, '$.ticket_id'));"
    ),
    (
        "CREATE INDEX IF NOT EXISTS idx_tier3_failure_id "
        "ON documents(json_extract(tier3_metadata, '$.failure_id'));"
    ),
    (
        "CREATE INDEX IF NOT EXISTS idx_tier3_tool_name "
        "ON documents(json_extract(tier3_metadata, '$.tool_name'));"
    ),
    # T-0078: tag-filter pre-filter. The composite (tag, document_id)
    # is the covering index for the rewritten EXISTS subquery. The
    # standalone (tag) index is redundant via left-prefix but is
    # called out in the ticket's acceptance criteria.
    "CREATE INDEX IF NOT EXISTS idx_document_tags_tag ON document_tags(tag);",
    ("CREATE INDEX IF NOT EXISTS idx_document_tags_tag_doc ON document_tags(tag, document_id);"),
]

# T-0074: single-column edge indexes superseded by the composite
# (source_id, edge_type) and (target_id, edge_type). SQLite uses the
# left-prefix of a composite for single-column lookups, so these are
# strictly redundant once the composites exist.
INDEX_REPLACEMENTS = [
    "DROP INDEX IF EXISTS idx_edges_source;",
    "DROP INDEX IF EXISTS idx_edges_target;",
]

# T-0079: natural-key uniqueness on production and staging edges.
# SQLite cannot ALTER TABLE ADD UNIQUE; a unique index is the
# semantic equivalent. ``IF NOT EXISTS`` keeps re-init idempotent;
# CREATE UNIQUE INDEX still fails (with sqlite3.IntegrityError) when
# the table already contains duplicate rows, and ``_initialize_sync``
# translates that into DuplicateEdgesPresentError pointing at
# scripts/dedup_edges.py. NULL semantics: SQLite treats NULLs as
# distinct in unique indexes, so multiple ``retracts`` edges with
# target_id=NULL on the same source remain legal (per CAS-ADR-017,
# retraction targets an edge instance via retracted_edge_id, not
# via the natural-key tuple).
UNIQUE_NATURAL_KEY_INDEXES = [
    (
        "edges",
        "idx_edges_uniq_natural_key",
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_edges_uniq_natural_key "
        "ON edges(source_id, target_id, edge_type);",
    ),
    (
        "staging_edges",
        "idx_staging_edges_uniq_natural_key",
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_staging_edges_uniq_natural_key "
        "ON staging_edges(source_id, target_id, edge_type);",
    ),
]


@dataclass(frozen=True)
class Backfill:
    """A one-shot data backfill that populates derived state.

    ``name`` is a short identifier for log/error messages. ``detect``
    returns True when the backfill has work to do; ``apply`` performs
    it. Both run inside the live migration transaction, so they must
    not commit on their own.
    """

    name: str
    detect: Callable[[sqlite3.Connection], bool]
    apply: Callable[[sqlite3.Connection], None]


def _backfill_rationale_kind_detect(conn: sqlite3.Connection) -> bool:
    """T-0080: edges whose rationale starts with a recognized prefix but
    whose ``rationale_kind`` is still the post-migration default
    ``'manual'``.
    """
    # _BACKFILL_RATIONALE_KIND_PAIRS is defined below; the detector
    # short-circuits on the first matching row to stay cheap.
    for _kind, prefix in _BACKFILL_RATIONALE_KIND_PAIRS:
        row = conn.execute(
            "SELECT 1 FROM edges WHERE rationale_kind = 'manual' AND rationale LIKE ? LIMIT 1",
            (prefix + "%",),
        ).fetchone()
        if row is not None:
            return True
    return False


def _backfill_rationale_kind_apply(conn: sqlite3.Connection) -> None:
    """T-0080: classify ``rationale_kind`` for each known prefix.

    Idempotent under repeated invocation against the same data. The
    ``AND rationale_kind = 'manual'`` guard prevents the backfill from
    clobbering writer-supplied kinds set after the initial migration.
    """
    for kind, prefix in _BACKFILL_RATIONALE_KIND_PAIRS:
        conn.execute(
            "UPDATE edges SET rationale_kind = ? "
            "WHERE rationale_kind = 'manual' AND rationale LIKE ?",
            (kind, prefix + "%"),
        )


def _backfill_document_tags_detect(conn: sqlite3.Connection) -> bool:
    """T-0078: documents with tags JSON but no corresponding join rows."""
    row = conn.execute(
        "SELECT 1 FROM documents "
        "WHERE tags IS NOT NULL AND tags != '[]' "
        "AND NOT EXISTS (SELECT 1 FROM document_tags "
        "WHERE document_tags.document_id = documents.id) "
        "LIMIT 1"
    ).fetchone()
    return row is not None


def _backfill_document_tags_apply(conn: sqlite3.Connection) -> None:
    """T-0078: populate document_tags from existing documents.tags JSON."""
    cur = conn.execute("SELECT id, tags FROM documents WHERE tags IS NOT NULL AND tags != '[]'")
    rows: list[tuple[str, str]] = []
    for doc_id, tags_json in cur.fetchall():
        try:
            tags = json.loads(tags_json)
        except (TypeError, ValueError):
            continue
        if not isinstance(tags, list):
            continue
        for tag in tags:
            if isinstance(tag, str):
                rows.append((doc_id, tag))
    if rows:
        conn.executemany(
            "INSERT OR IGNORE INTO document_tags (document_id, tag) VALUES (?, ?)",
            rows,
        )


# T-0080: prefix → kind pairs driving the one-time backfill UPDATE
# statements. The list is derived from
# ``sage.storage.edge_provenance.RATIONALE_PREFIX_TO_KIND`` so the helper
# and the backfill SQL cannot drift. The detector in
# ``_backfill_rationale_kind_detect`` and the applier in
# ``_backfill_rationale_kind_apply`` consume this list directly.
_BACKFILL_RATIONALE_KIND_PAIRS: list[tuple[str, str]] = []


def _populate_backfill_rationale_kind_pairs() -> None:
    # Late import to avoid a top-level cycle: edge_provenance imports
    # RationaleKind from sage.models.enums, and the storage layer
    # consumes it here without taking on the dependency at module load.
    from sage.storage.edge_provenance import RATIONALE_PREFIX_TO_KIND

    _BACKFILL_RATIONALE_KIND_PAIRS.clear()
    for prefix, kind in RATIONALE_PREFIX_TO_KIND.items():
        _BACKFILL_RATIONALE_KIND_PAIRS.append((kind.value, prefix))


_populate_backfill_rationale_kind_pairs()


BACKFILL_PLAN: list[Backfill] = [
    Backfill(
        name="document_tags",
        detect=_backfill_document_tags_detect,
        apply=_backfill_document_tags_apply,
    ),
    Backfill(
        name="rationale_kind",
        detect=_backfill_rationale_kind_detect,
        apply=_backfill_rationale_kind_apply,
    ),
]


def pending_backfills(
    conn: sqlite3.Connection,
    backfills: list["Backfill"] | None = None,
) -> list["Backfill"]:
    """Return backfills whose ``detect`` returns True for this database."""
    if backfills is None:
        backfills = BACKFILL_PLAN
    pending: list[Backfill] = []
    for b in backfills:
        try:
            if b.detect(conn):
                pending.append(b)
        except sqlite3.OperationalError:
            # Detection requires a table that may not yet exist; treat
            # as not-pending. The DDL pass will create the table and the
            # next initialize call will detect any actual work.
            continue
    return pending


MIGRATION_PLAN: list[Migration] = [
    # v1 -> v2: source file provenance (BH-049)
    Migration(
        "documents",
        "source_modified_at",
        "ALTER TABLE documents ADD COLUMN source_modified_at TEXT;",
    ),
    # v2 -> v3: metadata confirmation tracking (BE-014)
    Migration(
        "documents",
        "metadata_confirmed",
        "ALTER TABLE documents ADD COLUMN metadata_confirmed INTEGER NOT NULL DEFAULT 0;",
    ),
    # v3 -> v4: document date metadata (BH-062)
    Migration("documents", "document_date", "ALTER TABLE documents ADD COLUMN document_date TEXT;"),
    # Chain-scoped edge resolution anchors and retracts target (CAS-ADR-017).
    # All nullable; FKs enforced at the application layer since SQLite
    # ALTER TABLE cannot add FK constraints.
    Migration("edges", "resolution_policy", "ALTER TABLE edges ADD COLUMN resolution_policy TEXT;"),
    Migration(
        "edges",
        "source_valid_from_version",
        "ALTER TABLE edges ADD COLUMN source_valid_from_version TEXT;",
    ),
    Migration(
        "edges",
        "target_valid_from_version",
        "ALTER TABLE edges ADD COLUMN target_valid_from_version TEXT;",
    ),
    Migration(
        "edges", "valid_until_version", "ALTER TABLE edges ADD COLUMN valid_until_version TEXT;"
    ),
    Migration("edges", "retracted_edge_id", "ALTER TABLE edges ADD COLUMN retracted_edge_id TEXT;"),
    # T-0080: typed rationale-kind discriminator. NOT NULL with constant
    # default 'manual' is legal under SQLite ALTER TABLE ADD COLUMN, so
    # existing rows pick up the default at migration time; the paired
    # backfill (registered in BACKFILL_PLAN) then re-classifies rows whose
    # rationale text carries a recognized prefix.
    Migration(
        "edges",
        "rationale_kind",
        "ALTER TABLE edges ADD COLUMN rationale_kind TEXT NOT NULL DEFAULT 'manual';",
    ),
]

# Backwards-compatible string-of-DDL view for callers that just want the
# raw ALTER statements (e.g., tests that build a legacy-shaped DB and
# the standalone scripts/migrate_edge_anchors.py harness).
MIGRATIONS: list[str] = [m.ddl for m in MIGRATION_PLAN]

TABLES = [
    DOCUMENTS_TABLE,
    EDGES_TABLE,
    USERS_TABLE,
    STAGING_EDGES_TABLE,
    DOCUMENT_TAGS_TABLE,
]

# Indexes and other DDL that depend on columns added by MIGRATIONS.
# Must run AFTER migrations so that indexes on new columns (e.g.,
# metadata_confirmed) succeed against existing databases where
# CREATE TABLE IF NOT EXISTS was a no-op. Replacements run first so the
# obsolete single-column edge indexes are gone before the composites
# (which would otherwise coexist as a transient bloat window).
# T-0079: unique indexes follow the other indexes; their failure path
# is special-cased in graph_store._initialize_sync.
POST_MIGRATION_DDL = [
    *INDEX_REPLACEMENTS,
    *INDEXES,
    *[ddl for _table, _name, ddl in UNIQUE_NATURAL_KEY_INDEXES],
]

# Legacy alias used by tests and graph_store prior to ordering fix.
ALL_DDL = [*TABLES, *INDEXES]
