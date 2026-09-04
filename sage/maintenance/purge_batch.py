"""Bulk purge by ingestion-time window (permanently out-of-band per CAS-ADR-029).

Recovery tool for the wrong-vault-dump scenario: an ingest run targeted the wrong
vault, the operator noticed within seconds or minutes, and wants to remove the
batch cleanly. The selector is ingestion time, not query semantics -- "everything
ingested between T1 and T2 belongs in vault X, not vault Y."

Safeguards (SAGE No-Delete Invariant; CAS-ADR-029):
- Dry-run is the default. ``--apply`` is required for any state change.
- Targets are identified by a half-open ``created_at`` window ``[since, until)``
  only -- no query interpretation.
- The whole batch is rejected if **any** target has pending staging edges or a
  non-terminal ``pipeline_status``. Pre-flight rejection surfaces every violating
  target; the batch never proceeds partially.
- ``--apply`` requires the operator to retype the exact target count.
- Per-document cascade runs via ``_purge_one``. A per-document failure halts the
  batch immediately: already-succeeded docs stay deleted and audit-logged;
  remaining targets are untouched.
- Audit records carry a shared ``batch_id`` (UUID generated at batch start) so a
  later auditor can reconstruct which deletes belonged to one invocation.

This module is unreachable from the SAGE Core API and MCP server by architectural
invariant (import-topology test).

Usage::

    .venv/bin/python -m sage.maintenance.purge_batch \\
        --vault VAULT --ingested-since TIMESTAMP [--ingested-until TIMESTAMP] \\
        --reason TEXT [--apply]
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import uuid
from collections.abc import Callable
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from sage.adapters.interfaces import ContentStore, GraphStore
from sage.maintenance import _internal
from sage.models.enums import TERMINAL_PIPELINE_STATUS_VALUES

if TYPE_CHECKING:
    from sage.storage_binding import PurgeAuditSink


async def purge_batch(
    *,
    graph_store: GraphStore,
    content_store: ContentStore,
    audit_sink: PurgeAuditSink,
    since: datetime,
    until: datetime | None,
    reason: str,
    apply: bool,
    input_fn: Callable[[str], str] = input,
) -> int:
    """Purge every document ingested in ``[since, until)`` through injected stores.

    ``until=None`` resolves to ``datetime.now(timezone.utc)`` at call time.
    Returns a process exit code.
    """
    effective_until = until if until is not None else datetime.now(timezone.utc)
    targets = await graph_store.find_documents_ingested_between(since, effective_until)

    print(f"Window:       [{since.isoformat()}, {effective_until.isoformat()})")
    print(f"Reason:       {reason}")
    print(f"Target count: {len(targets)}")
    print()
    total_chunks = 0
    if targets:
        print("Targets:")
        for doc in targets:
            chunk_count = len(await content_store.get_all_chunks(doc.id))
            total_chunks += chunk_count
            print(f"  id:            {doc.id}")
            print(f"    title:         {doc.title}")
            print(f"    source_path:   {doc.source_path}")
            print(f"    doc_type:      {doc.doc_type}")
            print(f"    document_date: {doc.document_date}")
            print(f"    created_at:    {doc.created_at.isoformat()}")
            print(f"    content chunks:{chunk_count}")
        print()
        print(f"Total: {len(targets)} documents, {total_chunks} content chunks cumulative")
        print()

    if not targets:
        print("(no documents in window; nothing to do)")
        return 0

    if not apply:
        print("(dry-run; pass --apply to execute)")
        return 0

    staging_violations: list[str] = []
    pipeline_violations: list[str] = []
    for doc in targets:
        staging_ids = await _internal.list_staging_edge_ids_for(graph_store, doc.id)
        if staging_ids:
            staging_violations.append(f"{doc.id}: staging edges {', '.join(staging_ids)}")
        if doc.pipeline_status not in TERMINAL_PIPELINE_STATUS_VALUES:
            pipeline_violations.append(f"{doc.id}: pipeline_status={doc.pipeline_status!s}")

    if staging_violations or pipeline_violations:
        print(
            "refuse: batch rejected; resolve every violation below before retrying. "
            "No document was deleted.",
            file=sys.stderr,
        )
        if staging_violations:
            print("  pending staging edges:", file=sys.stderr)
            for v in staging_violations:
                print(f"    {v}", file=sys.stderr)
        if pipeline_violations:
            print("  non-terminal pipeline_status:", file=sys.stderr)
            for v in pipeline_violations:
                print(f"    {v}", file=sys.stderr)
        return 3

    target_count = len(targets)
    typed = input_fn(
        f"To confirm purge of {target_count} documents, retype the count ({target_count}): "
    )
    if typed != str(target_count):
        print(
            "refuse: typed confirmation did not match target count. Aborting; "
            "no document was deleted.",
            file=sys.stderr,
        )
        return 3

    batch_id = str(uuid.uuid4())
    succeeded: list[str] = []
    for doc in targets:
        result = await _internal._purge_one(
            document_id=doc.id,
            graph_store=graph_store,
            content_store=content_store,
            audit_sink=audit_sink,
            reason=reason,
            operation="purge_batch",
            batch_id=batch_id,
        )
        if not result.succeeded:
            print(
                f"error: per-document failure on {result.document_id!r}: {result.error}. "
                f"Halting batch. {len(succeeded)} document(s) already purged; "
                f"{target_count - len(succeeded) - 1} target(s) untouched. batch_id={batch_id}",
                file=sys.stderr,
            )
            return 4
        succeeded.append(result.document_id)

    print(f"purge complete: {len(succeeded)} document(s) removed. batch_id={batch_id}")
    return 0


async def _run(
    *, vault_id: str, since: datetime, until: datetime | None, reason: str, apply: bool
) -> int:
    opened = await _internal.open_vault_stores(vault_id)
    if opened is None:
        print(f"error: vault config not found for vault {vault_id!r}", file=sys.stderr)
        return 2
    graph_store, content_store, audit_sink, handle = opened
    try:
        return await purge_batch(
            graph_store=graph_store,
            content_store=content_store,
            audit_sink=audit_sink,
            since=since,
            until=until,
            reason=reason,
            apply=apply,
        )
    finally:
        await handle.close()


def _parse_timestamp(s: str) -> datetime:
    """Parse an ISO-8601 timestamp. Naive inputs are treated as UTC."""
    try:
        dt = datetime.fromisoformat(s)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"invalid ISO-8601 timestamp: {s!r} ({exc})") from exc
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="sage.maintenance.purge_batch",
        description=(
            "Remove every document in a SAGE vault whose created_at falls in the "
            "half-open window [--ingested-since, --ingested-until). Operator-only; "
            "dry-run by default."
        ),
    )
    parser.add_argument("--vault", required=True, help="Vault id (e.g. example_vault).")
    parser.add_argument(
        "--ingested-since",
        required=True,
        type=_parse_timestamp,
        help="Lower bound (inclusive). ISO-8601 timestamp; naive = UTC.",
    )
    parser.add_argument(
        "--ingested-until",
        required=False,
        default=None,
        type=_parse_timestamp,
        help=(
            "Upper bound (exclusive). ISO-8601 timestamp; naive = UTC. Defaults to "
            "script-start time (UTC) when omitted."
        ),
    )
    parser.add_argument(
        "--reason",
        required=True,
        help="Reason for the batch (recorded in every audit-log entry).",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Apply the cascade. Without this flag, prints a plan and exits.",
    )
    args = parser.parse_args(argv)
    return asyncio.run(
        _run(
            vault_id=args.vault,
            since=args.ingested_since,
            until=args.ingested_until,
            reason=args.reason,
            apply=args.apply,
        )
    )


if __name__ == "__main__":
    raise SystemExit(main())
