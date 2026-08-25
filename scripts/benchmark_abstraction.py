#!/usr/bin/env python3
"""Benchmark a candidate ``AbstractionProvider`` against a stratified
corpus of cas-vault documents.

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
moves the scorecard into the cas vault deliberately via ``ingest_document``
after the blind review.

Usage::

    .venv/bin/python -m scripts.benchmark_abstraction cas \\
        --model mlx-community/Qwen3-8B-Instruct-2507-4bit \\
        --corpus-size 20 --repeats 2

    # Dry run with the stub provider (no MLX load):
    .venv/bin/python -m scripts.benchmark_abstraction cas \\
        --model stub --corpus-size 3 --repeats 1

    # Long-context evaluation: run at a configured window, then probe
    # whether a prompt of that size is actually reachable.
    .venv/bin/python -m scripts.benchmark_abstraction cas \\
        --model <candidate> --context-window 131072 --context-probe

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
    probe_context_window,
    render_outputs_for_blind_review,
    render_scorecard,
    run_benchmark,
    select_corpus,
)
from sage.vault_management import config_path_for_vault

DEFAULT_MODEL = "mlx-community/Qwen3-8B-Instruct-2507-4bit"
DEFAULT_OUTPUT_DIR = Path.home() / "sage_vaults" / "test_vault" / "imports"


def _positive_int(raw: str) -> int:
    """argparse type for a token count that must be at least one."""
    value = int(raw)
    if value < 1:
        raise argparse.ArgumentTypeError(f"must be a positive number of tokens, got {value}")
    return value


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


def _build_provider(model: str, context_window: int | None = None):
    """Instantiate the candidate provider. ``stub`` swaps in the stub.

    ``context_window`` is forwarded verbatim, None included: None is the
    provider's unconfigured sentinel, so omitting the flag reproduces the
    built-in window exactly and keeps a run comparable with earlier ones.
    The stub carries no window and ignores it.
    """
    if model == "stub":
        return StubAbstractionProvider()
    from sage.adapters import abstraction_qwen3

    return abstraction_qwen3.get_qwen3_abstraction_provider(
        model_id=model, context_window=context_window
    )


def _record_context_window(result, provider, configured: int | None) -> None:
    """Copy the provider's resolved window onto the result.

    Read after the run so the model has loaded: the native window is only
    knowable from loaded weights, and the effective window is the smaller of
    it and the configured value. The stub carries none of this, in which case
    the fields stay unset and the scorecard reports the section as unrecorded.
    """
    result.configured_context_window = configured
    result.native_context_window = getattr(provider, "_native_context_window", None)
    resolver = getattr(provider, "_effective_context_window", None)
    if callable(resolver):
        result.effective_context_window = resolver()


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
        "peak_rss_bytes": result.peak_rss_bytes,
        "accelerator_peak_bytes": result.accelerator_peak_bytes,
        "machine_total_bytes": result.machine_total_bytes,
        "configured_context_window": result.configured_context_window,
        "native_context_window": result.native_context_window,
        "effective_context_window": result.effective_context_window,
        "context_probe": asdict(result.context_probe) if result.context_probe else None,
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
    prefill = [m.prefill_ms for m in result.measurements if m.prefill_ms is not None]
    if prefill:
        rates = [m.prefill_tps for m in result.measurements if m.prefill_tps is not None]
        print()
        print("Prefill:")
        print(f"     mean: {sum(prefill) / len(prefill):.1f} ms")
        if rates:
            print(f"     rate: {sum(rates) / len(rates):.1f} tokens/s")

    if result.peak_rss_bytes and result.machine_total_bytes:
        print()
        if result.accelerator_peak_bytes:
            print(
                f"Peak accelerator: {result.accelerator_peak_bytes / 1024**3:.1f} GiB "
                f"of {result.machine_total_bytes / 1024**3:.1f} GiB"
            )
        print(
            f"Peak resident:    {result.peak_rss_bytes / 1024**3:.1f} GiB "
            f"of {result.machine_total_bytes / 1024**3:.1f} GiB"
        )

    if result.effective_context_window is not None:
        print()
        print(
            f"Context window: configured={result.configured_context_window} "
            f"native={result.native_context_window} "
            f"effective={result.effective_context_window}"
        )

    if result.context_probe is not None:
        probe = result.context_probe
        print()
        print(
            f"Context probe: {probe.verdict} "
            f"(retained {probe.retained_tokens} of {probe.target_tokens} tokens)"
        )
        if probe.error_type:
            print(f"  error: {probe.error_type}: {probe.error_message}")

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
        provider = _build_provider(args.model, args.context_window)

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
        _record_context_window(result, provider, args.context_window)

        if args.context_probe:
            target = result.effective_context_window or args.context_window
            if target is None:
                print(
                    "warning: --context-probe needs a window to aim at; "
                    "pass --context-window or use a provider that reports one",
                    file=sys.stderr,
                )
            else:
                print(f"Probing the context window at {target} tokens...", flush=True)
                result.context_probe = await probe_context_window(
                    services=services,
                    corpus=corpus,
                    provider=provider,
                    abstraction_config=config.abstraction,
                    target_tokens=target,
                )
                # The probe allocates far more than the measured loop, so the
                # run's headline footprint has to account for it or it reports
                # the smaller of two very different numbers.
                result.peak_rss_bytes = max(
                    result.peak_rss_bytes, result.context_probe.peak_rss_bytes
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


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Build the parser and resolve *argv* (defaults to ``sys.argv[1:]``)."""
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
        "--context-window",
        type=_positive_int,
        default=None,
        help=(
            "Prompt window in tokens for the candidate provider. Omit to "
            "leave the provider on its built-in window, which reproduces a "
            "run recorded before this flag existed. A value above what the "
            "weights support is clamped at load and reported."
        ),
    )
    parser.add_argument(
        "--context-probe",
        action="store_true",
        help=(
            "After the measured loop, exercise the provider once on input "
            "assembled to fill the configured window, and report the prompt "
            "length the model actually received. The provider fits prompts "
            "to the window by design, so a window that cannot be reached "
            "produces an ordinary-looking successful call; the probe reads "
            "its verdict from the retained token count instead."
        ),
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
    return parser.parse_args(argv)


def main() -> None:
    rc = asyncio.run(run(_parse_args()))
    raise SystemExit(rc)


if __name__ == "__main__":
    main()
