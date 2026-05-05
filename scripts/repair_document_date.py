#!/usr/bin/env python3
"""Repair document_date values that were persisted in ISO-with-time form.

Caller-supplied paths (``sage_update_metadata`` and ``sage_ingest`` with
caller metadata) historically accepted any string for ``document_date``
and stored it verbatim. Some callers passed datetime-ISO strings
(``2026-05-05T00:00:00Z``) instead of the contract YYYY-MM-DD shape;
``sage_traverse`` then raised ``ValueError`` from ``strptime`` on those
records. The boundary is now validated by Pydantic; this script
normalizes any pre-existing rows back to YYYY-MM-DD.

For each document whose ``document_date`` is non-null but does not match
``^\\d{4}-\\d{2}-\\d{2}$``:

- If ``datetime.fromisoformat`` parses the value, store
  ``parsed.date().isoformat()``.
- If parsing raises, report the row and leave it untouched (the script
  does not heuristically guess on truly broken data, and a single bad
  row does not abort the rewrite of the rest).

Dry-run by default. Idempotent: a second run after ``--execute`` finds
no targets.

Usage::

    .venv/bin/python -m scripts.repair_document_date VAULT_ID
    .venv/bin/python -m scripts.repair_document_date VAULT_ID --execute
"""
from __future__ import annotations

import argparse
import asyncio
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime

from sage.adapters.stubs import StubAbstractionProvider
from sage.config import load_vault_config
from sage.mcp_init import initialize_services
from sage.storage.graph_store import GraphStore
from sage.vault_management import _config_path_for_vault

_DOCUMENT_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


@dataclass
class RepairTarget:
    doc_id: str
    old_value: str
    new_value: str


@dataclass
class RepairSkipped:
    doc_id: str
    value: str
    reason: str


@dataclass
class RepairResult:
    targets: list[RepairTarget] = field(default_factory=list)
    skipped: list[RepairSkipped] = field(default_factory=list)
    rewrites_applied: int = 0


def _normalize(value: str) -> str | None:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed.date().isoformat()


async def repair_with_services(
    *, graph: GraphStore, execute: bool
) -> RepairResult:
    """Service-level entry point. Tests call this directly with a graph fixture."""
    result = RepairResult()
    documents = await graph.list_all_documents()

    for doc in documents:
        value = doc.document_date
        if value is None or _DOCUMENT_DATE_RE.match(value):
            continue
        normalized = _normalize(value)
        if normalized is None:
            result.skipped.append(RepairSkipped(
                doc_id=doc.id,
                value=value,
                reason="not parseable as ISO-8601 datetime",
            ))
            continue
        result.targets.append(RepairTarget(
            doc_id=doc.id,
            old_value=value,
            new_value=normalized,
        ))

    if execute:
        for target in result.targets:
            await graph.update_document(
                target.doc_id, {"document_date": target.new_value}
            )
            result.rewrites_applied += 1

    return result


async def repair_vault(vault_id: str, *, execute: bool) -> int:
    config_path = _config_path_for_vault(vault_id)
    if not config_path.exists():
        print(f"vault config not found: {config_path}", file=sys.stderr)
        return 2

    config = load_vault_config(config_path)
    services = await initialize_services(
        config,
        config_path=config_path,
        abstraction_provider=StubAbstractionProvider(),
    )

    try:
        result = await repair_with_services(
            graph=services.graph_store, execute=execute
        )

        print(f"Vault: {vault_id}")
        print(f"Targets (parseable malformed): {len(result.targets)}")
        for t in result.targets:
            print(f"  {t.doc_id}  {t.old_value!r}  ->  {t.new_value!r}")
        if result.skipped:
            print(f"Skipped (unparseable): {len(result.skipped)}")
            for s in result.skipped:
                print(f"  {s.doc_id}  {s.value!r}  ({s.reason})")

        if not result.targets and not result.skipped:
            print("Nothing to do.")
            return 0

        if execute:
            print(f"\nApplied {result.rewrites_applied} rewrite(s).")
        else:
            print("\n(dry-run; pass --execute to apply)")
        return 0
    finally:
        await services.graph_store.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("vault_id")
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()
    rc = asyncio.run(repair_vault(args.vault_id, execute=args.execute))
    raise SystemExit(rc)


if __name__ == "__main__":
    main()
