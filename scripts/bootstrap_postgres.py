#!/usr/bin/env python3
"""Provision the SAGE Postgres schema into a local (or any) Postgres database.

Reads the stack-config ``postgres`` block (host/port/database/user/sslmode and
the extensions list) and runs the canonical idempotent schema bootstrap against
it -- creating the graph and content tables, their indexes, and the required
extensions. Re-running is safe; every statement is ``IF NOT EXISTS``.

For the on-box default the config connects over the local unix socket as the OS
user (peer auth, no password). A password, when a TCP/hosted endpoint needs one,
is read from ``$SAGE_PG_PASSWORD`` -- never from a file or an argument. Pass
``--dsn`` to point at an explicit Postgres (e.g. a throwaway), ``--schema`` to
provision a non-``public`` schema, and ``--extensions`` to override the list.

Examples::

    # Provision the local SAGE database from sage/config.yaml's postgres block:
    .venv/bin/python scripts/bootstrap_postgres.py

    # Provision a throwaway:
    .venv/bin/python scripts/bootstrap_postgres.py \\
        --dsn postgresql://localhost/sage_scratch
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from sage.mcp_init import load_stack_config_or_default
from sage.storage.postgres.pool import PostgresConnectionParams, build_conn_kwargs
from sage.storage.postgres.schema import DEFAULT_EXTENSIONS, bootstrap_schema


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Provision the SAGE Postgres schema (idempotent).",
    )
    parser.add_argument(
        "--dsn",
        default=None,
        help=(
            "Explicit Postgres conninfo/URL to provision. Default: composed from "
            "the stack-config postgres block (sage/config.yaml)."
        ),
    )
    parser.add_argument(
        "--schema",
        default="public",
        help="Target schema to create the tables in (default: public).",
    )
    parser.add_argument(
        "--extensions",
        default=None,
        help=(
            "Comma-separated extensions to enable, overriding the config "
            "(e.g. 'vector,pgstattuple,pg_repack'). 'vector' is always enabled."
        ),
    )
    return parser.parse_args(argv)


def _resolve_conninfo_and_extensions(args: argparse.Namespace) -> tuple[str, list[str]]:
    """Resolve the libpq conninfo and the extension list from args + config."""
    from psycopg.conninfo import make_conninfo

    cfg = load_stack_config_or_default()
    pg = cfg.postgres

    if args.extensions is not None:
        extensions = [e.strip() for e in args.extensions.split(",") if e.strip()]
    else:
        extensions = list(pg.extensions) or list(DEFAULT_EXTENSIONS)

    if args.dsn is not None:
        return args.dsn, extensions

    params = PostgresConnectionParams(
        host=pg.host,
        port=pg.port,
        database=pg.database,
        user=pg.user,
        sslmode=pg.sslmode,
    )
    return make_conninfo(**build_conn_kwargs(params)), extensions


async def _run(conninfo: str, schema: str, extensions: list[str]) -> None:
    import psycopg

    async with await psycopg.AsyncConnection.connect(conninfo, autocommit=True) as conn:
        await bootstrap_schema(conn, schema=schema, extensions=extensions)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    conninfo, extensions = _resolve_conninfo_and_extensions(args)
    asyncio.run(_run(conninfo, args.schema, extensions))
    print(
        f"Provisioned SAGE schema in schema={args.schema!r} (extensions: {', '.join(extensions)})."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
