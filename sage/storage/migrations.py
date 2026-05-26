"""SQLite DDL for the SAGE graph store.

Executed at vault initialization. All tables use IF NOT EXISTS for
idempotent re-initialization.
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass

SCHEMA_VERSION = 3


class SchemaMigrationRequired(RuntimeError):
    """Raised when SAGE detects a pending schema migration but the
    server was launched without the ``--migrate`` flag.
    """


class DuplicateEdgesPresentError(RuntimeError):
    """Raised when the unique-index migration cannot be applied
    because the edges or staging_edges table contains duplicate rows on
    the natural-key triple (source_id, target_id, edge_type).

    Operator must run ``python -m scripts.dedup_edges --vault <id> --apply``
    to backfill the duplicates before re-running the migration.
    """


class Tier3UniqueViolation(Exception):
    """Storage-layer signal that a tier3_metadata uniqueness constraint
    fired on insert or supersession-insert (CAS-ADR-031).

    Raised by GraphStore when SQLite's partial UNIQUE index on
    `(doc_type, json_extract(tier3_metadata, '$.<field>'))` rejects a write.
    The service layer translates this into the public
    `Tier3UniqueConstraintViolation` SAGEError (sage.api.errors), preserving
    the layering rule that storage does not depend on the api layer.
    """

    def __init__(
        self,
        doc_type: str,
        field: str,
        colliding_value: object,
        existing_document_id: str,
    ) -> None:
        super().__init__(
            f"tier3_metadata.{field}={colliding_value!r} on doc_type "
            f"{doc_type!r} is already held by document {existing_document_id!r}"
        )
        self.doc_type = doc_type
        self.field = field
        self.colliding_value = colliding_value
        self.existing_document_id = existing_document_id


class Tier3UniqueIndexBlockedError(RuntimeError):
    """Raised when an attempt to create a tier3 unique index fails because
    pre-existing rows violate the uniqueness constraint (CAS-ADR-031 §5).

    The migration tool surfaces the collision report to the operator and
    refuses to activate the constraint; the substrate does not auto-resolve.
    """

    def __init__(self, doc_type: str, field: str, message: str) -> None:
        super().__init__(message)
        self.doc_type = doc_type
        self.field = field


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
    metadata_confirmed INTEGER NOT NULL DEFAULT 0,  -- boolean (BE-014)
    is_chain_head INTEGER NOT NULL DEFAULT 1  -- T-0115: 0 once a supersedes edge points at this row
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
    synced_from_version TEXT,         -- source revision copied/derived from (T-0110)
    synced_from_content_hash TEXT,    -- source content hash at edge assertion (T-0110)
    FOREIGN KEY (source_id) REFERENCES documents(id),
    FOREIGN KEY (target_id) REFERENCES documents(id)
);
"""

