#!/usr/bin/env python3
"""Read-only audit of stored semantic abstracts for unattested acronym glosses.

For every document in a vault that carries a semantic abstract, rebuilds the
projection text from the stored body chunks and runs the deterministic
CAS-ADR-020 clause (e) detector against the stored abstract. Each finding is
reported with the document's lifecycle status so a reviewer can adjudicate
it and the detector's real-corpus false-positive rate can be measured.

The audit only reads: the vault is never modified, no abstract is
regenerated, and no inference runtime is loaded (services initialize with a
stub abstraction provider).

Usage::

    .venv/bin/python scripts/audit_abstraction_glosses.py            # cas vault
    .venv/bin/python scripts/audit_abstraction_glosses.py my_vault

Exit code 0 on a completed audit (findings or none); 2 when the vault
config cannot be found.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import Iterable
from dataclasses import dataclass

from sage.adapters.abstraction_utils import AcronymGloss, find_unattested_acronym_glosses
from sage.adapters.interfaces import SYNTHETIC_HEADER_HEADING_PATH
from sage.adapters.stubs import StubAbstractionProvider
from sage.config import load_vault_config
from sage.mcp_init import initialize_services
from sage.vault_management import config_path_for_vault


@dataclass(frozen=True)
class AuditEntry:
    """One document's stored abstract paired with its reconstructed source.

    ``doc_type`` is carried for the audits whose detector reads it (the
    type-restating-opener check keys on it) and defaulted so audits that
    need only text can build entries without one.
    """

    doc_id: str
    lifecycle_status: str
    abstract: str
    source_text: str
    doc_type: str | None = None


@dataclass(frozen=True)
class AuditFinding:
    """A document whose abstract carries at least one unattested gloss."""

    doc_id: str
    lifecycle_status: str
    glosses: tuple[AcronymGloss, ...]


def audit_entries(entries: Iterable[AuditEntry]) -> list[AuditFinding]:
    """Run the unattested-gloss detector across a catalog of entries.

    Pure core of the audit: storage access happens in ``run``, so this
    function is exercisable against a synthetic catalog.
    """
    findings: list[AuditFinding] = []
    for entry in entries:
        glosses = find_unattested_acronym_glosses(entry.abstract, entry.source_text)
        if glosses:
            findings.append(
                AuditFinding(
                    doc_id=entry.doc_id,
                    lifecycle_status=entry.lifecycle_status,
                    glosses=tuple(glosses),
                )
            )
    return findings


async def build_entries(services) -> list[AuditEntry]:
    """Catalog every document that has both an abstract and body content.

    The source text is reconstructed from stored body chunks exactly as the
    reabstract path does -- synthetic header excluded -- so the audit
    compares each abstract against the same text a regeneration would see.
    """
    entries: list[AuditEntry] = []
    docs = await services.graph_store.list_all_documents()
    for doc in docs:
        abstract = getattr(doc, "semantic_abstract", None)
        if not abstract:
            continue
        chunks = await services.content_store.get_all_chunks(doc.id)
        body_chunks = [c for c in chunks if c.heading_path != SYNTHETIC_HEADER_HEADING_PATH]
        source_text = "\n\n".join(chunk.content for chunk in body_chunks)
        if not source_text.strip():
            continue
        status = getattr(doc, "lifecycle_status", None)
        doc_type = getattr(doc, "doc_type", None)
        entries.append(
            AuditEntry(
                doc_id=doc.id,
                lifecycle_status=str(getattr(status, "value", status) or ""),
                abstract=abstract,
                source_text=source_text,
                doc_type=str(doc_type) if doc_type else None,
            )
        )
    return entries


def _print_report(findings: list[AuditFinding], total_audited: int) -> None:
    print()
    print(f"Documents audited:                 {total_audited}")
    print(f"Documents with unattested glosses: {len(findings)}")
    print(f"Total unattested glosses:          {sum(len(f.glosses) for f in findings)}")
    for finding in findings:
        print()
        print(f"{finding.doc_id}  [{finding.lifecycle_status}]")
        for gloss in finding.glosses:
            print(f"  {gloss.acronym} -> {gloss.expansion!r}")


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
        findings = audit_entries(entries)
        _print_report(findings, len(entries))
    finally:
        await services.graph_store.close()
    return 0


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Audit stored semantic abstracts for unattested acronym glosses."
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
