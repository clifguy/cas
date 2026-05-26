"""Chain-selector bulk purge (permanently out-of-band per CAS-ADR-029).

Removes every member of a named edge chain (default ``supersedes``) from a
SAGE vault as a single operator-invoked action. Shares the per-document
cascade with ``purge_document`` via
``sage.maintenance._internal._purge_one`` and adds a chain-level
confirmation gate: typed head id + typed chain length.

Safeguards (SAGE-Architecture v2.1 §6.4 No-Delete Invariant; CAS-ADR-029 v1.1):

- Dry-run is the default. ``--apply`` is required for any state change.
- Chain membership is resolved by walking the named edge type from the head
  via the same recursive-CTE logic as ``sage_chain``.
- ``--head-id`` is validated to be a genuine head (no inbound edges of the
  named type); a middle or tail id is refused with the actual head ids
  surfaced.
- Non-linear (branched) chains are refused unless ``--allow-branched`` is
  set; the flag exists for the case where the operator has verified that
  the divergence is intentional collateral.
- The whole chain is refused if any member has pending staging edges or a
  non-terminal ``pipeline_status``. No partial proceed.
- ``--apply`` prompts for the head id and the chain length, separately.
  Mismatch on either refuses the batch.
- Per-member cascade is the same as Document row + tags + edges +
  staging edges + LanceDB chunks. Audit record is written **before** each
  member's SQLite mutation; on a per-member failure the loop halts and
  earlier members stay deleted with audit records.
- Each invocation generates a UUID ``chain_id`` that is shared across every
  audit entry from the same run, distinct from ``batch_id``.

This module is operator-only by architectural invariant: nothing in
``sage.mcp_server`` or ``sage.api`` may import it (enforced by).

Usage::

    .venv/bin/python -m sage.maintenance.purge_chain \\
        --vault VAULT --head-id ID --reason TEXT \\
        [--edge-type TYPE] [--apply] [--allow-branched]
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
import uuid
from pathlib import Path

from sage.config import load_vault_config
from sage.maintenance import _internal
from sage.models.enums import TERMINAL_PIPELINE_STATUSES
from sage.vault_management import config_path_for_vault

# Rebind point for tests that need to simulate SQLite failures.
_sqlite_connect = sqlite3.connect

_TERMINAL_STATUS_VALUES: frozenset[str] = frozenset(s.value for s in TERMINAL_PIPELINE_STATUSES)


def _resolve_paths(vault_id: str) -> tuple[Path, Path, Path] | None:
    """Return (vault_dir, sqlite_path, lancedb_dir) or None if config missing."""
    config_path = config_path_for_vault(vault_id)
    if not config_path.exists():
        print(
            f"error: vault config not found for vault {vault_id!r}: {config_path}",
            file=sys.stderr,
        )
        return None
    config = load_vault_config(config_path)
    brain_root = Path(config.vault.brain_root).expanduser()
    return config_path.parent, brain_root / "graph.db", brain_root / "lancedb"


def purge_chain(
    *,
    vault_id: str,
    head_id: str,
    reason: str,
    edge_type: str = "supersedes",
    apply: bool = False,
    allow_branched: bool = False,
) -> int:
    """Service-level entry point. Returns 0 on success, non-zero on refusal/failure."""
    paths = _resolve_paths(vault_id)
    if paths is None:
        return 2
    vault_dir, sqlite_path, lancedb_dir = paths

    if not sqlite_path.exists():
        print(
            f"error: graph.db not found for vault {vault_id!r}: {sqlite_path}",
            file=sys.stderr,
        )
        return 2

    conn = _sqlite_connect(sqlite_path)
    try:
        # Confirm the head document exists at all.
        head_row = _internal._fetch_document_row(conn, head_id)
        if head_row is None:
            print(
                f"error: document {head_id!r} not found in vault {vault_id!r}",
                file=sys.stderr,
            )
            return 2

        # Walk the chain in both directions from head_id.
        walk = _internal._walk_chain(conn, head_id, edge_type)
        documents = walk["documents"]
        edges = walk["edges"]

        # Validate the supplied id is genuinely the head: zero inbound edges
        # of the named type. For a linear supersedes chain there is exactly
        # one such id; for branched chains there can be multiple.
        actual_heads = _internal._chain_head_ids(documents, edges)
        if head_id not in actual_heads:
            print(
                f"refuse: --head-id {head_id!r} is not the head of the "
                f"{edge_type} chain. Actual head id(s): "
                f"{', '.join(actual_heads) if actual_heads else '(none)'}.",
                file=sys.stderr,
            )
            return 3

        # Linearity safeguard.
        is_linear = _internal._chain_is_linear(documents, edges)
        if not is_linear and not allow_branched:
            print(
                f"refuse: {edge_type} chain from {head_id!r} is non-linear "
                f"(branched). Use --allow-branched to override after verifying "
                f"the divergence is intentional.",
                file=sys.stderr,
            )
            return 3

        # Order the chain head-first for display and iteration.
        ordered_ids = _internal._order_chain_from_head(documents, edges, head_id)
        doc_map = {d["doc_id"]: d for d in documents}
        chain_length = len(ordered_ids)

        # Per-member precondition scan (whole chain).
        members_with_staging: list[str] = []
        members_with_nonterminal: list[tuple[str, str]] = []
        for doc_id in ordered_ids:
            if _internal._list_staging_edge_ids(conn, doc_id):
                members_with_staging.append(doc_id)
            status = doc_map[doc_id]["pipeline_status"]
            if status not in _TERMINAL_STATUS_VALUES:
                members_with_nonterminal.append((doc_id, status))

        # Cumulative chunk count for the dry-run summary.
        total_chunks = sum(
            _internal._lancedb_chunk_count(lancedb_dir, doc_id) for doc_id in ordered_ids
        )

        # Dry-run enumeration. Print first so the operator sees the chain
        # state before any refusal message that follows.
        print(f"Vault:        {vault_id}")
        print(f"Edge type:    {edge_type}")
        print(f"Head:         {head_id}")
        print(f"Reason:       {reason}")
        print(f"Chain length: {chain_length}")
        print(f"Linear:       {is_linear}")
        print()
        print("Chain members (head -> tail):")
        for position, doc_id in enumerate(ordered_ids):
            d = doc_map[doc_id]
            print(
                f"  [{position}] {doc_id}\n"
                f"        title:           {d['title']}\n"
                f"        version_label:   {d['version_label']}\n"
                f"        doc_type:        {d['doc_type']}\n"
                f"        document_date:   {d['document_date']}"
            )
        print()
        print(f"Total LanceDB chunks across chain: {total_chunks}")
        print()

        # Precondition refusals (applied at --apply time; surface in dry-run
        # output too so the operator sees them before deciding to --apply).
        if members_with_staging:
            print(
                f"refuse: pending staging edges reference chain member(s): "
                f"{', '.join(members_with_staging)}. Resolve them (confirm or "
                "dismiss) before purging.",
                file=sys.stderr,
            )
            if apply:
                return 3

        if members_with_nonterminal:
            details = ", ".join(f"{doc_id}={status}" for doc_id, status in members_with_nonterminal)
            print(
                f"refuse: chain member(s) have non-terminal pipeline_status: "
                f"{details}. Terminal statuses are {sorted(_TERMINAL_STATUS_VALUES)}.",
                file=sys.stderr,
            )
            if apply:
                return 3

        if not apply:
            print("(dry-run; pass --apply to execute)")
            # Even in dry-run, surface refusals as a non-zero exit so a
            # scripted dry-run can detect blocked state.
            if members_with_staging or members_with_nonterminal:
                return 3
            return 0

        # Typed confirmation: head id, then chain length.
        head_typed = input(f"To confirm, retype the head id ({head_id}): ")
        if head_typed != head_id:
            print(
                "refuse: typed head id did not match. Aborting.",
                file=sys.stderr,
            )
            return 3

        length_typed = input(f"To confirm, retype the chain length ({chain_length}): ")
        if length_typed.strip() != str(chain_length):
            print(
                "refuse: typed chain length did not match. Aborting.",
                file=sys.stderr,
            )
            return 3

        # Apply: shared chain_id across every audit entry.
        chain_id = str(uuid.uuid4())

        for doc_id in ordered_ids:
            result = _internal._purge_one(
                document_id=doc_id,
                conn=conn,
                lancedb_dir=lancedb_dir,
                vault_dir=vault_dir,
                reason=reason,
                chain_id=chain_id,
            )
            if not result.succeeded:
                print(
                    f"error: chain purge halted on member {doc_id!r}: "
                    f"{result.error}. Earlier members in this invocation are "
                    f"already purged and audit-logged. Later members are "
                    f"untouched.",
                    file=sys.stderr,
                )
                return 4

        print(
            f"purge complete: chain of length {chain_length} from head "
            f"{head_id} removed from vault {vault_id!r} (chain_id={chain_id})."
        )
        return 0
    finally:
        conn.close()


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
        "--head-id",
        required=True,
        help="Document id of the head of the chain to purge.",
    )
    parser.add_argument(
        "--reason",
        required=True,
        help="Reason for the purge (recorded in the audit log).",
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
            "Permit purging a non-linear (branched) chain. Default refuses "
            "branched chains; this flag requires the operator to have verified "
            "the divergence is intentional collateral."
        ),
    )
    args = parser.parse_args(argv)
    return purge_chain(
        vault_id=args.vault,
        head_id=args.head_id,
        reason=args.reason,
        edge_type=args.edge_type,
        apply=args.apply,
        allow_branched=args.allow_branched,
    )


if __name__ == "__main__":
    raise SystemExit(main())
