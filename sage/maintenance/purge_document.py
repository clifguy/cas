"""Single-document purge (permanently out-of-band per CAS-ADR-029).

Removes one document and its whole footprint from a SAGE vault: the graph-store
row, its tags, its edges (both directions) and staging edges, and the
content-store chunks. Operator-invoked only; this module is unreachable from the
SAGE Core API and MCP server by architectural invariant (import-topology test).

Safeguards (SAGE No-Delete Invariant; CAS-ADR-029):
- Dry-run is the default. ``--apply`` is required for any state change.
- Target identification is by ``document_id`` only -- no query interpretation.
- Refuses if any staging edge references the document at either end.
- Refuses if the document's ``pipeline_status`` is non-terminal (see
  ``sage.models.enums.TERMINAL_PIPELINE_STATUSES``).
- ``--apply`` requires typed confirmation of the document_id at the prompt.
- The graph footprint is removed in one transaction. The audit record is written
  **before** the cascade, so the worst-case partial-failure outcome is "audit
  record with no delete", never "delete with no audit record". The content-store
  removal is a separate coordinated operation (no cross-store atomicity,
  CAS-ADR-042).
- Appends a JSONL audit record to ``~/sage_vaults/{vault_id}/.maintenance_log.jsonl``.

If a new ``PipelineStatus`` is added, declare its terminality in
``sage.models.enums.TERMINAL_PIPELINE_STATUSES``; this script reads that set.

Usage::

    .venv/bin/python -m sage.maintenance.purge_document \\
        --vault VAULT --document-id ID --reason TEXT [--apply]
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import Callable
from pathlib import Path

from sage.adapters.interfaces import ContentStore, GraphStore
from sage.maintenance import _internal
from sage.models.enums import TERMINAL_PIPELINE_STATUSES

_TERMINAL_STATUS_VALUES: frozenset[str] = frozenset(s.value for s in TERMINAL_PIPELINE_STATUSES)


async def purge_document(
    *,
    graph_store: GraphStore,
    content_store: ContentStore,
    vault_dir: Path,
    document_id: str,
    reason: str,
    apply: bool,
    input_fn: Callable[[str], str] = input,
) -> int:
    """Purge one document through injected stores. Returns a process exit code.

    ``apply=False`` prints the enumeration and returns 0. ``apply=True`` runs the
    safeguard chain: preconditions -> typed-confirmation prompt -> audit append
    -> graph cascade -> content removal (the last three via ``_purge_one``).
    """
    doc = await graph_store.get_document(document_id)
    if doc is None:
        print(f"error: document {document_id!r} not found", file=sys.stderr)
        return 2

    outbound = await graph_store.get_edges_by_source(document_id)
    inbound = await graph_store.get_edges_by_target(document_id)
    staging_ids = await _internal.list_staging_edge_ids_for(graph_store, document_id)
    chunk_count = len(await content_store.get_all_chunks(document_id))

    print(f"Document:     {document_id}")
    print(f"  title:           {doc.title}")
    print(f"  source_path:     {doc.source_path}")
    print(f"  doc_type:        {doc.doc_type}")
    print(f"  pipeline_status: {doc.pipeline_status}")
    print(f"  reason:          {reason}")
    print()
    print("Will delete:")
    print("  documents row:           1")
    print(f"  edges (outbound):        {len(outbound)}")
    print(f"  edges (inbound):         {len(inbound)}")
    print(f"  staging edges:           {len(staging_ids)}")
    print(f"  content chunks:          {chunk_count}")
    print()

    if not apply:
        print("(dry-run; pass --apply to execute)")
        return 0

    if staging_ids:
        print(
            f"refuse: pending staging edges reference {document_id!r}: "
            f"{', '.join(staging_ids)}. Resolve them (confirm or dismiss) before "
            "purging.",
            file=sys.stderr,
        )
        return 3

    if doc.pipeline_status not in TERMINAL_PIPELINE_STATUSES:
        print(
            f"refuse: pipeline_status {doc.pipeline_status!s} is non-terminal for "
            f"document {document_id!r}. Wait for a terminal status "
            f"({sorted(_TERMINAL_STATUS_VALUES)}) before purging.",
            file=sys.stderr,
        )
        return 3

    typed = input_fn(f"To confirm, retype the document_id ({document_id}): ")
    if typed != document_id:
        print(
            "refuse: typed confirmation did not match document_id. Aborting.",
            file=sys.stderr,
        )
        return 3

    result = await _internal._purge_one(
        document_id=document_id,
        graph_store=graph_store,
        content_store=content_store,
        vault_dir=vault_dir,
        reason=reason,
        operation="purge_document",
        batch_id=None,
    )
    if not result.succeeded:
        print(
            f"error: {result.error}; "
            f"audit_written={result.audit_written}, "
            f"graph_committed={result.graph_committed}, "
            f"content_removed={result.content_removed}",
            file=sys.stderr,
        )
        return 4

    print(f"purge complete: {document_id} removed.")
    return 0


async def _run(*, vault_id: str, document_id: str, reason: str, apply: bool) -> int:
    opened = await _internal.open_vault_stores(vault_id)
    if opened is None:
        print(f"error: vault config not found for vault {vault_id!r}", file=sys.stderr)
        return 2
    graph_store, content_store, vault_dir, handle = opened
    try:
        return await purge_document(
            graph_store=graph_store,
            content_store=content_store,
            vault_dir=vault_dir,
            document_id=document_id,
            reason=reason,
            apply=apply,
        )
    finally:
        await handle.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="sage.maintenance.purge_document",
        description=(
            "Remove one document and its whole footprint from a SAGE vault. "
            "Operator-only; dry-run by default."
        ),
    )
    parser.add_argument("--vault", required=True, help="Vault id (e.g. example_vault).")
    parser.add_argument("--document-id", required=True, help="Document id to purge.")
    parser.add_argument(
        "--reason", required=True, help="Reason for the purge (recorded in the audit log)."
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
            document_id=args.document_id,
            reason=args.reason,
            apply=args.apply,
        )
    )


if __name__ == "__main__":
    raise SystemExit(main())
