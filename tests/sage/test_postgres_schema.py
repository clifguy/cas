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


def test_schema_statements_creates_extensions_by_default():
    """The default statement list creates the extensions the content store needs.

    Anti-coincidental-pass: the extension CREATEs are a positive control -- a
    bootstrap that omitted them by default (or whose flag inverted) would fail
    here, which is the local/embedded contract (the connecting role creates its
    own extensions, with no admin bootstrap ahead of it).
    """
    stmts = pgschema.schema_statements(schema="sage_test_x", extensions=["vector", "pgstattuple"])
    assert any('CREATE EXTENSION IF NOT EXISTS "vector"' in s for s in stmts)
    assert any('CREATE EXTENSION IF NOT EXISTS "pgstattuple"' in s for s in stmts)


def test_schema_statements_omits_extensions_when_disabled():
    """`create_extensions=False` drops every CREATE EXTENSION but keeps the rest.

    The cloud managed-identity contract: the unprivileged workload role cannot
    issue an untrusted CREATE EXTENSION (Azure enforces the privilege at the
    command level, so IF NOT EXISTS does not bypass it even when the extension is
    already present), so the self-bootstrap relies on the admin-pre-created
    extensions and creates only its schema and tables.

    Anti-coincidental-pass: an implementation that ignored the flag would still
    emit a CREATE EXTENSION and fail the first assert; the positive controls
    (schema, search_path, a table) fail if the flag over-deletes the rest of the
    bootstrap rather than only the extension statements.
    """
    stmts = pgschema.schema_statements(schema="sage_test_x", create_extensions=False)
    assert not any("CREATE EXTENSION" in s for s in stmts)
    assert any(s.startswith('CREATE SCHEMA IF NOT EXISTS "sage_test_x"') for s in stmts)
    assert any("SET search_path" in s for s in stmts)
    assert any("CREATE TABLE IF NOT EXISTS documents" in s for s in stmts)


def test_chunks_table_has_vector_fts_and_indexes():
    """The content chunks table carries the pgvector embedding column, the
    generated tsvector full-text column, and their HNSW + GIN indexes."""
    assert "vector(768)" in pgschema.CHUNKS_TABLE
    assert "tsvector" in pgschema.CHUNKS_TABLE
    # The generated tsv weaves heading_path (weight A) and content (weight D)
    # so keyword search matches terms that appear only in a heading.
    assert "to_tsvector('english', heading_path)" in pgschema.CHUNKS_TABLE
    assert "to_tsvector('english', content)" in pgschema.CHUNKS_TABLE
    assert "setweight" in pgschema.CHUNKS_TABLE
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


def test_schema_statements_requires_an_explicit_schema():
    """Provisioning-by-omission is impossible: ``schema`` carries no default.

    Anti-coincidental: a restored ``schema="public"`` default would make this
    zero-argument call succeed (and target the shared schema), so the TypeError
    assertion fails exactly that regression.
    """
    with pytest.raises(TypeError):
        pgschema.schema_statements()  # type: ignore[call-arg]


def test_schema_statements_refuses_the_public_schema():
    """'public' is never a provisioning target: it carries extension objects
    only, not SAGE tables (CAS-ADR-042). A SAGE table in 'public' would sit
    behind every per-vault search_path, so a dropped vault schema's unqualified
    queries would silently resolve there instead of raising -- the assembly
    point fails loud instead of permitting that."""
    with pytest.raises(ValueError, match="public"):
        pgschema.schema_statements(schema="public")


async def test_bootstrap_schema_refuses_the_public_schema():
    """The refusal reaches the bootstrap entry point too, and fires before the
    connection is touched (the sentinel object would raise on any attribute
    access, so a pass proves no statement was attempted)."""
    with pytest.raises(ValueError, match="public"):
        await pgschema.bootstrap_schema(object(), schema="public")


def test_disposable_target_guard():
    """The disposable-target guard accepts sage_test_* schemas and refuses
    'public' and any non-prefixed name -- the 'never the dev DB' rule."""
    assert pgschema.assert_disposable_target("sage_test_abcd") == "sage_test_abcd"
    for bad in ("public", "sage", "scratch"):
        with pytest.raises(ValueError):
            pgschema.assert_disposable_target(bad)


