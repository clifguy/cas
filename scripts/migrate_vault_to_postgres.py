"""Migrate a vault's derived/curated state from LanceDB + SQLite into Postgres.

Reads each vault's embedded graph (SQLite) and content (LanceDB) stores and
writes equivalent rows into a per-vault Postgres schema through the Postgres
adapters, then reconciles the copy and emits a per-vault report. The migration
core lives in ``sage.migration.vault_to_postgres``; this is the operator
entrypoint (CAS-ADR-042).

Operator workflow:

    # Dry-run (no writes; reports the source counts that would be migrated).
    .venv/bin/python scripts/migrate_vault_to_postgres.py --vault-id cas

    # Migrate into a throwaway Postgres first and review the report.
    .venv/bin/python scripts/migrate_vault_to_postgres.py \\
        --vault-id cas --dsn postgresql:///sage_scratch --execute \\
        --report-dir /tmp/migration-reports

    # Every vault, into the engine configured by the stack config's postgres block.
    .venv/bin/python scripts/migrate_vault_to_postgres.py --all-vaults --execute

The target schema for a vault is the vault id. The source LanceDB/SQLite stores
are only read; they are left intact for rollback. Idempotent: a re-run resets
the target schema and reloads, reproducing the same rows. The reconciliation
report's ``ok`` is the cutover gate; a non-``ok`` executed run exits non-zero.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from sage.adapters.content_store_lancedb import LanceDBContentStore
from sage.adapters.content_store_postgres import PostgresContentStore
from sage.config import load_vault_config
from sage.mcp_init import load_stack_config_or_default
from sage.migration.vault_to_postgres import VaultMigrationReport, migrate_vault
from sage.storage.graph_store import SqliteGraphStore
from sage.storage.postgres.graph_store import PostgresGraphStore
from sage.storage.postgres.pool import (
    PostgresConnectionParams,
    build_conn_kwargs,
    create_pool,
    pool_from_conninfo,
)
from sage.storage.postgres.schema import bootstrap_schema, validate_schema_name
from sage.vault_management import config_path_for_vault

_VAULTS_ROOT = Path("~/sage_vaults").expanduser()


def discover_vault_ids() -> list[str]:
    """Return sorted vault ids that have a vault_config.yaml on disk."""
    if not _VAULTS_ROOT.exists():
        return []
    return sorted(
        p.name for p in _VAULTS_ROOT.iterdir() if p.is_dir() and (p / "vault_config.yaml").exists()
    )


def _build_target_pool(dsn: str | None, schema: str):
    """Build an unopened target pool bound to ``schema``.

    A ``--dsn`` overrides the stack config (the throwaway-Postgres path); without
    it, the connection parameters come from the stack config's postgres block.
    """
    search_path = f"{schema},public"
    if dsn:
        return pool_from_conninfo(dsn, search_path=search_path)
    pg = load_stack_config_or_default().postgres
    params = PostgresConnectionParams(
        host=pg.host,
        port=pg.port,
        database=pg.database,
        user=pg.user,
        sslmode=pg.sslmode,
        search_path=search_path,
        min_pool_size=pg.min_pool_size,
        max_pool_size=pg.max_pool_size,
    )
    return create_pool(params)


def _target_extensions(dsn: str | None) -> list[str]:
    if dsn:
        return ["vector", "pgstattuple"]
    return list(load_stack_config_or_default().postgres.extensions)


def _build_target_conninfo(dsn: str | None) -> str:
    """Compose the libpq conninfo for the target engine.

    A ``--dsn`` passes through verbatim; without it the connection parameters
    come from the stack config's postgres block (password from the
    environment, per the pool module's rule).
    """
    if dsn:
        return dsn
    from psycopg.conninfo import make_conninfo

    pg = load_stack_config_or_default().postgres
    params = PostgresConnectionParams(
        host=pg.host,
        port=pg.port,
        database=pg.database,
        user=pg.user,
        sslmode=pg.sslmode,
    )
    return make_conninfo(**build_conn_kwargs(params))


def _emit_report(report: VaultMigrationReport, report_dir: Path | None, out) -> None:
    """Print a one-line verdict and (optionally) write the full JSON report."""
    verb = "EXECUTED" if report.executed else "DRY-RUN"
    verdict = "ok" if report.ok else ("pre-flight" if not report.executed else "NOT OK")
    print(
        f"[{report.vault_id}] {verb}: {verdict} — "
        f"docs {report.documents.source_count}->{report.documents.target_count}, "
        f"edges {report.edges.source_count}->{report.edges.target_count}, "
        f"chunks {report.chunks.source_total}->{report.chunks.target_total}, "
        f"abstracts {report.abstracts.source_coverage}/{report.abstracts.total_documents}",
        file=out,
    )
    if report.tier3_indexes_blocked:
        for blocked in report.tier3_indexes_blocked:
            print(
                f"  tier3 BLOCKED: {blocked.doc_type}.{blocked.field} — {blocked.message}",
                file=sys.stderr,
            )
    if report.invalid_source_edges:
        for bad in report.invalid_source_edges:
            print(
                f"  invalid source edge SKIPPED: {bad.raw_id} "
                f"({bad.edge_type}: {bad.source_id} -> {bad.target_id}) — {bad.error}",
                file=sys.stderr,
            )
    if report_dir is not None:
        report_dir.mkdir(parents=True, exist_ok=True)
        path = report_dir / f"{report.vault_id}-migration-report.json"
        path.write_text(report.model_dump_json(indent=2))
        print(f"  report written to {path}", file=out)


async def _provision_target(dsn: str | None, schema: str, extensions: list[str]) -> None:
    """Idempotently create the target schema, extensions, tables, and indexes.

    Runs over a plain (non-pool) connection because the pool's per-connection
    configure hook registers the pgvector type, which exists only after the
    ``vector`` extension is created here. Must complete before the pool opens,
    or a fresh database can never serve a pooled connection.
    """
    import psycopg

    async with await psycopg.AsyncConnection.connect(
        _build_target_conninfo(dsn), autocommit=True
    ) as conn:
        await bootstrap_schema(conn, schema=schema, extensions=extensions)


async def _migrate_one(
    vault_id: str,
    *,
    execute: bool,
    dsn: str | None,
    report_dir: Path | None,
    out=None,
) -> int:
    """Migrate one vault; return a Unix exit code (0 ok / pre-flight, 1 not-ok, 2 config)."""
    out = out if out is not None else sys.stdout
    config_path = config_path_for_vault(vault_id)
    if not config_path.exists():
        print(f"[{vault_id}] vault config not found: {config_path}", file=sys.stderr)
        return 2

    config = load_vault_config(config_path)
    brain_root = Path(config.vault.brain_root).expanduser().resolve()
    schema = validate_schema_name(vault_id)

    source_graph = SqliteGraphStore(brain_root / "graph.db")
    await source_graph.initialize()
    source_content = LanceDBContentStore(brain_root)

    # Provisioning precedes pool.open(): the pool's configure hook needs the
    # vector extension to exist. A dry-run never touches the target, so the
    # pool stays unopened and an unprovisioned target is still inspectable.
    pool = _build_target_pool(dsn, schema)
    if execute:
        await _provision_target(dsn, schema, _target_extensions(dsn))
        await pool.open()
    try:
        report = await migrate_vault(
            source_graph=source_graph,
            source_content=source_content,
            target_graph=PostgresGraphStore(pool),
            target_content=PostgresContentStore(pool),
            target_pool=pool,
            config=config,
            vault_id=vault_id,
            execute=execute,
        )
        _emit_report(report, report_dir, out)
        if not execute:
            return 0
        return 0 if report.ok else 1
    finally:
        await pool.close()
        await source_graph.close()


async def _migrate_all(
    vault_ids: list[str],
    *,
    execute: bool,
    dsn: str | None,
    report_dir: Path | None,
) -> int:
    rc = 0
    for vault_id in vault_ids:
        try:
            sub_rc = await _migrate_one(vault_id, execute=execute, dsn=dsn, report_dir=report_dir)
            if sub_rc != 0:
                rc = sub_rc
        except Exception as exc:  # noqa: BLE001 -- one vault's failure must not abort the rest
            print(f"[{vault_id}] FAILED: {exc!r}", file=sys.stderr)
            rc = 1
    return rc


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Migrate a vault's LanceDB + SQLite state into a per-vault Postgres "
            "schema, then reconcile. Default is dry-run; pass --execute to write."
        ),
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--vault-id", help="Single vault id to migrate.")
    group.add_argument(
        "--all-vaults",
        action="store_true",
        help="Migrate every vault under ~/sage_vaults/ sequentially.",
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Write to Postgres. Default is dry-run (source counts only, no writes).",
    )
    parser.add_argument(
        "--dsn",
        default=None,
        help=(
            "libpq DSN/URL for the target Postgres (e.g. a throwaway scratch db). "
            "Omit to use the stack config's postgres block."
        ),
    )
    parser.add_argument(
        "--report-dir",
        default=None,
        help="Directory to write per-vault JSON reconciliation reports into.",
    )
    args = parser.parse_args(argv)
    report_dir = Path(args.report_dir).expanduser() if args.report_dir else None

    if args.all_vaults:
        vault_ids = discover_vault_ids()
        if not vault_ids:
            print(f"No vaults found under {_VAULTS_ROOT}. Nothing to do.", file=sys.stderr)
            return 1
        return asyncio.run(
            _migrate_all(vault_ids, execute=args.execute, dsn=args.dsn, report_dir=report_dir)
        )

    return asyncio.run(
        _migrate_one(args.vault_id, execute=args.execute, dsn=args.dsn, report_dir=report_dir)
    )


if __name__ == "__main__":
    sys.exit(main())
