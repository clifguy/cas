#!/usr/bin/env python3
"""Read-only audit of stored semantic abstracts for type-restating openers.

For every document in a vault that carries a semantic abstract, runs the
deterministic CAS-ADR-020 clause (f) type-restating-opener detector against
the stored abstract and the document's doc_type. Each finding is reported
with the document's lifecycle status and doc_type so a reviewer can
adjudicate it and the detector's real-corpus false-positive rate can be
measured -- the measurement CAS-ADR-020 required before this finding class
could be licensed for repair, and the instrument that re-measures it
whenever the finding definition or the abstraction provider changes.

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
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

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


def summarize_by_doc_type(findings: Iterable[TypeOpenerAuditFinding]) -> list[tuple[str, int]]:
    """Finding counts per doc_type, heaviest first, ties broken by name.

    The breach is concentrated by doc_type rather than spread evenly, so
    the per-type counts are the shape of the measurement rather than a
    convenience: a flat total hides which classes are elevated, and
    adjudication proceeds a type at a time.
    """
    counts = Counter(finding.doc_type or "" for finding in findings)
    return sorted(counts.items(), key=lambda item: (-item[1], item[0]))


def render_manifest(
    findings: Iterable[TypeOpenerAuditFinding],
    *,
    vault_id: str,
    total_audited: int,
    measured_at: str,
) -> str:
    """Render the flagged ids as a manifest, one id per line.

    The file is read twice and both readings shape it. A reviewer
    adjudicating each finding needs its evidence -- lifecycle, type, the
    frame that matched, and the opening sentence -- beside the id it
    belongs to; machinery replaying the flagged set needs nothing but the
    ids to survive. Every line that is not an id is therefore a ``#``
    comment, which ``scripts.reabstract_deferred --ids-file`` skips.

    Each opener is folded onto one line before it is written. Abstracts
    are stored prose and may wrap mid-sentence; interpolated raw, the tail
    of a wrapped opener would leave the comment and be read back as a
    document id.

    Ordered by doc_type then id, so findings of one type are adjacent for
    the reviewer and the rendering depends only on the findings -- not on
    the order the vault happened to enumerate them. A manifest that
    reordered itself between runs could not be diffed against an earlier
    one, which is how a narrowing is confirmed to have introduced nothing.
    """
    ordered = sorted(findings, key=lambda finding: (finding.doc_type or "", finding.doc_id))
    lines = [
        f"# vault: {vault_id}",
        f"# measured_at: {measured_at}",
        f"# documents_audited: {total_audited}",
        f"# documents_with_type_restating_opener: {len(ordered)}",
    ]
    for finding in ordered:
        lines.append("#")
        lines.append(f"# {finding.doc_id}  [{finding.lifecycle_status}]  ({finding.doc_type})")
        for opener in finding.findings:
            lines.append(f"#   {opener.verb!r} -> {opener.surface!r} via {opener.form}")
            lines.append(f"#   opener: {' '.join(opener.opener.split())}")
        lines.append(finding.doc_id)
    return "\n".join(lines) + "\n"


def _print_report(findings: list[TypeOpenerAuditFinding], total_audited: int) -> None:
    print()
    print(f"Documents audited:                    {total_audited}")
    print(f"Documents with type-restating opener: {len(findings)}")
    for doc_type, count in summarize_by_doc_type(findings):
        print(f"  {doc_type or '(none)':<24} {count}")
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
        if args.lifecycle is not None:
            entries = [entry for entry in entries if entry.lifecycle_status == args.lifecycle]
        findings = audit_type_opener_entries(entries)
        _print_report(findings, len(entries))
        if args.manifest is not None:
            args.manifest.write_text(
                render_manifest(
                    findings,
                    vault_id=args.vault_id,
                    total_audited=len(entries),
                    measured_at=datetime.now(UTC).isoformat(),
                )
            )
            print()
            print(f"Manifest written: {args.manifest}")
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
    parser.add_argument(
        "--manifest",
        type=Path,
        metavar="PATH",
        help=(
            "Write the flagged document ids here, grouped by doc_type, each "
            "with its finding as a comment for adjudication. Consumed by "
            "'scripts.reabstract_deferred --ids-file'."
        ),
    )
    parser.add_argument(
        "--lifecycle",
        metavar="STATUS",
        help=(
            "Audit only documents in this lifecycle status. Omit to audit "
            "every class, which is the default because a discovering agent's "
            "search surfaces archived documents alongside active ones."
        ),
    )
    return parser.parse_args(argv)


def main() -> None:
    args = _parse_args()
    raise SystemExit(asyncio.run(run(args)))


if __name__ == "__main__":
    main()
