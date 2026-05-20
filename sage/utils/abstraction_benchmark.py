"""Benchmark harness for evaluating candidate ``AbstractionProvider``
implementations (T-0084).

The harness is read-only against any vault graph: it pulls projection
text from the content store, invokes the provider directly, and never
calls ``IngestionService.reabstract``. Outputs land as filesystem
artifacts; the user ingests the scorecard into the cas vault
deliberately via ``sage_ingest`` after the blind review.

Module structure:

- Dataclasses: ``CatalogEntry``, ``MeasurementRecord``, ``BenchmarkResult``.
- ``select_corpus``: stratifies by ``doc_type`` and length tercile.
- ``MemorySampler``: async context manager that polls a memory probe
  during a provider call to capture the minimum free reading.
- ``measure_one`` / ``measure_with_determinism_check``: per-document
  measurement, replicating the production
  ``IngestionService._generate_abstract_text`` call shape.
- ``run_benchmark``: top-level run over a corpus.
- ``aggregate_latency``: distribution stats.
- ``render_scorecard`` / ``render_outputs_for_blind_review``:
  markdown emitters per framework §5 step 4 and §3.1 dim 7.
"""

from __future__ import annotations

import asyncio
import random
import statistics
import time
from dataclasses import dataclass, field
from typing import Callable

from sage.adapters.abstraction_utils import compute_max_tokens, trim_to_sentence_boundary
from sage.adapters.interfaces import (
    SYNTHETIC_HEADER_HEADING_PATH,
    AbstractionProvider,
)
from sage.config import VaultAbstractionConfig as AbstractionConfig

MemoryProbe = Callable[[], int]


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


@dataclass
class CatalogEntry:
    """One document's identity-and-length signal for corpus selection."""

    doc_id: str
    doc_type: str
    length_bytes: int


@dataclass
class MeasurementRecord:
    """One per-call measurement.

    ``memory_delta_bytes`` is free-before minus free-after (positive
    means memory was consumed). ``peak_used_bytes_during_call`` is
    free-before minus the minimum free seen during the call (captures
    transient spikes a before/after delta would miss).
    """

    doc_id: str
    doc_type: str | None
    word_count: int
    max_tokens: int
    wall_clock_ms: float
    tokens_generated: int
    output_text: str
    memory_delta_bytes: int
    peak_used_bytes_during_call: int


@dataclass
class BenchmarkResult:
    """Aggregated result of a benchmark run against a single candidate."""

    candidate_model_id: str
    corpus_size: int
    repeats: int
    measurements: list[MeasurementRecord]
    determinism_verdicts: dict[str, str]
    alt_outputs: dict[str, list[str]]
    latency_stats: dict[str, float]
    memory_stats: dict[str, float]
    started_at: str
    finished_at: str
    notes: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Corpus selection
# ---------------------------------------------------------------------------


