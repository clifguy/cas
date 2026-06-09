# Backing up and restoring the local SAGE Postgres database

SAGE's durable graph and content state externalizes to PostgreSQL (CAS-ADR-042).
Time Machine backs up *files*, so once the canonical corpus lives in Postgres it
is no longer recoverable by copying the data directory — a consistent dump is.
This runbook schedules that dump and documents the restore. It builds on the
runtime in `postgres-local-runtime.md` (install, launchd, schema bootstrap).

The split of responsibility:

- **Derived + curated Postgres state** → dumped here with `pg_dump`/`pg_dumpall`
  into a Time-Machine-covered folder. The dump is the authoritative backup.
- **Vault source files** (`~/sage_vaults/<id>/imports`, `sources`) → stay
  file-based and are the raw provenance; Time Machine covers them directly.

## What the backup does

`scripts/backup_postgres.py` writes two files per run into the backup folder:

- `sage-<stamp>.dump` — `pg_dump -Fc` of the SAGE database. The custom format is
  selective, layout-independent, and the input `pg_restore` consumes.
- `globals-<stamp>.sql` — `pg_dumpall --globals-only`: cluster roles and
  tablespaces (not per-database content), needed so a restored database has its
  owning role.

`<stamp>` is `YYYYMMDDTHHMMSSZ` (UTC), so runs never collide and sort
chronologically. `pg_dump` takes an MVCC-consistent snapshot, so a scheduled run
is **safe during active ingest**. The runner prunes runs beyond `--retain`
(default 14) and ignores any file that does not match the backup naming.

Manual run (also the smoke test):

```sh
.venv/bin/python scripts/backup_postgres.py            # default ~/sage_vaults_backups
.venv/bin/python scripts/backup_postgres.py --dir /tmp/pgbk --retain 30
```

## 1. Schedule it under launchd

The agent fires **thrice daily — 02:00, 12:00, 18:00 local**. The checked-in
plist `scripts/launchd/local.cas.sage.postgres-backup.plist` is a template with
`{{...}}` placeholders; substitute your host paths and install it:

```sh
REPO="$HOME/repos/cas"                  # your cas checkout
BACKUP_DIR="$HOME/sage_vaults_backups"  # Time-Machine-covered (see §3)
LOG_DIR="$HOME/Library/Logs/sage"
mkdir -p "$BACKUP_DIR" "$LOG_DIR"

sed -e "s|{{PYTHON}}|$REPO/.venv/bin/python|g" \
    -e "s|{{REPO}}|$REPO|g" \
    -e "s|{{BACKUP_DIR}}|$BACKUP_DIR|g" \
    -e "s|{{LOG_DIR}}|$LOG_DIR|g" \
    "$REPO/scripts/launchd/local.cas.sage.postgres-backup.plist" \
    > "$HOME/Library/LaunchAgents/local.cas.sage.postgres-backup.plist"

launchctl bootstrap gui/$(id -u) \
    "$HOME/Library/LaunchAgents/local.cas.sage.postgres-backup.plist"
launchctl enable gui/$(id -u)/local.cas.sage.postgres-backup
```

Run once now to confirm it works, then check the dump folder and logs:

```sh
launchctl kickstart -k gui/$(id -u)/local.cas.sage.postgres-backup
ls -la "$BACKUP_DIR"                    # a fresh sage-*.dump + globals-*.sql pair
cat "$LOG_DIR/sage-postgres-backup.err.log"
```

To remove or update the agent: `launchctl bootout gui/$(id -u)
"$HOME/Library/LaunchAgents/local.cas.sage.postgres-backup.plist"`, then re-run
the install block.

## 2. Exclude the live data directory from Time Machine

The live cluster (`$(brew --prefix)/var/postgresql@17`) must NOT be backed up by
Time Machine: copying open database files is unsafe and wasteful — the `pg_dump`
output is the correct backup. Add a fixed-path exclusion:

```sh
sudo tmutil addexclusion -p "$(brew --prefix)/var/postgresql@17"
tmutil isexcluded "$(brew --prefix)/var/postgresql@17"   # -> [Excluded]
```

## 3. Confirm the backup folder and vault tree ARE covered

The dump folder and the vault source tree must be *included* in Time Machine
(i.e. not excluded, and not under an excluded parent):

```sh
tmutil isexcluded "$HOME/sage_vaults_backups"   # -> [Included]
tmutil isexcluded "$HOME/sage_vaults"           # -> [Included]
```

If either reports `[Excluded]`, remove the exclusion with
`sudo tmutil removeexclusion -p <path>` (or adjust Time Machine Options in System
Settings).

## 4. Restore

Verified end-to-end against a scratch database. Reinstall the runtime first per
`postgres-local-runtime.md` §1–§2 (`brew install postgresql@17 pgvector`;
`brew services start postgresql@17`), then:

```sh
STAMP=<the run to restore, e.g. 20260608T020000Z>

# 1. Restore cluster globals (roles, tablespaces) into the fresh cluster.
psql -d postgres -f "$HOME/sage_vaults_backups/globals-$STAMP.sql"

# 2. Create the target database and restore its contents. The custom-format
#    archive recreates the schema, data, and the `vector`/`pgstattuple`
#    extensions (the matching extension packages must be installed first).
createdb sage
pg_restore -d sage "$HOME/sage_vaults_backups/sage-$STAMP.dump"

# 3. Sanity-check (bootstrap is idempotent — a belt-and-suspenders verify).
.venv/bin/python scripts/bootstrap_postgres.py
psql sage -c '\dt'
```

The restore is exercised automatically — a full dump → drop → `pg_restore` cycle
against a throwaway database — by:

```sh
SAGE_TEST_PG_DSN=postgresql:///sage_test \
    .venv/bin/pytest tests/sage/test_backup_postgres_roundtrip.py -v
```
