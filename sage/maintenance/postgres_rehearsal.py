"""Full-size migration rehearsal on owned, disposable databases only.

The serving source supplies a logical seed snapshot. No serving ACL, session,
checkpoint or data is changed. Rehearsal databases persist until explicit cleanup.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import re
import shutil
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

from psycopg import sql
from psycopg.conninfo import make_conninfo

from sage.maintenance.postgres_migration import PostgresStore, run_migration

REPORT_PREFIX = "CAS_REHEARSAL_REPORT "


def database_names(generation: str, run_id: str, serving: str) -> tuple[str, str]:
    if not re.fullmatch(r"[a-z][a-z0-9-]{0,11}", generation) or not re.fullmatch(
        r"r[0-9]{1,20}", run_id
    ):
        raise ValueError("invalid rehearsal run identity")
    prefix = f"cas_rehearsal_{generation.replace('-', '_')}_{run_id}"
    names = (prefix + "_source", prefix + "_target")
    if serving in names or serving.startswith("cas_rehearsal_"):
        raise ValueError("rehearsal aliases serving database")
    return names


class RehearsalDatabases:
    """Control connections remain on serving databases; writes name scratch DBs.

    All database commands below name a database explicitly. No schema, row, role,
    extension or session mutation is issued on these control connections.
    """

    def __init__(
        self, source: PostgresStore, target: PostgresStore, generation: str, run_id: str
    ) -> None:
        self.stores = (source, target)
        if source.identity == target.identity:
            raise ValueError("rehearsal serving identities do not match preparation")
        if (source.major, target.major) != (16, 17):
            raise ValueError("rehearsal requires the one-time 16-to-17 transition")
        self.names = database_names(generation, run_id, source.database)
        database_names(generation, run_id, target.database)
        self.run_id = run_id
        self.marker = json.dumps(
            {
                "purpose": "cas-postgres-rehearsal-v1",
                "source": source.identity,
                "target": target.identity,
                "generation": generation,
                "run_id": run_id,
            },
            sort_keys=True,
        )

    def _row(self, store: PostgresStore, name: str) -> Any:
        return store.conn.execute(
            "SELECT pg_get_userbyid(datdba)=current_user, "
            "shobj_description(oid,'pg_database') FROM pg_database WHERE datname=%s",
            (name,),
        ).fetchone()

    def create(self) -> None:
        # Check both before creating either; an interrupted create is never adopted.
        for store, name in zip(self.stores, self.names, strict=True):
            if self._row(store, name) is not None:
                raise ValueError(
                    "rehearsal database already exists; inspect and clean the recorded run"
                )
        for store, name in zip(self.stores, self.names, strict=True):
            store.conn.execute(
                sql.SQL(
                    "CREATE DATABASE {} WITH TEMPLATE template0 ALLOW_CONNECTIONS false"
                ).format(sql.Identifier(name))
            )
            # A kill between CREATE and COMMENT leaves an unowned database: cleanup refuses it.
            store.conn.execute(
                sql.SQL("COMMENT ON DATABASE {} IS {}").format(
                    sql.Identifier(name), sql.Literal(self.marker)
                )
            )
            store.conn.execute(
                sql.SQL("REVOKE ALL ON DATABASE {} FROM PUBLIC").format(sql.Identifier(name))
            )
            store.conn.execute(
                sql.SQL("ALTER DATABASE {} ALLOW_CONNECTIONS true").format(sql.Identifier(name))
            )

    def cleanup(self) -> None:
        existing = []
        for store, name in zip(self.stores, self.names, strict=True):
            row = self._row(store, name)
            if row is None:
                continue
            if row != (True, self.marker):
                raise ValueError("rehearsal database ownership is ambiguous; refusing cleanup")
            if store.conn.execute(
                "SELECT count(*) FROM pg_stat_activity WHERE datname=%s", (name,)
            ).fetchone()[0]:
                raise ValueError("active rehearsal connections; wait for the job to stop")
            existing.append((store, name))
        for store, name in existing:
            # Prevent a new connection racing the drop; never FORCE or kill a session.
            store.conn.execute(
                sql.SQL("ALTER DATABASE {} ALLOW_CONNECTIONS false").format(sql.Identifier(name))
            )
            store.conn.execute(sql.SQL("DROP DATABASE {}").format(sql.Identifier(name)))

    def connect(self, index: int) -> PostgresStore:
        store, name = self.stores[index], self.names[index]
        if self._row(store, name) != (True, self.marker):
            raise ValueError("rehearsal database ownership changed")
        return PostgresStore(
            make_conninfo(store.conninfo, dbname=name),
            store.app_roles,
            password=store.password,
            token_provider=store.token_provider,
        )


def validate_measurements(report: dict[str, Any]) -> None:
    keys = (
        "archive_bytes",
        "observed_peak_scratch_bytes",
        "scratch_initial_free_bytes",
        "scratch_min_free_bytes",
        "elapsed_seconds",
        "job_elapsed_seconds",
        "dump_seconds",
        "restore_seconds",
        "samples",
    )
    if any(
        k not in report
        or not isinstance(report[k], (int, float))
        or not math.isfinite(report[k])
        or report[k] <= 0
        for k in keys
    ):
        raise ValueError("incomplete rehearsal measurements")
    peak = max(
        report["archive_bytes"],
        report["observed_peak_scratch_bytes"],
        report["scratch_initial_free_bytes"] - report["scratch_min_free_bytes"],
    )
    if (
        report.get("monitor_error", True)
        or report["scratch_min_free_bytes"] < 256 * 1024**2
        or min(report["scratch_initial_free_bytes"], 4 * 1024**3) < peak * 1.25
        or report["elapsed_seconds"] >= 7200
        or report["job_elapsed_seconds"] >= 7200
        or report["dump_seconds"] >= 3000
        or report["restore_seconds"] >= 3000
    ):
        raise ValueError("rehearsal measurements exceed resource bounds or lack headroom")


class Measurements:
    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self.initial = shutil.disk_usage(directory).free
        self.minimum = self.initial
        self.samples = 0
        self.error = False
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self._monitor, daemon=True)
        self.stages: dict[str, float] = {}

    def sample(self) -> None:
        try:
            self.minimum = min(self.minimum, shutil.disk_usage(self.directory).free)
            self.samples += 1
        except OSError:
            self.error = True

    def _monitor(self) -> None:
        while not self.stop.wait(0.05):
            self.sample()

    def start(self) -> None:
        self.sample()
        self.thread.start()

    def close(self) -> None:
        self.stop.set()
        self.thread.join()
        self.sample()

    def call(self, name: str, operation: Any, *args: Any) -> Any:
        start = time.monotonic()
        try:
            return operation(*args)
        finally:
            self.stages[name] = self.stages.get(name, 0) + time.monotonic() - start
            self.sample()


class MeasuredStore:
    """Timing boundary around the exact engine's store operations; no replacement algorithm."""

    def __init__(self, store: PostgresStore, meter: Measurements, label: str) -> None:
        self.store, self.meter, self.label = store, meter, label
        self.identity, self.major = store.identity, store.major

    def snapshot(self) -> dict[str, Any]:
        return self.meter.call(self.label + "_snapshot", self.store.snapshot)

    def dump(self, path: Path) -> None:
        self.meter.call("dump", self.store.dump, path)

    def restore(self, path: Path) -> None:
        self.meter.call("restore", self.store.restore, path)

    def fence(self) -> None:
        self.meter.call("fence", self.store.fence)

    def assert_quiescent(self) -> None:
        self.store.assert_quiescent()

    def read_checkpoint(self) -> dict[str, Any] | None:
        return self.store.read_checkpoint()

    def save_checkpoint(self, value: dict[str, Any]) -> None:
        self.store.save_checkpoint(value)


