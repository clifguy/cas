#!/usr/bin/env python3
"""Re-mint edge and staging-edge ids that violate the UUID contract.

The ``edges.id`` and ``staging_edges.id`` columns carry RFC 4122 UUIDs,
enforced at the validated API boundary. Rows written before boundary
validation existed (hand repairs, historical imports) can carry
slug-style ids that every strict read surface — and the vault-to-
Postgres migration — rejects. No validated surface can delete or
rewrite such a row: the id fails validation before the operation
reaches storage. This script is the offline repair path.

Mechanism: each malformed id is rewritten **in place** (``UPDATE ...
SET id``), so every other column — provenance fields, timestamps,
anchors — survives byte-for-byte; the row is never deleted and
recreated. On the edges table, rows whose ``retracted_edge_id``
references the old id are cascade-updated to the new id inside the same
per-row transaction. Apply mode iterates scan/repair passes to a
fixpoint, so a malformed ``retracts`` row that references another
malformed id becomes repairable once its target has been re-minted.

Repairability: a row is repaired only if substituting a fresh UUID for
its id makes the full row validate through the canonical row
projection (the same hydration strict reads use). Rows that stay
invalid under a fresh id — an unknown edge_type, an unparseable
timestamp — are reported and left untouched.

Out of scope: ids that ``uuid.UUID`` parses but stores non-canonically
(strict reads already accept them), and duplicate natural-key rows
(see ``scripts.dedup_edges``).

Audit trail: one JSON line per malformed row (table, old_id, new_id,
source_id, target_id, edge_type, created_at, repairable, error,
referrer_count) followed by one JSON summary line (per-table scanned /
malformed / repaired / unrepairable totals, the apply flag, and the
aggregate referrer-update count). The output is auditable post-hoc and
sufficient to reconstruct every rewrite.

Usage:
    python -m scripts.repair_edge_ids --vault <vault_id>
    python -m scripts.repair_edge_ids --vault <vault_id> --apply

Exit codes: 0 on success (including a dry run with findings), 1 when
``--apply`` left unrepairable rows behind, 2 when the database is
missing.

Operational note: run offline (no live SAGE readers/writers). Long-
running server processes hold pooled connections and must be restarted
afterward regardless, since they may cache rows by id.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import uuid
from pathlib import Path
from typing import Callable

from sage.services._dry_run import DRY_RUN_SENTINEL_EDGE_ID
from sage.storage.graph_store import SqliteGraphStore
from scripts.dedup_edges import load_vault_config, resolve_db_path

# Tables whose id column carries the UUID contract, with the canonical
# row projection each is probed through. Only ``edges`` has a column
# that references edge ids (``retracted_edge_id``), so only it cascades.
_TARGET_TABLES: tuple[str, ...] = ("edges", "staging_edges")

_PROBES: dict[str, Callable] = {
    "edges": SqliteGraphStore._row_to_edge,
    "staging_edges": SqliteGraphStore._row_to_staging_edge,
}

# Attempts at minting a unique replacement id before reporting the row
# as failed. A uuid4 collision is negligible in practice; the bound
# exists so a broken generator cannot loop forever.
_MINT_ATTEMPTS = 3


def _new_edge_id() -> str:
    """Mint a replacement id. Module-level so tests can substitute it."""
    return str(uuid.uuid4())


def _is_malformed(raw_id: object) -> bool:
    """True when the stored id does not parse as a UUID.

    This is deliberately the same predicate the id validator applies, so
    parseable-but-noncanonical ids (which strict reads accept) are out
    of scope.
    """
    try:
        uuid.UUID(str(raw_id))
    except ValueError:
        return True
    return False


def _table_columns(conn: sqlite3.Connection, table: str) -> list[str]:
    return [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]


def _probe_error(conn: sqlite3.Connection, table: str, raw_id: str) -> str | None:
    """Re-validate the row with a well-formed id substituted in.

    Selects the live row with its id column aliased to the nil-UUID
    sentinel and hydrates it through the canonical projection. Returns
    None when the row would validate cleanly under a fresh id, else the
    validation error. Probing the live row (not a scan snapshot) keeps
    classification current as cross-row repairs land.
    """
    cols = _table_columns(conn, table)
    select_clause = ", ".join("? AS id" if c == "id" else c for c in cols)
    row = conn.execute(
        f"SELECT {select_clause} FROM {table} WHERE id = ?",  # noqa: S608 -- table/columns from sqlite metadata of trusted _TARGET_TABLES
        (DRY_RUN_SENTINEL_EDGE_ID, raw_id),
    ).fetchone()
    try:
        _PROBES[table](row)
    except Exception as exc:  # noqa: BLE001 -- per-row containment: classify, never abort
        return str(exc)
    return None


def _count_referrers(conn: sqlite3.Connection, table: str, raw_id: str) -> int:
    if table != "edges":
        return 0
    row = conn.execute(
        "SELECT COUNT(*) FROM edges WHERE retracted_edge_id = ?", (raw_id,)
    ).fetchone()
    return int(row[0])


def _repair_row(conn: sqlite3.Connection, table: str, raw_id: str) -> tuple[str, int] | None:
    """Rewrite one id in place; cascade edge referrers in the same transaction.

    Returns ``(new_id, referrer_update_count)`` on success, None when no
    unique replacement id could be minted. The transaction is per-row:
    a failure rolls back both the id rewrite and the cascade, leaving
    earlier repairs committed.
    """
    for _ in range(_MINT_ATTEMPTS):
        new_id = _new_edge_id()
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                f"UPDATE {table} SET id = ? WHERE id = ?",  # noqa: S608 -- table from trusted _TARGET_TABLES
                (new_id, raw_id),
            )
            referrers = 0
            if table == "edges":
                cur = conn.execute(
                    "UPDATE edges SET retracted_edge_id = ? WHERE retracted_edge_id = ?",
                    (new_id, raw_id),
                )
                referrers = cur.rowcount
            conn.execute("COMMIT")
            return new_id, referrers
        except sqlite3.IntegrityError:
            conn.execute("ROLLBACK")
            continue
        except Exception:
            conn.execute("ROLLBACK")
            raise
    return None


def _entry(
    table: str,
    row: dict,
    *,
    new_id: str | None,
    repairable: bool,
    error: str | None,
    referrer_count: int,
) -> dict:
    keys = row.keys()
    return {
        "table": table,
        "old_id": row["id"],
        "new_id": new_id,
        "source_id": row["source_id"] if "source_id" in keys else None,
        "target_id": row["target_id"] if "target_id" in keys else None,
        "edge_type": row["edge_type"] if "edge_type" in keys else None,
        "created_at": row["created_at"] if "created_at" in keys else None,
        "repairable": repairable,
        "error": error,
        "referrer_count": referrer_count,
    }


def run(db_path: Path, *, apply: bool = False) -> int:
    """Drive the repair pass against the SQLite DB at ``db_path``.

    Returns a process exit code: 0 on success, 1 if ``apply`` left
    unrepairable rows behind, 2 if the database file is missing.
    """
    if not db_path.exists():
        print(f"Database not found: {db_path}", file=sys.stderr)
        return 2

    conn = sqlite3.connect(str(db_path), isolation_level=None)
    conn.row_factory = sqlite3.Row
    try:
        scanned: dict[str, int] = {}
        malformed: dict[str, dict[str, dict]] = {}
        for table in _TARGET_TABLES:
            rows = conn.execute(f"SELECT * FROM {table}").fetchall()  # noqa: S608 -- table from trusted _TARGET_TABLES
            scanned[table] = len(rows)
            malformed[table] = {r["id"]: dict(r) for r in rows if _is_malformed(r["id"])}

        repaired: dict[str, dict[str, tuple[str, int]]] = {t: {} for t in _TARGET_TABLES}
        if apply:
            # Iterate to a fixpoint: repairing one row can make another
            # repairable (a retracts row referencing a re-minted id).
            progress = True
            while progress:
                progress = False
                for table in _TARGET_TABLES:
                    pending = [
                        raw_id for raw_id in malformed[table] if raw_id not in repaired[table]
                    ]
                    for raw_id in pending:
                        if _probe_error(conn, table, raw_id) is not None:
                            continue
                        result = _repair_row(conn, table, raw_id)
                        if result is not None:
                            repaired[table][raw_id] = result
                            progress = True

        entries: list[dict] = []
        unrepairable_total = 0
        referrers_updated = 0
        for table in _TARGET_TABLES:
            for raw_id, row in malformed[table].items():
                if raw_id in repaired[table]:
                    new_id, referrers = repaired[table][raw_id]
                    referrers_updated += referrers
                    entries.append(
                        _entry(
                            table,
                            row,
                            new_id=new_id,
                            repairable=True,
                            error=None,
                            referrer_count=referrers,
                        )
                    )
                    continue
                error = _probe_error(conn, table, raw_id)
                repairable = error is None
                if not repairable:
                    unrepairable_total += 1
                entries.append(
                    _entry(
                        table,
                        row,
                        new_id=None,
                        repairable=repairable,
                        error=error,
                        referrer_count=_count_referrers(conn, table, raw_id),
                    )
                )

        for entry in entries:
            print(json.dumps(entry))

        totals = {
            table: {
                "scanned": scanned[table],
                "malformed": len(malformed[table]),
                "repaired": len(repaired[table]),
                "unrepairable": sum(
                    1 for e in entries if e["table"] == table and not e["repairable"]
                ),
            }
            for table in _TARGET_TABLES
        }
        print(
            json.dumps(
                {
                    "totals": totals,
                    "apply": apply,
                    "referrers_updated": referrers_updated,
                }
            )
        )

        malformed_total = sum(len(m) for m in malformed.values())
        repaired_total = sum(len(r) for r in repaired.values())
        if not apply:
            print(
                f"# dry run: {malformed_total} malformed id(s) found. "
                f"Re-run with --apply to re-mint.",
                file=sys.stderr,
            )
            return 0
        print(
            f"# applied: re-minted {repaired_total} id(s); "
            f"{unrepairable_total} unrepairable row(s) remain.",
            file=sys.stderr,
        )
        return 1 if unrepairable_total else 0
    finally:
        conn.close()


def main(argv: list[str] | None = None) -> int:
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
        help="Re-mint malformed ids. Without this flag the script is "
        "a dry run that only emits the audit trail.",
    )
    args = parser.parse_args(argv)

    if (args.vault is None) == (args.db is None):
        parser.error("Specify exactly one of --vault or --db")

    if args.db is not None:
        db_path = args.db
    else:
        vault_config = load_vault_config(args.vault)
        db_path = resolve_db_path(vault_config)

    return run(db_path, apply=args.apply)


if __name__ == "__main__":
    sys.exit(main())
