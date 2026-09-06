#!/usr/bin/env python3
"""Re-run abstraction for documents in a vault.

Standalone fallback for operator workflows (cron-style, no MCP server
running). Agents should call the ``recompute_deferred_vault_abstracts``
MCP tool instead -- it reuses the running MCP server's already-loaded
AbstractionProvider so the dual-Qwen3 RAM hazard does not apply. The
``--all`` mode (full-vault sweep regardless of pipeline_status)
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
    * ``--ids-file`` mode: reabstracts exactly the ids the named file
      lists, in file order. Where the other two modes ask "which
      documents currently satisfy this predicate", this one replays a
      set decided when some earlier survey ran -- so the pass is
      auditable against that survey and repeatable from it. A named id
      is honored regardless of pipeline_status or source_type, and an
      id absent from the vault aborts the run rather than shortening it.
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

    # Reabstract exactly the documents a survey manifest names
    .venv/bin/python -m scripts.reabstract_deferred VAULT_ID --ids-file ids.txt

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
from pathlib import Path

from sage.config import load_vault_config
from sage.mcp_init import initialize_services
from sage.models.enums import TERMINAL_PIPELINE_STATUS_VALUES, PipelineStatus
from sage.models.schemas import Document
from sage.vault_management import config_path_for_vault


def _truncate(s: str | None, n: int) -> str:
    if not s:
        return ""
    return s if len(s) <= n else s[: n - 1] + "…"


def _load_ids_file(path: Path) -> list[str]:
    """Read document ids from a manifest, one per line, in file order.

    Blank lines and ``#`` comments are skipped, so a manifest may carry a
    provenance header -- which vault it describes, at what window, measured
    when -- above the ids without the header becoming part of the worklist.

    Raises:
        ValueError: The file carries no ids. An empty list must not reach
            the worklist builder, where it would be indistinguishable from
            "no id filter requested" and would sweep the whole vault.
    """
    ids = [
        stripped
        for line in path.read_text().splitlines()
        if (stripped := line.strip()) and not stripped.startswith("#")
    ]
    if not ids:
        raise ValueError(f"no document ids found in {path}")
    return ids


def _select_named(all_docs: list[Document], document_ids: list[str]) -> list[Document]:
    """Select exactly the named documents, in the order given.

    An id absent from the vault raises rather than shortening the worklist:
    a pass that quietly abstracted fewer documents than it was handed would
    report a clean result for one it never read. Mirrors the selection
    semantics ``sage.utils.abstraction_benchmark.select_named_corpus``
    applies to a benchmark corpus; the two differ only in the record type
    they select over.

    Raises:
        ValueError: An id appears more than once.
        KeyError: An id has no document in the vault.
    """
    seen: set[str] = set()
    for document_id in document_ids:
        if document_id in seen:
            raise ValueError(f"document id requested more than once: {document_id}")
        seen.add(document_id)

    by_id = {doc.id: doc for doc in all_docs}
    missing = [document_id for document_id in document_ids if document_id not in by_id]
    if missing:
        raise KeyError(f"document ids not found in vault: {', '.join(missing)}")

    return [by_id[document_id] for document_id in document_ids]


def _build_worklist(
    all_docs: list[Document],
    *,
    include_all_statuses: bool,
    include_pdf: bool,
    document_ids: list[str] | None = None,
) -> list[Document]:
    """Filter the full document list to the reabstract worklist.

    With ``document_ids``: exactly those documents, in the order given, and
    neither filter below applies. Naming a document is a stronger statement
    than any predicate, so a named id is honored whatever its
    ``pipeline_status`` or ``source_type`` -- the caller has already settled
    which documents belong in the pass.

    Otherwise the predicate modes apply. Default
    (``include_all_statuses=False``): only documents with
    ``pipeline_status='abstraction_skipped'``. With
    ``include_all_statuses=True``: every document regardless of status
    (used for model-switch or prompt-change full-vault sweeps).
    The PDF filter composes independently: PDFs are excluded unless
    ``include_pdf=True``.
    """
    if document_ids is not None:
        return _select_named(all_docs, document_ids)

    return [
        d
        for d in all_docs
        if (include_all_statuses or d.pipeline_status == PipelineStatus.ABSTRACTION_SKIPPED.value)
        and (include_pdf or d.source_type != "pdf")
    ]


# Ceiling on the per-document wait, matching the in-process service path. A
# document can stop advancing toward a terminal status entirely -- the
# abstraction worker is cancelled on teardown, which drops queued jobs and
# leaves their documents stranded mid-pipeline -- and an unbounded poll
# against one of those never returns, stalling the whole sweep on one
# document with no output. Sized to clear the slowest legitimate document
# rather than the typical one: a full-context generation runs roughly 25
# minutes and the worker spends up to abstraction.max_attempts of them.
WAIT_TIMEOUT_SECONDS = 7200.0

# Returned when the document disappears between dispatch and settling, and
# when the ceiling is reached. Neither is a pipeline_status value, so
# neither can collide with a real one; both are printed in the per-document
# line, so the operator sees which happened.
WAIT_MISSING = "missing"
WAIT_TIMEOUT = "timeout"


async def _wait_for_terminal(
    graph_store,
    document_id: str,
    poll_interval: float,
    timeout_seconds: float = WAIT_TIMEOUT_SECONDS,
) -> str:
    """Poll the document until pipeline_status is terminal, then return it.

    Terminal is read from the shared vocabulary rather than restated: this
    sweep's own worklist is built from ``abstraction_skipped``, so a
    restatement omitting it would leave the poller waiting on a document
    that had already settled back where it started.

    Returns :data:`WAIT_MISSING` if the document disappears mid-flight and
    :data:`WAIT_TIMEOUT` if it has not settled within ``timeout_seconds``.
    """
    deadline = asyncio.get_running_loop().time() + timeout_seconds
    while True:
        doc = await graph_store.get_document(document_id)
        if doc is None:
            return WAIT_MISSING
        status = doc.pipeline_status
        if status in TERMINAL_PIPELINE_STATUS_VALUES:
            return status
        # Checked after the status read so a document that settled exactly
        # at the deadline is reported as settled rather than abandoned one
        # poll short of its own terminal status.
        if asyncio.get_running_loop().time() >= deadline:
            return WAIT_TIMEOUT
        await asyncio.sleep(poll_interval)


async def run(
    vault_id: str,
    *,
    include_all_statuses: bool,
    include_pdf: bool,
    poll_interval: float,
    document_ids: list[str] | None = None,
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
            document_ids=document_ids,
        )
        total = len(worklist)
        # PDFs that would have entered the worklist if --include-pdf were set.
        # Named ids bypass the PDF filter, so nothing was skipped on that
        # basis and reporting a count would misdescribe the run.
        skipped_pdfs = (
            []
            if document_ids is not None
            else [
                d
                for d in all_docs
                if (
                    include_all_statuses
                    or d.pipeline_status == PipelineStatus.ABSTRACTION_SKIPPED.value
                )
                and d.source_type == "pdf"
                and not include_pdf
            ]
        )

        if document_ids is not None:
            mode_label = "named documents"
        elif include_all_statuses:
            mode_label = "all documents"
        else:
            mode_label = "deferred-abstract documents"
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
    # --all sweeps by predicate, --ids-file by name. A run is one or the
    # other; supplying both leaves the selected set ambiguous.
    selection = parser.add_mutually_exclusive_group()
    selection.add_argument(
        "--all",
        action="store_true",
        help=(
            "Reabstract every document regardless of pipeline_status. "
            "Without this flag, only documents in 'abstraction_skipped' "
            "are enumerated (the original behavior). Use for a full-vault "
            "model-switch or prompt-change reabstract pass."
        ),
    )
    selection.add_argument(
        "--ids-file",
        type=Path,
        metavar="PATH",
        help=(
            "Reabstract exactly the document ids listed in this file, one "
            "per line, in file order. Blank lines and '#' comments are "
            "skipped, so a manifest may carry a provenance header. Named "
            "ids are honored regardless of pipeline_status or source_type, "
            "and an id absent from the vault aborts the run rather than "
            "shortening it. Use to reproduce a pass over a set fixed by an "
            "earlier survey."
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

    document_ids = None
    if args.ids_file is not None:
        try:
            document_ids = _load_ids_file(args.ids_file)
        except (OSError, ValueError) as exc:
            print(f"cannot read ids file: {exc}", file=sys.stderr)
            raise SystemExit(2) from exc

    try:
        rc = asyncio.run(
            run(
                args.vault_id,
                include_all_statuses=args.all,
                include_pdf=args.include_pdf,
                poll_interval=args.poll_interval,
                document_ids=document_ids,
            )
        )
    except (KeyError, ValueError) as exc:
        # A defective id list is a caller error, not a crash worth a
        # traceback: report which ids were wrong and stop before any
        # document is reabstracted.
        print(f"worklist rejected: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
    raise SystemExit(rc)


if __name__ == "__main__":
    main()
