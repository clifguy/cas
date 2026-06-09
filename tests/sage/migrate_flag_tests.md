# SAGE Schema Migration CLI Gating Tests

Behavioral tests for the `--migrate` CLI switch on `python -m sage`. The
flag is OFF by default. When OFF and a schema migration is required,
SAGE refuses to start. When ON and a migration is required, SAGE applies
it (LanceDB migration is destructive and rebuilds the chunks table from
a parquet backup).

Owner bootstrap is data initialization, not schema conversion, and
remains always-on regardless of the flag.

---

## TEST-SAGE-MIG-001: SqliteGraphStore detects pending SQLite migrations

**Artifact:** `sage/storage/migrations.py`, `sage/storage/graph_store.py`
**Category:** detection

**Precondition:** A SQLite database created from an older `TABLES` set
that omits one or more columns added by `MIGRATIONS` (e.g., the database
exists but does not have the `metadata_confirmed` column on `documents`).

**Input:** Call a detection helper exposed by the migrations module
that returns the list of pending migrations for the given database.

**Expected:**
- The returned list contains one entry per missing column.
- Each entry identifies the table and the column.
- No `ALTER TABLE` statements have been executed against the database.
- The function is read-only.

---

## TEST-SAGE-MIG-002: SqliteGraphStore.initialize(migrate=False) on legacy schema raises

**Artifact:** `sage/storage/graph_store.py:initialize`
**Category:** gating

**Precondition:** A SQLite database missing one or more migration columns.

**Input:** Construct `SqliteGraphStore(db_path)` and call `initialize(migrate=False)`.

**Expected:**
- A `SchemaMigrationRequired` exception (or equivalent named subclass of
  `RuntimeError`) is raised.
- The exception message lists the pending migrations (table + column).
- The exception message instructs the user to re-run with `--migrate`.
- The legacy database is unchanged: missing columns remain missing.

---

## TEST-SAGE-MIG-003: SqliteGraphStore.initialize(migrate=True) on legacy schema applies migrations

**Artifact:** `sage/storage/graph_store.py:initialize`
**Category:** application

**Precondition:** A SQLite database missing one or more migration columns,
populated with at least one row in `documents` and `edges` to verify
that data is preserved across the migration.

**Input:** Construct `SqliteGraphStore(db_path)` and call `initialize(migrate=True)`.

**Expected:**
- Initialization succeeds.
- All previously missing columns now exist.
- All pre-existing rows are preserved with their original values.
- `POST_MIGRATION_DDL` indexes exist after migration completes.

---

## TEST-SAGE-MIG-004: SqliteGraphStore.initialize(migrate=False) on current schema starts normally

**Artifact:** `sage/storage/graph_store.py:initialize`
**Category:** no-op

**Precondition:** A SQLite database whose schema matches the current
`TABLES + MIGRATIONS` definition (i.e., no pending migrations).

**Input:** Call `initialize(migrate=False)` on a fresh SqliteGraphStore.

**Expected:**
- Initialization succeeds without raising.
- The database is unchanged.

---

## TEST-SAGE-MIG-005: LanceDBContentStore detects pending column additions

**Artifact:** `sage/adapters/content_store_lancedb.py`
**Category:** detection

**Precondition:** A LanceDB database whose `chunks` table exists but is
missing one or more columns from the current `CHUNKS_SCHEMA` (e.g., a
table created before the `doc_type` column was added).

**Input:** Call a detection helper on `LanceDBContentStore` that returns
whether a schema migration is required without performing one.

**Expected:**
- The helper returns the set of columns that would be added.
- The chunks table is not dropped, recreated, or otherwise modified.
- No parquet backup file is written.

---

## TEST-SAGE-MIG-006: LanceDBContentStore(migrate=False) on legacy schema raises

**Artifact:** `sage/adapters/content_store_lancedb.py:__init__`
**Category:** gating

**Precondition:** A LanceDB database whose `chunks` table is missing one
or more columns from the current `CHUNKS_SCHEMA`. The table contains at
least one row.

**Input:** Construct `LanceDBContentStore(brain_root, migrate=False)`.

**Expected:**
- A `SchemaMigrationRequired` exception is raised.
- The exception message names the missing columns and instructs the user
  to re-run with `--migrate`.
