"""Rehearsal admission and measurement tests exercise the shipped implementation."""

import importlib.util
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from sage.maintenance.postgres_migration import PostgresStore
from tests.sage.test_postgres_migration_roundtrip import databases as databases


def runtime() -> ModuleType:
    spec = importlib.util.find_spec("sage.maintenance.postgres_rehearsal")
    assert spec is not None, "full-size rehearsal runtime is missing"
    from sage.maintenance import postgres_rehearsal

    return postgres_rehearsal


@pytest.mark.parametrize("run_id", ["../sage", "", "A", "r" * 50, "r123;DROP DATABASE sage"])
def test_rehearsal_names_reject_untrusted_run_id(run_id: str) -> None:
    with pytest.raises(ValueError, match="run identity"):
        runtime().database_names("pg17", run_id, "sage")


def test_rehearsal_names_cannot_alias_serving_database() -> None:
    module = runtime()
    source, target = module.database_names("pg17", "r123", "sage")
    assert source != target and source != "sage" and target != "sage"
    with pytest.raises(ValueError, match="serving database"):
        module.database_names("pg17", "r123", source)


def test_measurements_refuse_incomplete_or_exhausted_evidence() -> None:
    module = runtime()
    good = dict(
        archive_bytes=1024,
        observed_peak_scratch_bytes=1024,
        scratch_initial_free_bytes=1024**3,
        scratch_min_free_bytes=900 * 1024**2,
        elapsed_seconds=30,
        job_elapsed_seconds=60,
        dump_seconds=5,
        restore_seconds=10,
        samples=5,
        monitor_error=False,
    )
    module.validate_measurements(good)
    for key, value in [
        ("archive_bytes", 0),
        ("samples", 0),
        ("scratch_min_free_bytes", 0),
        ("elapsed_seconds", 7201),
        ("job_elapsed_seconds", 7201),
        ("dump_seconds", 3001),
        ("restore_seconds", 3001),
        ("monitor_error", True),
    ]:
        with pytest.raises(ValueError, match="rehearsal measurements"):
            module.validate_measurements({**good, key: value})


def test_measurements_missing_fields_are_not_a_pass() -> None:
    with pytest.raises(ValueError, match="rehearsal measurements"):
        runtime().validate_measurements({})


def test_rehearsal_roundtrip_preserves_serving_database(
    databases: tuple[PostgresStore, PostgresStore, str], tmp_path: Path
) -> None:
    source, target, role = databases
    module = runtime()
    before = source.snapshot()
    grants = source.conn.execute(
        "SELECT datacl::text FROM pg_database WHERE datname=current_database()"
    ).fetchone()
    serving_pid = source.conn.info.backend_pid
    admin = module.RehearsalDatabases(source, target, "g17", "r123")
    try:
        report = module.execute_rehearsal(
            admin, tmp_path, image="test:fixed", job_started=module.time.monotonic()
        )
        assert report["status"] == "rehearsal_verified"
        assert report["measurements"]["archive_bytes"] > 0
        assert report["measurements"]["samples"] > 0
        assert report["source_major"] == 16 and report["target_major"] == 17
        assert source.snapshot() == before
        assert source.conn.info.backend_pid == serving_pid
        assert (
            source.conn.execute(
                "SELECT datacl::text FROM pg_database WHERE datname=current_database()"
            ).fetchone()
            == grants
        )
        assert target.snapshot()["tables"] == {}
        with pytest.raises(ValueError, match="already exists"):
            module.execute_rehearsal(
                admin, tmp_path, image="test:fixed", job_started=module.time.monotonic()
            )
    finally:
        admin.cleanup()
    for store, name in zip((source, target), admin.names, strict=True):
        assert (
            store.conn.execute("SELECT 1 FROM pg_database WHERE datname=%s", (name,)).fetchone()
            is None
        )


def test_cleanup_refuses_foreign_database_and_live_session(
    databases: tuple[PostgresStore, PostgresStore, str],
) -> None:
    import psycopg
    from psycopg import sql
    from psycopg.conninfo import make_conninfo

    source, target, _ = databases
    module = runtime()
    admin = module.RehearsalDatabases(source, target, "g17", "r456")
    admin.create()
    name = admin.names[0]
    connection = psycopg.connect(make_conninfo(source.conninfo, dbname=name), autocommit=True)
    try:
        with pytest.raises(ValueError, match="active rehearsal connections"):
            admin.cleanup()
    finally:
        connection.close()
    source.conn.execute(
        sql.SQL("COMMENT ON DATABASE {} IS {}").format(sql.Identifier(name), sql.Literal("foreign"))
    )
    try:
        with pytest.raises(ValueError, match="ownership"):
            admin.cleanup()
        assert target.conn.execute(
            "SELECT 1 FROM pg_database WHERE datname=%s", (admin.names[1],)
        ).fetchone()
    finally:
        source.conn.execute(
            sql.SQL("COMMENT ON DATABASE {} IS {}").format(
                sql.Identifier(name), sql.Literal(admin.marker)
            )
        )
        admin.cleanup()


@pytest.mark.parametrize("failure", ["disk", "timeout"])
def test_failed_seed_preserves_serving_state_and_is_cleanable(
    databases: tuple[PostgresStore, PostgresStore, str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    import errno
    import subprocess

    from sage.maintenance import postgres_migration

    source, target, _ = databases
    module = runtime()
    before = source.snapshot()
    admin = module.RehearsalDatabases(source, target, "g17", "r789")
    original = subprocess.run

    def fail_command(argv: list[str], **kwargs: Any) -> Any:
        if Path(argv[0]).name == "pg_dump":
            if failure == "disk":
                raise OSError(errno.ENOSPC, "disposable disk full")
            raise subprocess.TimeoutExpired(argv, 3000)
        return original(argv, **kwargs)

    monkeypatch.setattr(postgres_migration.subprocess, "run", fail_command)
    try:
        with pytest.raises((OSError, subprocess.TimeoutExpired)):
            module.execute_rehearsal(
                admin, tmp_path, image="test:fixed", job_started=module.time.monotonic()
            )
        assert source.snapshot() == before
        assert target.snapshot()["tables"] == {}
    finally:
        admin.cleanup()


def test_monitor_failure_cannot_be_reported_as_headroom(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = runtime()
    meter = module.Measurements(tmp_path)

    def unavailable(_: Path) -> Any:
        raise OSError("unavailable scratch statistics")

    monkeypatch.setattr(module.shutil, "disk_usage", unavailable)
    meter.sample()
    assert meter.error and meter.samples == 0


def test_measurements_do_not_treat_host_free_space_as_replica_quota() -> None:
    report = dict(
        archive_bytes=4 * 1024**3,
        observed_peak_scratch_bytes=4 * 1024**3,
        scratch_initial_free_bytes=100 * 1024**3,
        scratch_min_free_bytes=96 * 1024**3,
        elapsed_seconds=30,
        job_elapsed_seconds=60,
        dump_seconds=5,
        restore_seconds=10,
        samples=5,
        monitor_error=False,
    )
    with pytest.raises(ValueError, match="resource bounds"):
        runtime().validate_measurements(report)
