"""Per-process throwaway-database isolation for the Postgres storage test harness.

Concurrent pytest processes that share one ``SAGE_TEST_PG_DSN`` database collide:
every test provisions its vault schema under the fixed id ``test_vault`` (the
Postgres binding names each vault's schema by its vault id), and an autouse
fixture drops that schema after each test, so a second process's
``DROP SCHEMA "test_vault" CASCADE`` lands mid-test in the first. The fix
reinterprets ``SAGE_TEST_PG_DSN`` as naming a *maintenance* database on a server
and derives a per-process throwaway database on that server, so concurrent runs
share nothing.

These helpers are test-only. Database-level DDL deliberately lives here rather
than in ``sage.storage.postgres.schema``, which documents an invariant that no
code path there can emit ``DROP DATABASE`` (production never creates throwaway
databases). Identifier safety reuses ``validate_schema_name`` from that module.
"""

from __future__ import annotations

import math
import os
import re
import time
from dataclasses import dataclass

import psycopg
from psycopg.conninfo import conninfo_to_dict, make_conninfo

from sage.storage.postgres.schema import (
    DEFAULT_EXTENSIONS,
    validate_extension,
    validate_schema_name,
)

# Per-process throwaway databases carry this prefix; the guard refuses to create
# or drop anything without it, and the orphan sweep matches on it.
DISPOSABLE_DATABASE_PREFIX = "sage_test_db_"

# A throwaway younger than this is never swept: a sibling pytest process holds
# no connection to its database between creating it and its first storage
# test, so liveness alone cannot tell a fresh sibling from a crashed run's
# leftover. Longer than any run of the suite; short enough that orphans are
# reclaimed by the next day's first run.
ORPHAN_MIN_AGE_SECONDS = 3600.0

# Names embed their creation epoch: ``sage_test_db_<10-digit epoch>_<hex>``.
# Names without the epoch segment predate it and read as infinitely old.
_EPOCH_NAME_RE = re.compile(rf"^{DISPOSABLE_DATABASE_PREFIX}(\d{{10}})_[0-9a-f]{{8}}$")

# Databases the guard refuses outright regardless of the caller-supplied
# maintenance name -- the cluster template databases and the default working db.
_NEVER_DISPOSABLE = frozenset({"postgres", "template0", "template1"})


def assert_disposable_database(name: str, *, maintenance_db: str | None = None) -> str:
    """Return ``name`` if it is a disposable throwaway database, else raise.

    A disposable target is a valid lowercase identifier that carries the
    ``sage_test_db_`` prefix, has a non-empty suffix after it, is not a cluster
    template or the default ``postgres`` database, and is not the maintenance
    database the DSN names (passed as ``maintenance_db``). The guard makes it
    impossible to point ``CREATE``/``DROP DATABASE`` at the working database.
    """
    validate_schema_name(name)  # lowercase-identifier allowlist; blocks injection
    if (
        name in _NEVER_DISPOSABLE
        or not name.startswith(DISPOSABLE_DATABASE_PREFIX)
        or len(name) <= len(DISPOSABLE_DATABASE_PREFIX)
        or (maintenance_db is not None and name == maintenance_db)
    ):
        raise ValueError(
            f"refusing to treat database {name!r} as disposable: a disposable "
            f"target must carry the {DISPOSABLE_DATABASE_PREFIX!r} prefix with a "
            "non-empty suffix, must not be a template/postgres database, and must "
            "not be the maintenance database the DSN names"
        )
    return name


def derive_throwaway_dbname(now: float | None = None) -> str:
    """A fresh per-process throwaway database name, stamped with its epoch.

    ``now`` overrides the embedded creation time (tests craft old names with
    it); the random suffix keeps concurrent processes from colliding.
    """
    epoch = int(time.time() if now is None else now)
    return f"{DISPOSABLE_DATABASE_PREFIX}{epoch:010d}_{os.urandom(4).hex()}"


def database_age_seconds(name: str, now: float | None = None) -> float:
    """Seconds since the throwaway ``name`` was created, from its embedded epoch.

    A disposable name without an epoch segment is from before names carried
    one; it is reported as infinitely old so the sweep still reclaims it.
    Raises ``ValueError`` for a name that is not disposable at all.
    """
    assert_disposable_database(name)
    match = _EPOCH_NAME_RE.match(name)
    if match is None:
        return math.inf
    current = time.time() if now is None else now
    return current - int(match.group(1))


def rewrite_dsn_dbname(dsn: str, dbname: str) -> str:
    """Return ``dsn`` retargeted at ``dbname``, preserving every other field.

    Parses through libpq's own conninfo machinery so host, port, user, password,
    sslmode, and socket-form DSNs (no host/user) all round-trip unchanged and
    only the database name moves. Mirrors ``_conninfo_for_db`` in
    ``test_backup_postgres_roundtrip``.
    """
    parsed = conninfo_to_dict(dsn)
    parsed["dbname"] = dbname
    return make_conninfo(**parsed)


