# Local Postgres runtime for SAGE

SAGE's durable graph and content state externalizes to PostgreSQL in both
deployment targets (CAS-ADR-042). This runbook stands up the **local** target on
macOS (Apple Silicon, Homebrew): a persistent Postgres instance under `launchd`,
the extensions SAGE needs, and the schema bootstrap. The embedded SQLite/LanceDB
stores remain the live local binding for now — this provisions the engine; it
does not switch SAGE onto it.

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