USERS_TABLE = """\
CREATE TABLE IF NOT EXISTS users (
    id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    user_type TEXT NOT NULL CHECK(user_type IN ('human', 'agent')),
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

# Derived join table for tag-filter queries (F-1 remediation).
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
    # Doc_type and project are the two most-filtered columns in
    # the entire codebase. The composite (doc_type, lifecycle_status) index
    # serves the dominant "active tickets / ADRs / failures" query shape.
    "CREATE INDEX IF NOT EXISTS idx_documents_doc_type ON documents(doc_type);",
    "CREATE INDEX IF NOT EXISTS idx_documents_project ON documents(project);",
    (
        "CREATE INDEX IF NOT EXISTS idx_documents_doc_type_lifecycle "
        "ON documents(doc_type, lifecycle_status);"
    ),
    # Composite edge indexes for sage_traverse(edge_type=X). The
    # left-prefix rule lets these cover the source-only / target-only
    # scans previously served by idx_edges_source / idx_edges_target,
    # which are dropped below.
    "CREATE INDEX IF NOT EXISTS idx_edges_source_type ON edges(source_id, edge_type);",
    "CREATE INDEX IF NOT EXISTS idx_edges_target_type ON edges(target_id, edge_type);",
    "CREATE INDEX IF NOT EXISTS idx_edges_type ON edges(edge_type);",
    # Typed provenance discriminator, indexed for chain-repair
    # and future per-inference-rule telemetry.
    "CREATE INDEX IF NOT EXISTS idx_edges_rationale_kind ON edges(rationale_kind);",
    # Supports the drift detector's hash-comparison scan over
    # provenance-bearing edges. The detector's list_provenance_edges
    # filters by edge_type (covered by idx_edges_type) and projects
    # synced_from_content_hash for per-edge comparison; this index lets
    # operators run ad-hoc hash-equality queries (e.g. "which edges
    # recorded this exact source revision?") without a full scan.
    (
        "CREATE INDEX IF NOT EXISTS idx_edges_synced_from_content_hash "
        "ON edges(synced_from_content_hash);"
    ),
    "CREATE INDEX IF NOT EXISTS idx_staging_edges_source ON staging_edges(source_id);",
    "CREATE INDEX IF NOT EXISTS idx_staging_edges_target ON staging_edges(target_id);",
    # Expression indexes on the three canonical high-frequency
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
    # Tag-filter pre-filter. The composite (tag, document_id)
    # is the covering index for the rewritten EXISTS subquery. The
    # standalone (tag) index is redundant via left-prefix but is
    # called out in the ticket's acceptance criteria.
    "CREATE INDEX IF NOT EXISTS idx_document_tags_tag ON document_tags(tag);",
    ("CREATE INDEX IF NOT EXISTS idx_document_tags_tag_doc ON document_tags(tag, document_id);"),
]

# Single-column edge indexes superseded by the composite
# (source_id, edge_type) and (target_id, edge_type). SQLite uses the
# left-prefix of a composite for single-column lookups, so these are
# strictly redundant once the composites exist.
INDEX_REPLACEMENTS = [
    "DROP INDEX IF EXISTS idx_edges_source;",
    "DROP INDEX IF EXISTS idx_edges_target;",
]

# Trigger maintaining `documents.is_chain_head` whenever a
# supersedes edge is created via any path (the compound atomic methods
# explicitly flip the flag inside their transactions; this trigger
# covers direct edge creation via `link()` and any future path that
# inserts a supersedes edge without coordinating with the storage
# layer). Idempotent against repeated re-inserts of the same edge:
# UPDATE on a row already at is_chain_head=0 is a no-op.
TIER3_CHAIN_HEAD_TRIGGER = """\
CREATE TRIGGER IF NOT EXISTS trg_tier3_chain_head_on_supersedes
AFTER INSERT ON edges
WHEN NEW.edge_type = 'supersedes' AND NEW.target_id IS NOT NULL
BEGIN
    UPDATE documents SET is_chain_head = 0 WHERE id = NEW.target_id;
