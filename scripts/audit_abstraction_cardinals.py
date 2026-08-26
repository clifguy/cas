#!/usr/bin/env python3
"""Read-only audit of stored semantic abstracts for fabricated cardinals.

For every document in a vault that carries a semantic abstract, rebuilds the
projection text from the stored body chunks and runs the deterministic
CAS-ADR-020 clause (e) fabricated-cardinal detector against the stored
abstract. Each finding is reported with the document's lifecycle status so a
reviewer can adjudicate it and the detector's real-corpus false-positive
rate can be measured -- the measurement CAS-ADR-020 requires before this
finding class could ever be licensed for repair.

The audit only reads: the vault is never modified, no abstract is
regenerated, and no inference runtime is loaded (services initialize with a
stub abstraction provider). The document catalog is the same one the gloss
audit builds, so the two instruments read one reconstruction of each
document's source.

Usage::

    .venv/bin/python scripts/audit_abstraction_cardinals.py            # cas vault
    .venv/bin/python scripts/audit_abstraction_cardinals.py my_vault

Exit code 0 on a completed audit (findings or none); 2 when the vault
config cannot be found.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import Iterable
from dataclasses import dataclass

from sage.adapters.abstraction_utils import FabricatedCardinal, find_fabricated_cardinals
from sage.adapters.stubs import StubAbstractionProvider
from sage.config import load_vault_config
from sage.mcp_init import initialize_services
from sage.vault_management import config_path_for_vault
from scripts.audit_abstraction_glosses import AuditEntry, build_entries


@dataclass(frozen=True)
class CardinalAuditFinding:
    """A document whose abstract asserts at least one unlicensed count."""

    doc_id: str
    lifecycle_status: str
    claims: tuple[FabricatedCardinal, ...]


def audit_cardinal_entries(entries: Iterable[AuditEntry]) -> list[CardinalAuditFinding]:
    """Run the fabricated-cardinal detector across a catalog of entries.

    Pure core of the audit: storage access happens in ``run``, so this
    function is exercisable against a synthetic catalog.
    """
    findings: list[CardinalAuditFinding] = []
    for entry in entries:
        claims = find_fabricated_cardinals(entry.abstract, entry.source_text)
        if claims:
            findings.append(
                CardinalAuditFinding(
                    doc_id=entry.doc_id,
                    lifecycle_status=entry.lifecycle_status,
                    claims=tuple(claims),
                )
            )
    return findings


def _print_report(findings: list[CardinalAuditFinding], total_audited: int) -> None:
    print()
    print(f"Documents audited:                    {total_audited}")
    print(f"Documents with fabricated cardinals:  {len(findings)}")
    print(f"Total fabricated cardinals:           {sum(len(f.claims) for f in findings)}")
    for finding in findings:
        print()
        print(f"{finding.doc_id}  [{finding.lifecycle_status}]")
        for claim in finding.claims:
            print(
                f"  {claim.surface!r}: value {claim.value} vs derived "
                f"{claim.derived}, attested={claim.attested}"
            )


async def run(args: argparse.Namespace) -> int:
    config_path = config_path_for_vault(args.vault_id)
    if not config_path.exists():
        print(f"vault config not found: {config_path}", file=sys.stderr)
        return 2

    config = load_vault_config(config_path)

    # Stub abstraction provider during services init so the production
    # provider does not lazy-load; the audit regenerates nothing.
    print(f"Loading SAGE services for vault {args.vault_id!r}...", flush=True)
    services = await initialize_services(
        config,
        config_path=config_path,
        abstraction_provider=StubAbstractionProvider(),
    )

    try:
        print("Enumerating documents with stored abstracts...", flush=True)
        entries = await build_entries(services)
        findings = audit_cardinal_entries(entries)
        _print_report(findings, len(entries))
    finally:
        await services.graph_store.close()
    return 0


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Audit stored semantic abstracts for fabricated cardinals."
    )
    parser.add_argument(
        "vault_id",
        nargs="?",
        default="cas",
        help="Vault to audit (default: cas)",
    )
    return parser.parse_args(argv)


def main() -> None:
    args = _parse_args()
    raise SystemExit(asyncio.run(run(args)))


if __name__ == "__main__":
    main()
