"""Benchmark harness for evaluating candidate ``AbstractionProvider``
implementations.

The harness is read-only against any vault graph: it pulls projection
text from the content store, invokes the provider directly, and never
calls ``IngestionService.reabstract``. Outputs land as filesystem
artifacts; the user ingests the scorecard into the cas vault
deliberately via ``ingest_document`` after the blind review.

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
import json
import logging
import random
import resource
import statistics
import sys
import time
from dataclasses import dataclass, field
from typing import Callable

from sage.adapters.abstraction_utils import compute_max_tokens, trim_to_sentence_boundary
from sage.adapters.interfaces import (
    SYNTHETIC_HEADER_HEADING_PATH,
    AbstractionProvider,
)
from sage.config import VaultAbstractionConfig as AbstractionConfig
from sage.utils.unified_memory import free_unified_memory_bytes

logger = logging.getLogger(__name__)

MemoryProbe = Callable[[], int]

#: Probe used when a caller supplies none. Named at module scope so the
#: default-resolution path is substitutable without patching call internals.
#: Importing the helper is safe on every platform -- it reaches macOS
#: ``vm_stat`` only when called.
DEFAULT_MEM_PROBE: MemoryProbe = free_unified_memory_bytes

# The local provider publishes its per-phase breakdown as a structured log
# record rather than as a return value, because the providers share no
# vocabulary below wall-clock time. Reading it back through the logging
# system is what keeps the harness a measurement: the provider is invoked
# exactly as production invokes it, and nothing about the generation changes
# because a benchmark happened to be watching.
TIMING_LOGGER_NAME = "sage.abstraction.timing"

# Fields lifted onto each measurement. Names track the emitted record
# exactly -- a rename upstream must surface as an absent column here rather
# than as a plausible number read from a field that no longer means what it
# did.
_PHASE_FIELDS = (
    "prefill_ms",
    "prefill_tps",
    "decode_ms",
    "decode_tps",
    "input_tokens",
    "retained_tokens",
    "prompt_tokens",
)


def _probe_or_zero(probe: MemoryProbe) -> int:
    """Call a diagnostic memory probe, reporting zero when it cannot answer.

    The memory figures are metadata about a run, not results of it: latency,
    determinism, and output text do not depend on them. The probes read
    platform-specific interfaces, so a host that does not offer them should
    cost the run its memory columns and nothing else. Zero is the
    "unrecorded" signal the scorecard already renders as such, which keeps an
    unavailable figure distinguishable from a measured one.
    """
    try:
        return probe()
    except Exception as exc:  # noqa: BLE001 -- diagnostic metadata is never fatal
        logger.debug("memory probe unavailable: %s", exc)
        return 0


def _self_rss_bytes() -> int:
    """Return this process's peak resident set size in bytes.

    ``ru_maxrss`` is reported in bytes on Darwin and in kibibytes on Linux,
    so the unit is normalized here rather than at each call site. It is a
    high-water mark the kernel maintains, which is the figure a memory budget
    is decided against -- an instantaneous reading would miss the allocation
    peak entirely.
    """
    raw = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return raw if sys.platform == "darwin" else raw * 1024


class _RecordCollector(logging.Handler):
    """Parses each timing record and holds the most recent one."""

    def __init__(self) -> None:
        super().__init__(level=logging.INFO)
        self.record: dict | None = None

    def emit(self, record: logging.LogRecord) -> None:
        try:
            payload = json.loads(record.getMessage())
        except (ValueError, TypeError):
            return
        if isinstance(payload, dict):
            self.record = payload


class LatencyRecordCapture:
    """Capture the abstraction provider's structured latency record.

    Scoped to a single generation: ``reset`` clears the slot before a call so
    the record read afterwards belongs to that call and cannot be inherited
    from an earlier one.

    The capture raises the logger to INFO for its lifetime. Without that the
    record is discarded before any handler runs, since a process that has not
    configured logging leaves the effective level at the root default -- so a
    capture that only attached a handler would report every phase as
    unmeasured and look exactly like a provider that publishes nothing.
    Handlers, level, and propagation are all restored on exit.
    """

    def __init__(self, logger_name: str = TIMING_LOGGER_NAME) -> None:
        self._logger = logging.getLogger(logger_name)
        self._handler = _RecordCollector()
        self._prev_level: int | None = None
        self._prev_propagate: bool | None = None

    def __enter__(self) -> "LatencyRecordCapture":
        self._prev_level = self._logger.level
        self._prev_propagate = self._logger.propagate
        self._logger.setLevel(logging.INFO)
        # Records are consumed here, so re-emitting them through the root
        # handlers would put one JSON line per generation on the operator's
        # console for no benefit.
        self._logger.propagate = False
        self._logger.addHandler(self._handler)
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self._logger.removeHandler(self._handler)
        if self._prev_level is not None:
            self._logger.setLevel(self._prev_level)
        if self._prev_propagate is not None:
            self._logger.propagate = self._prev_propagate

    def reset(self) -> None:
        """Drop any held record so the next read reflects the next call."""
        self._handler.record = None

    @property
    def record(self) -> dict | None:
        return self._handler.record

    def phase_fields(self) -> dict[str, object | None]:
        """Return the phase figures, every one None when nothing was captured."""
        payload = self.record or {}
        fields: dict[str, object | None] = {name: payload.get(name) for name in _PHASE_FIELDS}
        fields["reported_generated_tokens"] = payload.get("generated_tokens")
        return fields


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

    # Phase breakdown lifted from the provider's own latency record. None
    # throughout for a provider that publishes none, which is a different
    # reading from a measured zero and has to render as one.
    prefill_ms: float | None = None
    prefill_tps: float | None = None
    decode_ms: float | None = None
    decode_tps: float | None = None
    input_tokens: int | None = None
    retained_tokens: int | None = None
    prompt_tokens: int | None = None
    reported_generated_tokens: int | None = None


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

    # Run-level memory figures. The footprint is a peak across the run; the
    # total is the machine capacity it is read against.
    peak_rss_bytes: int = 0
    machine_total_bytes: int = 0

    # Window the run was conducted at. All three are reported because they
    # can disagree: a configured value above what the weights support is
    # clamped, and the effective figure is the only one the prompt was
    # actually fitted to.
    configured_context_window: int | None = None
    native_context_window: int | None = None
    effective_context_window: int | None = None

    context_probe: "ContextProbeOutcome | None" = None


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

    Exit is prompt. The poll loop waits on a stop event with the poll
    interval as a timeout rather than sleeping the interval out, so
    ``__aexit__`` returns as soon as it is entered instead of blocking
    for the remainder of a pending sleep. A sampler that drained by
    waiting out that sleep would add up to ``poll_interval_s`` of dead
    wall time to every call it wrapped.
    """

    def __init__(self, probe: MemoryProbe, poll_interval_s: float = 0.5) -> None:
        self._probe = probe
        self._poll_interval_s = poll_interval_s
        self._baseline: int = 0
        self._min_during: int = 0
        self._after: int = 0
        self._stop: asyncio.Event | None = None
        self._task: asyncio.Task | None = None

    async def __aenter__(self) -> "MemorySampler":
        self._baseline = self._probe()
        self._min_during = self._baseline
        self._stop = asyncio.Event()
        self._task = asyncio.create_task(self._poll_loop(self._stop))
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        if self._stop is not None:
            self._stop.set()
        if self._task is not None:
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self._after = self._probe()

    async def _poll_loop(self, stop: asyncio.Event) -> None:
        while True:
            try:
                await asyncio.wait_for(stop.wait(), timeout=self._poll_interval_s)
            except TimeoutError:
                pass  # Interval elapsed with no stop requested: take a sample.
            else:
                return  # Stop requested: exit without a further reading.
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
    with LatencyRecordCapture() as capture:
        capture.reset()
        async with sampler:
            # Time the provider call alone. The sampler's baseline and final
            # probe readings are measurement overhead, not provider latency,
            # and a probe can be arbitrarily slow -- the default one shells
            # out to a subprocess. Timing across the enclosing ``async with``
            # would fold that overhead into the reported latency.
            t0 = time.perf_counter()
            raw_output = await provider.generate_abstract(projection_text, max_tokens, doc_type)
            elapsed_ms = (time.perf_counter() - t0) * 1000.0
        phase = capture.phase_fields()

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
        **phase,
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
    rss_probe: MemoryProbe | None = None,
    total_memory_probe: MemoryProbe | None = None,
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
        mem_probe = DEFAULT_MEM_PROBE
    if rss_probe is None:
        rss_probe = _self_rss_bytes
    if total_memory_probe is None:
        from sage.utils.unified_memory import total_unified_memory_bytes

        total_memory_probe = total_unified_memory_bytes

    # Sampled once per document and kept as a running maximum. Resident size
    # falls back after a large allocation is released, so the last reading
    # would understate the footprint the machine actually had to hold.
    peak_rss = _probe_or_zero(rss_probe)

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
        peak_rss = max(peak_rss, _probe_or_zero(rss_probe))

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
        peak_rss_bytes=peak_rss,
        machine_total_bytes=_probe_or_zero(total_memory_probe),
    )