def dbname_of(dsn: str) -> str | None:
    """The database name a DSN targets, or ``None`` when it names none."""
    return conninfo_to_dict(dsn).get("dbname")


def create_database(
    maintenance_dsn: str, dbname: str, *, maintenance_db: str | None = None
) -> None:
    """Create the throwaway database over an autocommit maintenance connection.

    ``CREATE DATABASE`` cannot run inside a transaction, so the connection is
    autocommit. ``dbname`` is guarded first; it cannot be a bind parameter, so
    the guard's identifier allowlist is what keeps it safe to interpolate.
    """
    assert_disposable_database(dbname, maintenance_db=maintenance_db)
    with psycopg.connect(maintenance_dsn, autocommit=True) as conn:
        conn.execute(f'CREATE DATABASE "{dbname}"')  # noqa: S608 -- guarded identifier


def drop_database(
    maintenance_dsn: str,
    dbname: str,
    *,
    force: bool = True,
    maintenance_db: str | None = None,
) -> None:
    """Drop the throwaway database over an autocommit maintenance connection.

    ``force`` adds ``WITH (FORCE)`` to terminate the owning process's lingering
    pool connections at teardown. The orphan sweep passes ``force=False`` so a
    concurrent process's live database (which still has backends) is never
    force-dropped.
    """
    assert_disposable_database(dbname, maintenance_db=maintenance_db)
    suffix = " WITH (FORCE)" if force else ""
    with psycopg.connect(maintenance_dsn, autocommit=True) as conn:
        conn.execute(f'DROP DATABASE IF EXISTS "{dbname}"{suffix}')  # noqa: S608 -- guarded


def sweep_orphan_databases(
    maintenance_dsn: str, *, min_age_seconds: float = ORPHAN_MIN_AGE_SECONDS
) -> list[str]:
    """Drop idle, old ``sage_test_db_*`` databases left by crashed prior runs.

    Two conditions guard a concurrent process's throwaway. Liveness: only
    databases with **zero** backends in ``pg_stat_activity`` are candidates, and
    the drop is a plain ``DROP DATABASE IF EXISTS`` with no ``FORCE``, so a
    sibling holding pool connections is untouched. Age: a candidate younger
    than ``min_age_seconds`` is skipped, because a sibling that has just
    created its database holds no connection to it until its first storage
    test -- liveness alone would drop it. The maintenance database itself never
    matches the prefix guard. Per-database errors are swallowed (another
    process may drop the same orphan first); returns the names actually dropped.
    """
    maintenance_db = dbname_of(maintenance_dsn)
    dropped: list[str] = []
    with psycopg.connect(maintenance_dsn, autocommit=True) as conn:
        rows = conn.execute(
            "SELECT d.datname FROM pg_database d "
            "WHERE d.datname LIKE %s "
            "AND NOT EXISTS ("
            "  SELECT 1 FROM pg_stat_activity a WHERE a.datname = d.datname"
            ")",
            (DISPOSABLE_DATABASE_PREFIX + "%",),
        ).fetchall()
        for (name,) in rows:
            try:
                assert_disposable_database(name, maintenance_db=maintenance_db)
            except ValueError:
                continue
            if database_age_seconds(name) < min_age_seconds:
                continue
            try:
                conn.execute(f'DROP DATABASE IF EXISTS "{name}"')  # noqa: S608 -- guarded
                dropped.append(name)
            except psycopg.Error:
                # A racing concurrent sweep or a backend that connected between
                # the scan and the drop -- leave it for the next sweep.
                continue
    return dropped


def create_extensions(dsn: str, extensions=DEFAULT_EXTENSIONS) -> None:
    """Install the storage extensions in a freshly-created database.

    A database made with ``CREATE DATABASE`` inherits ``template1`` and so
    carries no extensions; the content store's ``vector`` column and the bloat
    tooling's ``pgstattuple`` must be created per-database. This mirrors what
    ``scripts/bootstrap_postgres.py`` does for the shared local database, run
    once per throwaway. Idempotent (``IF NOT EXISTS``).
    """
    with psycopg.connect(dsn, autocommit=True) as conn:
        for ext in extensions:
            validate_extension(ext)
            conn.execute(f'CREATE EXTENSION IF NOT EXISTS "{ext}"')  # noqa: S608 -- guarded


@dataclass(frozen=True)
class IsolatedTestDB:
    """Handles the session provisioner yields for tests that need the server.

    ``maintenance_dsn`` names the server + the working database the harness
    connects to for ``CREATE``/``DROP DATABASE``; ``throwaway_dsn`` is that DSN
    retargeted at this process's private database, which the provisioner keeps
    a connection open to for the whole session.
    """

    maintenance_dsn: str
    maintenance_dbname: str | None
    throwaway_dsn: str
    throwaway_dbname: str
    #: Backend pid of the connection the provisioner holds on the throwaway for
    #: the whole session, so the database is never backend-less; ``None`` when
    #: the provisioner did not open one.
    keepalive_backend_pid: int | None = None
