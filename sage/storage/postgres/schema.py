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

CHUNKS_TABLE = f"""\
CREATE TABLE IF NOT EXISTS chunks (
    document_id text NOT NULL,
    heading_path text NOT NULL,
    content text NOT NULL,
    chunk_index integer NOT NULL,
    embedding vector({EMBEDDING_DIM}),
    doc_type text,
    lifecycle_status text,
    project text,
    tsv tsvector GENERATED ALWAYS AS (
        setweight(to_tsvector('{TEXT_SEARCH_CONFIG}', heading_path), 'A')
        || setweight(to_tsvector('{TEXT_SEARCH_CONFIG}', content), 'D')
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

CONTENT_INDEXES: tuple[str, ...] = (
    "CREATE INDEX IF NOT EXISTS idx_chunks_document_id ON chunks(document_id);",
    "CREATE INDEX IF NOT EXISTS idx_chunks_tsv_gin ON chunks USING GIN (tsv);",
    "CREATE INDEX IF NOT EXISTS idx_chunks_embedding_hnsw "
    "ON chunks USING hnsw (embedding vector_cosine_ops);",
)

_TABLES: tuple[str, ...] = (
    DOCUMENTS_TABLE,
    USERS_TABLE,
    EDGES_TABLE,
    STAGING_EDGES_TABLE,
    DOCUMENT_TAGS_TABLE,
    CHUNKS_TABLE,
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
    """The schema-internal table and index DDL, in dependency order."""
    return [*_TABLES, *GRAPH_INDEXES, *UNIQUE_NATURAL_KEY_INDEXES, *CONTENT_INDEXES]


def schema_statements(
    schema: str = "public",
    extensions: Iterable[str] = DEFAULT_EXTENSIONS,
    *,
    create_extensions: bool = True,
) -> list[str]:
    """The full ordered statement list :func:`bootstrap_schema` executes.

    Extensions are created before the search_path is narrowed so they land in
    the database-global ``public`` schema (shared across disposable schemas);
    tables are then created in ``schema``. Exposed for inspection so the
    idempotency of every DDL statement is unit-testable without a server.

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
    schema: str = "public",
    extensions: Iterable[str] = DEFAULT_EXTENSIONS,
    create_extensions: bool = True,
) -> None:
    """Idempotently create the SAGE schema on a Postgres connection.

    ``conn`` is an open async psycopg connection. The whole statement list runs
    in one transaction so a partial failure leaves nothing behind, and re-running
    is a no-op because every statement is ``IF NOT EXISTS``.

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