def test_drop_schema_statement_is_schema_scoped():
    """`drop_schema_statement` emits exactly one schema-scoped DROP ... CASCADE.

    Anti-coincidental-pass: the exact string is asserted, so any change to the
    verb (a DROP DATABASE in particular), the target, or the CASCADE/IF EXISTS
    clauses fails here. The statement is structurally incapable of dropping the
    database -- there is no code path in the module that emits DROP DATABASE.
    """
    assert pgschema.drop_schema_statement("myvault") == 'DROP SCHEMA IF EXISTS "myvault" CASCADE'


def test_drop_schema_statement_rejects_bad_id():
    """`drop_schema_statement` validates the schema name before interpolating it.

    Anti-coincidental-pass: a safe name is the positive control (it returns a
    statement); each bad name -- quote/semicolon injection, a space, mixed case,
    empty, leading digit -- must raise, proving the guard runs before the
    identifier reaches SQL.
    """
    assert pgschema.drop_schema_statement("sage_test_abc").endswith('"sage_test_abc" CASCADE')
    for bad in ('v"; DROP DATABASE sage', "has space", "Mixed", "", "1abc"):
        with pytest.raises(ValueError):
            pgschema.drop_schema_statement(bad)


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

        # The weighted tsv covers heading_path (weight A): a term that appears
        # only in a heading is still findable via ts_rank. A distinct embedding
        # keeps doc1 the unique nearest neighbour for the vector query below.
        far_embedding = [1.0] + [0.0] * 767
        await conn.execute(
            "INSERT INTO chunks (document_id, heading_path, content, chunk_index, embedding) "
            "VALUES (%s, %s, %s, %s, %s)",
            ("doc2", "Section > headingonlyterm notes", "no body match present", 0, far_embedding),
        )
        cur = await conn.execute(
            "SELECT document_id FROM chunks WHERE tsv @@ websearch_to_tsquery('english', %s)",
            ("headingonlyterm",),
        )
        assert (await cur.fetchone())[0] == "doc2"

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


async def test_drop_schema_removes_a_provisioned_schema(pg_dsn):
    """After bootstrap, `drop_schema` removes the schema entirely: its tables and
    the schema row itself are gone from the catalog. The pre-drop existence probe
    is the positive control proving the drop, not a never-created schema, is why
    the post-drop probe is empty."""
    import psycopg

    from sage.storage.postgres.schema import (
        assert_disposable_target,
        bootstrap_schema,
        drop_schema,
    )

    schema = assert_disposable_target("sage_test_drop_" + os.urandom(3).hex())
    async with await psycopg.AsyncConnection.connect(pg_dsn, autocommit=True) as conn:
        await bootstrap_schema(conn, schema=schema, extensions=["vector", "pgstattuple"])
        cur = await conn.execute(
            "SELECT 1 FROM information_schema.schemata WHERE schema_name = %s", (schema,)
        )
        assert await cur.fetchone() is not None

        await drop_schema(conn, schema)

        cur = await conn.execute(
            "SELECT 1 FROM information_schema.schemata WHERE schema_name = %s", (schema,)
        )
        assert await cur.fetchone() is None
        cur = await conn.execute(
            "SELECT count(*) FROM information_schema.tables WHERE table_schema = %s", (schema,)
        )
        assert (await cur.fetchone())[0] == 0


async def test_drop_schema_is_idempotent(pg_dsn):
    """Dropping an already-absent schema is a silent no-op (IF EXISTS): neither
    the first nor a repeat drop raises."""
    import psycopg

    from sage.storage.postgres.schema import drop_schema

    schema = "sage_test_absent_" + os.urandom(3).hex()
    async with await psycopg.AsyncConnection.connect(pg_dsn, autocommit=True) as conn:
        await drop_schema(conn, schema)
        await drop_schema(conn, schema)


async def test_schema_exists_reflects_bootstrap_and_drop(pg_dsn):
    """`schema_exists` is False before bootstrap, True after, and False after drop.

    Anti-coincidental-pass: the False->True->False transition across bootstrap and
    drop is the whole predicate; a schema_exists hard-wired to a constant would fail
    one of the three probes.
    """
    import psycopg

    from sage.storage.postgres.schema import (
        assert_disposable_target,
        bootstrap_schema,
        drop_schema,
        schema_exists,
    )

    schema = assert_disposable_target("sage_test_exists_" + os.urandom(3).hex())
    async with await psycopg.AsyncConnection.connect(pg_dsn, autocommit=True) as conn:
        assert await schema_exists(conn, schema) is False
        await bootstrap_schema(conn, schema=schema, extensions=["vector", "pgstattuple"])
        try:
            assert await schema_exists(conn, schema) is True
        finally:
            await drop_schema(conn, schema)
        assert await schema_exists(conn, schema) is False
