"""Per-process throwaway-database isolation for the storage test harness.

Concurrent pytest processes sharing one ``SAGE_TEST_PG_DSN`` database destroy
each other: every test provisions its vault schema under the fixed id
``test_vault``, and the autouse ``_drop_leaked_vault_schemas`` fixture drops
that schema after each test, so a second process's drop lands mid-test in the
first. The harness gives each process its own throwaway *database* on the
server the DSN names; these tests cover the guard, the DSN rewrite, and the
provisioning fixture that makes concurrent runs share nothing.

The guard and rewrite tests (``guard`` / ``rewrite`` in the name) need no
server. The rest require ``SAGE_TEST_PG_DSN`` and skip without it.
"""

from __future__ import annotations

import math
import re
import time

import psycopg
import pytest
from psycopg.conninfo import conninfo_to_dict

from tests.helpers.pg_isolation import (
    DISPOSABLE_DATABASE_PREFIX,
    ORPHAN_MIN_AGE_SECONDS,
    assert_disposable_database,
    database_age_seconds,
    derive_throwaway_dbname,
    rewrite_dsn_dbname,
    sweep_orphan_databases,
)

# ---------------------------------------------------------------------------
# A. Guard unit tests (no server)
# ---------------------------------------------------------------------------


def test_guard_accepts_prefixed_throwaway():
    name = "sage_test_db_ab12cd34"
    assert assert_disposable_database(name) == name


@pytest.mark.parametrize(
    "name",
    [
        "postgres",  # the CI maintenance db -- must never be a drop target
        "template0",
        "template1",
        "sage",  # a plausible working db
        "sage_test_db",  # missing the trailing prefix underscore
        "sage_test_db_",  # prefix present but no random suffix
        "sage_test",  # a plausible maintenance db, wrong prefix
        "sage_test_db_x; DROP DATABASE postgres",  # not a bare identifier
        "SAGE_TEST_DB_ABC",  # uppercase fails the lowercase identifier rule
        "",
    ],
)
def test_guard_rejects_non_disposable(name):
    with pytest.raises(ValueError):
        assert_disposable_database(name)


def test_guard_rejects_the_maintenance_db_even_when_prefixed():
    # Degenerate case: the DSN itself already names a sage_test_db_* database.
    # The guard must still refuse to treat the maintenance db as a drop target.
    prefixed_maintenance = "sage_test_db_main0000"
    with pytest.raises(ValueError):
        assert_disposable_database(prefixed_maintenance, maintenance_db=prefixed_maintenance)


def test_derive_throwaway_dbname_is_prefixed_and_disposable():
    name = derive_throwaway_dbname()
    assert name.startswith(DISPOSABLE_DATABASE_PREFIX)
    assert len(name) > len(DISPOSABLE_DATABASE_PREFIX)
    # A derived name must pass its own guard.
    assert assert_disposable_database(name) == name


def test_derive_throwaway_dbname_varies():
    assert derive_throwaway_dbname() != derive_throwaway_dbname()


def test_derive_throwaway_dbname_embeds_its_creation_epoch():
    """PGI-a: the name carries a creation timestamp so the orphan sweep can
    tell a crashed run's leftover from a sibling process's fresh database."""
    before = int(time.time())
    name = derive_throwaway_dbname()
    match = re.fullmatch(r"sage_test_db_(\d{10})_[0-9a-f]{8}", name)
    assert match, name
    assert abs(int(match.group(1)) - before) <= 5


@pytest.mark.parametrize(
    ("name", "now", "expected"),
    [
        ("sage_test_db_1700000100_deadbeef", 1700000200, 100),
        ("sage_test_db_deadbeef", 1700000200, math.inf),  # legacy: no epoch, always sweepable
    ],
)
def test_database_age_seconds(name, now, expected):
    """PGI-b: age from the embedded epoch; legacy names read as infinitely old."""
    assert database_age_seconds(name, now=now) == expected


def test_database_age_seconds_rejects_non_disposable_names():
    with pytest.raises(ValueError):
        database_age_seconds("postgres", now=0)


# ---------------------------------------------------------------------------
# B. DSN-rewrite unit tests (no server)
# ---------------------------------------------------------------------------


def test_rewrite_url_form_swaps_dbname_preserving_all_other_fields():
    src = "postgresql://u:p@h:5432/sage_test"
    out = rewrite_dsn_dbname(src, "sage_test_db_x")

    before = conninfo_to_dict(src)
    after = conninfo_to_dict(out)
    assert after["dbname"] == "sage_test_db_x"
    assert after["dbname"] != before["dbname"]
    # Every other field must survive byte-for-byte -- a naive str.replace on the
    # dbname substring could corrupt host/user/password.
    for field in ("user", "password", "host", "port"):
        assert after[field] == before[field]


def test_rewrite_socket_form_swaps_dbname():
    # The documented local DSN carries no host/user/password (peer auth).
    src = "postgresql:///sage_test"
    out = rewrite_dsn_dbname(src, "sage_test_db_y")

    after = conninfo_to_dict(out)
    assert after["dbname"] == "sage_test_db_y"
    # No host/user/password were present; the rewrite must not invent them.
    assert "host" not in after or after.get("host") in (None, "")


# ---------------------------------------------------------------------------
# Server-gated fixtures/tests (need SAGE_TEST_PG_DSN)
# ---------------------------------------------------------------------------


