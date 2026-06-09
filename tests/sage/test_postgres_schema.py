"""Canonical Postgres schema + bootstrap (CAS-ADR-042).

Pure tests inspect the DDL statements and the identifier/disposable guards with
no server; the PG-touching tests (which skip when SAGE_TEST_PG_DSN is unset)
prove the bootstrap provisions the schema, is idempotent, and yields a schema the
future store adapters can actually use -- graph tables, pgvector nearest-neighbour
search, and ts_rank full-text search together.
"""

from __future__ import annotations

import os

import pytest

from sage.storage.postgres import schema as pgschema

# ---------------------------------------------------------------------------
# Pure: DDL shape + guards
# ---------------------------------------------------------------------------


def test_every_create_statement_is_idempotent():
    """Every CREATE statement the bootstrap runs carries IF NOT EXISTS.

    Anti-coincidental-pass: there ARE create statements (positive control), and
    each must be idempotent. Dropping an IF NOT EXISTS would make a re-run raise.
    """
    stmts = pgschema.schema_statements(schema="sage_test_x", extensions=["vector", "pgstattuple"])
    creates = [s for s in stmts if s.strip().upper().startswith("CREATE ")]
    assert creates, "expected CREATE statements in the bootstrap"
    for stmt in creates:
        assert "IF NOT EXISTS" in stmt, f"non-idempotent statement: {stmt!r}"


def test_chunks_table_has_vector_fts_and_indexes():
    """The content chunks table carries the pgvector embedding column, the
    generated tsvector full-text column, and their HNSW + GIN indexes."""
    assert "vector(768)" in pgschema.CHUNKS_TABLE
    assert "to_tsvector('english', content)" in pgschema.CHUNKS_TABLE
    assert "tsvector" in pgschema.CHUNKS_TABLE
    content_idx = "\n".join(pgschema.CONTENT_INDEXES)
    assert "USING GIN (tsv)" in content_idx
    assert "USING hnsw (embedding vector_cosine_ops)" in content_idx


def test_graph_tables_present_and_typed():
    """The graph tables exist with their Postgres types and key constraints."""
    ddl = "\n".join(pgschema.ddl_statements())
    for table in ("documents", "edges", "staging_edges", "users", "document_tags"):
        assert f"CREATE TABLE IF NOT EXISTS {table}" in ddl
    assert "tags jsonb" in pgschema.DOCUMENTS_TABLE
    assert "tier3_metadata jsonb" in pgschema.DOCUMENTS_TABLE
    assert "CHECK (user_type IN ('human', 'agent'))" in pgschema.USERS_TABLE
    nat = "\n".join(pgschema.UNIQUE_NATURAL_KEY_INDEXES)
    assert "idx_edges_uniq_natural_key" in nat
    assert "idx_staging_edges_uniq_natural_key" in nat


def test_identifier_guards_reject_injection():
    """Schema and extension identifier guards accept safe names and reject any
    name carrying a quote, semicolon, space, or leading digit."""
    assert pgschema.validate_schema_name("sage_test_abc") == "sage_test_abc"
    assert pgschema.validate_extension("pg_repack") == "pg_repack"
    for bad in ('public"; DROP TABLE x', "sage; DROP", "Sage", "1abc", "has space"):
        with pytest.raises(ValueError):
            pgschema.validate_schema_name(bad)
    with pytest.raises(ValueError):
        pgschema.validate_extension("vector; DROP")


def test_disposable_target_guard():
    """The disposable-target guard accepts sage_test_* schemas and refuses
    'public' and any non-prefixed name -- the 'never the dev DB' rule."""
    assert pgschema.assert_disposable_target("sage_test_abcd") == "sage_test_abcd"
    for bad in ("public", "sage", "scratch"):
        with pytest.raises(ValueError):
            pgschema.assert_disposable_target(bad)


# ---------------------------------------------------------------------------
# PG-touching (skip without SAGE_TEST_PG_DSN)
# ---------------------------------------------------------------------------

_EXPECTED_TABLES = {"documents", "edges", "staging_edges", "users", "document_tags", "chunks"}