# ---------------------------------------------------------------------------
# Long-context prefill probe
# ---------------------------------------------------------------------------


# Characters of assembled text per token of target window. Real text runs
# closer to four characters per token; the margin is deliberate, because the
# probe is only meaningful when the window is the binding constraint. Supply
# too little text and a short verdict describes the corpus rather than the
# model.
_PROBE_CHARS_PER_TOKEN = 8

# Fraction of the target the retained count must reach to read as "ok". It is
# below one because the window also has to hold the generation budget and the
# chat template, so a prompt that filled the window still lands a few thousand
# tokens under it. The gap this must separate is not that rounding difference
# but a clamp to a smaller native window, which shows up as a multiple-fold
# shortfall rather than a percentage.
_PROBE_RETAINED_TOLERANCE = 0.95


@dataclass
class ContextProbeOutcome:
    """Result of exercising a provider at a target context length.

    ``verdict`` is one of:

    - ``ok``      -- the model's own prompt length reached the target.
    - ``short``   -- the call succeeded, but on far less input than asked
      for. The provider truncates to fit by design, so this is the ordinary
      way a long-context claim turns out to be false: nothing raises and the
      output looks entirely normal.
    - ``failed``  -- the call raised. The exception is preserved, since an
      allocation failure and a load failure lead to opposite recommendations.
    - ``unknown`` -- no latency record was published, so the prompt length was
      never observed. Distinct from ``short``, which asserts a truncation
      something actually measured.
    """

    verdict: str
    target_tokens: int
    retained_tokens: int | None = None
    prompt_tokens: int | None = None
    prefill_ms: float | None = None
    prefill_tps: float | None = None
    input_chars: int = 0
    error_type: str | None = None
    error_message: str | None = None