@pytest.fixture
def isolated_db(_provision_isolated_test_database):
    """The IsolatedTestDB the session provisioner yielded, or skip.

    ``_provision_isolated_test_database`` yields ``None`` when
    ``SAGE_TEST_PG_DSN`` is unset; the server-gated tests skip in that case.
    """
    info = _provision_isolated_test_database
    if info is None:
        pytest.skip("set SAGE_TEST_PG_DSN to run the isolation integration tests")
    return info


# ---------------------------------------------------------------------------
# C. Provisioner-prong isolation
# ---------------------------------------------------------------------------


def test_stack_config_binds_to_the_throwaway_database(isolated_db):
    """The provisioner prong (StackPostgresConfig.database), not just the raw-DSN
    fixtures, points at the throwaway db. A one-prong fix fails here."""
    from tests.conftest import _test_stack_config

    cfg, _password = _test_stack_config()
    assert cfg.postgres.database == isolated_db.throwaway_dbname
    assert cfg.postgres.database != isolated_db.maintenance_dbname


# ---------------------------------------------------------------------------
# D. Orphan-sweep safety
# ---------------------------------------------------------------------------


def test_sweep_drops_idle_orphan_but_spares_a_live_sibling(isolated_db):
    """The sweep drops a zero-connection sage_test_db_* orphan, but a database
    with a live backend (a concurrent process's throwaway) must survive -- the
    whole point of the cross-process fix."""
    maintenance = isolated_db.maintenance_dsn
    # Both old enough to be sweep candidates, so liveness alone spares `live`.
    old = time.time() - 2 * ORPHAN_MIN_AGE_SECONDS
    idle = derive_throwaway_dbname(now=old)
    live = derive_throwaway_dbname(now=old)

    admin = psycopg.connect(maintenance, autocommit=True)
    try:
        admin.execute(f'CREATE DATABASE "{idle}"')
        admin.execute(f'CREATE DATABASE "{live}"')
    finally:
        admin.close()

    # Hold an open connection to `live` so it looks like a concurrent process.
    live_conn = psycopg.connect(rewrite_dsn_dbname(maintenance, live))
    try:
        dropped = sweep_orphan_databases(maintenance)
        assert idle in dropped
        assert live not in dropped
        assert _database_exists(maintenance, live)
        assert not _database_exists(maintenance, idle)
    finally:
        live_conn.close()
        _force_drop(maintenance, live)
        _force_drop(maintenance, idle)  # no-op if the sweep already dropped it


def test_sweep_spares_a_young_idle_sibling(isolated_db):
    """PGI-c: a freshly created database has no backend yet (the provisioner
    connects lazily), so a sibling pytest process's sweep must not drop it.
    Age, not liveness, is what protects it; an old idle one is still reclaimed."""
    maintenance = isolated_db.maintenance_dsn
    young = derive_throwaway_dbname()
    old = derive_throwaway_dbname(now=time.time() - 2 * ORPHAN_MIN_AGE_SECONDS)

    admin = psycopg.connect(maintenance, autocommit=True)
    try:
        admin.execute(f'CREATE DATABASE "{young}"')
        admin.execute(f'CREATE DATABASE "{old}"')
    finally:
        admin.close()

    try:
        dropped = sweep_orphan_databases(maintenance)
        assert old in dropped
        assert young not in dropped
        assert _database_exists(maintenance, young)
        assert not _database_exists(maintenance, old)
    finally:
        _force_drop(maintenance, young)
        _force_drop(maintenance, old)


# ---------------------------------------------------------------------------
# E. Database independence
# ---------------------------------------------------------------------------


def test_work_lands_in_throwaway_not_maintenance(isolated_db):
    """A schema created against the isolated (rewritten) DSN does not appear in
    the maintenance db -- proving the two are distinct databases and the shared
    db is untouched."""
    throwaway = isolated_db.throwaway_dsn
    maintenance = isolated_db.maintenance_dsn

    conn = psycopg.connect(throwaway, autocommit=True)
    try:
        conn.execute('CREATE SCHEMA IF NOT EXISTS "test_vault"')
        assert _schema_exists(throwaway, "test_vault")
        assert not _schema_exists(maintenance, "test_vault")
    finally:
        conn.execute('DROP SCHEMA IF EXISTS "test_vault" CASCADE')
        conn.close()


# ---------------------------------------------------------------------------
# Small server probes
# ---------------------------------------------------------------------------


def _database_exists(maintenance_dsn: str, dbname: str) -> bool:
    with psycopg.connect(maintenance_dsn, autocommit=True) as conn:
        row = conn.execute("SELECT 1 FROM pg_database WHERE datname = %s", (dbname,)).fetchone()
    return row is not None


def _schema_exists(dsn: str, schema: str) -> bool:
    with psycopg.connect(dsn, autocommit=True) as conn:
        row = conn.execute(
            "SELECT 1 FROM information_schema.schemata WHERE schema_name = %s", (schema,)
        ).fetchone()
    return row is not None


def _force_drop(maintenance_dsn: str, dbname: str) -> None:
    with psycopg.connect(maintenance_dsn, autocommit=True) as conn:
        conn.execute(f'DROP DATABASE IF EXISTS "{dbname}" WITH (FORCE)')
