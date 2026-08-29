#!/usr/bin/env python3
"""Read-only audit of stored semantic abstracts for type-restating openers.

For every document in a vault that carries a semantic abstract, runs the
deterministic CAS-ADR-020 clause (f) type-restating-opener detector against
the stored abstract and the document's doc_type. Each finding is reported
with the document's lifecycle status and doc_type so a reviewer can
adjudicate it and the detector's real-corpus false-positive rate can be
measured -- the measurement CAS-ADR-020 requires before this finding class
could ever be licensed for repair.

The audit only reads: the vault is never modified, no abstract is
regenerated, and no inference runtime is loaded (services initialize with a
stub abstraction provider). The document catalog is the same one the gloss
audit builds, so all three abstract audits read one reconstruction of each
document's source.

Usage::

    .venv/bin/python scripts/audit_abstraction_type_openers.py            # cas vault
    .venv/bin/python scripts/audit_abstraction_type_openers.py my_vault

Exit code 0 on a completed audit (findings or none); 2 when the vault
config cannot be found.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import Iterable
from dataclasses import dataclass

from sage.adapters.abstraction_utils import TypeRestatingOpener, find_type_restating_opener
from sage.adapters.stubs import StubAbstractionProvider
from sage.config import load_vault_config
from sage.mcp_init import initialize_services
from sage.vault_management import config_path_for_vault
from scripts.audit_abstraction_glosses import AuditEntry, build_entries


@dataclass(frozen=True)
class TypeOpenerAuditFinding:
    """A document whose stored abstract opens by restating its own type."""

    doc_id: str
    lifecycle_status: str
    doc_type: str | None
    findings: tuple[TypeRestatingOpener, ...]


def audit_type_opener_entries(entries: Iterable[AuditEntry]) -> list[TypeOpenerAuditFinding]:
    """Run the type-restating-opener detector across a catalog of entries.

    Pure core of the audit: storage access happens in ``run``, so this
    function is exercisable against a synthetic catalog. Entries without
    a doc_type fall out silently through the detector.
    """
    findings: list[TypeOpenerAuditFinding] = []
    for entry in entries:
        openers = find_type_restating_opener(entry.abstract, entry.doc_type)
        if openers:
            findings.append(
                TypeOpenerAuditFinding(
                    doc_id=entry.doc_id,
                    lifecycle_status=entry.lifecycle_status,
                    doc_type=entry.doc_type,
                    findings=tuple(openers),
                )
            )
    return findings


def _print_report(findings: list[TypeOpenerAuditFinding], total_audited: int) -> None:
    print()
    print(f"Documents audited:                    {total_audited}")
    print(f"Documents with type-restating opener: {len(findings)}")
    for finding in findings:
        print()
        print(f"{finding.doc_id}  [{finding.lifecycle_status}]  ({finding.doc_type})")
        for opener in finding.findings:
            print(f"  {opener.verb!r} -> {opener.surface!r} via {opener.form}")
            print(f"  opener: {opener.opener}")


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
        findings = audit_type_opener_entries(entries)
        _print_report(findings, len(entries))
    finally:
        await services.graph_store.close()
    return 0


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Audit stored semantic abstracts for type-restating openers."
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
