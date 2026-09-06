#!/usr/bin/env python3
"""Backfill `references` edges from inline identifier mentions.

The inference rule runs at ingest time and creates `references` edges
from documents that mention vault-resident artifacts by their canonical
identifier strings (CAS-ADR-NNN, T-NNNN, F-N). It does not retroactively
process documents already in the vault.

This script sweeps every active document in the named vault, reads its
projected body text from the content store, scans for configured identifier
patterns, and writes `references` edges via the same code path as the
live rule (``plan_identifier_mention_edges`` +
``GraphOpsService._create_edge``). Idempotent: re-running produces no
duplicates because ``_create_edge`` enforces the natural-key UNIQUE
constraint.

Usage::

    .venv/bin/python scripts/backfill_references_mentions.py
    .venv/bin/python scripts/backfill_references_mentions.py --execute
    .venv/bin/python scripts/backfill_references_mentions.py --vault cas --limit 5
    .venv/bin/python scripts/backfill_references_mentions.py --doc-type adr
    .venv/bin/python scripts/backfill_references_mentions.py --reconcile --execute

Dry-run is the default; pass ``--execute`` to write edges.

Plain backfill is create-only: it adds missing edges but never removes a
stale one. ``--reconcile`` additionally deletes inferred
(``references_mention``) edges whose target the current config no longer
resolves the body to -- the repair path for a graph that accreted
wrong-target edges under a misconfigured resolver. Manual edges are never
touched (CAS-ADR-019 provenance gate).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from collections import Counter
from dataclasses import asdict, dataclass, field
from pathlib import Path

# Ensure project root on path when run directly
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import yaml  # noqa: E402

from sage.adapters.stubs import (  # noqa: E402
    StubAbstractionProvider,
    StubEmbeddingProvider,
)
from sage.config import VaultConfig  # noqa: E402
from sage.mcp_init import initialize_services  # noqa: E402
from sage.models.enums import EdgeType, RationaleKind  # noqa: E402
from sage.models.schemas import LinkRequest  # noqa: E402
from sage.services.identifier_mention_inference import (  # noqa: E402
    plan_identifier_mention_edges,
    plan_reference_reconcile,
)
from sage.vault_management import config_path_for_vault  # noqa: E402


@dataclass
class DocReport:
    document_id: str
    title: str
    doc_type: str | None
    edges_planned: int
    edge_targets: list[tuple[str, str]] = field(default_factory=list)
    # (identifier_literal, target_doc_id)
    # Reconcile-mode only: inferred edges whose target the plan no longer
    # includes (deleted) and planned targets not yet linked (created).
    targets_deleted: list[str] = field(default_factory=list)
    targets_created: list[str] = field(default_factory=list)


def _load_vault_config(vault_id: str) -> VaultConfig:
    config_path = config_path_for_vault(vault_id)
    if not config_path.exists():
        raise FileNotFoundError(f"Vault config not found: {config_path}")
    with open(config_path) as f:
        cfg = yaml.safe_load(f)
    return VaultConfig.model_validate(cfg)


async def _enumerate_active_documents(services, *, doc_type: str | None, limit: int | None) -> list:
    filters: dict[str, object] = {"lifecycle_status": "active"}
    if doc_type:
        filters["doc_type"] = doc_type
    page_size = 100
    offset = 0
    out: list = []
    while True:
        docs, total = await services.graph_store.query_documents(
            filters=filters, limit=page_size, offset=offset
        )
        out.extend(docs)
        if limit is not None and len(out) >= limit:
            return out[:limit]
        if offset + page_size >= total:
            return out
        offset += page_size


async def _create_planned_edge(services, plan) -> None:
    await services.graph_ops_service._create_edge(
        LinkRequest(
            source_id=plan.source_doc_id,
            target_id=plan.target_doc_id,
            edge_type=EdgeType.REFERENCES,
            source_valid_from_version=plan.source_doc_id,
            target_valid_from_version=plan.target_doc_id,
            rationale=plan.evidence,
            rationale_kind=RationaleKind.REFERENCES_MENTION,
        )
    )


async def _process_document(
    services,
    doc,
    *,
    edge_inference_cfg: dict,
    resolution_states: frozenset[str],
    cache: dict[str, str | None],
    execute: bool,
    reconcile: bool = False,
) -> DocReport:
    chunks = await services.content_store.get_all_chunks(doc.id)
    body_text = "\n".join(c.content for c in chunks)
    planned = await plan_identifier_mention_edges(
        source_doc_id=doc.id,
        body_text=body_text,
        edge_inference_config=edge_inference_cfg,
        graph_store=services.graph_store,
        resolution_states=resolution_states,
        resolution_cache=cache,
    )
    report = DocReport(
        document_id=doc.id,
        title=doc.title,
        doc_type=doc.doc_type,
        edges_planned=len(planned),
        edge_targets=[(p.identifier, p.target_doc_id) for p in planned],
    )

    if reconcile:
        # Repair sweep: the create-only path leaves a stale wrong-target edge
        # in place, so reconcile against the freshly planned target set --
        # delete inferred edges the plan dropped, create the ones it added.
        # Manual edges are never touched (CAS-ADR-019 provenance gate).
        existing = await services.graph_store.get_edges_by_source(doc.id, "references")
        plan_by_target = {p.target_doc_id: p for p in planned}
        inferred_edge_by_target = {
            e.target_id: e for e in existing if e.rationale_kind == RationaleKind.REFERENCES_MENTION
        }
        to_delete, to_create = plan_reference_reconcile(existing, plan_by_target.keys())
        report.targets_deleted = sorted(to_delete)
        report.targets_created = sorted(to_create)
        if execute:
            for tgt in to_delete:
                edge = inferred_edge_by_target.get(tgt)
                if edge is not None:
                    await services.graph_store.delete_edge(edge.id)
            for tgt in to_create:
                await _create_planned_edge(services, plan_by_target[tgt])
        return report

    if execute and planned:
        for p in planned:
            await _create_planned_edge(services, p)
    return report


async def run(args: argparse.Namespace) -> int:
    os.environ.setdefault("SAGE_TEST_STUB_PROVIDERS", "1")
    config = _load_vault_config(args.vault)
    services = await initialize_services(
        config,
        embedding_provider=StubEmbeddingProvider(),
        abstraction_provider=StubAbstractionProvider(),
    )
    cache: dict[str, str | None] = {}
    reports: list[DocReport] = []
    by_pattern: Counter[str] = Counter()
    try:
        docs = await _enumerate_active_documents(services, doc_type=args.doc_type, limit=args.limit)
        print(
            f"[backfill] {args.vault}: scanning {len(docs)} active document(s)"
            f"{' (dry-run)' if not args.execute else ' (executing)'}"
        )
        for doc in docs:
            report = await _process_document(
                services,
                doc,
                edge_inference_cfg=config.edge_inference,
                resolution_states=config.lifecycle.supersession_surviving_states(),
                cache=cache,
                execute=args.execute,
                reconcile=args.reconcile,
            )
            touched = report.edges_planned or report.targets_deleted or report.targets_created
            if touched:
                reports.append(report)
                for ident, _ in report.edge_targets:
                    by_pattern[ident.split("-")[0] + "-*"] += 1
        summary = {
            "vault": args.vault,
            "execute": args.execute,
            "reconcile": args.reconcile,
            "doc_type_filter": args.doc_type,
            "documents_scanned": len(docs),
            "documents_with_mentions": len(reports),
            "edges_planned_total": sum(r.edges_planned for r in reports),
            "edges_deleted_total": sum(len(r.targets_deleted) for r in reports),
            "edges_created_total": sum(len(r.targets_created) for r in reports),
            "by_pattern": dict(by_pattern),
        }
        print("[backfill] summary:")
        print(json.dumps(summary, indent=2))
        if args.audit:
            audit_path = Path(args.audit)
            audit_path.write_text(
                json.dumps(
                    {"summary": summary, "documents": [asdict(r) for r in reports]},
                    indent=2,
                ),
                encoding="utf-8",
            )
            print(f"[backfill] audit JSON written to {audit_path}")
        return 0
    finally:
        await services.graph_store.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else "")
    parser.add_argument("--vault", default="cas", help="Vault id (default: cas)")
    parser.add_argument(
        "--doc-type",
        default=None,
        help="Restrict scan to one doc_type (e.g., adr, ticket).",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Stop after N active documents (for testing).",
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Write edges. Default is dry-run.",
    )
    parser.add_argument(
        "--reconcile",
        action="store_true",
        help=(
            "Repair mode: in addition to creating missing edges, delete "
            "inferred (references_mention) edges whose target the current "
            "config no longer resolves the body to. Manual edges are never "
            "touched. Combine with --execute to write; default stays dry-run."
        ),
    )
    parser.add_argument(
        "--audit",
        default=None,
        help="Optional path to write a JSON audit log of every planned edge.",
    )
    args = parser.parse_args()
    return asyncio.run(run(args))


if __name__ == "__main__":
    raise SystemExit(main())