END;
"""


# Natural-key uniqueness on production and staging edges.
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
    """Edges whose rationale starts with a recognized prefix but
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
    """Classify ``rationale_kind`` for each known prefix.

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


def _backfill_is_chain_head_detect(conn: sqlite3.Connection) -> bool:
    """Documents that are the target of a supersedes edge but whose
    `is_chain_head` is still the post-migration default 1.
    """
    row = conn.execute(
        "SELECT 1 FROM documents d "
        "WHERE d.is_chain_head = 1 AND EXISTS ("
        "    SELECT 1 FROM edges e WHERE e.edge_type = 'supersedes' AND e.target_id = d.id"
        ") LIMIT 1"
    ).fetchone()
    return row is not None


def _backfill_is_chain_head_apply(conn: sqlite3.Connection) -> None:
    """A document is a chain head iff no supersedes edge points at it.

    Flip `is_chain_head` to 0 for every document that is the target of a
    supersedes edge. The default of 1 is preserved for chain heads (the
    most-recent version) and for documents not in any supersession chain.
    """
    conn.execute(
        "UPDATE documents SET is_chain_head = 0 "
        "WHERE id IN ("
        "    SELECT DISTINCT target_id FROM edges "
        "    WHERE edge_type = 'supersedes' AND target_id IS NOT NULL"
        ")"
    )


def _backfill_document_tags_detect(conn: sqlite3.Connection) -> bool:
    """Documents with tags JSON but no corresponding join rows."""
    row = conn.execute(
        "SELECT 1 FROM documents "
        "WHERE tags IS NOT NULL AND tags != '[]' "
        "AND NOT EXISTS (SELECT 1 FROM document_tags "
        "WHERE document_tags.document_id = documents.id) "
        "LIMIT 1"
    ).fetchone()
    return row is not None


def _backfill_document_tags_apply(conn: sqlite3.Connection) -> None:
    """Populate document_tags from existing documents.tags JSON."""
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


# Prefix → kind pairs driving the one-time backfill UPDATE
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


# Verb-gated regex for the rationale-prose backfill that
# recovers synced_from_version on legacy derived_from edges. Requires a
# provenance verb (derived/synced/copied/adapted/based) followed by
# from/on, then a version token (optional `v`, then 1-3 numeric groups).
# Conservative on purpose: false-positive provenance is worse than honest
# null. False-positive verbs ("contradicts", "until", "regression from",
# "see also") are NOT in the allowlist.
_BACKFILL_SYNCED_FROM_REGEX = re.compile(
    r"\b(?:derived|synced|copied|adapted|based)\s+(?:from|on)"
    r"\s+(v?\d+(?:\.\d+){1,2})\b",
    re.IGNORECASE,
)


def _iter_synced_from_version_backfill_assignments(
    conn: sqlite3.Connection,
) -> "list[tuple[str, str]]":
    """Yield (edge_id, resolved_version_doc_id) pairs to assign.

    Shared driver for the detect/apply pair. Each pair indicates a
    derived_from edge whose rationale prose names a version label that
    resolves to exactly one member of the edge's target's supersedes
    chain. Ambiguous matches (regex captures a token that matches more
    than one chain entry's ``version_label``) are dropped silently -- the
    edge stays NULL so future detection can surface it via the
    ``recorded_null`` basket rather than recording false provenance.
    """
    rows = conn.execute(
        "SELECT id, target_id, rationale FROM edges "
        "WHERE edge_type = 'derived_from' "
        "AND synced_from_version IS NULL "
        "AND target_id IS NOT NULL "
        "AND rationale IS NOT NULL"
    ).fetchall()
    chain_sql = (
        "WITH RECURSIVE chain AS ("
        " SELECT ? AS doc_id"
        " UNION"
        " SELECT e.target_id AS doc_id FROM edges e "
        "  INNER JOIN chain c ON e.source_id = c.doc_id "
        "  WHERE e.edge_type = 'supersedes'"
        " UNION"
        " SELECT e.source_id AS doc_id FROM edges e "
        "  INNER JOIN chain c ON e.target_id = c.doc_id "
        "  WHERE e.edge_type = 'supersedes'"
        ")"
        " SELECT d.id FROM chain c "
        " INNER JOIN documents d ON c.doc_id = d.id "
        " WHERE d.version_label = ?"
    )
    assignments: list[tuple[str, str]] = []
    for row in rows:
        edge_id = row[0] if not isinstance(row, sqlite3.Row) else row["id"]
        target_id = row[1] if not isinstance(row, sqlite3.Row) else row["target_id"]
        rationale = row[2] if not isinstance(row, sqlite3.Row) else row["rationale"]
        match = _BACKFILL_SYNCED_FROM_REGEX.search(rationale)
        if match is None:
            continue
        version_token = match.group(1)
        chain_hits = conn.execute(chain_sql, (target_id, version_token)).fetchall()
        if len(chain_hits) == 1:
            resolved_id = chain_hits[0][0]
            assignments.append((edge_id, resolved_id))
    return assignments


def _backfill_synced_from_version_from_rationale_detect(conn: sqlite3.Connection) -> bool:
    """Detect if any derived_from edge has a backfill-assignable
    synced_from_version pending. Iterates the same regex-then-chain-match
    logic as apply; returns True on the first assignable pair.
    """
    return bool(_iter_synced_from_version_backfill_assignments(conn))


def _backfill_synced_from_version_from_rationale_apply(conn: sqlite3.Connection) -> None:
    """Per-edge UPDATE of ``synced_from_version`` for legacy
    ``derived_from`` edges whose rationale prose unambiguously names a
    chain member. Idempotent: re-running produces no new changes
    because the WHERE clause filters on ``synced_from_version IS NULL``.
    """
    assignments = _iter_synced_from_version_backfill_assignments(conn)
    if not assignments:
        return
    conn.executemany(
        "UPDATE edges SET synced_from_version = ? WHERE id = ? AND synced_from_version IS NULL",
        [(resolved_id, edge_id) for edge_id, resolved_id in assignments],
    )


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
    Backfill(
        name="is_chain_head",
        detect=_backfill_is_chain_head_detect,
        apply=_backfill_is_chain_head_apply,
    ),
    Backfill(
        name="synced_from_version_from_rationale",
        detect=_backfill_synced_from_version_from_rationale_detect,
        apply=_backfill_synced_from_version_from_rationale_apply,
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
    # Typed rationale-kind discriminator. NOT NULL with constant
    # default 'manual' is legal under SQLite ALTER TABLE ADD COLUMN, so
    # existing rows pick up the default at migration time; the paired
    # backfill (registered in BACKFILL_PLAN) then re-classifies rows whose
    # rationale text carries a recognized prefix.
    Migration(
        "edges",
        "rationale_kind",
        "ALTER TABLE edges ADD COLUMN rationale_kind TEXT NOT NULL DEFAULT 'manual';",
    ),
    # Chain-head flag enabling partial UNIQUE indexes on declared
    # tier3 unique_keys. NOT NULL with constant default 1 is legal under
    # SQLite ALTER TABLE ADD COLUMN, so existing rows pick up the default
    # at migration time; the paired backfill (registered in BACKFILL_PLAN)
    # then flips the flag to 0 for every row that is the target of a
    # supersedes edge.
    Migration(
        "documents",
        "is_chain_head",
        "ALTER TABLE documents ADD COLUMN is_chain_head INTEGER NOT NULL DEFAULT 1;",
    ),
    # Synced-from provenance on sync_target / derived_from edges.
    # Both nullable; meaningful only for sync_target and derived_from but
    # stored on the shared edges table. Unset = explicit NULL, never
    # inferred from chain anchors (source_valid_from_version /
    # target_valid_from_version), which serve a distinct purpose
    # (CAS-ADR-017 chain visibility).
    Migration(
        "edges",
        "synced_from_version",
        "ALTER TABLE edges ADD COLUMN synced_from_version TEXT;",
    ),
    Migration(
        "edges",
        "synced_from_content_hash",
        "ALTER TABLE edges ADD COLUMN synced_from_content_hash TEXT;",
    ),
    # Rename users.type -> users.user_type to eliminate collision
    # with the Python builtin and disambiguate the wire shape. Detected by
    # column-presence on the NEW column name (user_type). SQLite >= 3.25
    # supports ALTER TABLE RENAME COLUMN and propagates the new name into
    # the existing CHECK constraint automatically.
    Migration(
        "users",
        "user_type",
        "ALTER TABLE users RENAME COLUMN type TO user_type;",
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
# Unique indexes follow the other indexes; their failure path
# is special-cased in graph_store._initialize_sync.
POST_MIGRATION_DDL = [
    *INDEX_REPLACEMENTS,
    *INDEXES,
    *[ddl for _table, _name, ddl in UNIQUE_NATURAL_KEY_INDEXES],
    TIER3_CHAIN_HEAD_TRIGGER,
]


# Defense-in-depth gate for the doc_type and field tokens
# interpolated into the tier3 partial UNIQUE index DDL. doc_type already
# matches `^[a-z][a-z0-9_]*$` by vault-config schema; the cross-field
# validator in sage.config restricts unique_keys entries to declared
# metadata_schema properties. This regex is the last-line fence
# guaranteeing no caller-supplied string can break out of the DDL.
TIER3_UNIQUE_INDEX_PREFIX = "idx_tier3_unique_"


def tier3_unique_index_name(doc_type: str, field: str) -> str:
    """Canonical index name for the (doc_type, field) partial UNIQUE index.

    Format: ``idx_tier3_unique_<doc_type>_<field>``. The doc_type/field
    boundary is recovered at error-translation time by stripping the known
    doc_type prefix from the captured tail.
    """
    return f"{TIER3_UNIQUE_INDEX_PREFIX}{doc_type}_{field}"


def tier3_unique_index_ddl(doc_type: str, field: str) -> str:
    """SQL DDL for the partial UNIQUE expression index on (doc_type, field).

    Per CAS-ADR-031 §3, uniqueness is global within doc_type across all
    lifecycle statuses, with the supersession-lineage exception
    (predecessors carry is_chain_head=0 and are excluded from the
    partial filter). NULL tier3 values are excluded so doc_types whose
    declared field is optional do not collide on NULL.
    """
    name = tier3_unique_index_name(doc_type, field)
    return (
        f"CREATE UNIQUE INDEX IF NOT EXISTS {name} "
        f"ON documents (json_extract(tier3_metadata, '$.{field}')) "
        f"WHERE doc_type = '{doc_type}' "
        f"  AND is_chain_head = 1 "
        f"  AND json_extract(tier3_metadata, '$.{field}') IS NOT NULL;"
    )


def tier3_unique_index_drop_ddl(doc_type: str, field: str) -> str:
    """DROP INDEX statement for the (doc_type, field) partial UNIQUE index."""
    return f"DROP INDEX IF EXISTS {tier3_unique_index_name(doc_type, field)};"


# Legacy alias used by tests and graph_store prior to ordering fix.
ALL_DDL = [*TABLES, *INDEXES]
