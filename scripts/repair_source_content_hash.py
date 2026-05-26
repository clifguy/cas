#!/usr/bin/env python3
"""Repair source_content_hash values persisted in bare-hex form.

Records ingested before part 3/3 (commit 764b448) stored
``source_content_hash`` as bare 64-char hex (``hashlib.sha256(...).hexdigest()``).
The boundary canonicalization at
``sage/services/ingestion.py`` now emits the prefixed shape
``sha256:`` + 64 hex, and ``Sha256Str`` rejects anything else.

The storage read at ``_row_to_document`` uses ``model_construct`` to
tolerate legacy values, but ``DocumentsService.get_document_with_content``
re-validates by constructing ``DocumentWithContent(**doc.model_dump())``,
which fails for bare-hex rows. This script normalizes pre-existing rows
back to the canonical shape.

For each document whose ``source_content_hash`` does not match
``^sha256:[0-9a-f]{64}$``:

- If it matches ``^[0-9a-f]{64}$`` (bare lowercase hex), store
  ``f"sha256:{value}"``.
- Otherwise (wrong length, mixed case, non-hex, empty), report the row
  and leave it untouched. A single bad row does not abort the rewrite
  of the rest.

Dry-run by default. Idempotent: a second run after ``--execute`` finds
no targets.

Usage::

    .venv/bin/python -m scripts.repair_source_content_hash VAULT_ID
    .venv/bin/python -m scripts.repair_source_content_hash VAULT_ID --execute
"""

from __future__ import annotations

import argparse
import asyncio
import re
import sys
from dataclasses import dataclass, field

from sage.adapters.stubs import StubAbstractionProvider
from sage.config import load_vault_config
from sage.mcp_init import initialize_services
from sage.storage.graph_store import GraphStore
from sage.vault_management import config_path_for_vault

_CANONICAL_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_BARE_HEX_RE = re.compile(r"^[0-9a-f]{64}$")


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


async def repair_with_services(*, graph: GraphStore, execute: bool) -> RepairResult:
    """Service-level entry point. Tests call this directly with a graph fixture."""
    result = RepairResult()
    documents = await graph.list_all_documents()

    for doc in documents:
        value = doc.source_content_hash
        if value is not None and _CANONICAL_RE.match(value):
            continue
        if value is not None and _BARE_HEX_RE.match(value):
            result.targets.append(
                RepairTarget(
                    doc_id=doc.id,
                    old_value=value,
                    new_value=f"sha256:{value}",
                )
            )
            continue
        result.skipped.append(
            RepairSkipped(
                doc_id=doc.id,
                value=value if value is not None else "",
                reason="not bare 64-char lowercase hex; manual review required",
            )
        )

    if execute:
        for target in result.targets:
            await graph.update_document(target.doc_id, {"source_content_hash": target.new_value})
            result.rewrites_applied += 1

    return result


async def repair_vault(vault_id: str, *, execute: bool) -> int:
    config_path = config_path_for_vault(vault_id)
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
        result = await repair_with_services(graph=services.graph_store, execute=execute)

        print(f"Vault: {vault_id}")
        print(f"Targets (bare-hex): {len(result.targets)}")
        for t in result.targets:
            print(f"  {t.doc_id}  {t.old_value!r}  ->  {t.new_value!r}")
        if result.skipped:
            print(f"Skipped (non-bare-hex, non-canonical): {len(result.skipped)}")
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
