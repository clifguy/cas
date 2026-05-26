"""Bulk purge by ingestion-time window (permanently out-of-band per CAS-ADR-029).

Recovery tool for the wrong-vault-dump scenario: an ingest run targeted
the wrong vault, the operator noticed within seconds or minutes, and
wants to remove the batch cleanly before further confusion accrues. The
selector is ingestion time, not query semantics — "everything ingested
between T1 and T2 belongs in vault X, not vault Y."

Safeguards (SAGE-Architecture v2.1 §6.4 No-Delete Invariant, ADR-029 v1.1):
- Dry-run is the default. ``--apply`` is required for any state change.
- Targets are identified by ``created_at`` window only — no query
  interpretation. The window is half-open: ``[since, until)``.
- The whole batch is rejected if **any** target has pending staging
  edges or a non-terminal ``pipeline_status``. Pre-flight rejection
  surfaces every violating target up front; the batch never proceeds
  partially.
- ``--apply`` requires the operator to retype the exact target count.
- Per-document cascade runs in a single SQLite transaction via
  ``sage.maintenance._internal._purge_one``. A per-document failure
  halts the batch immediately: already-succeeded docs remain deleted
  and audit-logged; remaining targets are untouched.
- Audit records carry a shared ``batch_id`` (UUID generated at batch
  start) so a later auditor can reconstruct which deletes belonged to
  one invocation.

This module is unreachable from the SAGE Core API and MCP server by
architectural invariant (import-topology test).

Usage::

    .venv/bin/python -m sage.maintenance.purge_batch \\
        --vault VAULT \\
        --ingested-since TIMESTAMP \\
        [--ingested-until TIMESTAMP] \\
        --reason TEXT [--apply]
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sage.config import load_vault_config
from sage.maintenance._internal import (
    _list_staging_edge_ids,
    _purge_one,
)
from sage.models.enums import TERMINAL_PIPELINE_STATUSES
from sage.vault_management import config_path_for_vault

# Rebind point for tests that need to simulate SQLite failures.
_sqlite_connect = sqlite3.connect

_TERMINAL_STATUS_VALUES: frozenset[str] = frozenset(s.value for s in TERMINAL_PIPELINE_STATUSES)


def _resolve_paths(vault_id: str) -> tuple[Path, Path, Path, Path] | None:
    """Return (vault_dir, sqlite_path, lancedb_dir, storage_root) or None."""
    config_path = config_path_for_vault(vault_id)
    if not config_path.exists():
        print(
            f"error: vault config not found for vault {vault_id!r}: {config_path}",
            file=sys.stderr,
        )
        return None
    config = load_vault_config(config_path)
    brain_root = Path(config.vault.brain_root).expanduser()
    storage_root = Path(config.vault.storage_root).expanduser()
    return (
        config_path.parent,
        brain_root / "graph.db",
        brain_root / "lancedb",
        storage_root,
    )


def _select_targets(
    conn: sqlite3.Connection, since: datetime, until: datetime
) -> list[dict[str, Any]]:
    """Documents whose ``created_at`` falls in ``[since, until)``.

    ``created_at`` is stored as ``datetime.isoformat()`` (always UTC with
    microseconds — see ``GraphStore._exec_insert_document``). Lexicographic
    comparison of those strings matches chronological order as long as
    both sides carry the same format suffix. The CLI normalizes naive
    inputs to UTC, so ``since.isoformat()`` and ``until.isoformat()``
    share the stored format.
    """
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT id, title, source_path, source_content_hash, doc_type, "
        "pipeline_status, document_date, created_at "
        "FROM documents WHERE created_at >= ? AND created_at < ? "
        "ORDER BY created_at",
        (since.isoformat(), until.isoformat()),
    ).fetchall()
    return [dict(r) for r in rows]


def _compute_content_size(storage_root: Path, source_path: str) -> int:
    """Best-effort file size from ``storage_root / source_path``.

    Missing files or vault-resident docs without an on-disk source
    contribute 0. We do not refuse on missing source files; the batch
    can include docs whose source is gone (the wrong-vault-dump may
    itself have orphaned them) or vault-resident docs that never had
    a source file in the first place.
    """
    if not source_path:
        return 0
    full = storage_root / source_path
    try:
        return full.stat().st_size
    except (FileNotFoundError, OSError):
        return 0


def _preflight_batch_rejection(
    conn: sqlite3.Connection, targets: list[dict[str, Any]]
) -> tuple[list[str], list[str]]:
    """Collect per-target violations across the full target set.

    Returns ``(staging_violations, pipeline_violations)``. Each list
    holds one human-readable line per violating target. Either list
    non-empty → caller refuses the whole batch.
    """
    staging_violations: list[str] = []
    pipeline_violations: list[str] = []
    for doc in targets:
        doc_id = doc["id"]
        staging_ids = _list_staging_edge_ids(conn, doc_id)
        if staging_ids:
            staging_violations.append(f"{doc_id}: staging edges {', '.join(staging_ids)}")
        status = doc["pipeline_status"]
        if status not in _TERMINAL_STATUS_VALUES:
            pipeline_violations.append(f"{doc_id}: pipeline_status={status!r}")
    return staging_violations, pipeline_violations


def _render_dry_run_summary(
    *,
    vault_id: str,
    since: datetime,
    until: datetime,
    reason: str,
    targets: list[dict[str, Any]],
    content_sizes: list[int],
) -> None:
    """Print the per-target enumeration and summary to stdout."""
    total_size = sum(content_sizes)
    print(f"Vault:        {vault_id}")
    print(f"Window:       [{since.isoformat()}, {until.isoformat()})")
    print(f"Reason:       {reason}")
    print(f"Target count: {len(targets)}")
    print()
    if not targets:
        return
    print("Targets:")
    for doc, size in zip(targets, content_sizes):
        print(f"  id:            {doc['id']}")
        print(f"    title:         {doc['title']}")
        print(f"    source_path:   {doc['source_path']}")
        print(f"    doc_type:      {doc['doc_type']}")
        print(f"    document_date: {doc['document_date']}")
        print(f"    created_at:    {doc['created_at']}")
        print(f"    content_size:  {size} bytes")
    print()
    print(f"Total: {len(targets)} documents, {total_size} bytes cumulative content_size")
    print()


def purge(
    *,
    vault_id: str,
    since: datetime,
    until: datetime | None,
    reason: str,
    apply: bool,
) -> int:
    """Service-level entry point. Returns 0 on success, non-zero on refusal.

    ``until=None`` resolves to ``datetime.now(timezone.utc)`` at the
    moment this function is called.
    """
    paths = _resolve_paths(vault_id)
    if paths is None:
        return 2
    vault_dir, sqlite_path, lancedb_dir, storage_root = paths

    if not sqlite_path.exists():
        print(
            f"error: graph.db not found for vault {vault_id!r}: {sqlite_path}",
            file=sys.stderr,
        )
        return 2

    effective_until = until if until is not None else datetime.now(timezone.utc)

    conn = _sqlite_connect(sqlite_path)
    try:
        targets = _select_targets(conn, since, effective_until)
        content_sizes = [_compute_content_size(storage_root, doc["source_path"]) for doc in targets]
        _render_dry_run_summary(
            vault_id=vault_id,
            since=since,
            until=effective_until,
            reason=reason,
            targets=targets,
            content_sizes=content_sizes,
        )

        if not targets:
            print("(no documents in window; nothing to do)")
            return 0

        if not apply:
            print("(dry-run; pass --apply to execute)")
            return 0

        staging_violations, pipeline_violations = _preflight_batch_rejection(conn, targets)
        if staging_violations or pipeline_violations:
            print(
                "refuse: batch rejected; resolve every violation below "
                "before retrying. No document was deleted.",
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
        prompt = (
            f"To confirm purge of {target_count} documents from vault "
            f"{vault_id!r}, retype the count ({target_count}): "
        )
        typed = input(prompt)
        if typed != str(target_count):
            print(
                "refuse: typed confirmation did not match target count. "
                "Aborting; no document was deleted.",
                file=sys.stderr,
            )
            return 3

        batch_id = str(uuid.uuid4())
        succeeded: list[str] = []
        for doc in targets:
            result = _purge_one(
                document_id=doc["id"],
                conn=conn,
                lancedb_dir=lancedb_dir,
                vault_dir=vault_dir,
                reason=reason,
                batch_id=batch_id,
            )
            if not result.succeeded:
                print(
                    f"error: per-document failure on {result.document_id!r}: "
                    f"{result.error}. Halting batch. "
                    f"{len(succeeded)} document(s) already purged "
                    f"before this failure; "
                    f"{target_count - len(succeeded) - 1} target(s) untouched. "
                    f"batch_id={batch_id}",
                    file=sys.stderr,
                )
                return 4
            succeeded.append(result.document_id)

        print(
            f"purge complete: {len(succeeded)} document(s) removed from "
            f"vault {vault_id!r}. batch_id={batch_id}"
        )
        return 0
    finally:
        conn.close()


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
            "Remove every document in a SAGE vault whose created_at falls "
            "in the half-open window [--ingested-since, --ingested-until). "
            "Operator-only; dry-run by default."
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
            "Upper bound (exclusive). ISO-8601 timestamp; naive = UTC. "
            "Defaults to script-start time (UTC) when omitted."
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
    return purge(
        vault_id=args.vault,
        since=args.ingested_since,
        until=args.ingested_until,
        reason=args.reason,
        apply=args.apply,
    )


if __name__ == "__main__":
    raise SystemExit(main())
