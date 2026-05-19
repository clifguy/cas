#!/usr/bin/env python3
"""Dedup the edges and staging_edges tables before T-0079's UNIQUE index lands.

The T-0079 migration adds a UNIQUE index on (source_id, target_id, edge_type)
to both ``edges`` and ``staging_edges``. SQLite's CREATE UNIQUE INDEX
fails if the underlying table already contains duplicates, so this script
must run first to backfill the deduplication.

Selection logic (kept edge): the row with the oldest ``created_at``;
tiebreak by lexicographic ``id`` for determinism. All other rows in the
duplicate group are dropped. Rationale: the first-written edge is the
canonical provenance under SAGE's single-source-of-truth principle.

Divergent-rationale safeguard: a duplicate group whose rationale strings
are not all identical represents state the application has been
tolerating. The default ``--apply`` behavior refuses to delete any of
those rows; passing ``--force-divergent-rationale`` overrides the gate
after the operator reviews the JSON audit trail.

Audit trail: every duplicate group emits a JSON line to stdout listing
the kept id, the dropped ids, the (possibly differing) rationales, and
the divergent-rationale flag. The output is auditable post-hoc and
sufficient to reconstruct what was removed.

Usage:
    python -m scripts.dedup_edges --vault <vault_id>
    python -m scripts.dedup_edges --vault <vault_id> --apply
    python -m scripts.dedup_edges --vault <vault_id> --apply --force-divergent-rationale

Operational note: run offline (no live SAGE readers/writers). The
subsequent CREATE UNIQUE INDEX briefly locks the table; running this
script and the migration together avoids WAL checkpoint contention.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

import yaml

# Tables targeted by T-0079, with their natural-key column tuple and
# a per-table label used in the JSON audit trail.
_TARGET_TABLES: tuple[tuple[str, tuple[str, str, str]], ...] = (
    ("edges", ("source_id", "target_id", "edge_type")),
    ("staging_edges", ("source_id", "target_id", "edge_type")),
)


@dataclass
class DuplicateGroup:
    """A set of rows sharing the natural-key triple."""

    table: str
    source_id: str
    target_id: str | None
    edge_type: str
    rows: list[sqlite3.Row] = field(default_factory=list)

    @property
    def rationales(self) -> list[str | None]:
        # `staging_edges` has no rationale column; use inference_evidence
        # as the analogous provenance string for divergent-content gating.
        col = "rationale" if "rationale" in self.rows[0].keys() else "inference_evidence"
        return [row[col] for row in self.rows]

    @property
    def divergent(self) -> bool:
        provenance = self.rationales
        return len(set(provenance)) > 1

    def keep_and_drop(self) -> tuple[sqlite3.Row, list[sqlite3.Row]]:
        """Return (kept, dropped) per the selection rule.

        Oldest by ``created_at`` (lexicographic ISO-8601 sort is correct
        for the stored format), tiebreak by lexicographic ``id``.
        """
        ordered = sorted(self.rows, key=lambda r: (r["created_at"], r["id"]))
        return ordered[0], ordered[1:]


def load_vault_config(vault_id: str) -> dict:
    config_path = Path.home() / "sage_vaults" / vault_id / "vault_config.yaml"
    if not config_path.exists():
        raise FileNotFoundError(f"Vault config not found: {config_path}")
    with open(config_path) as f:
        return yaml.safe_load(f)


def resolve_db_path(vault_config: dict) -> Path:
    """Resolve the graph store SQLite path from the vault config.

    SAGE convention (mcp_init.py): the graph store lives at
    ``{brain_root}/graph.db``.
    """
    vault = vault_config.get("vault") or {}
    brain_root_raw = vault.get("brain_root")
    if brain_root_raw is None:
        raise KeyError("Vault config has no vault.brain_root")
    return Path(str(brain_root_raw)).expanduser() / "graph.db"


def find_duplicate_groups(
    conn: sqlite3.Connection, table: str, key_cols: tuple[str, str, str]
) -> list[DuplicateGroup]:
    """Return one DuplicateGroup per (source_id, target_id, edge_type)
    triple that has more than one row in ``table``."""
    src, tgt, etype = key_cols
    # SQLite GROUP BY treats NULL target_id values as distinct, matching
    # the desired UNIQUE-index behavior (`retracts` rows with NULL
    # target_id are not duplicates of each other). The HAVING COUNT(*) > 1
    # surfaces only real duplicate groups.
    rows = conn.execute(
        f"SELECT {src} AS src, {tgt} AS tgt, {etype} AS etype, COUNT(*) AS cnt "  # noqa: S608 -- key_cols values from trusted _TARGET_TABLES constant
        f"FROM {table} "
        f"GROUP BY {src}, {tgt}, {etype} "
        f"HAVING cnt > 1"
    ).fetchall()

    groups: list[DuplicateGroup] = []
    for row in rows:
        if row["tgt"] is None:
            triplet_rows = conn.execute(
                f"SELECT * FROM {table} "  # noqa: S608 -- table value is trusted (_TARGET_TABLES)
                f"WHERE {src} = ? AND {tgt} IS NULL AND {etype} = ?",
                (row["src"], row["etype"]),
            ).fetchall()
        else:
            triplet_rows = conn.execute(
                f"SELECT * FROM {table} "  # noqa: S608 -- table value is trusted (_TARGET_TABLES)
                f"WHERE {src} = ? AND {tgt} = ? AND {etype} = ?",
                (row["src"], row["tgt"], row["etype"]),
            ).fetchall()
        groups.append(
            DuplicateGroup(
                table=table,
                source_id=row["src"],
                target_id=row["tgt"],
                edge_type=row["etype"],
                rows=list(triplet_rows),
            )
        )
    return groups


def emit_audit_record(group: DuplicateGroup, kept: sqlite3.Row, dropped: list[sqlite3.Row]) -> None:
    """Write a single JSON line to stdout describing the dedup decision."""
    record = {
        "table": group.table,
        "source_id": group.source_id,
        "target_id": group.target_id,
        "edge_type": group.edge_type,
        "kept": kept["id"],
        "dropped": [row["id"] for row in dropped],
        "rationales": group.rationales,
        "divergent_rationale": group.divergent,
    }
    print(json.dumps(record))


def delete_rows(conn: sqlite3.Connection, table: str, ids: Iterable[str]) -> int:
    """Delete the given ids from `table`. Returns rows-deleted count."""
    id_list = list(ids)
    if not id_list:
        return 0
    placeholders = ",".join("?" for _ in id_list)
    cur = conn.execute(
        f"DELETE FROM {table} WHERE id IN ({placeholders})",  # noqa: S608 -- table value is trusted (_TARGET_TABLES); ids are placeholders
        id_list,
    )
    return cur.rowcount


def run(
    db_path: Path,
    *,
    apply: bool,
    force_divergent_rationale: bool,
) -> int:
    """Drive the dedup pass against the SQLite DB at ``db_path``.

    Returns a process exit code: 0 on success, 1 if divergent-rationale
    duplicates are present and the override flag was not set (only
    applies when ``apply=True``).
    """
    if not db_path.exists():
        print(f"Database not found: {db_path}", file=sys.stderr)
        return 2

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        all_groups: list[DuplicateGroup] = []
        for table, key_cols in _TARGET_TABLES:
            groups = find_duplicate_groups(conn, table, key_cols)
            all_groups.extend(groups)

        if not all_groups:
            print("# no duplicate (source_id, target_id, edge_type) tuples found", file=sys.stderr)
            return 0

        # Emit audit records for every group; this happens whether or
        # not --apply was passed.
        divergent_groups: list[DuplicateGroup] = []
        kept_dropped: list[tuple[DuplicateGroup, sqlite3.Row, list[sqlite3.Row]]] = []
        for group in all_groups:
            kept, dropped = group.keep_and_drop()
            emit_audit_record(group, kept, dropped)
            kept_dropped.append((group, kept, dropped))
            if group.divergent:
                divergent_groups.append(group)

        if not apply:
            print(
                f"# dry run: {len(all_groups)} duplicate group(s); "
                f"{len(divergent_groups)} with divergent rationale. "
                f"Re-run with --apply to delete.",
                file=sys.stderr,
            )
            return 0

        if divergent_groups and not force_divergent_rationale:
            print(
                f"# refused: {len(divergent_groups)} duplicate group(s) have "
                f"divergent rationale strings. Review the audit trail above, "
                f"then re-run with --apply --force-divergent-rationale to "
                f"proceed.",
                file=sys.stderr,
            )
            return 1

        deleted_total = 0
        for group, _kept, dropped in kept_dropped:
            deleted_total += delete_rows(conn, group.table, (row["id"] for row in dropped))
        conn.commit()
        print(
            f"# applied: deleted {deleted_total} duplicate row(s) "
            f"across {len(all_groups)} group(s)",
            file=sys.stderr,
        )
        return 0
    finally:
        conn.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--vault",
        required=False,
        help="Vault id under ~/sage_vaults/<id>/vault_config.yaml. Mutually exclusive with --db.",
    )
    parser.add_argument(
        "--db",
        required=False,
        type=Path,
        help="Direct path to the SQLite database (for tests).",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Delete duplicate rows. Without this flag the script is "
        "a dry run that only emits the audit trail.",
    )
    parser.add_argument(
        "--force-divergent-rationale",
        action="store_true",
        help="Allow --apply to proceed even when duplicate groups have "
        "divergent rationale strings. Requires reviewing the audit trail.",
    )
    args = parser.parse_args()

    if (args.vault is None) == (args.db is None):
        parser.error("Specify exactly one of --vault or --db")

    if args.db is not None:
        db_path = args.db
    else:
        vault_config = load_vault_config(args.vault)
        db_path = resolve_db_path(vault_config)

    return run(
        db_path,
        apply=args.apply,
        force_divergent_rationale=args.force_divergent_rationale,
    )


if __name__ == "__main__":
    sys.exit(main())
