"""Postgres sink for the purge audit record (CAS-ADR-029).

The out-of-band purge tooling writes an audit record for every document it
removes, *before* the removal — the worst-case partial-failure outcome is
"audit record with no delete", never "delete with no audit record". This sink
gives that record a durable home inside the vault's own Postgres schema, so it
is uniform across deployment profiles (Postgres is the storage port's sole
durable store under both, CAS-ADR-042) and is reclaimed with the schema when
the vault is torn down.

The ``purge_audit`` table is created on demand at first append rather than by
the canonical schema bootstrap: it is a maintenance artifact, not part of the
request-surface schema contract.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from sage.storage.postgres.schema import validate_schema_name

if TYPE_CHECKING:
    from collections.abc import Mapping

    from sage.storage.postgres.pool import PostgresConnectionParams


def _create_table_sql(schema: str) -> str:
    return (
        f'CREATE TABLE IF NOT EXISTS "{schema}".purge_audit ('
        "id bigserial PRIMARY KEY, "
        "ts timestamptz NOT NULL, "
        "operation text NOT NULL, "
        "document_id text NOT NULL, "
        "title text, "
        "source_path text, "
        "source_content_hash text, "
        "doc_type text, "
        "reason text NOT NULL, "
        "batch_id text, "
        "chain_id text)"
    )


def _insert_sql(schema: str) -> str:
    # The schema identifier is validated by the sink's constructor; every value
    # travels as a bound parameter.
    return (
        f'INSERT INTO "{schema}".purge_audit '  # noqa: S608
        "(ts, operation, document_id, title, source_path, source_content_hash, "
        "doc_type, reason, batch_id, chain_id) "
        "VALUES (%s::timestamptz, %s, %s, %s, %s, %s, %s, %s, %s, %s)"
    )


class PostgresPurgeAuditSink:
    """Append purge audit records to a vault schema's ``purge_audit`` table.

    Each ``append`` opens its own short-lived autocommit connection under the
    active profile's auth (the same plain-connection path the provisioner's
    teardown operations use) and idempotently creates the table before the
    insert. The per-append connection is by design: the audit-first invariant
    (CAS-ADR-029) forbids deferring the record into a caller-owned transaction,
    and purge volumes are operator-scale.

    The record's ``"timestamp"`` key maps to the ``ts`` column; ``batch_id`` /
    ``chain_id`` are nullable and read with ``.get()`` so a record that omits
    them (the single-document mode) inserts NULL.
    """

    def __init__(
        self,
        params: PostgresConnectionParams,
        *,
        schema: str,
        connection_class=None,
        environ: dict[str, str] | None = None,
    ) -> None:
        validate_schema_name(schema)
        self._params = params
        self._schema = schema
        self._connection_class = connection_class
        self._environ = environ

    async def append(self, record: Mapping[str, Any]) -> None:
        """Create the table if absent, then insert one audit row."""
        import psycopg
        from psycopg.conninfo import make_conninfo

        from sage.storage.postgres.pool import build_conn_kwargs

        conninfo = make_conninfo(**build_conn_kwargs(self._params, self._environ))
        conn_class = self._connection_class or psycopg.AsyncConnection
        async with await conn_class.connect(conninfo, autocommit=True) as conn:
            await conn.execute(_create_table_sql(self._schema))
            await conn.execute(
                _insert_sql(self._schema),
                (
                    record["timestamp"],
                    record["operation"],
                    record["document_id"],
                    record.get("title"),
                    record.get("source_path"),
                    record.get("source_content_hash"),
                    record.get("doc_type"),
                    record["reason"],
                    record.get("batch_id"),
                    record.get("chain_id"),
                ),
            )
