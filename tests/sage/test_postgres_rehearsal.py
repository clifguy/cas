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


@pytest.mark.parametrize("failure", ["measurement", "dump", "restore", "seed_dump"])
def test_main_retains_sanitized_failed_rehearsal_evidence(
    databases: tuple[PostgresStore, PostgresStore, str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    failure: str,
) -> None:
    import errno
    import json
    import subprocess
    from types import SimpleNamespace

    from psycopg.conninfo import conninfo_to_dict

    from sage.storage.postgres import managed_identity

    module = runtime()
    source, target, role = databases
    target.conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
    target.conn.execute("CREATE EXTENSION IF NOT EXISTS pgstattuple")
    source.assert_extensions_match(target)
    admin = module.RehearsalDatabases(source, target, "g17", "r9001")
    original_command = PostgresStore._command
    original_validate = module.validate_measurements
    secret = "PRIVATE_DATABASE_CONTENT_DO_NOT_LOG"

    def connect(conninfo: str, roles: tuple[str, ...], **kwargs: Any) -> PostgresStore:
        host = conninfo_to_dict(conninfo)["host"]
        if host in ("source", "target"):
            store = source if host == "source" else target
            return PostgresStore(store.conninfo, store.app_roles, password=store.password)
        return PostgresStore(conninfo, roles, **kwargs)

    def command(store: PostgresStore, argv: list[str]) -> None:
        fail_seed = (
            failure == "seed_dump" and Path(argv[0]).name == "pg_dump" and argv[0] != "pg_dump"
        )
        fail_dump = failure == "dump" and argv[0] == "pg_dump"
        if fail_seed or fail_dump:
            Path(argv[argv.index("--file") + 1]).write_bytes(b"partial archive")
            raise OSError(errno.ENOSPC, secret)
        if failure == "restore" and argv[0] == "pg_restore" and "--dbname" in argv:
            raise subprocess.TimeoutExpired(argv, 3000, stderr=secret)
        original_command(store, argv)

    def validate(measurements: dict[str, Any]) -> None:
        if failure == "measurement":
            measurements["job_elapsed_seconds"] = 7201
        original_validate(measurements)

    async def token(*args: Any) -> Any:
        return SimpleNamespace(token=secret)

    async def close() -> None:
        pass

    for key, value in {
        "PG_SOURCE_FQDN": "source",
        "PG_TARGET_FQDN": "target",
        "PG_DATABASE": "test",
        "PG_ADMIN_USER": "administrator",
        "SAGE_DB_ROLE": role,
        "BFF_DB_ROLE": role,
        "PG_MIGRATION_RUN_ID": "g17",
        "PG_MIGRATION_IMAGE": "test:fixed",
    }.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(module, "PostgresStore", connect)
    monkeypatch.setattr(PostgresStore, "_command", command)
    monkeypatch.setattr(module, "validate_measurements", validate)
    monkeypatch.setattr(
        managed_identity, "get_postgres_credential", lambda: SimpleNamespace(get_token=token)
    )
    monkeypatch.setattr(managed_identity, "close_postgres_credential", close)
    try:
        assert module.main(["rehearse", "r9001"]) == 1
        output = capsys.readouterr().out
        assert secret not in output
        report = json.loads(output.removeprefix(module.REPORT_PREFIX))
        assert report["status"] == "failed"
        assert report["image"] == "test:fixed"
        assert report["seed_source"] == source.identity
        assert report["source"].endswith("/" + admin.names[0])
        assert report["target"].endswith("/" + admin.names[1])
        assert (report["source_major"], report["target_major"]) == (16, 17)
        measurements = report["measurements"]
        assert measurements["samples"] > 0 and measurements["scratch_min_free_bytes"] > 0
        if failure == "measurement":
            assert report["reason"] == "measurement_rejected"
            assert measurements["job_elapsed_seconds"] == 7201
            assert measurements["archive_bytes"] > 0
            assert report["stage_seconds"]["restore"] > 0
            assert report["fingerprint"]
        else:
            assert report["stage"] == failure
            assert report["reason"] == ("command_timeout" if failure == "restore" else "disk_full")
            assert report["stage_seconds"][failure] > 0
            assert report["fingerprint"] is None
            if failure == "seed_dump":
                assert report["seed_archive_bytes"] == len(b"partial archive")
                assert measurements["archive_bytes"] is None
                assert measurements["elapsed_seconds"] is None
            else:
                assert measurements["archive_bytes"] > 0
        assert source.snapshot()["tables"]
        assert target.snapshot()["tables"] == {}
    finally:
        admin.cleanup()
