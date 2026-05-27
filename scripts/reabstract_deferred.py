#!/usr/bin/env python3
"""Re-run abstraction for documents in a vault.

Standalone fallback for operator workflows (cron-style, no MCP server
running). Agents should call the ``recompute_deferred_vault_abstracts``
MCP tool instead -- it reuses the running MCP server's already-loaded
AbstractionProvider so the dual-Qwen3 RAM hazard does not apply (,
F-8). The ``--all`` mode (full-vault sweep regardless of pipeline_status)
is still operator-only and remains the reason this script lives on.

By default, picks up every document with ``pipeline_status='abstraction_skipped'``
(the "deferred abstracts" health indicator). With ``--all``, enumerates
every document regardless of pipeline_status -- the mode used for
full-vault model-switch or prompt-change reabstract passes.

Per CAS-ADR-011 graceful degradation, ingest writes
``pipeline_status='abstraction_skipped'`` when the abstraction stage was
bypassed (provider unavailable, vault config previously disabled,
empty projection text). The records carry no ``semantic_abstract``.
Indexing completed before the skip decision (see
``IngestionService._run_background_pipeline``), so the chunks are
present in the content store and ``recompute_abstract`` can rebuild the
projection text and write a fresh abstract.

Behavior:
    * Default mode: enumerates documents with
      ``pipeline_status='abstraction_skipped'`` in the named vault.
    * ``--all`` mode: enumerates every document in the vault regardless
      of pipeline_status. Use for model-switch or prompt-change passes
      where every existing abstract must be regenerated.
    * Skips scanned-PDF rows by default (``--include-pdf`` to include them).
      The scanned-PDF row has no extractable text; reabstract returns
      ``no_projection`` or a degenerate abstract.
    * Sequential. Calls ``IngestionService.reabstract`` (fire-and-forget),
      then polls the document row until pipeline_status is terminal
      (``abstraction_complete`` or ``failed``).
    * Prints ``[i/N] title vN`` on each completion, plus a final summary.

Usage::

    # Default: reabstract only the 'abstraction_skipped' worklist
    .venv/bin/python -m scripts.reabstract_deferred VAULT_ID

    # Reabstract every document (model switch, prompt change, full sweep)
    .venv/bin/python -m scripts.reabstract_deferred VAULT_ID --all

    # Include scanned PDFs in the worklist
    .venv/bin/python -m scripts.reabstract_deferred VAULT_ID --include-pdf

    # Custom polling interval (seconds)
    .venv/bin/python -m scripts.reabstract_deferred VAULT_ID --poll-interval 2.0

Operational note: the script loads its own Qwen3 MLX abstraction
provider (~16 GB resident for the 30B; ~5 GB for the 8B). The running
SAGE MCP server lazily loads its own provider on first abstraction
call -- do not invoke any abstraction-triggering MCP tool
(``ingest_document`` without ``abstraction.enabled=false``,
``recompute_abstract``) while this script is running, or both processes
will hold the model and oversubscribe RAM (F-8 precedent). For ``--all``
passes that switch the vault's abstraction model, stop the MCP server,
edit ``vault_config.yaml``, then run this script in a separate process.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import datetime, timezone

from sage.config import load_vault_config
from sage.mcp_init import initialize_services
from sage.models.enums import PipelineStatus
from sage.models.schemas import Document
from sage.vault_management import config_path_for_vault


def _truncate(s: str | None, n: int) -> str:
    if not s:
        return ""
    return s if len(s) <= n else s[: n - 1] + "…"


def _build_worklist(
    all_docs: list[Document],
    *,
    include_all_statuses: bool,
    include_pdf: bool,
) -> list[Document]:
    """Filter the full document list to the reabstract worklist.

    Default (``include_all_statuses=False``): only documents with
    ``pipeline_status='abstraction_skipped'``. With
    ``include_all_statuses=True``: every document regardless of status
    (used for model-switch or prompt-change full-vault sweeps).
    The PDF filter composes independently: PDFs are excluded unless
    ``include_pdf=True``.
    """
    return [
        d
        for d in all_docs
        if (include_all_statuses or d.pipeline_status == PipelineStatus.ABSTRACTION_SKIPPED.value)
        and (include_pdf or d.source_type != "pdf")
    ]


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


async def run(
    vault_id: str,
    *,
    include_all_statuses: bool,
    include_pdf: bool,
    poll_interval: float,
) -> int:
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
        worklist = _build_worklist(
            all_docs,
            include_all_statuses=include_all_statuses,
            include_pdf=include_pdf,
        )
        total = len(worklist)
        # PDFs that would have entered the worklist if --include-pdf were set.
        skipped_pdfs = [
            d
            for d in all_docs
            if (
                include_all_statuses
                or d.pipeline_status == PipelineStatus.ABSTRACTION_SKIPPED.value
            )
            and d.source_type == "pdf"
            and not include_pdf
        ]

        mode_label = "all documents" if include_all_statuses else "deferred-abstract documents"
        print(f"Vault: {vault_id}")
        print(f"{mode_label.capitalize()} found: {total + len(skipped_pdfs)}")
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


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Re-abstract documents in a vault. Default: the "
            "'abstraction_skipped' worklist. With --all: every document "
            "regardless of pipeline_status. See script docstring."
        )
    )
    parser.add_argument("vault_id", help="Vault id (e.g. example_vault)")
    parser.add_argument(
        "--all",
        action="store_true",
        help=(
            "Reabstract every document regardless of pipeline_status. "
            "Without this flag, only documents in 'abstraction_skipped' "
            "are enumerated (the original behavior). Use for a full-vault "
            "model-switch or prompt-change reabstract pass."
        ),
    )
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
    return parser


def main() -> None:
    args = _build_parser().parse_args()

    rc = asyncio.run(
        run(
            args.vault_id,
            include_all_statuses=args.all,
            include_pdf=args.include_pdf,
            poll_interval=args.poll_interval,
        )
    )
    raise SystemExit(rc)


if __name__ == "__main__":
    main()