async def _assemble_probe_text(services, corpus: list[CatalogEntry], target_chars: int) -> str:
    """Concatenate corpus projections until *target_chars* is reached.

    Cycles back through the corpus when it is exhausted, so a vault whose
    documents are all short can still fill a long window.
    """
    if not corpus:
        return ""

    parts: list[str] = []
    total = 0
    index = 0
    while total < target_chars:
        entry = corpus[index % len(corpus)]
        chunks = await services.content_store.get_all_chunks(entry.doc_id)
        body = [c for c in chunks if c.heading_path != SYNTHETIC_HEADER_HEADING_PATH]
        text = "\n\n".join(c.content for c in body)
        index += 1
        if not text:
            # A corpus of entirely empty documents would otherwise spin here.
            if index >= len(corpus) and not parts:
                return ""
            continue
        parts.append(text)
        total += len(text)
    return "\n\n".join(parts)


async def probe_context_window(
    services,
    corpus: list[CatalogEntry],
    provider: AbstractionProvider,
    abstraction_config: AbstractionConfig,
    target_tokens: int,
    mem_probe: MemoryProbe | None = None,
    poll_interval_s: float = 0.5,
    doc_type: str | None = None,
) -> ContextProbeOutcome:
    """Exercise the provider at *target_tokens* and report what actually ran.

    The verdict is read from the prompt length the model reports, never from
    the call having returned without raising. Those come apart precisely where
    it matters: a provider that quietly truncates a long input generates a
    perfectly good abstract on a fraction of it, so success proves the call
    worked and says nothing about the window.
    """
    if mem_probe is None:
        from sage.utils.unified_memory import free_unified_memory_bytes

        mem_probe = free_unified_memory_bytes

    text = await _assemble_probe_text(
        services, corpus, target_chars=target_tokens * _PROBE_CHARS_PER_TOKEN
    )
    try:
        record = await measure_one(
            provider=provider,
            projection_text=text,
            doc_type=doc_type,
            abstraction_config=abstraction_config,
            mem_probe=mem_probe,
            poll_interval_s=poll_interval_s,
            doc_id="__context_probe__",
        )
    except Exception as exc:  # noqa: BLE001 -- the failure is the measurement
        return ContextProbeOutcome(
            verdict="failed",
            target_tokens=target_tokens,
            input_chars=len(text),
            error_type=type(exc).__name__,
            error_message=str(exc),
        )

    retained = record.retained_tokens
    if retained is None:
        verdict = "unknown"
    elif retained >= target_tokens * _PROBE_RETAINED_TOLERANCE:
        verdict = "ok"
    else:
        verdict = "short"

    return ContextProbeOutcome(
        verdict=verdict,
        target_tokens=target_tokens,
        retained_tokens=retained,
        prompt_tokens=record.prompt_tokens,
        prefill_ms=record.prefill_ms,
        prefill_tps=record.prefill_tps,
        input_chars=len(text),
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


_UNMEASURED = "_Not measured -- this provider publishes no phase breakdown._"


def _gib(value: int) -> str:
    return f"{value / 1024**3:.1f} GiB"


def _thousands(value: int | None) -> str:
    return "—" if value is None else f"{value:,}"


def _render_context_window(result: BenchmarkResult) -> list[str]:
    """Configured, native, and effective windows, which need not agree."""
    lines = ["## Context window", ""]
    if result.effective_context_window is None:
        lines.extend(["_Not recorded for this run._", ""])
        return lines
    lines.append("| Window | Tokens |")
    lines.append("|---|---|")
    lines.append(f"| configured | {_thousands(result.configured_context_window)} |")
    lines.append(
        f"| native (advertised by the weights) | {_thousands(result.native_context_window)} |"
    )
    lines.append(
        f"| effective (what prompts were fitted to) "
        f"| {_thousands(result.effective_context_window)} |"
    )
    lines.append("")
    return lines


def _render_phase_breakdown(result: BenchmarkResult) -> list[str]:
    """Prefill and decode, reported as durations and as rates.

    The rates carry the comparison across window sizes: prefill duration
    scales with how much there was to read, so only throughput distinguishes
    a model that slowed down from one that simply had more input.
    """
    lines = ["## Prefill vs decode", ""]

    prefill_ms = [m.prefill_ms for m in result.measurements if m.prefill_ms is not None]
    decode_ms = [m.decode_ms for m in result.measurements if m.decode_ms is not None]
    if not prefill_ms and not decode_ms:
        lines.extend([_UNMEASURED, ""])
        return lines

    prefill_tps = [m.prefill_tps for m in result.measurements if m.prefill_tps is not None]
    decode_tps = [m.decode_tps for m in result.measurements if m.decode_tps is not None]

    lines.append("| Phase | mean (ms) | median (ms) | p95 (ms) | mean rate (tokens/s) |")
    lines.append("|---|---|---|---|---|")
    for name, durations, rates in (
        ("prefill", prefill_ms, prefill_tps),
        ("decode", decode_ms, decode_tps),
    ):
        if not durations:
            lines.append(f"| {name} | — | — | — | — |")
            continue
        stats = aggregate_latency(durations)
        rate = f"{statistics.mean(rates):.1f}" if rates else "—"
        lines.append(
            f"| {name} | {stats['mean']:.1f} | {stats['median']:.1f} "
            f"| {stats['p95']:.1f} | {rate} |"
        )
    lines.append("")
    return lines


def _render_resident_memory(result: BenchmarkResult) -> list[str]:
    """Resident footprint against machine capacity.

    The per-call figure above measures transient pressure; this one measures
    what the machine had to hold for the whole run, which is the number the
    budget question turns on.
    """
    lines = ["## Resident memory", ""]
    if not result.peak_rss_bytes or not result.machine_total_bytes:
        lines.extend(["_Not recorded for this run._", ""])
        return lines

    headroom = result.machine_total_bytes - result.peak_rss_bytes
    share = result.peak_rss_bytes / result.machine_total_bytes * 100
    lines.append(f"- Peak resident: **{_gib(result.peak_rss_bytes)}**")
    lines.append(f"- Machine total: {_gib(result.machine_total_bytes)}")
    lines.append(f"- Share of total: {share:.1f}%")
    lines.append(f"- Headroom at peak: {_gib(headroom)}")
    lines.append("")
    return lines


def _render_context_probe(result: BenchmarkResult) -> list[str]:
    """The measured long-context result, verdict beside its evidence."""
    lines = ["## Long-context prefill probe", ""]
    probe = result.context_probe
    if probe is None:
        lines.extend(["_Not run._", ""])
        return lines

    lines.append(f"- Verdict: **{probe.verdict}**")
    lines.append(f"- Target window: {_thousands(probe.target_tokens)} tokens")
    lines.append(f"- Prompt actually retained: {_thousands(probe.retained_tokens)} tokens")
    lines.append(f"- Model prompt length: {_thousands(probe.prompt_tokens)} tokens")
    lines.append(f"- Assembled input: {_thousands(probe.input_chars)} chars")
    if probe.prefill_ms is not None:
        lines.append(f"- Prefill: {probe.prefill_ms:.1f} ms")
    if probe.prefill_tps is not None:
        lines.append(f"- Prefill rate: {probe.prefill_tps:.1f} tokens/s")
    if probe.error_type:
        lines.append(f"- Error: `{probe.error_type}` -- {probe.error_message}")
    lines.append("")
    lines.append(
        "A `short` verdict means the call succeeded on far less input than "
        "asked for: the provider fits the prompt to the window by design, so "
        "a truncated long-context run raises nothing and reads as ordinary."
    )
    lines.append("")
    return lines


def _render_runtime_pin() -> list[str]:
    """Author-filled: whether the runtime moved to support the candidate."""
    return [
        "## Runtime pin",
        "",
        "_To be filled from the load evidence:_",
        "",
        "- Runtime version exercised: ",
        "- Bump required: yes / no",
        "- Verification reach: the runtime is declared for Apple Silicon only, "
        "so CI never installs or exercises it. Any movement of this pin is "
        "verified by a local run alone.",
        "",
    ]


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

    lines.extend(_render_context_window(result))
    lines.extend(_render_phase_breakdown(result))
    lines.extend(_render_resident_memory(result))
    lines.extend(_render_context_probe(result))
    lines.extend(_render_runtime_pin())

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
