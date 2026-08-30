#!/usr/bin/env python3
"""Repair stored semantic abstracts by excising type-restating openers.

The stored-output disposition of CAS-ADR-020 clause (f): for every
document whose stored abstract opens by classifying the document as an
instance of its own type, the classifying frame is cut and the relative
clause's finite verb spliced onto the deictic subject -- the same repair
the ingestion seam applies to new abstracts -- written in place with no
regeneration, since greedy decoding would reproduce the breached output.

Repair is licensed for one shape only: a relative clause standing
directly against the type phrase, where the clause attaches to the
document itself. Measured against stored abstracts, that shape is a
minority of findings. The rest are planned, reported, and deliberately
left alone: a participial or prepositional modifier would need a finite
verb composed rather than cut, and a relativizer further out in the
sentence attaches to something the document merely mentions, so excising
to it would assert something false in fluent prose. The plan's
``residual`` is what distinguishes the two, and the apply path writes
only where it is empty.

Dry-run by default: the per-document plan prints and nothing is written.
``--apply`` writes each writable abstract through the ingestion service
(which refreshes the retrieval header chunk) and appends one operation
record to the vault's maintenance log.

Usage::

    .venv/bin/python scripts/repair_abstraction_type_openers.py            # dry run, cas vault
    .venv/bin/python scripts/repair_abstraction_type_openers.py --apply
    .venv/bin/python scripts/repair_abstraction_type_openers.py my_vault --lifecycle active

Exit code 0 on completion (plans or none); 2 when the vault config
cannot be found.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

# The audit script owns the entry catalog this planner consumes; make its
# package importable when this file is run by path rather than as a module.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from sage.adapters.abstraction_utils import (  # noqa: E402
    TypeRestatingOpener,
    find_type_restating_opener,
    strip_type_restating_opener,
)
from sage.adapters.stubs import StubAbstractionProvider  # noqa: E402
from sage.config import load_vault_config  # noqa: E402
from sage.mcp_init import initialize_services  # noqa: E402
from sage.services.maintenance_log import MAINTENANCE_LOG_FILENAME  # noqa: E402
from sage.vault_management import config_path_for_vault  # noqa: E402
from scripts.audit_abstraction_glosses import AuditEntry, build_entries  # noqa: E402


@dataclass(frozen=True)
class RepairPlan:
    """One document's planned abstract repair."""

    doc_id: str
    lifecycle_status: str
    doc_type: str | None
    openers: tuple[TypeRestatingOpener, ...]
    abstract_before: str
    abstract_after: str
    residual: tuple[TypeRestatingOpener, ...]


def plan_repairs(
    entries: Iterable[AuditEntry], lifecycles: frozenset[str] | None = None
) -> list[RepairPlan]:
    """Plan the excision for every entry carrying a type-restating opener.

    Pure core of the repair: storage access and writes happen in ``run``,
    so the planner is exercisable against a synthetic catalog.
    ``residual`` holds any finding the detector still reports on the
    planned text. Unlike the clause (e) planner, a non-empty residual
    here is expected rather than anomalous -- it is how the unlicensed
    shapes declare themselves -- and the apply path declines exactly
    those, so detection and repair cannot drift apart.
    """
    plans: list[RepairPlan] = []
    for entry in entries:
        if lifecycles is not None and entry.lifecycle_status not in lifecycles:
            continue
        openers = find_type_restating_opener(entry.abstract, entry.doc_type)
        if not openers:
            continue
        after = strip_type_restating_opener(entry.abstract, entry.doc_type)
        residual = find_type_restating_opener(after, entry.doc_type)
        plans.append(
            RepairPlan(
                doc_id=entry.doc_id,
                lifecycle_status=entry.lifecycle_status,
                doc_type=entry.doc_type,
                openers=tuple(openers),
                abstract_before=entry.abstract,
                abstract_after=after,
                residual=tuple(residual),
            )
        )
    return plans


def _print_plans(plans: list[RepairPlan], total_entries: int) -> None:
    writable = [plan for plan in plans if not plan.residual]
    print()
    print(f"Documents examined:      {total_entries}")
    print(f"Documents with a finding: {len(plans)}")
    print(f"Documents writable:       {len(writable)}")
    print(f"Findings left recorded:   {len(plans) - len(writable)}")
    for plan in plans:
        if not plan.residual:
            print()
            print(f"{plan.doc_id}  [{plan.lifecycle_status}]  ({plan.doc_type})")
            print(f"  before: {plan.abstract_before[:140]}")
            print(f"  after:  {plan.abstract_after[:140]}")


def _append_maintenance_record(
    vault_dir: Path, documents_repaired: int, findings_left_recorded: int
) -> None:
    """Append one operation record to the vault's maintenance log.

    The log is operation-keyed JSONL and readers filter on ``operation``,
    so a new operation type is additive. The count left recorded rides
    alongside the count repaired because this pass, by design, corrects a
    minority of what it finds; a record carrying only the repairs would
    read as a clean sweep.
    """
    record = {
        "operation": "repair_abstraction_type_openers",
        "timestamp": datetime.now(UTC).isoformat(),
        "documents_repaired": documents_repaired,
        "findings_left_recorded": findings_left_recorded,
    }
    log_path = vault_dir / MAINTENANCE_LOG_FILENAME
    with log_path.open("a") as handle:
        handle.write(json.dumps(record) + "\n")


async def run(args: argparse.Namespace) -> int:
    config_path = config_path_for_vault(args.vault_id)
    if not config_path.exists():
        print(f"vault config not found: {config_path}", file=sys.stderr)
        return 2

    config = load_vault_config(config_path)
    lifecycles = frozenset(part.strip() for part in args.lifecycle.split(",") if part.strip())

    # Stub abstraction provider during services init so the production
    # provider does not lazy-load; the repair regenerates nothing.
    print(f"Loading SAGE services for vault {args.vault_id!r}...", flush=True)
    services = await initialize_services(
        config,
        config_path=config_path,
        abstraction_provider=StubAbstractionProvider(),
    )

    try:
        print("Enumerating documents with stored abstracts...", flush=True)
        entries = await build_entries(services)
        plans = plan_repairs(entries, lifecycles=lifecycles)
        _print_plans(plans, len(entries))

        if not args.apply:
            print()
            print("Dry run -- nothing written. Re-run with --apply to repair.")
            return 0

        repaired = 0
        for plan in plans:
            if plan.residual:
                continue
            await services.ingestion_service.update_semantic_abstract(
                plan.doc_id, plan.abstract_after
            )
            repaired += 1

        left = len(plans) - repaired
        _append_maintenance_record(config_path.parent, repaired, left)
        print()
        print(f"Repaired {repaired} document(s); {left} finding(s) left recorded.")
    finally:
        await services.graph_store.close()
    return 0


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Repair stored semantic abstracts by excising type-restating openers."
    )
    parser.add_argument(
        "vault_id",
        nargs="?",
        default="cas",
        help="Vault to repair (default: cas)",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Write the planned repairs (default: dry run)",
    )
    parser.add_argument(
        "--lifecycle",
        default="active,completed,archived",
        help="Comma-separated lifecycle classes to include (default: all three)",
    )
    return parser.parse_args(argv)


def main() -> None:
    args = _parse_args()
    raise SystemExit(asyncio.run(run(args)))


if __name__ == "__main__":
    main()
