#!/usr/bin/env python3
"""Benchmark a candidate ``AbstractionProvider`` against a stratified
corpus of cas-vault documents (T-0084).

Reads documents from the named vault (default ``cas``), selects a
stratified corpus by ``doc_type`` and length tercile, instantiates the
candidate provider, and times each call. Writes three artifacts to the
output directory (default ``~/sage_vaults/test_vault/imports/`` --
disposable scratch space):

  - ``<DATE>_CAS_REF_AbstractionBenchmarkScorecard_<slug>_v1_0.md`` --
    the one-page scorecard per framework §5 step 4.
  - ``<DATE>_CAS_REF_AbstractionBenchmarkOutputs_<slug>_v1_0.md`` --
    masked side-by-side abstracts for blind review.
  - ``<DATE>_CAS_REF_AbstractionBenchmarkRaw_<slug>_v1_0.json`` --
    raw per-call records for reproducibility.

The harness is read-only against any vault graph; it does not invoke
``IngestionService.reabstract`` and writes no abstracts back. The user
moves the scorecard into the cas vault deliberately via ``sage_ingest``
after the blind review.

Usage::

    .venv/bin/python -m scripts.benchmark_abstraction cas \\
        --model mlx-community/Qwen3-8B-Instruct-2507-4bit \\
        --corpus-size 20 --repeats 2

    # Dry run with the stub provider (no MLX load):
    .venv/bin/python -m scripts.benchmark_abstraction cas \\
        --model stub --corpus-size 3 --repeats 1

Operational note: the candidate provider loads ~5 GB (Qwen3-8B) or
larger (other candidates) into unified memory. Do not run while the
SAGE MCP server's abstraction provider is resident; both processes
would oversubscribe RAM.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from sage.adapters.interfaces import SYNTHETIC_HEADER_HEADING_PATH
from sage.adapters.stubs import StubAbstractionProvider
from sage.config import load_vault_config
from sage.mcp_init import initialize_services
from sage.utils.abstraction_benchmark import (
    CatalogEntry,
    render_outputs_for_blind_review,
    render_scorecard,
    run_benchmark,
    select_corpus,
)
from sage.vault_management import config_path_for_vault

DEFAULT_MODEL = "mlx-community/Qwen3-8B-Instruct-2507-4bit"
DEFAULT_OUTPUT_DIR = Path.home() / "sage_vaults" / "test_vault" / "imports"


def _slug(model_id: str) -> str:
    """Sanitize a model id for embedding in a filename."""
    return re.sub(r"[^A-Za-z0-9._-]+", "-", model_id).strip("-") or "unknown"


def _now_date() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


async def _build_catalog(services) -> list[CatalogEntry]:
    """Enumerate documents in the vault and compute their length signal."""
    docs = await services.graph_store.list_all_documents()
    catalog: list[CatalogEntry] = []
    for doc in docs:
        if doc.doc_type is None:
            continue
        chunks = await services.content_store.get_all_chunks(doc.id)
        body_chunks = [c for c in chunks if c.heading_path != SYNTHETIC_HEADER_HEADING_PATH]
        length_bytes = sum(len(c.content) for c in body_chunks)
        if length_bytes == 0:
            continue
        catalog.append(
            CatalogEntry(doc_id=doc.id, doc_type=doc.doc_type, length_bytes=length_bytes)
        )
    return catalog


async def _collect_baseline_outputs(services, corpus: list[CatalogEntry]) -> dict[str, str]:
    """Fetch the stored ``semantic_abstract`` for each corpus doc."""
    baselines: dict[str, str] = {}
    for entry in corpus:
        doc = await services.graph_store.get_document(entry.doc_id)
        if doc is not None and getattr(doc, "semantic_abstract", None):
            baselines[entry.doc_id] = doc.semantic_abstract
    return baselines


def _build_provider(model: str):
    """Instantiate the candidate provider. ``stub`` swaps in the stub."""
    if model == "stub":
        return StubAbstractionProvider()
    from sage.adapters.abstraction_qwen3 import get_qwen3_abstraction_provider

    return get_qwen3_abstraction_provider(model_id=model)


def _serialize_result(result, baselines: dict[str, str]) -> dict:
    return {
        "candidate_model_id": result.candidate_model_id,
        "corpus_size": result.corpus_size,
        "repeats": result.repeats,
        "started_at": result.started_at,
        "finished_at": result.finished_at,
        "latency_stats": result.latency_stats,
        "memory_stats": result.memory_stats,
        "determinism_verdicts": result.determinism_verdicts,
        "alt_outputs": result.alt_outputs,
        "measurements": [asdict(m) for m in result.measurements],
        "baselines": baselines,
        "notes": result.notes,
    }


def _print_summary(result) -> None:
    print()
    print(f"Candidate:  {result.candidate_model_id}")
    print(f"Corpus:     {result.corpus_size} documents × {result.repeats} repeats")
    print()
    print("Latency (ms):")
    for k in ("mean", "median", "p95", "p99", "min", "max"):
        print(f"  {k:>7}: {result.latency_stats.get(k, 0):.1f}")
    print()
    print("Peak unified-memory used during call (bytes):")
    for k in ("mean", "median", "p95", "p99", "min", "max"):
        print(f"  {k:>7}: {result.memory_stats.get(k, 0):.0f}")
    drift = sum(1 for v in result.determinism_verdicts.values() if v == "drift")
    print()
    print(f"Determinism: {drift} drift / {len(result.determinism_verdicts)} documents")


async def run(args: argparse.Namespace) -> int:
    config_path = config_path_for_vault(args.vault_id)
    if not config_path.exists():
        print(f"vault config not found: {config_path}", file=sys.stderr)
        return 2

    config = load_vault_config(config_path)
    output_dir = args.output_dir.expanduser()
    output_dir.mkdir(parents=True, exist_ok=True)

    # Stub abstraction provider during services init so the production
    # Qwen3 provider does not lazy-load on top of the candidate.
    print(f"Loading SAGE services for vault {args.vault_id!r}...", flush=True)
    services = await initialize_services(
        config,
        config_path=config_path,
        abstraction_provider=StubAbstractionProvider(),
    )

    try:
        print("Enumerating corpus...", flush=True)
        catalog = await _build_catalog(services)
        print(f"  {len(catalog)} candidate documents in vault {args.vault_id}")
        if len(catalog) < args.corpus_size:
            print(
                f"warning: catalog has only {len(catalog)} documents; "
                f"reducing target from {args.corpus_size}",
                file=sys.stderr,
            )

        corpus = select_corpus(catalog, target=args.corpus_size, seed=args.corpus_seed)
        print(f"  selected {len(corpus)} documents (seed={args.corpus_seed})")

        baselines: dict[str, str] = {}
        if args.with_baseline_outputs:
            print("Collecting stored baseline abstracts...", flush=True)
            baselines = await _collect_baseline_outputs(services, corpus)
            print(f"  found {len(baselines)} of {len(corpus)} baselines")

        print(f"Instantiating provider: {args.model}", flush=True)
        provider = _build_provider(args.model)

        print(
            f"Running benchmark: {len(corpus)} docs × {args.repeats} repeats "
            f"({len(corpus) * args.repeats} total calls)...",
            flush=True,
        )
        result = await run_benchmark(
            services=services,
            corpus=corpus,
            provider=provider,
            abstraction_config=config.abstraction,
            repeats=args.repeats,
            candidate_model_id=args.model,
            warmup_calls=args.warmup_calls,
        )

        date = _now_date()
        slug = _slug(args.model)
        scorecard_path = output_dir / f"{date}_CAS_REF_AbstractionBenchmarkScorecard_{slug}_v1_0.md"
        outputs_path = output_dir / f"{date}_CAS_REF_AbstractionBenchmarkOutputs_{slug}_v1_0.md"
        raw_path = output_dir / f"{date}_CAS_REF_AbstractionBenchmarkRaw_{slug}_v1_0.json"

        scorecard_path.write_text(render_scorecard(result), encoding="utf-8")
        outputs_path.write_text(
            render_outputs_for_blind_review(result, baseline_outputs=baselines or None),
            encoding="utf-8",
        )
        raw_path.write_text(
            json.dumps(_serialize_result(result, baselines), indent=2),
            encoding="utf-8",
        )

        _print_summary(result)
        print()
        print(f"Scorecard: {scorecard_path}")
        print(f"Outputs:   {outputs_path}")
        print(f"Raw:       {raw_path}")
        return 0
    finally:
        await services.graph_store.close()


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Benchmark a candidate AbstractionProvider against a stratified "
            "vault corpus. Reads documents from VAULT_ID (default cas); does "
            "not write back to any vault."
        )
    )
    parser.add_argument(
        "vault_id",
        nargs="?",
        default="cas",
        help="Vault id to read documents from (default: cas).",
    )
    parser.add_argument(
        "--model",
        default=DEFAULT_MODEL,
        help=(
            "MLX model identifier for the candidate provider. "
            "Pass 'stub' to use StubAbstractionProvider for dry-runs. "
            f"Default: {DEFAULT_MODEL}"
        ),
    )
    parser.add_argument(
        "--corpus-size",
        type=int,
        default=20,
        help="Target corpus size (default: 20).",
    )
    parser.add_argument(
        "--corpus-seed",
        type=int,
        default=42,
        help="Random seed for stratified corpus selection (default: 42).",
    )
    parser.add_argument(
        "--repeats",
        type=int,
        default=2,
        help="Repeats per document for determinism check (default: 2).",
    )
    parser.add_argument(
        "--warmup-calls",
        type=int,
        default=1,
        help=(
            "Throwaway calls before the measured loop, to amortize the "
            "provider's lazy load and Metal kernel compilation per framework "
            "§3.2. Results are discarded. Default: 1. Set to 0 to disable."
        ),
    )
    parser.add_argument(
        "--with-baseline-outputs",
        action="store_true",
        default=True,
        help=(
            "Include the stored semantic_abstract from each corpus document "
            "as the Card A baseline in the masked outputs file (default: on)."
        ),
    )
    parser.add_argument(
        "--no-baseline-outputs",
        dest="with_baseline_outputs",
        action="store_false",
        help="Disable baseline collection.",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=DEFAULT_OUTPUT_DIR,
        help=(
            "Directory to write the scorecard, masked outputs, and raw JSON. "
            f"Default: {DEFAULT_OUTPUT_DIR} (disposable scratch space)."
        ),
    )
    args = parser.parse_args()

    rc = asyncio.run(run(args))
    raise SystemExit(rc)


if __name__ == "__main__":
    main()
