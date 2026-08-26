#!/usr/bin/env python3
"""Repair stored semantic abstracts by collapsing unattested acronym glosses.

The stored-output disposition of CAS-ADR-020 clause (e): for every
document whose stored abstract carries glosses the source does not
attest, the gloss collapses to its bare acronym -- the same repair the
ingestion seam applies to new abstracts -- written in place with no
regeneration, since greedy decoding would reproduce the breached output.

Dry-run by default: the per-document plan prints and nothing is written.
``--apply`` writes each planned abstract through the ingestion service
(which refreshes the retrieval header chunk) and appends one operation
record to the vault's maintenance log.

Usage::

    .venv/bin/python scripts/repair_abstraction_glosses.py            # dry run, cas vault
    .venv/bin/python scripts/repair_abstraction_glosses.py --apply
    .venv/bin/python scripts/repair_abstraction_glosses.py my_vault --lifecycle active,completed

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
from datetime import datetime, timezone
from pathlib import Path

# The audit script owns the entry catalog this planner consumes; make its
# package importable when this file is run by path rather than as a module.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from sage.adapters.abstraction_utils import (  # noqa: E402
    AcronymGloss,
    collapse_unattested_acronym_glosses,
    find_unattested_acronym_glosses,
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
    glosses: tuple[AcronymGloss, ...]
    abstract_before: str
    abstract_after: str
    residual: tuple[AcronymGloss, ...]


def plan_repairs(
    entries: Iterable[AuditEntry], lifecycles: frozenset[str] | None = None
) -> list[RepairPlan]:
    """Plan the collapse for every entry carrying unattested glosses.

    Pure core of the repair: storage access and writes happen in ``run``,
    so the planner is exercisable against a synthetic catalog. ``residual``
    holds any finding the detector still reports on the planned text; it
    is expected empty, and the apply path refuses to write a plan whose
    residual is not -- a guard against detection and repair drifting apart.
    """
    plans: list[RepairPlan] = []
    for entry in entries:
        if lifecycles is not None and entry.lifecycle_status not in lifecycles:
            continue
        glosses = find_unattested_acronym_glosses(entry.abstract, entry.source_text)
        if not glosses:
            continue
        after = collapse_unattested_acronym_glosses(entry.abstract, entry.source_text)
        residual = find_unattested_acronym_glosses(after, entry.source_text)
        plans.append(
            RepairPlan(
                doc_id=entry.doc_id,
                lifecycle_status=entry.lifecycle_status,
                glosses=tuple(glosses),
                abstract_before=entry.abstract,
                abstract_after=after,
                residual=tuple(residual),
            )
        )
    return plans


def _print_plans(plans: list[RepairPlan], total_entries: int) -> None:
    print()
    print(f"Documents examined:  {total_entries}")
    print(f"Documents planned:   {len(plans)}")
    print(f"Glosses to collapse: {sum(len(plan.glosses) for plan in plans)}")
    for plan in plans:
        print()
        print(f"{plan.doc_id}  [{plan.lifecycle_status}]")
        for gloss in plan.glosses:
            print(f"  {gloss.acronym} -> {gloss.expansion!r}")
        print(f"  abstract: {len(plan.abstract_before)} -> {len(plan.abstract_after)} chars")


def _append_maintenance_record(
    vault_dir: Path, documents_repaired: int, glosses_collapsed: int
) -> None:
    """Append one operation record to the vault's maintenance log.

    The log is operation-keyed JSONL and readers filter on ``operation``,
    so a new operation type is additive. This record is what gives the
    repaired cohort a durable trace beyond the per-process logs.
    """
    record = {
        "operation": "repair_abstraction_glosses",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "documents_repaired": documents_repaired,
        "glosses_collapsed": glosses_collapsed,
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
        collapsed = 0
        for plan in plans:
            if plan.residual:
                print(f"SKIPPED {plan.doc_id}: planned text still carries findings")
                continue
            await services.ingestion_service.update_semantic_abstract(
                plan.doc_id, plan.abstract_after
            )
            repaired += 1
            collapsed += len(plan.glosses)

        _append_maintenance_record(config_path.parent, repaired, collapsed)
        print()
        print(f"Repaired {repaired} document(s); collapsed {collapsed} gloss(es).")
    finally:
        await services.graph_store.close()
    return 0


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Repair stored semantic abstracts by collapsing unattested acronym glosses."
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