def execute_rehearsal(
    admin: RehearsalDatabases, directory: Path, *, image: str, job_started: float
) -> dict[str, Any]:
    source, _ = admin.stores
    admin.create()
    clones = []
    seed_started = time.monotonic()
    meter = Measurements(directory)
    meter.start()
    try:
        # pg_dump's consistent logical snapshot is representative at seed time,
        # not a claim about later live production writes or physical storage size.
        seed = directory / "seed.dump"
        seed_client = Path(os.environ.get("PG_SEED_CLIENT_DIR", "/usr/lib/postgresql/16/bin"))
        source._command(
            [
                str(seed_client / "pg_dump"),
                "--format=custom",
                "--exclude-schema=_cas_migration",
                "--file",
                str(seed),
                "--dbname",
                source.conninfo,
            ]
        )
        seed.chmod(0o600)
        seed_bytes = seed.stat().st_size
        meter.sample()
        clones.append(admin.connect(0))
        clones.append(admin.connect(1))
        clones[0]._command(
            [
                str(seed_client / "pg_restore"),
                "--exit-on-error",
                "--single-transaction",
                "--dbname",
                clones[0].conninfo,
                str(seed),
            ]
        )
        source.assert_extensions_match(clones[0])
        # The normal preparation checked installed extension parity on both servers.
        # pg_restore creates extensions from the archive; require parity again after restore.
        clones[0].assert_restore_privileges(source.restore_owners())
        clones[1].assert_restore_privileges(source.restore_owners())
        seed.unlink()
        seed_seconds = time.monotonic() - seed_started
        archive = directory / "migration.dump"
        started = time.monotonic()
        result = run_migration(
            MeasuredStore(clones[0], meter, "source"),
            MeasuredStore(clones[1], meter, "target"),
            archive,
            admin.run_id,
            source.major,
            admin.stores[1].major,
        )
        clones[0].assert_extensions_match(clones[1])
        elapsed = time.monotonic() - started
        meter.close()
        measurement = {
            "archive_bytes": archive.stat().st_size,
            "scratch_initial_free_bytes": meter.initial,
            "scratch_min_free_bytes": meter.minimum,
            "observed_peak_scratch_bytes": max(
                seed_bytes, archive.stat().st_size, meter.initial - meter.minimum
            ),
            "elapsed_seconds": elapsed,
            "job_elapsed_seconds": time.monotonic() - job_started,
            "dump_seconds": meter.stages.get("dump", 0),
            "restore_seconds": meter.stages.get("restore", 0),
            "samples": meter.samples,
            "monitor_error": meter.error,
        }
        validate_measurements(measurement)
        return {
            "status": "rehearsal_verified",
            "run_id": admin.run_id,
            "image": image,
            "source": clones[0].identity,
            "target": clones[1].identity,
            "source_major": clones[0].major,
            "target_major": clones[1].major,
            "seed_source": source.identity,
            "seed_seconds": seed_seconds,
            "seed_archive_bytes": seed_bytes,
            "source_database_bytes": source.capacity_report(directory)["database_bytes"],
            "clone_database_bytes": clones[0].capacity_report(directory)["database_bytes"],
            "fingerprint": result["fingerprint"],
            "archive_sha256": result["archive_sha256"],
            "table_count": len(result["tables"]),
            "row_count": sum(result["tables"].values()),
            "stage_seconds": meter.stages,
            "measurements": measurement,
            "resources": {
                "cpu": 1,
                "memory": "2Gi",
                "ephemeral_storage_limit_bytes": 4 * 1024**3,
                "replica_timeout_seconds": 7200,
                "command_timeout_seconds": 3000,
            },
        }
    finally:
        meter.close()
        for clone in clones:
            clone.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("rehearse", "cleanup-rehearsal"))
    parser.add_argument("run_id")
    args = parser.parse_args(argv)
    from sage.storage.postgres.managed_identity import (
        POSTGRES_AAD_SCOPE,
        close_postgres_credential,
        get_postgres_credential,
    )

    started = time.monotonic()
    stores = []
    report: dict[str, Any] = {"status": "failed", "run_id": args.run_id}
    with asyncio.Runner() as runner:

        def token() -> str:
            return runner.run(get_postgres_credential().get_token(POSTGRES_AAD_SCOPE)).token

        try:
            database_names(
                os.environ["PG_MIGRATION_RUN_ID"], args.run_id, os.environ["PG_DATABASE"]
            )
            for key in ("PG_SOURCE_FQDN", "PG_TARGET_FQDN"):
                stores.append(
                    PostgresStore(
                        make_conninfo(
                            host=os.environ[key],
                            dbname=os.environ["PG_DATABASE"],
                            user=os.environ["PG_ADMIN_USER"],
                            sslmode="require",
                        ),
                        (os.environ["SAGE_DB_ROLE"], os.environ["BFF_DB_ROLE"]),
                        password=token(),
                        token_provider=token,
                    )
                )
            admin = RehearsalDatabases(*stores, os.environ["PG_MIGRATION_RUN_ID"], args.run_id)
            if args.mode == "cleanup-rehearsal":
                admin.cleanup()
                report = {"status": "rehearsal_cleaned", "run_id": args.run_id}
            else:
                stores[0].assert_extensions_match(stores[1])
                with tempfile.TemporaryDirectory(prefix="postgres-rehearsal-") as directory:
                    report = execute_rehearsal(
                        admin,
                        Path(directory),
                        image=os.environ["PG_MIGRATION_IMAGE"],
                        job_started=started,
                    )
            return 0
        except Exception as exc:
            # Database errors can echo data. Persist only safe classification, never str(exc).
            report["error_type"] = type(exc).__name__
            return 1
        finally:
            print(REPORT_PREFIX + json.dumps(report, sort_keys=True), flush=True)
            for store in stores:
                store.close()
            runner.run(close_postgres_credential())


if __name__ == "__main__":
    raise SystemExit(main())