async def test_bootstrap_provisions_schema(pg_dsn):
    """Bootstrapping a fresh schema creates every table, the vector extension,
    and the HNSW + GIN content indexes."""
    import psycopg

    from sage.storage.postgres.schema import assert_disposable_target, bootstrap_schema

    schema = assert_disposable_target("sage_test_prov_" + os.urandom(3).hex())
    async with await psycopg.AsyncConnection.connect(pg_dsn, autocommit=True) as conn:
        try:
            await bootstrap_schema(conn, schema=schema, extensions=["vector", "pgstattuple"])

            cur = await conn.execute(
                "SELECT table_name FROM information_schema.tables WHERE table_schema = %s",
                (schema,),
            )
            tables = {r[0] for r in await cur.fetchall()}
            assert _EXPECTED_TABLES <= tables

            cur = await conn.execute("SELECT 1 FROM pg_extension WHERE extname = 'vector'")
            assert await cur.fetchone() is not None

            cur = await conn.execute(
                "SELECT indexname FROM pg_indexes WHERE schemaname = %s", (schema,)
            )
            indexes = {r[0] for r in await cur.fetchall()}
            assert "idx_chunks_embedding_hnsw" in indexes
            assert "idx_chunks_tsv_gin" in indexes
        finally:
            await conn.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')  # noqa: S608


async def test_bootstrap_is_idempotent(pg_dsn):
    """Running the bootstrap twice on one schema raises nothing and leaves the
    same six tables -- the headline idempotency acceptance criterion."""
    import psycopg

    from sage.storage.postgres.schema import assert_disposable_target, bootstrap_schema

    schema = assert_disposable_target("sage_test_idem_" + os.urandom(3).hex())
    async with await psycopg.AsyncConnection.connect(pg_dsn, autocommit=True) as conn:
        try:
            await bootstrap_schema(conn, schema=schema, extensions=["vector", "pgstattuple"])
            await bootstrap_schema(conn, schema=schema, extensions=["vector", "pgstattuple"])

            cur = await conn.execute(
                "SELECT count(*) FROM information_schema.tables WHERE table_schema = %s",
                (schema,),
            )
            assert (await cur.fetchone())[0] == len(_EXPECTED_TABLES)
        finally:
            await conn.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')  # noqa: S608


async def test_schema_is_usable_end_to_end(pg_pool):
    """The provisioned schema supports the operations the future adapters need:
    a graph edge round-trips, ts_rank full-text search finds a chunk, and a
    pgvector nearest-neighbour query returns it."""
    insert_doc = (
        "INSERT INTO documents (id, title, source_type, source_path, source_content_hash, "
        "adapter_version, created_by, created_at, last_modified_by, updated_at) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)"
    )
    async with pg_pool.connection() as conn:
        await conn.execute(
            insert_doc,
            ("doc1", "Title", "markdown", "imports/d.md", "sha256:x", "1", "c", "t", "c", "t"),
        )
        await conn.execute(
            insert_doc,
            ("doc2", "Other", "markdown", "imports/e.md", "sha256:y", "1", "c", "t", "c", "t"),
        )
        await conn.execute(
            "INSERT INTO edges (id, source_id, target_id, edge_type, created_at) "
            "VALUES (%s, %s, %s, %s, %s)",
            ("e1", "doc1", "doc2", "references", "t"),
        )
        embedding = [0.1] * 768
        await conn.execute(
            "INSERT INTO chunks (document_id, heading_path, content, chunk_index, embedding) "
            "VALUES (%s, %s, %s, %s, %s)",
            ("doc1", "Root", "alpha beta gamma keyword", 0, embedding),
        )

        cur = await conn.execute("SELECT target_id FROM edges WHERE source_id = %s", ("doc1",))
        assert (await cur.fetchone())[0] == "doc2"

        cur = await conn.execute(
            "SELECT document_id FROM chunks WHERE tsv @@ websearch_to_tsquery('english', %s)",
            ("keyword",),
        )
        assert (await cur.fetchone())[0] == "doc1"

        cur = await conn.execute(
            "SELECT document_id FROM chunks ORDER BY embedding <=> %s::vector LIMIT 1",
            (embedding,),
        )
        assert (await cur.fetchone())[0] == "doc1"


async def test_natural_key_uniqueness_enforced(pg_pool):
    """A duplicate (source_id, target_id, edge_type) edge is rejected by the
    natural-key unique index -- the constraint the adapters rely on for atomic
    upsert/dedup."""
    import psycopg

    insert_doc = (
        "INSERT INTO documents (id, title, source_type, source_path, source_content_hash, "
        "adapter_version, created_by, created_at, last_modified_by, updated_at) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)"
    )
    insert_edge = (
        "INSERT INTO edges (id, source_id, target_id, edge_type, created_at) "
        "VALUES (%s, %s, %s, %s, %s)"
    )
    async with pg_pool.connection() as conn:
        for did in ("d1", "d2"):
            await conn.execute(
                insert_doc,
                (did, "T", "markdown", "imports/" + did, "sha256:x", "1", "c", "t", "c", "t"),
            )
        await conn.execute(insert_edge, ("e1", "d1", "d2", "references", "t"))
        with pytest.raises(psycopg.errors.UniqueViolation):
            await conn.execute(insert_edge, ("e2", "d1", "d2", "references", "t"))
