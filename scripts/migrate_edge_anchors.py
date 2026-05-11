#!/usr/bin/env python3
"""Backfill edge resolution_policy and anchor fields (CAS-ADR-017, Chunk 3).

Legacy edges written before Chunk 2 carry NULL resolution_policy and NULL
anchor columns. This script populates them according to the default edge-
type registry:

  - policy `none` (supersedes, retracts, merged_from):
      resolution_policy = 'none', anchors remain NULL.
  - policy `transitive_source` (derived_from):
      resolution_policy set; source_valid_from_version = source_id.
      target_valid_from_version stays NULL (null-means-not-applicable
      per CAS-ADR-017; target is frozen at derivation, version
      specificity already carried by target_id).
  - policy `transitive_both` (references, covers, bundles_with,
    depends_on, instantiated_from):
      resolution_policy set; source_valid_from_version = source_id,
      target_valid_from_version = target_id.
  - policy `TBD` (authoritative_for, sync_target):
      error. No rows are modified. Offending edges are printed to stderr.

Idempotent: rows whose resolution_policy is already non-NULL are skipped.

Usage:
    python scripts/migrate_edge_anchors.py                  # dry-run
    python scripts/migrate_edge_anchors.py --execute        # apply backfill
    python scripts/migrate_edge_anchors.py --vault OTHER_ID # different vault
    python scripts/migrate_edge_anchors.py --reverse        # dry-run reverse
    python scripts/migrate_edge_anchors.py --reverse --execute  # null columns

`--reverse` nulls all five ADR-017 edge columns (resolution_policy,
source_valid_from_version, target_valid_from_version, valid_until_version,
retracted_edge_id) across every edge. Blunt dev-only tool; does not
distinguish backfilled rows from validator-written rows.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from dataclasses import dataclass
from pathlib import Path

import yaml

from sage.models.edge_registry import EdgeTypeRegistry
from sage.models.enums import EdgeType, ResolutionPolicy


@dataclass
class BackfillPlan:
    none_edge_ids: list[str]
    transitive_source_edge_ids: list[tuple[str, str, str]]  # (id, source_id, target_id)
    transitive_target_edge_ids: list[tuple[str, str, str]]
    transitive_both_edge_ids: list[tuple[str, str, str]]
    tbd_edges: list[tuple[str, str]]  # (id, edge_type)
    unknown_edges: list[tuple[str, str]]  # edge_type not in registry

    @property
    def total_applicable(self) -> int:
        return (
            len(self.none_edge_ids)
            + len(self.transitive_source_edge_ids)
            + len(self.transitive_target_edge_ids)
            + len(self.transitive_both_edge_ids)
        )


@dataclass
class ReversePlan:
    affected_edge_ids: list[str]


def load_vault_config(vault_id: str) -> dict:
    config_path = Path.home() / "sage_vaults" / vault_id / "vault_config.yaml"
    if not config_path.exists():
        raise FileNotFoundError(f"Vault config not found: {config_path}")
    with open(config_path) as f:
        return yaml.safe_load(f)


def build_backfill_plan(conn: sqlite3.Connection, registry: EdgeTypeRegistry) -> BackfillPlan:
    """Scan edges with NULL resolution_policy and classify by policy."""
    plan = BackfillPlan(
        none_edge_ids=[],
        transitive_source_edge_ids=[],
        transitive_target_edge_ids=[],
        transitive_both_edge_ids=[],
        tbd_edges=[],
        unknown_edges=[],
    )
    rows = conn.execute(
        "SELECT id, source_id, target_id, edge_type FROM edges WHERE resolution_policy IS NULL"
    ).fetchall()
    for edge_id, source_id, target_id, edge_type_str in rows:
        try:
            edge_type = EdgeType(edge_type_str)
        except ValueError:
            plan.unknown_edges.append((edge_id, edge_type_str))
            continue
        try:
            policy = registry.policy_for(edge_type)
        except KeyError:
            plan.unknown_edges.append((edge_id, edge_type_str))
            continue
        if policy == ResolutionPolicy.TBD:
            plan.tbd_edges.append((edge_id, edge_type_str))
        elif policy == ResolutionPolicy.NONE:
            plan.none_edge_ids.append(edge_id)
        elif policy == ResolutionPolicy.TRANSITIVE_SOURCE:
            plan.transitive_source_edge_ids.append((edge_id, source_id, target_id))
        elif policy == ResolutionPolicy.TRANSITIVE_TARGET:
            plan.transitive_target_edge_ids.append((edge_id, source_id, target_id))
        elif policy == ResolutionPolicy.TRANSITIVE_BOTH:
            plan.transitive_both_edge_ids.append((edge_id, source_id, target_id))
        else:
            plan.unknown_edges.append((edge_id, edge_type_str))
    return plan


def apply_backfill(conn: sqlite3.Connection, plan: BackfillPlan) -> None:
    """Execute the backfill. Caller must verify plan.tbd_edges is empty."""
    if plan.tbd_edges:
        raise RuntimeError(
            "Refusing to apply backfill: TBD-policy edges present. "
            "Resolve policy assignments before migrating."
        )
    for edge_id in plan.none_edge_ids:
        conn.execute(
            "UPDATE edges SET resolution_policy = ? WHERE id = ?",
            (ResolutionPolicy.NONE.value, edge_id),
        )
    for edge_id, source_id, _target_id in plan.transitive_source_edge_ids:
        # transitive_source: target anchor is not applicable per CAS-ADR-017
        # (null-means-not-applicable). Target version specificity is
        # already carried by target_id; target_valid_from_version stays null.
        conn.execute(
            "UPDATE edges SET resolution_policy = ?, source_valid_from_version = ? WHERE id = ?",
            (
                ResolutionPolicy.TRANSITIVE_SOURCE.value,
                source_id,
                edge_id,
            ),
        )
    for edge_id, _source_id, target_id in plan.transitive_target_edge_ids:
        # Mirror of transitive_source: source anchor is not applicable.
        conn.execute(
            "UPDATE edges SET resolution_policy = ?, target_valid_from_version = ? WHERE id = ?",
            (
                ResolutionPolicy.TRANSITIVE_TARGET.value,
                target_id,
                edge_id,
            ),
        )
    for edge_id, source_id, target_id in plan.transitive_both_edge_ids:
        conn.execute(
            "UPDATE edges SET resolution_policy = ?, "
            "source_valid_from_version = ?, target_valid_from_version = ? "
            "WHERE id = ?",
            (
                ResolutionPolicy.TRANSITIVE_BOTH.value,
                source_id,
                target_id,
                edge_id,
            ),
        )


def build_reverse_plan(conn: sqlite3.Connection) -> ReversePlan:
    rows = conn.execute(
        "SELECT id FROM edges WHERE "
        "resolution_policy IS NOT NULL "
        "OR source_valid_from_version IS NOT NULL "
        "OR target_valid_from_version IS NOT NULL "
        "OR valid_until_version IS NOT NULL "
        "OR retracted_edge_id IS NOT NULL"
    ).fetchall()
    return ReversePlan(affected_edge_ids=[row[0] for row in rows])


def apply_reverse(conn: sqlite3.Connection) -> None:
    conn.execute(
        "UPDATE edges SET resolution_policy = NULL, "
        "source_valid_from_version = NULL, "
        "target_valid_from_version = NULL, "
        "valid_until_version = NULL, "
        "retracted_edge_id = NULL"
    )


def run_backfill(
    db_path: Path,
    execute: bool,
    registry: EdgeTypeRegistry | None = None,
    out=None,
    err=None,
) -> int:
    """Run the forward backfill. Returns exit code (0 on success, 2 on TBD)."""
    registry = registry or EdgeTypeRegistry.default()
    out = out if out is not None else sys.stdout
    err = err if err is not None else sys.stderr
    conn = sqlite3.connect(str(db_path))
    try:
        plan = build_backfill_plan(conn, registry)
        print(f"Edges needing backfill: {plan.total_applicable}", file=out)
        print(f"  policy=none:              {len(plan.none_edge_ids)}", file=out)
        print(
            f"  policy=transitive_source: {len(plan.transitive_source_edge_ids)}",
            file=out,
        )
        print(
            f"  policy=transitive_target: {len(plan.transitive_target_edge_ids)}",
            file=out,
        )
        print(
            f"  policy=transitive_both:   {len(plan.transitive_both_edge_ids)}",
            file=out,
        )
        if plan.unknown_edges:
            print(
                f"\nWARNING: {len(plan.unknown_edges)} edges with unknown edge_type:",
                file=err,
            )
            for edge_id, edge_type in plan.unknown_edges:
                print(f"  {edge_id}: edge_type={edge_type!r}", file=err)
        if plan.tbd_edges:
            print(
                f"\nERROR: {len(plan.tbd_edges)} edges with TBD-policy "
                f"edge_type (authoritative_for, sync_target). No changes made.",
                file=err,
            )
            for edge_id, edge_type in plan.tbd_edges:
                print(f"  {edge_id}: edge_type={edge_type}", file=err)
            print(
                "Resolve these edges (delete or reassign policy) before migrating.",
                file=err,
            )
            return 2
        if plan.unknown_edges:
            print(
                "Refusing to proceed with unknown edge_types in the table.",
                file=err,
            )
            return 2
        if not execute:
            print(
                f"\nDry run. Use --execute to apply backfill to {plan.total_applicable} edges.",
                file=out,
            )
            return 0
        apply_backfill(conn, plan)
        conn.commit()
        print(f"\nBackfilled {plan.total_applicable} edges.", file=out)
        return 0
    finally:
        conn.close()


def run_reverse(db_path: Path, execute: bool, out=None) -> int:
    out = out if out is not None else sys.stdout
    conn = sqlite3.connect(str(db_path))
    try:
        plan = build_reverse_plan(conn)
        print(
            f"Edges with ADR-017 columns set: {len(plan.affected_edge_ids)}",
            file=out,
        )
        if not execute:
            print(
                f"\nDry run. Use --execute to null the five ADR-017 edge "
                f"columns on {len(plan.affected_edge_ids)} edges.",
                file=out,
            )
            return 0
        apply_reverse(conn)
        conn.commit()
        print(
            f"\nReversed ADR-017 columns on {len(plan.affected_edge_ids)} edges.",
            file=out,
        )
        return 0
    finally:
        conn.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Backfill edge resolution_policy and anchor fields (CAS-ADR-017)"
    )
    parser.add_argument(
        "--vault",
        default="pim_health",
        help="Vault ID (default: pim_health)",
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Apply changes (default: dry-run)",
    )
    parser.add_argument(
        "--reverse",
        action="store_true",
        help="Null all five ADR-017 edge columns. Dev-only.",
    )
    args = parser.parse_args(argv)

    config = load_vault_config(args.vault)
    brain_root = Path(config["vault"]["brain_root"]).expanduser().resolve()
    db_path = brain_root / "graph.db"
    if not db_path.exists():
        print(f"Graph database not found: {db_path}", file=sys.stderr)
        return 1

    print(f"Vault:      {args.vault}")
    print(f"Brain root: {brain_root}")
    print(f"DB:         {db_path}")
    print(
        f"Mode:       {'REVERSE' if args.reverse else 'FORWARD'} "
        f"({'EXECUTE' if args.execute else 'DRY-RUN'})"
    )
    print()

    if args.reverse:
        return run_reverse(db_path, args.execute)
    return run_backfill(db_path, args.execute)


if __name__ == "__main__":
    sys.exit(main())