- The chunks table is unchanged. No parquet backup file is created.

---

## TEST-SAGE-MIG-007: LanceDBContentStore(migrate=True) on legacy schema rebuilds

**Artifact:** `sage/adapters/content_store_lancedb.py:__init__`
**Category:** application

**Precondition:** A LanceDB database whose `chunks` table is missing one
or more columns from `CHUNKS_SCHEMA`. The table contains at least one
row of legacy-shaped data.

**Input:** Construct `LanceDBContentStore(brain_root, migrate=True)`.

**Expected:**
- Construction succeeds.
- The chunks table now matches `CHUNKS_SCHEMA` (all columns present).
- All pre-existing rows are present in the new table; values for the
  new columns are NULL.
- The parquet backup file has been deleted on success.

---

## TEST-SAGE-MIG-008: LanceDBContentStore refuses to overwrite existing backup

**Artifact:** `sage/adapters/content_store_lancedb.py:_migrate_schema_if_needed`
**Category:** safety

**Precondition:** A LanceDB database with a pending schema migration AND
a `chunks_migration_backup.parquet` file already present in `brain_root`
(simulating an unresolved prior failed migration).

**Input:** Construct `LanceDBContentStore(brain_root, migrate=True)`.

**Expected:**
- A `RuntimeError` (or subclass) is raised.
- The exception message references the existing backup path and tells
  the user to inspect/remove it before retrying.
- The chunks table is not dropped or rebuilt.
- The pre-existing backup file is not overwritten.

---

## TEST-SAGE-MIG-009: LanceDBContentStore logs row count before destructive rebuild

**Artifact:** `sage/adapters/content_store_lancedb.py:_migrate_schema_if_needed`
**Category:** observability

**Precondition:** A LanceDB database with a pending schema migration and
a known number of rows in the chunks table (e.g., 7).

**Input:** Construct `LanceDBContentStore(brain_root, migrate=True)`
with logging captured at INFO level.

**Expected:**
- An INFO-level log record is emitted before the table is dropped that
  reports the row count being migrated (e.g., contains "7 rows").
- The migration completes successfully.

---

## TEST-SAGE-MIG-010: initialize_services threads migrate flag to SqliteGraphStore and LanceDB

**Artifact:** `sage/mcp_init.py:initialize_services`
**Category:** plumbing

**Precondition:** A vault directory whose graph.db and lancedb directory
both have pending migrations.

**Input:** Call `initialize_services(config, migrate=False)`.

**Expected:**
- `SchemaMigrationRequired` is raised before services are returned.
- The graph.db and lancedb directory are unchanged.

When the same call is made with `migrate=True`, both stores are
migrated and `SAGEServices` is returned.

---

## TEST-SAGE-MIG-011: __main__ default (no flag) does not migrate

**Artifact:** `sage/__main__.py`
**Category:** CLI default

**Precondition:** A vault directory with a pending SQLite migration.

**Input:** Invoke the module entry point with `[config_path]` and no
`--migrate` flag (via the parser, without actually starting uvicorn).

**Expected:**
- The argparse-parsed namespace has `migrate=False`.
- When `create_app(config_paths=..., migrate=False)` runs the lifespan,
  it raises `SchemaMigrationRequired` rather than silently mutating the
  database.

---

## TEST-SAGE-MIG-012: __main__ with --migrate applies migrations

**Artifact:** `sage/__main__.py`
**Category:** CLI on

**Precondition:** A vault directory with a pending SQLite migration.

**Input:** Invoke the module entry point with `[config_path, "--migrate"]`
(via the parser, without starting uvicorn).

**Expected:**
- The argparse-parsed namespace has `migrate=True`.
- `create_app(config_paths=..., migrate=True)` runs the lifespan to
  completion; the SQLite database is migrated.

---

## TEST-SAGE-MIG-013: Owner bootstrap remains always-on

**Artifact:** `sage/services/user_service.py:bootstrap_owner`
**Category:** scope guard

**Precondition:** A fresh vault with no pending schema migrations and
no pre-existing owner row.

**Input:** Call `initialize_services(config, migrate=False)`.

**Expected:**
- Initialization succeeds.
- The vault owner row exists in the `users` table after init.
- This confirms the `--migrate` gate covers schema conversion only,
  not data initialization.
