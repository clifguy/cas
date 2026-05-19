#!/usr/bin/env python3
"""Re-run abstraction for every document in a vault whose pipeline_status
is ``abstraction_skipped`` (the "deferred abstracts" health indicator).

Per CAS-ADR-011 graceful degradation, ingest writes
``pipeline_status='abstraction_skipped'`` when the abstraction stage was
bypassed (provider unavailable, vault config previously disabled,
empty projection text). The records carry no ``semantic_abstract``.
Indexing completed before the skip decision (see
``IngestionService._run_background_pipeline``), so the chunks are
present in the content store and ``sage_reabstract`` can rebuild the
projection text and write a fresh abstract.

Behavior:
    * Picks up every document with ``pipeline_status='abstraction_skipped'``
      in the named vault.
    * Skips scanned-PDF rows by default (``--include-pdf`` to include them).
      The scanned-PDF row has no extractable text; reabstract returns
      ``no_projection`` or a degenerate abstract.
    * Sequential. Calls ``IngestionService.reabstract`` (fire-and-forget),
      then polls the document row until pipeline_status is terminal
      (``abstraction_complete`` or ``failed``).
    * Prints ``[i/N]  title  vN`` on each completion, plus a final summary.

Usage::

    .venv/bin/python -m scripts.reabstract_deferred VAULT_ID

    # Include scanned PDFs in the worklist
    .venv/bin/python -m scripts.reabstract_deferred VAULT_ID --include-pdf

    # Custom polling interval (seconds)
    .venv/bin/python -m scripts.reabstract_deferred VAULT_ID --poll-interval 2.0

Operational note: the script loads its own Qwen3 MLX abstraction
provider (~16 GB resident). The running SAGE MCP server lazily loads
its own provider on first abstraction call -- do not invoke any
abstraction-triggering MCP tool (``sage_ingest`` without
``abstraction.enabled=false``, ``sage_reabstract``) while this script
is running, or both processes will hold the model and oversubscribe RAM.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import datetime, timezone

from sage.config import load_vault_config
from sage.mcp_init import initialize_services
from sage.models.enums import PipelineStatus
from sage.vault_management import config_path_for_vault


def _truncate(s: str | None, n: int) -> str:
    if not s:
        return ""
    return s if len(s) <= n else s[: n - 1] + "…"


async def _wait_for_terminal(graph_store, document_id: str, poll_interval: float) -> str:
    """Poll the document until pipeline_status is terminal. Returns the
    final pipeline_status value. Terminal = abstraction_complete or failed.
    """
    terminal = {PipelineStatus.ABSTRACTION_COMPLETE.value, PipelineStatus.FAILED.value}
    while True:
        doc = await graph_store.get_document(document_id)
        if doc is None:
            return "missing"
        status = doc.pipeline_status
        if status in terminal:
            return status
        await asyncio.sleep(poll_interval)


async def run(vault_id: str, *, include_pdf: bool, poll_interval: float) -> int:
    config_path = config_path_for_vault(vault_id)
    if not config_path.exists():
        print(f"vault config not found: {config_path}", file=sys.stderr)
        return 2

    config = load_vault_config(config_path)
    if not config.abstraction.enabled:
        print(
            f"abstraction.enabled is false for vault {vault_id!r}; aborting. "
            "Update vault_config.yaml first.",
            file=sys.stderr,
        )
        return 2

    print(f"Loading SAGE services for vault {vault_id!r}...", flush=True)
    services = await initialize_services(config, config_path=config_path)

    try:
        all_docs = await services.graph_store.list_all_documents()
        worklist = [
            d
            for d in all_docs
            if d.pipeline_status == PipelineStatus.ABSTRACTION_SKIPPED.value
            and (include_pdf or d.source_type != "pdf")
        ]
        total = len(worklist)
        skipped_pdfs = [
            d
            for d in all_docs
            if d.pipeline_status == PipelineStatus.ABSTRACTION_SKIPPED.value
            and d.source_type == "pdf"
            and not include_pdf
        ]

        print(f"Vault: {vault_id}")
        print(f"Deferred-abstract documents found: {total + len(skipped_pdfs)}")
        print(f"  to reabstract:    {total}")
        print(f"  skipped (pdf):    {len(skipped_pdfs)}")
        if total == 0:
            print("Nothing to do.")
            return 0

        started = datetime.now(timezone.utc)
        succeeded = 0
        failed: list[tuple[str, str, str | None]] = []  # (id, title, version_label)
        version_width = max((len(d.version_label or "") for d in worklist), default=1)

        for i, doc in enumerate(worklist, 1):
            doc_started = datetime.now(timezone.utc)
            try:
                await services.ingestion_service.reabstract(doc.id)
            except Exception as exc:
                print(
                    f"[{i:4d}/{total}]  {doc.id}  dispatch failed: {exc!r}",
                    flush=True,
                )
                failed.append((doc.id, doc.title or "", doc.version_label))
                continue

            status = await _wait_for_terminal(services.graph_store, doc.id, poll_interval)
            elapsed_s = (datetime.now(timezone.utc) - doc_started).total_seconds()
            title_disp = _truncate(doc.title, 60)
            version_disp = doc.version_label or ""
            marker = "✓" if status == PipelineStatus.ABSTRACTION_COMPLETE.value else "✗"
            print(
                f"[{i:4d}/{total}]  {marker}  "
                f"{title_disp:60s}  {version_disp:>{version_width}}  "
                f"{elapsed_s:6.1f}s  ({status})",
                flush=True,
            )
            if status == PipelineStatus.ABSTRACTION_COMPLETE.value:
                succeeded += 1
            else:
                failed.append((doc.id, doc.title or "", doc.version_label))

        elapsed = datetime.now(timezone.utc) - started
        print()
        print(f"Done. {succeeded}/{total} succeeded in {elapsed.total_seconds() / 60:.1f} min.")
        if failed:
            print(f"{len(failed)} failure(s):")
            for fid, ftitle, fver in failed:
                print(f"  {fid}  {_truncate(ftitle, 60)}  {fver or ''}")
        return 0 if not failed else 1
    finally:
        await services.graph_store.close()


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Re-abstract every document in a vault whose pipeline_status "
            "is 'abstraction_skipped'. See script docstring."
        )
    )
    parser.add_argument("vault_id", help="Vault id (e.g. pim_health)")
    parser.add_argument(
        "--include-pdf",
        action="store_true",
        help=(
            "Include source_type=pdf rows. Default is to skip them; "
            "scanned PDFs typically have no extractable text."
        ),
    )
    parser.add_argument(
        "--poll-interval",
        type=float,
        default=1.0,
        help="Seconds between pipeline_status polls (default: 1.0).",
    )
    args = parser.parse_args()

    rc = asyncio.run(
        run(
            args.vault_id,
            include_pdf=args.include_pdf,
            poll_interval=args.poll_interval,
        )
    )
    raise SystemExit(rc)


if __name__ == "__main__":
    main()
