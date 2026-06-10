# Local Postgres runtime for SAGE

SAGE's durable graph and content state externalizes to PostgreSQL in both
deployment targets (CAS-ADR-042). This runbook stands up the **local** target on
macOS (Apple Silicon, Homebrew): a persistent Postgres instance under `launchd`,
the extensions SAGE needs, and the schema bootstrap. Postgres is the local
profile's default durable-storage binding (`storage_backend: postgres` in
`sage/config.yaml`); §6 covers selecting between it and the embedded
SQLite/LanceDB fallback, the cutover order, and rollback.

> The connection defaults live in the `postgres` block of `sage/config.yaml`
> (host `null` ⇒ local unix socket, user `null` ⇒ the OS user via peer auth,
> database `sage`). No password is needed for a local socket; for any TCP/hosted
> endpoint the password is read from `$SAGE_PG_PASSWORD`, never from a file.

## 1. Install

```sh
brew install postgresql@17   # server + contrib modules (incl. pgstattuple)
brew install pgvector        # the `vector` extension, built for the installed server
brew install pg_repack       # optional: online bloat reclamation (used later)
```

`pgvector` and `pg_repack` must be built for the **same** Postgres major version
as the running server. If you upgrade the server major version, reinstall both.

## 2. Run it under launchd

```sh
brew services start postgresql@17
```

`brew services` registers a `launchd` agent, so the server starts at login and
stays up. It listens on `localhost:5432` and a local unix-domain socket;
Homebrew initializes the cluster with your OS user as a superuser, so socket
connections authenticate by peer with no password.

Confirm it is up:

```sh
pg_isready            # "/tmp:5432 - accepting connections" (or your socket dir)
```

## 3. Create the SAGE database

```sh
createdb sage
```

## 4. Enable extensions + create the schema

The bootstrap is idempotent — it enables the extensions and creates every table
and index, and is safe to re-run:

```sh
# Reads the postgres block from sage/config.yaml (socket, database `sage`),
# enables `vector` + `pgstattuple`, and creates the graph + content schema:
.venv/bin/python scripts/bootstrap_postgres.py
```

To also enable `pg_repack` (the online-repack capability a later content-store
adapter uses), pass the full extension list:

```sh
.venv/bin/python scripts/bootstrap_postgres.py \
    --extensions vector,pgstattuple,pg_repack
```

Verify:

```sh
psql sage -c '\dx'   # vector, pgstattuple (and pg_repack if enabled)
psql sage -c '\dt'   # documents, edges, staging_edges, users, document_tags, chunks
```

## 5. A throwaway database for the test harness

The storage test suite runs against a real Postgres named by `SAGE_TEST_PG_DSN`;
when it is unset the storage tests skip. Point it at a **separate** throwaway
database — never the working `sage` database — so the harness's disposable
`sage_test_*` schemas never touch live data:

```sh
createdb sage_test
.venv/bin/python scripts/bootstrap_postgres.py --dsn "postgresql:///sage_test"

export SAGE_TEST_PG_DSN="postgresql:///sage_test"   # socket form (peer auth)
.venv/bin/pytest tests/sage -k postgres -v          # all pass, none skipped
```

The harness creates and drops its own `sage_test_*` schemas inside that database;
the one-time bootstrap above is only needed to enable the `vector` extension in
the throwaway database (extensions are database-global; schemas are not).

## 6. Storage backend selection, cutover, and rollback

The `storage_backend` key in `sage/config.yaml` selects the durable-storage
binding as one co-varying switch: `postgres` (the default) binds the graph and
content stores to the Postgres adapters over the `postgres:` block above;
`embedded` selects the file-based fallback pair (SQLite graph store + LanceDB
content store under each vault's brain root). The binding is resolved once at
startup — flipping the key takes effect at the next server restart and never
moves data by itself.

Under the Postgres binding, each vault's rows live in a schema named by its
vault id, bootstrapped idempotently at vault open. The vault id must therefore
be a valid schema identifier (`^[a-z_][a-z0-9_]*$`); a vault whose id is not
fails loud at startup and is skipped. Each vault opens its own connection pool
(`min_pool_size`/`max_pool_size` from the `postgres:` block), so N vaults hold
at least N idle connections and up to N×`max_pool_size` under load — comfortable
against the server's default `max_connections = 100` for a handful of vaults,
worth revisiting past a dozen.

**Cutover (embedded → postgres).** Order matters: copy the data before
repointing the binding.

```sh
# 1. Copy each vault's embedded state into its Postgres schema and reconcile.
#    Dry-run first; --execute writes. Exit 0 + "ok" per vault gates the flip.
.venv/bin/python scripts/migrate_vault_to_postgres.py --all-vaults
.venv/bin/python scripts/migrate_vault_to_postgres.py --all-vaults --execute

# 2. Set `storage_backend: postgres` in sage/config.yaml (the committed default).
# 3. Restart SAGE (and any MCP server processes).
```

The migration reads the embedded stores and never modifies them; they stay
intact under each vault's brain root as the rollback copy.

**Rollback (postgres → embedded).** Set `storage_backend: embedded` in
`sage/config.yaml` and restart. The vaults reopen from the untouched embedded
stores. The caveat is the divergence window: writes made while on Postgres do
**not** flow back — there is no reverse migration — so rolling back repoints to
the embedded state frozen at the last cutover migration. Re-running the
migration with `--execute` truncates and reloads the Postgres schemas from the
embedded stores, which re-converges the two in the forward direction only.

## Notes

- **Backups:** the durable Postgres state is dumped on a schedule and restored
  per `postgres-backup-restore.md`. The live data directory is excluded from Time
  Machine there; the `pg_dump` output is the authoritative backup.
- **Stop / restart:** `brew services stop postgresql@17` /
  `brew services restart postgresql@17`. Logs: `~/Library/LaunchAgents` plus the
  Homebrew log under `$(brew --prefix)/var/log`.
- **Data location:** the cluster lives under `$(brew --prefix)/var/postgresql@17`
  — outside this repository and outside cloud-synced directories, consistent with
  the database-file placement rule.
- **Hosted target:** the same schema bootstrap provisions a managed endpoint;
  there the connection is TCP with `sslmode=require` and a password from
  `$SAGE_PG_PASSWORD`. That cutover is out of scope here.
