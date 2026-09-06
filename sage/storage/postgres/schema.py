"""Canonical Postgres schema for the SAGE storage engine (CAS-ADR-042).

The single source of DDL both deployment targets -- the on-box local runtime and
the managed hosted endpoint -- provision from. The graph tables mirror the
embedded SQLite store dialect-substituted for Postgres (jsonb for the JSON
columns, native booleans, server-enforced foreign keys, ``->>`` expression
indexes); the content ``chunks`` table carries the pgvector embedding column and
a generated ``tsvector`` full-text column with a GIN index, the keyword-arm shape
the relevance evaluation settled on.

This module is dependency-light: it holds the DDL as plain strings and operates
on a caller-supplied async connection, so it imports no database driver and stays
importable without one. Two behavioral concerns the embedded graph store layers
on top of the bare schema are deliberately out of scope here and belong to the
graph-store implementation that owns that behavior: the chain-head maintenance
trigger and the per-vault tier3 uniqueness indexes. The ``is_chain_head`` column
ships; the trigger that maintains it does not.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

# nomic-embed-text embedding width; the content store's vector column dimension.
# Coupled to the embedding model, exactly as the embedded content store's fixed
# vector field is.
EMBEDDING_DIM = 768

# Postgres text-search configuration backing the generated full-text column and
# the keyword search arm.
TEXT_SEARCH_CONFIG = "english"

# Extensions the bootstrap enables by default. ``vector`` (pgvector) is required
# for the content store's embedding column; ``pgstattuple`` backs bloat
# measurement. ``pg_repack`` is intentionally excluded -- it is enabled in the
# local runbook but is not in every target's managed allowlist.
DEFAULT_EXTENSIONS: tuple[str, ...] = ("vector", "pgstattuple")

# Disposable test schemas carry this prefix; the harness guard refuses to
# provision or drop anything that does not.
DISPOSABLE_SCHEMA_PREFIX = "sage_test_"

_IDENTIFIER_RE = re.compile(r"^[a-z_][a-z0-9_]*$")


# ---------------------------------------------------------------------------
# Identifier guards
#
# Schema and extension names are interpolated into DDL as identifiers (they
# cannot be bind parameters), so each is validated against a strict lowercase
# allowlist before it reaches the SQL -- the same discipline the embedded store
# applies to its tier3 field paths.
# ---------------------------------------------------------------------------


def _validate_identifier(name: str, kind: str) -> str:
    if not isinstance(name, str) or not _IDENTIFIER_RE.match(name):
        raise ValueError(f"{kind} {name!r} is not a safe lowercase identifier")
    return name


def validate_schema_name(name: str) -> str:
    """Return ``name`` if it is a safe schema identifier, else raise."""
    return _validate_identifier(name, "schema name")


def validate_extension(name: str) -> str:
    """Return ``name`` if it is a safe extension identifier, else raise."""
    return _validate_identifier(name, "extension")


def assert_disposable_target(schema: str) -> str:
    """Return ``schema`` if it is a disposable test schema, else raise.

    The storage test harness provisions and drops schemas; this guard makes it
    impossible to point it at the live working database. A target is disposable
    only when it is a validated identifier carrying the ``sage_test_`` prefix and
    is not the shared ``public`` schema.
    """
    validate_schema_name(schema)
    if schema == "public" or not schema.startswith(DISPOSABLE_SCHEMA_PREFIX):
        raise ValueError(
            f"refusing to treat schema {schema!r} as disposable: a disposable "
            f"target must carry the {DISPOSABLE_SCHEMA_PREFIX!r} prefix and must "
            "not be 'public'"
        )
    return schema


# ---------------------------------------------------------------------------
# Graph tables (dialect-substituted from the embedded SQLite store)
# ---------------------------------------------------------------------------

DOCUMENTS_TABLE = """\
CREATE TABLE IF NOT EXISTS documents (
    id text PRIMARY KEY,
    title text NOT NULL,
    source_type text NOT NULL,
    source_path text NOT NULL,
    lifecycle_status text NOT NULL DEFAULT 'active',
    version_label text,
    project text,
    tags jsonb,
    authority_scope text,
    doc_type text,
    source_content_hash text NOT NULL,
    stored_content_hash text,
    adapter_version text NOT NULL,
    created_by text NOT NULL,
    created_at text NOT NULL,
    last_modified_by text NOT NULL,
    updated_at text NOT NULL,
    projected_at text,
    indexed_at text,
    source_modified_at text,
    document_date text,
    semantic_abstract text,
    pipeline_status text NOT NULL DEFAULT 'projection_complete',
    pipeline_error text,
    tier3_metadata jsonb,
    metadata_confirmed boolean NOT NULL DEFAULT false,
    is_chain_head boolean NOT NULL DEFAULT true
);
"""

USERS_TABLE = """\
CREATE TABLE IF NOT EXISTS users (
    id text PRIMARY KEY,
    display_name text NOT NULL,
    user_type text NOT NULL CHECK (user_type IN ('human', 'agent')),
    created_at text NOT NULL
);
"""

EDGES_TABLE = """\
CREATE TABLE IF NOT EXISTS edges (
    id text PRIMARY KEY,
    source_id text NOT NULL,
    target_id text,
    edge_type text NOT NULL,
    resolution_policy text,
    source_valid_from_version text,
    target_valid_from_version text,
    valid_until_version text,
    retracted_edge_id text,
    created_at text NOT NULL,
    notes text,
    rationale text,
    rationale_kind text NOT NULL DEFAULT 'manual',
    synced_from_version text,
    synced_from_content_hash text,
    FOREIGN KEY (source_id) REFERENCES documents(id),
    FOREIGN KEY (target_id) REFERENCES documents(id)
);
"""

STAGING_EDGES_TABLE = """\
CREATE TABLE IF NOT EXISTS staging_edges (
    id text PRIMARY KEY,
    source_id text NOT NULL,
    target_id text NOT NULL,
    edge_type text NOT NULL,
    inference_evidence text NOT NULL,
    confidence_tier integer NOT NULL DEFAULT 2,
    created_at text NOT NULL,
    FOREIGN KEY (source_id) REFERENCES documents(id),
    FOREIGN KEY (target_id) REFERENCES documents(id)
);
"""

DOCUMENT_TAGS_TABLE = """\
CREATE TABLE IF NOT EXISTS document_tags (
    document_id text NOT NULL,
    tag text NOT NULL,
    PRIMARY KEY (document_id, tag),
    FOREIGN KEY (document_id) REFERENCES documents(id) ON DELETE CASCADE
);
"""

# ---------------------------------------------------------------------------
# Content table (mirrors the embedded content store's chunk schema, with the
# pgvector embedding column and a generated tsvector full-text column)
# ---------------------------------------------------------------------------

# CAS-ADR-049 Decision 3 separates a passage's *address* -- ``heading_path``,
# unchanged, what enumeration returns and a section read accepts -- from its
# *indexed structure*, that path relative to the document. Only the second
# reaches the top ranking weight, so a title that a source format made its
# top-level heading stops being indexed into every passage of its document.
#
# ``coalesce`` is a compatibility clause rather than a shorthand.
# ``indexed_structure`` reaches an existing vault through ADDITIVE_COLUMNS on
# its next open and is filled by the migration; between those two moments every
# row is NULL and this expression reproduces the pre-decision behaviour exactly.
# That window is not hypothetical -- it is every vault, from the first open
# after deploy until an operator runs the migration.
#
# The column is nullable with no default because an empty relative structure is
# a legitimate value: it is what the top-level heading's own passage carries.
# ``NULL`` therefore has to be what "not yet derived" means. ``DEFAULT ''``
# would make ``coalesce`` return the empty string for every unmigrated row and
# index nothing at all at weight A.
CHUNKS_INDEXED_STRUCTURE_EXPRESSION = "coalesce(indexed_structure, heading_path)"

CHUNKS_TSV_EXPRESSION = (
    f"setweight(to_tsvector('{TEXT_SEARCH_CONFIG}', "
    f"{CHUNKS_INDEXED_STRUCTURE_EXPRESSION}), 'A')"
    f" || setweight(to_tsvector('{TEXT_SEARCH_CONFIG}', content), 'D')"
)

CHUNKS_TABLE = f"""\
CREATE TABLE IF NOT EXISTS chunks (
    document_id text NOT NULL,
    heading_path text NOT NULL,
    indexed_structure text,
    content text NOT NULL,
    chunk_index integer NOT NULL,
    embedding vector({EMBEDDING_DIM}),
    doc_type text,
    lifecycle_status text,
    project text,
    tsv tsvector GENERATED ALWAYS AS ({CHUNKS_TSV_EXPRESSION}) STORED
);
"""

# ---------------------------------------------------------------------------
# Document surface (CAS-ADR-049): document-level text as its own retrieval
# surface, one row per document, carrying the same filter columns as chunks so
# a caller's predicates apply identically to both.
# ---------------------------------------------------------------------------
# Provenance is expressed as two generated columns rather than as a convention
# each consumer must remember. ``matchable`` holds authored text -- the title
# and tags, plus their normalized renderings -- and is the only column the
# match arm may read, so derived text cannot satisfy a caller's term.
# ``orienting`` holds derived text -- the generated abstract, the source
# filename stem, and that stem's expansion -- and reaches only ``tsv_rank``, so
# it still ranks and orients. A consumer added later cannot forget the rule:
# the match arm has no column to read derived text from.

DOCUMENT_SURFACE_TABLE = f"""\
CREATE TABLE IF NOT EXISTS document_surface (
    document_id text NOT NULL,
    matchable text NOT NULL,
    orienting text NOT NULL,
    embedding vector({EMBEDDING_DIM}),
    doc_type text,
    lifecycle_status text,
    project text,
    tsv_match tsvector GENERATED ALWAYS AS (
        setweight(to_tsvector('{TEXT_SEARCH_CONFIG}', matchable), 'A')
    ) STORED,
    tsv_rank tsvector GENERATED ALWAYS AS (
        setweight(to_tsvector('{TEXT_SEARCH_CONFIG}', matchable), 'A')
        || setweight(to_tsvector('{TEXT_SEARCH_CONFIG}', orienting), 'D')
    ) STORED
);
"""

# ---------------------------------------------------------------------------
# Indexes
# ---------------------------------------------------------------------------

GRAPH_INDEXES: tuple[str, ...] = (
    "CREATE INDEX IF NOT EXISTS idx_documents_source_path ON documents(source_path);",
    "CREATE INDEX IF NOT EXISTS idx_documents_source_hash ON documents(source_content_hash);",
    "CREATE INDEX IF NOT EXISTS idx_documents_lifecycle ON documents(lifecycle_status);",
    "CREATE INDEX IF NOT EXISTS idx_documents_pipeline ON documents(pipeline_status);",
    "CREATE INDEX IF NOT EXISTS idx_documents_metadata_confirmed ON documents(metadata_confirmed);",
    "CREATE INDEX IF NOT EXISTS idx_documents_doc_type ON documents(doc_type);",
    "CREATE INDEX IF NOT EXISTS idx_documents_project ON documents(project);",
    "CREATE INDEX IF NOT EXISTS idx_documents_doc_type_lifecycle "
    "ON documents(doc_type, lifecycle_status);",
    "CREATE INDEX IF NOT EXISTS idx_edges_source_type ON edges(source_id, edge_type);",
    "CREATE INDEX IF NOT EXISTS idx_edges_target_type ON edges(target_id, edge_type);",
    "CREATE INDEX IF NOT EXISTS idx_edges_type ON edges(edge_type);",
    "CREATE INDEX IF NOT EXISTS idx_edges_rationale_kind ON edges(rationale_kind);",
    "CREATE INDEX IF NOT EXISTS idx_edges_synced_from_content_hash "
    "ON edges(synced_from_content_hash);",
    "CREATE INDEX IF NOT EXISTS idx_staging_edges_source ON staging_edges(source_id);",
    "CREATE INDEX IF NOT EXISTS idx_staging_edges_target ON staging_edges(target_id);",
    # Tier3 expression indexes. The embedded store indexes
    # json_extract(tier3_metadata, '$.<field>'); the Postgres equivalent is a
    # functional index on the jsonb ``->>`` text accessor.
    "CREATE INDEX IF NOT EXISTS idx_tier3_ticket_id ON documents((tier3_metadata->>'ticket_id'));",
    "CREATE INDEX IF NOT EXISTS idx_tier3_failure_id "
    "ON documents((tier3_metadata->>'failure_id'));",
    "CREATE INDEX IF NOT EXISTS idx_tier3_tool_name ON documents((tier3_metadata->>'tool_name'));",
    "CREATE INDEX IF NOT EXISTS idx_document_tags_tag ON document_tags(tag);",
    "CREATE INDEX IF NOT EXISTS idx_document_tags_tag_doc ON document_tags(tag, document_id);",
)

# Natural-key uniqueness on production and staging edges. Postgres treats NULLs
# as distinct in a unique index by default (the same semantics the embedded store
# relies on), so multiple ``retracts`` edges with target_id NULL on one source
# remain legal.
UNIQUE_NATURAL_KEY_INDEXES: tuple[str, ...] = (
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_edges_uniq_natural_key "
    "ON edges(source_id, target_id, edge_type);",
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_staging_edges_uniq_natural_key "
    "ON staging_edges(source_id, target_id, edge_type);",
)

# Hoisted because the generated-column rebuild has to recreate this index: a
# dropped column takes its indexes with it. One string, so a rebuilt index
# cannot be defined differently from the one the bootstrap builds.
IDX_CHUNKS_TSV_GIN = "CREATE INDEX IF NOT EXISTS idx_chunks_tsv_gin ON chunks USING GIN (tsv);"

CONTENT_INDEXES: tuple[str, ...] = (
    "CREATE INDEX IF NOT EXISTS idx_chunks_document_id ON chunks(document_id);",
    IDX_CHUNKS_TSV_GIN,
    "CREATE INDEX IF NOT EXISTS idx_chunks_embedding_hnsw "
    "ON chunks USING hnsw (embedding vector_cosine_ops);",
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_document_surface_document_id "
    "ON document_surface(document_id);",
    "CREATE INDEX IF NOT EXISTS idx_document_surface_tsv_match_gin "
    "ON document_surface USING GIN (tsv_match);",
    "CREATE INDEX IF NOT EXISTS idx_document_surface_tsv_rank_gin "
    "ON document_surface USING GIN (tsv_rank);",
    "CREATE INDEX IF NOT EXISTS idx_document_surface_embedding_hnsw "
    "ON document_surface USING hnsw (embedding vector_cosine_ops);",
)

_TABLES: tuple[str, ...] = (
    DOCUMENTS_TABLE,
    USERS_TABLE,
    EDGES_TABLE,
    STAGING_EDGES_TABLE,
    DOCUMENT_TAGS_TABLE,
    CHUNKS_TABLE,
    DOCUMENT_SURFACE_TABLE,
)

# Columns added to a table after its first release. ``CREATE TABLE IF NOT
# EXISTS`` is a no-op against an already-provisioned vault, so a column added to
# a table definition above reaches a new vault and no existing one; the matching
# ``ADD COLUMN IF NOT EXISTS`` here is what carries it to a vault provisioned
# before the column existed. The bootstrap runs on every vault open, so an
# existing vault picks the column up on its next open with no operator step, and
# a vault that already has it is unaffected. Every statement must be idempotent,
# like the table and index DDL it runs alongside.
ADDITIVE_COLUMNS: tuple[str, ...] = (
    "ALTER TABLE documents ADD COLUMN IF NOT EXISTS stored_content_hash text;",
    # Nullable with no default, deliberately: NULL is what "not yet derived"
    # means, and it is the value CHUNKS_TSV_EXPRESSION's coalesce reads to keep
    # an unmigrated vault on the pre-decision behaviour. A default of '' would
    # empty the top ranking weight for every row it touched. Adding a nullable
    # column with no default is catalog-only, so this is safe to run on every
    # vault open even on a table of tens of thousands of passages.
    "ALTER TABLE chunks ADD COLUMN IF NOT EXISTS indexed_structure text;",
)


# The generated-column rebuild. Deliberately NOT in ``ddl_statements()``: the
# drop is destructive and the add is not idempotent, so both bootstrap gates
# would reject them -- correctly, since the bootstrap runs on every vault open
# and this rewrites the table. It runs once per vault, from the migration.
#
# Drop-and-re-add rather than ``ALTER COLUMN tsv SET EXPRESSION AS (...)``,
# which is a PostgreSQL 17 feature. The workstation and every CI service
# container run 17 while the deployed target is Flexible Server 16, so the
# 17-only form would pass every test this repository runs and fail only at
# deploy. Nothing here can catch that at runtime; the DDL is written to the
# floor and a static test pins it.
#
# Dropping the column drops the GIN index over it, so the index is recreated in
# the same statement list.
CHUNKS_TSV_REBUILD: tuple[str, ...] = (
    "ALTER TABLE chunks DROP COLUMN IF EXISTS tsv;",
    "ALTER TABLE chunks ADD COLUMN tsv tsvector GENERATED ALWAYS AS "
    f"({CHUNKS_TSV_EXPRESSION}) STORED;",
    IDX_CHUNKS_TSV_GIN,
)

# Whether a vault's passage vector already ranks the relative structure.
#
# The test is whether the stored expression *names the column*, not whether it
# equals CHUNKS_TSV_EXPRESSION: Postgres stores its own normalization of a
# generated expression -- `'english'::regconfig`, `'A'::"char"`, added
# parentheses -- so a whole-string comparison would report "not yet migrated"
# forever and rewrite the table on every migration call. The column name is the
# minimal discriminator that survives that normalization, and it is exactly the
# property that matters: does weight A read the relative structure, or the
# address?
CHUNKS_TSV_GENERATION_EXPRESSION_PROBE = (
    "SELECT generation_expression FROM information_schema.columns"
    " WHERE table_schema = current_schema()"
    " AND table_name = 'chunks' AND column_name = 'tsv'"
)


# ---------------------------------------------------------------------------
# Statement assembly + bootstrap
# ---------------------------------------------------------------------------


def _ordered_extensions(extensions: Iterable[str]) -> list[str]:
    """Validated extension list with ``vector`` guaranteed first.

    ``vector`` underpins the content store's embedding column, so it is always
    created regardless of the caller-supplied list; the remainder follow in order
    with duplicates dropped.
    """
    ordered = ["vector"]
    seen = {"vector"}
    for ext in extensions:
        validate_extension(ext)
        if ext not in seen:
            seen.add(ext)
            ordered.append(ext)
    return ordered


def ddl_statements() -> list[str]:
    """The schema-internal table and index DDL, in dependency order.

    Additive column DDL follows the table creates and precedes the indexes, so a
    vault provisioned before a column existed gains it before any index that
    might reference it is built.
    """
    return [
        *_TABLES,
        *ADDITIVE_COLUMNS,
        *GRAPH_INDEXES,
        *UNIQUE_NATURAL_KEY_INDEXES,
        *CONTENT_INDEXES,
    ]


def schema_statements(
    schema: str,
    extensions: Iterable[str] = DEFAULT_EXTENSIONS,
    *,
    create_extensions: bool = True,
) -> list[str]:
    """The full ordered statement list :func:`bootstrap_schema` executes.

    Extensions are created before the search_path is narrowed so they land in
    the database-global ``public`` schema (shared across disposable schemas);
    tables are then created in ``schema``. Exposed for inspection so the
    idempotency of every DDL statement is unit-testable without a server.

    ``schema`` is required and must not be ``public``: storage tenancy is one
    schema per vault (CAS-ADR-042), and ``public`` carries extension objects
    only, never SAGE tables. Because ``public`` trails every per-vault
    search_path, a SAGE table there would silently absorb the unqualified
    queries of any vault whose own schema was dropped out of band -- masking
    the error those queries are expected to raise.

    ``create_extensions`` gates the ``CREATE EXTENSION`` statements. The default
    creates them, which the local runtime requires (the connecting role can
    create extensions and no separate bootstrap runs ahead of it). When False,
    the extensions are assumed to be an already-established precondition and the
    statement list creates only the schema and tables: a managed endpoint may
    enforce extension creation as a privileged operation the connecting role
    lacks, so the workload must rely on an out-of-band administrator having
    created them (CAS-ADR-042).
    """
    validate_schema_name(schema)
    if schema == "public":
        raise ValueError(
            "refusing to provision SAGE tables into the shared 'public' schema: "
            "tenancy is one schema per vault (CAS-ADR-042) and 'public' carries "
            "extension objects only -- a SAGE table there would mask a dropped "
            "vault schema behind the per-vault search_path fallback"
        )
    statements = [f'CREATE SCHEMA IF NOT EXISTS "{schema}"']  # noqa: S608
    if create_extensions:
        statements += [
            f'CREATE EXTENSION IF NOT EXISTS "{ext}"'  # noqa: S608
            for ext in _ordered_extensions(extensions)
        ]
    statements.append(f'SET search_path TO "{schema}", public')  # noqa: S608
    statements += ddl_statements()
    return statements


async def bootstrap_schema(
    conn,
    *,
    schema: str,
    extensions: Iterable[str] = DEFAULT_EXTENSIONS,
    create_extensions: bool = True,
) -> None:
    """Idempotently create the SAGE schema on a Postgres connection.

    ``conn`` is an open async psycopg connection. The whole statement list runs
    in one transaction so a partial failure leaves nothing behind, and re-running
    is a no-op because every statement is ``IF NOT EXISTS``. ``schema`` is
    required and must not be ``public`` (see :func:`schema_statements`).

    ``create_extensions`` is threaded to :func:`schema_statements`: pass False
    when the extensions are an out-of-band precondition the connecting role may
    not create itself (see :func:`schema_statements`).
    """
    statements = schema_statements(schema, extensions, create_extensions=create_extensions)
    async with conn.transaction():
        for stmt in statements:
            await conn.execute(stmt)


def drop_schema_statement(schema: str) -> str:
    """The single ``DROP SCHEMA IF EXISTS "<schema>" CASCADE`` statement.

    The teardown counterpart of :func:`schema_statements`. ``schema`` is
    validated as a lowercase identifier (storage tenancy is one schema per vault,
    named by the vault id) and interpolated as a quoted identifier -- it cannot be
    a bind parameter. ``IF EXISTS`` makes the drop idempotent; ``CASCADE`` removes
    the vault's tables, indexes, and generated columns in one statement. The
    statement is schema-scoped by construction: there is no code path here that
    can emit ``DROP DATABASE``, which would destroy every vault's schema in the
    shared database (CAS-ADR-042).
    """
    validate_schema_name(schema)
    return f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'  # noqa: S608


async def drop_schema(conn, schema: str) -> None:
    """Idempotently drop one vault's schema on a Postgres connection.

    ``conn`` is an open async psycopg connection. Runs the single
    :func:`drop_schema_statement`; re-running against an already-absent schema is
    a no-op (``IF EXISTS``). The caller owns the connection's transaction mode --
    the teardown path opens an autocommit connection, mirroring
    :meth:`PostgresVaultStorageProvisioner._bootstrap`.
    """
    await conn.execute(drop_schema_statement(schema))


async def schema_exists(conn, schema: str) -> bool:
    """Whether ``schema`` is present in the shared database (CAS-ADR-042).

    The read-only companion to :func:`drop_schema`: probes ``information_schema``
    for the named schema. ``conn`` is an open async psycopg connection. The schema
    name is a bind parameter (data, not an interpolated identifier), so no
    identifier validation is required. Used by the out-of-band teardown's snapshot
    step to skip a schema dump when the schema is already gone (a resume after a
    partial teardown), rather than erroring on ``pg_dump`` against an absent schema.
    """
    cur = await conn.execute(
        "SELECT 1 FROM information_schema.schemata WHERE schema_name = %s", (schema,)
    )
    return await cur.fetchone() is not None
