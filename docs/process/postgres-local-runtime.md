# Local Postgres runtime for SAGE

SAGE's durable graph and content state externalizes to PostgreSQL in both
deployment targets (CAS-ADR-042). This runbook stands up the **local** target on
macOS (Apple Silicon, Homebrew): a persistent Postgres instance under `launchd`,
the extensions SAGE needs, and the schema bootstrap. Postgres is the storage
port's sole binding (`storage_backend: postgres` in `sage/config.yaml`); §6
covers the schema bootstrap's idempotence and per-vault connection footprint.

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

The whole suite requires a real Postgres named by `SAGE_TEST_PG_DSN` -- storage
is the storage port's sole binding, so every uninjected service construction
(not just the dedicated storage tests) opens it. When the variable is unset,
the root conftest pins storage to an unresolvable sentinel host instead of
falling back to the local `sage` socket, so an accidental open fails fast
rather than touching live data; most of the suite then fails outright instead
of skipping. Point the variable at a **separate** throwaway database — never
the working `sage` database — so the harness's disposable `sage_test_*`
schemas and its per-test vault schemas never touch live data:

```sh
createdb sage_test
.venv/bin/python scripts/bootstrap_postgres.py --dsn "postgresql:///sage_test"

export SAGE_TEST_PG_DSN="postgresql:///sage_test"   # socket form (peer auth)
.venv/bin/pytest tests/                             # full suite, all green
```

The harness creates and drops its own `sage_test_*` schemas inside that database;
the one-time bootstrap above is only needed to enable the `vector` extension in
the throwaway database (extensions are database-global; schemas are not).

## 6. Storage binding and per-vault connection footprint

The `storage_backend` key in `sage/config.yaml` is the storage port's sole
binding: `postgres` (the only value) binds the graph and content stores to
the Postgres adapters over the `postgres:` block above. A config still
carrying a retired value (e.g. `embedded`) fails loud at startup.

Each vault's rows live in a schema named by its vault id, bootstrapped
idempotently at vault open. The vault id must therefore be a valid schema
identifier (`^[a-z_][a-z0-9_]*$`); a vault whose id is not fails loud at
startup and is skipped. Each vault opens its own connection pool
(`min_pool_size`/`max_pool_size` from the `postgres:` block), so N vaults hold
at least N idle connections and up to N×`max_pool_size` under load — comfortable
against the server's default `max_connections = 100` for a handful of vaults,
worth revisiting past a dozen.

There is no config-flip rollback path to a file-based fallback: Postgres has
carried all live load since the 2026-06-10 cutover, and the file-based
fallback binding was retired once that track record was established.
Recovering from a bad Postgres state is a restore from the scheduled backup
(`postgres-backup-restore.md`), not a binding flip.

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