def _length_tercile_boundaries(catalog: list[CatalogEntry]) -> tuple[int, int]:
    """Compute the low and high length-tercile boundaries across the catalog."""
    sorted_lengths = sorted(e.length_bytes for e in catalog)
    n = len(sorted_lengths)
    if n == 0:
        return (0, 0)
    low = sorted_lengths[n // 3]
    high = sorted_lengths[(2 * n) // 3]
    return (low, high)


def _tercile(length: int, low: int, high: int) -> int:
    if length < low:
        return 0
    if length < high:
        return 1
    return 2


def select_corpus(catalog: list[CatalogEntry], target: int, seed: int = 42) -> list[CatalogEntry]:
    """Stratify the catalog across (doc_type, length_tercile) cells.

    Round-robins through cells, picking one entry per cell per pass,
    until ``target`` entries are selected. Within each cell, entries
    are visited in a seeded shuffle so the selection is reproducible.
    Sparse cells are passed over; dense cells contribute multiple
    entries on later passes.
    """
    if target <= 0 or not catalog:
        return []

    low, high = _length_tercile_boundaries(catalog)
    # Seeded RNG for reproducible corpus selection; not used for security.
    rng = random.Random(seed)  # noqa: S311

    cells: dict[tuple[str, int], list[CatalogEntry]] = {}
    for entry in catalog:
        cell = (entry.doc_type, _tercile(entry.length_bytes, low, high))
        cells.setdefault(cell, []).append(entry)

    # Shuffle entries within each cell so selection isn't biased by
    # catalog order, and shuffle cell order so the visit sequence is
    # also seed-driven.
    for entries in cells.values():
        rng.shuffle(entries)
    cell_keys = list(cells.keys())
    rng.shuffle(cell_keys)

    selected: list[CatalogEntry] = []
    seen_ids: set[str] = set()
    # Track per-cell cursors so we don't pick the same entry twice.
    cursors: dict[tuple[str, int], int] = {k: 0 for k in cell_keys}

    while len(selected) < target:
        made_progress = False
        for key in cell_keys:
            if len(selected) >= target:
                break
            entries = cells[key]
            cursor = cursors[key]
            if cursor >= len(entries):
                continue
            entry = entries[cursor]
            cursors[key] = cursor + 1
            if entry.doc_id in seen_ids:
                continue
            selected.append(entry)
            seen_ids.add(entry.doc_id)
            made_progress = True
        if not made_progress:
            break

    return selected


# ---------------------------------------------------------------------------
# Memory sampler
# ---------------------------------------------------------------------------


class MemorySampler:
    """Polls a memory probe during a provider call.

    Records a baseline reading on enter, polls at ``poll_interval_s``
    intervals while the body runs (tracking the minimum), and records
    an after reading on exit. ``peak_used_bytes_during_call`` is
    ``baseline - min(during)``; ``memory_delta_bytes`` is
    ``baseline - after``.
    """

    def __init__(self, probe: MemoryProbe, poll_interval_s: float = 0.5) -> None:
        self._probe = probe
        self._poll_interval_s = poll_interval_s
        self._baseline: int = 0
        self._min_during: int = 0
        self._after: int = 0
        self._stop = False
        self._task: asyncio.Task | None = None

    async def __aenter__(self) -> "MemorySampler":
        self._baseline = self._probe()
        self._min_during = self._baseline
        self._stop = False
        self._task = asyncio.create_task(self._poll_loop())
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        self._stop = True
        if self._task is not None:
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self._after = self._probe()

    async def _poll_loop(self) -> None:
        while not self._stop:
            await asyncio.sleep(self._poll_interval_s)
            if self._stop:
                break
            val = self._probe()
            if val < self._min_during:
                self._min_during = val

    @property
    def peak_used_bytes_during_call(self) -> int:
        return max(0, self._baseline - self._min_during)

    @property
    def memory_delta_bytes(self) -> int:
        return self._baseline - self._after


# ---------------------------------------------------------------------------
# Measurement
# ---------------------------------------------------------------------------


async def measure_one(
    provider: AbstractionProvider,
    projection_text: str,
    doc_type: str | None,
    abstraction_config: AbstractionConfig,
    mem_probe: MemoryProbe,
    poll_interval_s: float = 0.5,
    doc_id: str = "",
) -> MeasurementRecord:
    """Single-call measurement against a provider.

    Replicates the call shape of ``IngestionService._generate_abstract_text``:
    computes the same density-proportional ``max_tokens`` via the shared
    ``compute_max_tokens`` helper and applies ``trim_to_sentence_boundary``
    to the result.
    """
    word_count = len(projection_text.split())
    max_tokens = compute_max_tokens(word_count, abstraction_config)

    sampler = MemorySampler(mem_probe, poll_interval_s=poll_interval_s)
    t0 = time.perf_counter()
    async with sampler:
        raw_output = await provider.generate_abstract(projection_text, max_tokens, doc_type)
    elapsed_ms = (time.perf_counter() - t0) * 1000.0

    trimmed = trim_to_sentence_boundary(raw_output)
    tokens_generated = len(trimmed.split())

    return MeasurementRecord(
        doc_id=doc_id,
        doc_type=doc_type,
        word_count=word_count,
        max_tokens=max_tokens,
        wall_clock_ms=elapsed_ms,
        tokens_generated=tokens_generated,
        output_text=trimmed,
        memory_delta_bytes=sampler.memory_delta_bytes,
        peak_used_bytes_during_call=sampler.peak_used_bytes_during_call,
    )


async def measure_with_determinism_check(
    provider: AbstractionProvider,
    projection_text: str,
    doc_type: str | None,
    abstraction_config: AbstractionConfig,
    mem_probe: MemoryProbe,
    poll_interval_s: float = 0.5,
    repeats: int = 2,
    doc_id: str = "",
) -> tuple[MeasurementRecord, str, list[str]]:
    """Call ``measure_one`` ``repeats`` times and verify byte-identical output.

    Returns ``(primary_record, verdict, outputs)`` where ``verdict`` is
    ``"identical"`` if every repeat produced the same trimmed output,
    otherwise ``"drift"``.
    """
    if repeats < 1:
        raise ValueError("repeats must be at least 1")

    records: list[MeasurementRecord] = []
    outputs: list[str] = []
    for _ in range(repeats):
        record = await measure_one(
            provider=provider,
            projection_text=projection_text,
            doc_type=doc_type,
            abstraction_config=abstraction_config,
            mem_probe=mem_probe,
            poll_interval_s=poll_interval_s,
            doc_id=doc_id,
        )
        records.append(record)
        outputs.append(record.output_text)

    verdict = "identical" if all(o == outputs[0] for o in outputs) else "drift"
    return records[0], verdict, outputs


# ---------------------------------------------------------------------------
# Benchmark run
# ---------------------------------------------------------------------------


def _now_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


async def run_benchmark(
    services,
    corpus: list[CatalogEntry],
    provider: AbstractionProvider,
    abstraction_config: AbstractionConfig,
    repeats: int = 2,
    mem_probe: MemoryProbe | None = None,
    poll_interval_s: float = 0.5,
    candidate_model_id: str | None = None,
    warmup_calls: int = 1,
) -> BenchmarkResult:
    """Run the candidate provider over the corpus.

    Reads chunks via ``services.content_store.get_all_chunks`` and
    joins them into projection text (filtering the synthetic header
    chunk). Never touches ``services.ingestion_service`` -- the
    benchmark is read-only against the vault graph.

    ``warmup_calls`` invocations are made against the first corpus
    document's projection text BEFORE the measured loop starts, with
    their results discarded. This amortizes the provider's lazy load,
    Metal kernel compilation, and any first-N-calls warm-up variance
    so the measured latency reflects steady-state behaviour per the
    Abstraction Provider Evaluation Framework §3.2.
    """
    if mem_probe is None:
        from sage.utils.unified_memory import free_unified_memory_bytes

        mem_probe = free_unified_memory_bytes

    started_at = _now_iso()
    measurements: list[MeasurementRecord] = []
    determinism: dict[str, str] = {}
    alt_outputs: dict[str, list[str]] = {}

    # Warm up the provider before timing anything. Use the first corpus
    # doc's text so the warmup work is representative of real input.
    if warmup_calls > 0 and corpus:
        warmup_chunks = await services.content_store.get_all_chunks(corpus[0].doc_id)
        warmup_body = [c for c in warmup_chunks if c.heading_path != SYNTHETIC_HEADER_HEADING_PATH]
        warmup_text = "\n\n".join(c.content for c in warmup_body)
        warmup_word_count = len(warmup_text.split())
        warmup_max_tokens = compute_max_tokens(warmup_word_count, abstraction_config)
        for _ in range(warmup_calls):
            await provider.generate_abstract(warmup_text, warmup_max_tokens, corpus[0].doc_type)

    for entry in corpus:
        chunks = await services.content_store.get_all_chunks(entry.doc_id)
        body_chunks = [c for c in chunks if c.heading_path != SYNTHETIC_HEADER_HEADING_PATH]
        projection_text = "\n\n".join(c.content for c in body_chunks)

        record, verdict, outputs = await measure_with_determinism_check(
            provider=provider,
            projection_text=projection_text,
            doc_type=entry.doc_type,
            abstraction_config=abstraction_config,
            mem_probe=mem_probe,
            poll_interval_s=poll_interval_s,
            repeats=repeats,
            doc_id=entry.doc_id,
        )
        measurements.append(record)
        determinism[entry.doc_id] = verdict
        alt_outputs[entry.doc_id] = outputs

    finished_at = _now_iso()
    latency_stats = aggregate_latency([m.wall_clock_ms for m in measurements])
    memory_stats = aggregate_latency([float(m.peak_used_bytes_during_call) for m in measurements])

    return BenchmarkResult(
        candidate_model_id=candidate_model_id or "unknown",
        corpus_size=len(corpus),
        repeats=repeats,
        measurements=measurements,
        determinism_verdicts=determinism,
        alt_outputs=alt_outputs,
        latency_stats=latency_stats,
        memory_stats=memory_stats,
        started_at=started_at,
        finished_at=finished_at,
    )


# ---------------------------------------------------------------------------
# Statistics
# ---------------------------------------------------------------------------


def aggregate_latency(values: list[float]) -> dict[str, float]:
    """Return mean / median / p95 / p99 / min / max over ``values``."""
    if not values:
        return {"mean": 0.0, "median": 0.0, "p95": 0.0, "p99": 0.0, "min": 0.0, "max": 0.0}

    sorted_vals = sorted(values)
    mean = statistics.mean(values)
    median = statistics.median(values)

    if len(values) >= 2:
        quantiles = statistics.quantiles(values, n=100, method="inclusive")
        p95 = quantiles[94]
        p99 = quantiles[98]
    else:
        p95 = sorted_vals[0]
        p99 = sorted_vals[0]

    return {
        "mean": mean,
        "median": median,
        "p95": p95,
        "p99": p99,
        "min": sorted_vals[0],
        "max": sorted_vals[-1],
    }


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def render_scorecard(result: BenchmarkResult) -> str:
    """Render the one-page scorecard per framework §5 step 4.

    Quantitative half is filled programmatically; blind-review rubric
    and prompt verdict are left blank for the user to complete after
    review.
    """
    target_mean_ms = 13_000  # framework §7 criterion 1
    crit_1_met = result.latency_stats.get("mean", 0.0) <= target_mean_ms

    drift_count = sum(1 for v in result.determinism_verdicts.values() if v == "drift")
    determinism_status = (
        "identical across re-runs" if drift_count == 0 else f"drift on {drift_count} documents"
    )

    lines: list[str] = []
    lines.append(f"# Abstraction Provider Benchmark Scorecard — {result.candidate_model_id}")
    lines.append("")
    lines.append(f"**Run window:** {result.started_at} → {result.finished_at}")
    lines.append(f"**Corpus size:** {result.corpus_size} documents × {result.repeats} repeats")
    lines.append("")

    lines.append("## Candidate")
    lines.append("")
    lines.append(f"- Model id: `{result.candidate_model_id}`")
    lines.append(f"- Corpus size: {result.corpus_size}")
    lines.append(f"- Repeats per document: {result.repeats}")
    lines.append("")

    lines.append("## Latency")
    lines.append("")
    lines.append("| Metric | Value (ms) |")
    lines.append("|---|---|")
    for k in ("mean", "median", "p95", "p99", "min", "max"):
        lines.append(f"| {k} | {result.latency_stats.get(k, 0):.1f} |")
    lines.append("")

    lines.append("## Memory footprint")
    lines.append("")
    lines.append("Peak unified-memory used during call (bytes):")
    lines.append("")
    lines.append("| Metric | Value (bytes) |")
    lines.append("|---|---|")
    for k in ("mean", "median", "p95", "p99", "min", "max"):
        lines.append(f"| {k} | {result.memory_stats.get(k, 0):.0f} |")
    lines.append("")

    lines.append("## Determinism")
    lines.append("")
    lines.append(f"- Verdict: {determinism_status}")
    lines.append(f"- Documents with drift: {drift_count} of {result.corpus_size}")
    lines.append("")

    lines.append("## Prompt verdict")
    lines.append("")
    lines.append("_To be filled after blind review:_")
    lines.append("- [ ] verbatim worked")
    lines.append("- [ ] light tune required")
    lines.append("- [ ] heavy tune required")
    lines.append("")

    lines.append("## Decision criteria (§7)")
    lines.append("")
    auto_1 = "x" if crit_1_met else " "
    mean_ms = result.latency_stats.get("mean", 0)
    lines.append(
        f"- [{auto_1}] 1. Mean wall-clock under 13 s per abstract (measured mean: {mean_ms:.1f} ms)"
    )
    lines.append("- [ ] 2. Quality verdict is pilot-worthy (per §6, requires blind review)")
    lines.append(
        "- [ ] 3. Memory footprint fits within the unified-memory budget "
        "(≥ 20 GB free during worst-case generation)"
    )
    lines.append("- [ ] 4. Operational fit acceptable: deferred load and idle eviction preserved")
    lines.append("- [ ] 5. Prompt verdict is verbatim-worked or light-tune-required")
    lines.append("")

    lines.append("## Blind review")
    lines.append("")
    lines.append(
        "Per the framework §6 rubric. Score each dimension as "
        "Excellent / Acceptable / Borderline / Unusable."
    )
    lines.append("")
    lines.append("| Dimension | Score | Notes |")
    lines.append("|---|---|---|")
    lines.append("| Coverage |  |  |")
    lines.append("| Faithfulness |  |  |")
    lines.append("| Describe-don't-imitate |  |  |")
    lines.append("| Length proportionality |  |  |")
    lines.append("| Doc-type voice |  |  |")
    lines.append("| Metadata restraint |  |  |")
    lines.append(
        "| Determinism | {} |  |".format("Excellent" if drift_count == 0 else "Borderline")
    )
    lines.append("")
    lines.append(
        "**Per-corpus verdict:** _Pilot-worthy / Borderline / Reject (fill in after review)_"
    )
    lines.append("")

    return "\n".join(lines)


def render_outputs_for_blind_review(
    result: BenchmarkResult, baseline_outputs: dict[str, str] | None = None
) -> str:
    """Render a masked side-by-side outputs file for blind review.

    Per-document sections present each abstract under stable masked
    labels (``Card A``, ``Card B``). The provider→card mapping appears
    only in a ``<details>``-wrapped ``## Reveal`` section at the end.
    """
    baseline_outputs = baseline_outputs or {}
    lines: list[str] = []
    lines.append("# Abstraction Benchmark — Blind Review")
    lines.append("")
    lines.append(
        "Each section shows two abstracts under masked labels. Score each "
        "against the framework §6 rubric without consulting the reveal block "
        "until you are done."
    )
    lines.append("")

    for record in result.measurements:
        baseline = baseline_outputs.get(record.doc_id)
        lines.append(f"## Document {record.doc_id}")
        lines.append("")
        if baseline is not None:
            lines.append("### Card A")
            lines.append("")
            lines.append(baseline)
            lines.append("")
            lines.append("### Card B")
            lines.append("")
            lines.append(record.output_text)
            lines.append("")
        else:
            lines.append("### Card B")
            lines.append("")
            lines.append(record.output_text)
            lines.append("")
            lines.append("_(Card A baseline not supplied for this document.)_")
            lines.append("")

    lines.append("## Reveal")
    lines.append("")
    lines.append("<details>")
    lines.append("<summary>Click to reveal the provider behind each card.</summary>")
    lines.append("")
    lines.append("- **Card A** — baseline (stored Qwen3-30B abstract from the cas vault).")
    lines.append(f"- **Card B** — candidate (`{result.candidate_model_id}`).")
    lines.append("")
    lines.append("</details>")
    lines.append("")

    return "\n".join(lines)
