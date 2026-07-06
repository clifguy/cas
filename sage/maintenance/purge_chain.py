"""Chain-selector bulk purge (permanently out-of-band per CAS-ADR-029).

Removes every member of a named edge chain (default ``supersedes``) from a SAGE
vault as one operator-invoked action. Shares the per-document cascade with
``purge_document`` via ``_purge_one`` and adds a chain-level confirmation gate:
typed head id + typed chain length.

Safeguards (SAGE No-Delete Invariant; CAS-ADR-029):
- Dry-run is the default. ``--apply`` is required for any state change.
- Chain membership is resolved by walking the named edge type from the head via
  ``GraphStore.chain_walk`` (the same recursive-CTE walk ``chain`` uses).
- ``--head-id`` is validated to be a genuine head (no inbound edges of the named
  type); a middle or tail id is refused with the actual head ids surfaced.
- Non-linear (branched) chains are refused unless ``--allow-branched`` is set.
- The whole chain is refused if any member has pending staging edges or a
  non-terminal ``pipeline_status``. No partial proceed.
- ``--apply`` prompts for the head id and the chain length, separately. Mismatch
  on either refuses the batch.
- Per-member cascade is the same graph-footprint + content removal as
  ``purge_document``. The audit record is written **before** each member's
  removal; on a per-member failure the loop halts and earlier members stay
  deleted with audit records.
- Each invocation generates a UUID ``chain_id`` shared across every audit entry
  from the same run, distinct from ``batch_id``.

This module is operator-only by architectural invariant: nothing in
``sage.mcp_server`` or ``sage.api`` may import it (import-topology test).

Usage::

    .venv/bin/python -m sage.maintenance.purge_chain \\
        --vault VAULT --head-id ID --reason TEXT \\
        [--edge-type TYPE] [--apply] [--allow-branched]
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import uuid
from collections.abc import Callable
from typing import TYPE_CHECKING

from sage.adapters.interfaces import ContentStore, GraphStore
from sage.maintenance import _internal
from sage.models.enums import TERMINAL_PIPELINE_STATUSES

if TYPE_CHECKING:
    from sage.storage_binding import PurgeAuditSink

_TERMINAL_STATUS_VALUES: frozenset[str] = frozenset(s.value for s in TERMINAL_PIPELINE_STATUSES)


async def purge_chain(
    *,
    graph_store: GraphStore,
    content_store: ContentStore,
    audit_sink: PurgeAuditSink,
    head_id: str,
    reason: str,
    edge_type: str = "supersedes",
    apply: bool = False,
    allow_branched: bool = False,
    input_fn: Callable[[str], str] = input,
) -> int:
    """Purge a whole edge chain through injected stores. Returns an exit code."""
    if await graph_store.get_document(head_id) is None:
        print(f"error: document {head_id!r} not found", file=sys.stderr)
        return 2

    walk = await graph_store.chain_walk(head_id, edge_type)
    documents = walk["documents"]
    edges = walk["edges"]

    actual_heads = _internal._chain_head_ids(documents, edges)
    if head_id not in actual_heads:
        print(
            f"refuse: --head-id {head_id!r} is not the head of the {edge_type} chain. "
            f"Actual head id(s): {', '.join(actual_heads) if actual_heads else '(none)'}.",
            file=sys.stderr,
        )
        return 3

    is_linear = _internal._chain_is_linear(documents, edges)
    if not is_linear and not allow_branched:
        print(
            f"refuse: {edge_type} chain from {head_id!r} is non-linear (branched). "
            "Use --allow-branched to override after verifying the divergence is "
            "intentional.",
            file=sys.stderr,
        )
        return 3

    ordered_ids = _internal._order_chain_from_head(documents, edges, head_id)
    chain_length = len(ordered_ids)
    member_docs = {doc_id: await graph_store.get_document(doc_id) for doc_id in ordered_ids}

    members_with_staging: list[str] = []
    members_with_nonterminal: list[tuple[str, str]] = []
    total_chunks = 0
    for doc_id in ordered_ids:
        if await _internal.list_staging_edge_ids_for(graph_store, doc_id):
            members_with_staging.append(doc_id)
        status = member_docs[doc_id].pipeline_status
        if status not in TERMINAL_PIPELINE_STATUSES:
            members_with_nonterminal.append((doc_id, str(status)))
        total_chunks += len(await content_store.get_all_chunks(doc_id))

    print(f"Edge type:    {edge_type}")
    print(f"Head:         {head_id}")
    print(f"Reason:       {reason}")
    print(f"Chain length: {chain_length}")
    print(f"Linear:       {is_linear}")
    print()
    print("Chain members (head -> tail):")
    for position, doc_id in enumerate(ordered_ids):
        d = member_docs[doc_id]
        print(
            f"  [{position}] {doc_id}\n"
            f"        title:         {d.title}\n"
            f"        version_label: {d.version_label}\n"
            f"        doc_type:      {d.doc_type}\n"
            f"        document_date: {d.document_date}"
        )
    print()
    print(f"Total content chunks across chain: {total_chunks}")
    print()

    if members_with_staging:
        print(
            f"refuse: pending staging edges reference chain member(s): "
            f"{', '.join(members_with_staging)}. Resolve them (confirm or dismiss) "
            "before purging.",
            file=sys.stderr,
        )
    if members_with_nonterminal:
        details = ", ".join(f"{doc_id}={status}" for doc_id, status in members_with_nonterminal)
        print(
            f"refuse: chain member(s) have non-terminal pipeline_status: {details}. "
            f"Terminal statuses are {sorted(_TERMINAL_STATUS_VALUES)}.",
            file=sys.stderr,
        )

    if not apply:
        print("(dry-run; pass --apply to execute)")
        # Surface refusals as a non-zero exit so a scripted dry-run detects them.
        if members_with_staging or members_with_nonterminal:
            return 3
        return 0

    if members_with_staging or members_with_nonterminal:
        return 3

    head_typed = input_fn(f"To confirm, retype the head id ({head_id}): ")
    if head_typed != head_id:
        print("refuse: typed head id did not match. Aborting.", file=sys.stderr)
        return 3

    length_typed = input_fn(f"To confirm, retype the chain length ({chain_length}): ")
    if length_typed.strip() != str(chain_length):
        print("refuse: typed chain length did not match. Aborting.", file=sys.stderr)
        return 3

    chain_id = str(uuid.uuid4())
    for doc_id in ordered_ids:
        result = await _internal._purge_one(
            document_id=doc_id,
            graph_store=graph_store,
            content_store=content_store,
            audit_sink=audit_sink,
            reason=reason,
            operation="purge_chain",
            chain_id=chain_id,
        )
        if not result.succeeded:
            print(
                f"error: chain purge halted on member {doc_id!r}: {result.error}. "
                "Earlier members in this invocation are already purged and "
                "audit-logged; later members are untouched.",
                file=sys.stderr,
            )
            return 4

    print(
        f"purge complete: chain of length {chain_length} from head {head_id} removed "
        f"(chain_id={chain_id})."
    )
    return 0


async def _run(
    *,
    vault_id: str,
    head_id: str,
    reason: str,
    edge_type: str,
    apply: bool,
    allow_branched: bool,
) -> int:
    opened = await _internal.open_vault_stores(vault_id)
    if opened is None:
        print(f"error: vault config not found for vault {vault_id!r}", file=sys.stderr)
        return 2
    graph_store, content_store, audit_sink, handle = opened
    try:
        return await purge_chain(
            graph_store=graph_store,
            content_store=content_store,
            audit_sink=audit_sink,
            head_id=head_id,
            reason=reason,
            edge_type=edge_type,
            apply=apply,
            allow_branched=allow_branched,
        )
    finally:
        await handle.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="sage.maintenance.purge_chain",
        description=(
            "Remove every member of a named edge chain from a SAGE vault. "
            "Operator-only; dry-run by default."
        ),
    )
    parser.add_argument("--vault", required=True, help="Vault id (e.g. example_vault).")
    parser.add_argument(
        "--head-id", required=True, help="Document id of the head of the chain to purge."
    )
    parser.add_argument(
        "--reason", required=True, help="Reason for the purge (recorded in the audit log)."
    )
    parser.add_argument(
        "--edge-type",
        default="supersedes",
        help="Edge type to walk for chain membership. Default: supersedes.",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Apply the cascade. Without this flag, prints a plan and exits.",
    )
    parser.add_argument(
        "--allow-branched",
        action="store_true",
        help=(
            "Permit purging a non-linear (branched) chain. Default refuses branched "
            "chains; this flag requires the operator to have verified the divergence "
            "is intentional collateral."
        ),
    )
    args = parser.parse_args(argv)
    return asyncio.run(
        _run(
            vault_id=args.vault,
            head_id=args.head_id,
            reason=args.reason,
            edge_type=args.edge_type,
            apply=args.apply,
            allow_branched=args.allow_branched,
        )
    )


if __name__ == "__main__":
    raise SystemExit(main())
