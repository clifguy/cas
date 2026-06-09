#!/usr/bin/env python3
"""Back up the local SAGE Postgres database into a file-based folder.

SAGE's durable state externalizes to PostgreSQL (CAS-ADR-042). Time Machine
backs up *files*, so once the canonical corpus lives in Postgres it is no longer
recoverable by copying files -- a consistent dump file is. This runner produces
that dump: ``pg_dump -Fc`` of the SAGE database (custom format -- selective and
``pg_restore``-able) plus ``pg_dumpall --globals-only`` (cluster roles and
tablespaces), both written into a Time-Machine-covered folder, then prunes runs
beyond a retention count. ``pg_dump`` takes an MVCC-consistent snapshot, so it is
safe to run during active ingest.

Scheduled by a launchd LaunchAgent (see
``scripts/launchd/local.cas.sage.postgres-backup.plist`` and the runbook
``docs/process/postgres-backup-restore.md``). The connection is resolved from the
``postgres`` block of ``sage/config.yaml`` (the on-box default is the local unix
socket as the OS user); pass ``--dsn`` to target an explicit Postgres.

Examples::

    # Dump the local SAGE database into the default folder (~/sage_vaults_backups):
    .venv/bin/python scripts/backup_postgres.py

    # Dump a specific database into a chosen folder, keeping 30 runs:
    .venv/bin/python scripts/backup_postgres.py \\
        --dsn postgresql:///sage_test --dir /tmp/pgbk --retain 30
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from collections.abc import Callable, Iterable
from datetime import datetime, timezone
from pathlib import Path

# Default number of dump runs to retain; older runs are pruned each backup.
_DEFAULT_RETAIN = 14

# Timestamp form embedded in dump filenames: lexically sortable == chronological.
_STAMP_FORMAT = "%Y%m%dT%H%M%SZ"

# Matches a backup file produced by this runner, capturing the run timestamp.
# Anchored so unrelated files in the backup folder are never selected for prune.
_BACKUP_NAME_RE = re.compile(r"^(?:sage|globals)-(\d{8}T\d{6}Z)\.(?:dump|sql)$")


def default_backup_dir() -> Path:
    """The default backup folder: ``~/sage_vaults_backups``.

    Under ``$HOME`` (Time-Machine-covered by default) and a sibling of the vault
    tree (``~/sage_vaults``); outside the repository so dumps are never committed.
    """
    return Path.home() / "sage_vaults_backups"


def dump_filenames(ts: datetime) -> tuple[str, str]:
    """Return the (database-dump, globals) filenames for a run at ``ts``.

    Names embed a ``YYYYMMDDTHHMMSSZ`` stamp so distinct runs never collide and
    sort chronologically by name.
    """
    stamp = ts.strftime(_STAMP_FORMAT)
    return f"sage-{stamp}.dump", f"globals-{stamp}.sql"


def build_dump_argv(conninfo: str, out_file: Path) -> list[str]:
    """Argv for ``pg_dump`` of one database in custom format.

    ``-Fc`` is the custom archive format ``pg_restore`` consumes; it is what
    makes the dump selective and layout-independent.
    """
    return ["pg_dump", "-Fc", "-d", conninfo, "-f", str(out_file)]


def build_globals_argv(conninfo: str, out_file: Path) -> list[str]:
    """Argv for ``pg_dumpall --globals-only`` (roles + tablespaces, plain SQL)."""
    return ["pg_dumpall", "--globals-only", "-d", conninfo, "-f", str(out_file)]


def resolve_conninfo(dsn: str | None) -> str:
    """Resolve the libpq conninfo to dump.

    ``--dsn`` passes through verbatim; otherwise the conninfo is composed from
    the stack-config ``postgres`` block, mirroring ``bootstrap_postgres.py`` (host
    null -> local socket, user null -> OS user via peer auth).
    """
    if dsn is not None:
        return dsn

    from psycopg.conninfo import make_conninfo

    from sage.mcp_init import load_stack_config_or_default
    from sage.storage.postgres.pool import PostgresConnectionParams, build_conn_kwargs

    pg = load_stack_config_or_default().postgres
    params = PostgresConnectionParams(
        host=pg.host,
        port=pg.port,
        database=pg.database,
        user=pg.user,
        sslmode=pg.sslmode,
    )
    return make_conninfo(**build_conn_kwargs(params))


def select_prunable(files: Iterable[Path], retain: int) -> list[Path]:
    """Return the backup files belonging to runs older than the ``retain`` newest.

    Files are grouped by their embedded run timestamp; the ``retain`` newest
    timestamps are kept and every file of any older run is returned for pruning.
    Files that do not match the backup naming are ignored entirely -- never
    returned -- so an unrelated file in the folder is never deleted.
    """
    by_stamp: dict[str, list[Path]] = {}
    for path in files:
        match = _BACKUP_NAME_RE.match(path.name)
        if match is None:
            continue
        by_stamp.setdefault(match.group(1), []).append(path)

    keep_stamps = sorted(by_stamp, reverse=True)[:retain]
    prunable: list[Path] = []
    for stamp, paths in by_stamp.items():
        if stamp not in keep_stamps:
            prunable.extend(paths)
    return prunable


def run_backup(
    directory: Path,
    conninfo: str,
    *,
    timestamp: datetime | None = None,
    retain: int = _DEFAULT_RETAIN,
    runner: Callable[..., object] = subprocess.run,
) -> tuple[Path, Path, list[Path]]:
    """Dump the database + globals into ``directory`` and prune old runs.

    Returns the (database-dump path, globals path, pruned paths). ``runner`` is
    injectable for testing; it is invoked with ``check=True`` so a failing dump
    aborts loudly rather than leaving a truncated archive.
    """
    directory.mkdir(parents=True, exist_ok=True)
    ts = timestamp or datetime.now(timezone.utc)
    db_name, globals_name = dump_filenames(ts)
    db_path = directory / db_name
    globals_path = directory / globals_name

    runner(build_dump_argv(conninfo, db_path), check=True)
    runner(build_globals_argv(conninfo, globals_path), check=True)

    pruned = select_prunable(sorted(directory.iterdir()), retain)
    for path in pruned:
        path.unlink()
    return db_path, globals_path, pruned


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Dump the SAGE Postgres database + globals into a backup folder.",
    )
    parser.add_argument(
        "--dir",
        default=None,
        help="Backup folder. Default: ~/sage_vaults_backups.",
    )
    parser.add_argument(
        "--dsn",
        default=None,
        help=(
            "Explicit Postgres conninfo/URL to dump. Default: composed from the "
            "stack-config postgres block (sage/config.yaml)."
        ),
    )
    parser.add_argument(
        "--retain",
        type=int,
        default=_DEFAULT_RETAIN,
        help=f"Number of dump runs to keep (default: {_DEFAULT_RETAIN}).",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    directory = Path(args.dir).expanduser() if args.dir else default_backup_dir()
    conninfo = resolve_conninfo(args.dsn)
    db_path, globals_path, pruned = run_backup(directory, conninfo, retain=args.retain)
    print(f"Wrote {db_path.name} and {globals_path.name} to {directory}.")
    if pruned:
        print(f"Pruned {len(pruned)} file(s) beyond retain={args.retain}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
