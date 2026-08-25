"""Tests for the abstraction-provider benchmark harness.

The harness exercises a candidate ``AbstractionProvider`` against a
stratified corpus and emits a per-candidate scorecard. The core is
provider-agnostic (tested here with stubs) and never invokes
``IngestionService.reabstract`` -- the benchmark is read-only against
any vault graph.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from unittest.mock import AsyncMock, MagicMock

import pytest

from sage.adapters.abstraction_utils import compute_max_tokens, trim_to_sentence_boundary
from sage.adapters.interfaces import SYNTHETIC_HEADER_HEADING_PATH, AbstractionProvider, Chunk
from sage.config import VaultAbstractionConfig as AbstractionConfig
from sage.utils import abstraction_benchmark, unified_memory
from sage.utils.abstraction_benchmark import (
    BenchmarkResult,
    CatalogEntry,
    ContextProbeOutcome,
    LatencyRecordCapture,
    MeasurementRecord,
    aggregate_latency,
    measure_one,
    measure_with_determinism_check,
    probe_context_window,
    render_outputs_for_blind_review,
    render_scorecard,
    run_benchmark,
    select_corpus,
)

# ---------------------------------------------------------------------------
# Test scaffolding
# ---------------------------------------------------------------------------


class RecordingProvider(AbstractionProvider):
    """Records the arguments of every generate_abstract call."""

    def __init__(self, output: str = "fixed stub abstract", sleep_s: float = 0.0) -> None:
        self.output = output
        self.sleep_s = sleep_s
        self.calls: list[dict] = []

    async def generate_abstract(self, text: str, max_tokens: int, doc_type: str | None) -> str:
        self.calls.append({"text": text, "max_tokens": max_tokens, "doc_type": doc_type})
        if self.sleep_s > 0:
            await asyncio.sleep(self.sleep_s)
        return self.output


class YieldingProvider(AbstractionProvider):
    """Yields control many times so the memory sampler has chances to poll."""

    def __init__(self, output: str, yield_count: int = 30, per_yield_s: float = 0.001) -> None:
        self.output = output
        self.yield_count = yield_count
        self.per_yield_s = per_yield_s

    async def generate_abstract(self, text: str, max_tokens: int, doc_type: str | None) -> str:
        for _ in range(self.yield_count):
            await asyncio.sleep(self.per_yield_s)
        return self.output


class FlakyProvider(AbstractionProvider):
    """Returns a different output on each call."""

    def __init__(self) -> None:
        self.call_count = 0

    async def generate_abstract(self, text: str, max_tokens: int, doc_type: str | None) -> str:
        self.call_count += 1
        return f"output run {self.call_count}."


class CountingProbe:
    """Memory probe that returns the next value in a fixed sequence.

    After the sequence is exhausted, returns the last value indefinitely.
    Records every call so tests can assert on call counts.
    """

    def __init__(self, sequence: list[int]) -> None:
        self.sequence = list(sequence)
        self.calls = 0

    def __call__(self) -> int:
        i = min(self.calls, len(self.sequence) - 1)
        self.calls += 1
        return self.sequence[i]


class SlowProbe:
    """Memory probe with a fixed per-call cost.

    Models the real probe, which shells out to a subprocess on every
    reading. Used to prove that probe cost does not land in the
    measured provider latency.
    """

    def __init__(self, value: int = 10_000, cost_s: float = 0.05) -> None:
        self.value = value
        self.cost_s = cost_s
        self.calls = 0

    def __call__(self) -> int:
        self.calls += 1
        time.sleep(self.cost_s)
        return self.value


def _make_entry(doc_id: str, doc_type: str, length: int) -> CatalogEntry:
    return CatalogEntry(doc_id=doc_id, doc_type=doc_type, length_bytes=length)


def _default_config() -> AbstractionConfig:
    return AbstractionConfig(
        enabled=True,
        max_abstract_tokens=1500,
        base_abstract_tokens=150,
        tokens_per_word=0.02,
    )


# ---------------------------------------------------------------------------
# 1-3. Corpus selection
# ---------------------------------------------------------------------------


def _balanced_catalog(per_cell: int = 4) -> list[CatalogEntry]:
    """Build a catalog with 6 doc_types x 3 length tiers x per_cell docs."""
    doc_types = [
        "adr",
        "ticket",
        "failure_record",
        "tooling_entry",
        "steering_document",
        "reference_document",
    ]
    # Lengths chosen so terciles bucket cleanly: 0-99 short, 100-999 medium, 1000+ long.
    length_buckets = [50, 500, 5000]
    catalog: list[CatalogEntry] = []
    for dt in doc_types:
        for length in length_buckets:
            for i in range(per_cell):
                catalog.append(_make_entry(f"{dt}-{length}-{i}", dt, length + i))
    return catalog


def test_select_corpus_stratifies_by_doc_type_and_length():
    catalog = _balanced_catalog(per_cell=4)  # 72 entries across 18 cells
    selected = select_corpus(catalog, target=20, seed=1)

    assert len(selected) == 20
    # Every (doc_type, length_tercile) cell with at least one source must be
    # represented at least once. With 18 cells and target 20, two cells will
    # be sampled twice; none should be empty.
    cells_seen: set[tuple[str, int]] = set()
    for entry in selected:
        bucket = (entry.doc_type, _tercile(entry.length_bytes, catalog))
        cells_seen.add(bucket)
    # All 18 cells should be hit. The "return catalog[:20]" implementation
    # only hits cells of the first few doc_types, so it would fail this.
    assert len(cells_seen) >= 18

    # No duplicate doc_id.
    assert len({e.doc_id for e in selected}) == 20


def _tercile(length: int, catalog: list[CatalogEntry]) -> int:
    """Mirror of the production tercile boundary computation (for assertions)."""
    sorted_lengths = sorted(e.length_bytes for e in catalog)
    n = len(sorted_lengths)
    low = sorted_lengths[n // 3]
    high = sorted_lengths[(2 * n) // 3]
    if length < low:
        return 0
    if length < high:
        return 1
    return 2


def test_select_corpus_handles_sparse_doc_types():
    catalog = _balanced_catalog(per_cell=4)
    # Strip one doc_type down to a single doc.
    catalog = [e for e in catalog if e.doc_type != "reference_document"]
    catalog.append(_make_entry("reference_document-only", "reference_document", 500))

    selected = select_corpus(catalog, target=20, seed=1)
    assert len(selected) == 20
    assert any(e.doc_id == "reference_document-only" for e in selected)


def test_select_corpus_is_deterministic_for_a_given_seed():
    catalog = _balanced_catalog(per_cell=4)
    a = select_corpus(catalog, target=20, seed=42)
    b = select_corpus(catalog, target=20, seed=42)
    assert [e.doc_id for e in a] == [e.doc_id for e in b]


# ---------------------------------------------------------------------------
# 4-7. Measurement plumbing
# ---------------------------------------------------------------------------


async def test_measure_one_records_wall_clock_tokens_memory_and_output():
    provider = RecordingProvider(output="fixed stub abstract.", sleep_s=0.05)
    probe = CountingProbe([10_000, 9_000, 9_000, 9_000, 9_000])
    config = _default_config()

    record = await measure_one(
        provider=provider,
        projection_text="alpha beta gamma delta epsilon",
        doc_type="adr",
        abstraction_config=config,
        mem_probe=probe,
        poll_interval_s=0.005,
    )

    assert isinstance(record, MeasurementRecord)
    assert record.wall_clock_ms >= 50
    assert record.tokens_generated > 0
    # "fixed stub abstract." has a complete sentence boundary; trim is a no-op.
    assert record.output_text == "fixed stub abstract."
    assert isinstance(record.memory_delta_bytes, int)


async def test_measure_one_replicates_ingestion_max_tokens_formula():
    text = " ".join(["word"] * 250)  # word_count = 250
    config = AbstractionConfig(
        enabled=True,
        model="test-model",
        max_abstract_tokens=1500,
        base_abstract_tokens=150,
        tokens_per_word=0.02,
    )
    expected = compute_max_tokens(250, config)

    provider = RecordingProvider(output="canned output.")
    probe = CountingProbe([0, 0])

    await measure_one(
        provider=provider,
        projection_text=text,
        doc_type="adr",
        abstraction_config=config,
        mem_probe=probe,
        poll_interval_s=0.005,
    )

    assert len(provider.calls) == 1
    assert provider.calls[0]["max_tokens"] == expected
    assert provider.calls[0]["doc_type"] == "adr"


async def test_measure_one_applies_trim_to_sentence_boundary():
    raw = "First sentence. Second sentence. Third partial"
    expected = trim_to_sentence_boundary(raw)
    assert expected.endswith("Second sentence.")  # sanity on the helper

    provider = RecordingProvider(output=raw)
    probe = CountingProbe([0, 0])

    record = await measure_one(
        provider=provider,
        projection_text="ignored",
        doc_type=None,
        abstraction_config=_default_config(),
        mem_probe=probe,
        poll_interval_s=0.005,
    )

    assert record.output_text == expected


async def test_memory_sampler_reports_minimum_free_during_call():
    # Probe values: baseline 10_000, then mid-call 9_000, 7_000, 8_000, 9_500.
    # Min free seen during call = 7_000, so peak_used = 10_000 - 7_000 = 3_000.
    # A before/after-only sampler would compute 10_000 - 9_500 = 500 and fail.
    probe = CountingProbe([10_000, 9_000, 7_000, 8_000, 9_500])
    provider = YieldingProvider(output="canned output.", yield_count=20, per_yield_s=0.001)

    record = await measure_one(
        provider=provider,
        projection_text="ignored",
        doc_type=None,
        abstraction_config=_default_config(),
        mem_probe=probe,
        poll_interval_s=0.0005,
    )

    assert record.peak_used_bytes_during_call == 3_000
    assert probe.calls >= 4  # baseline + at least 3 mid-call samples


# ---------------------------------------------------------------------------
# 7b. Default memory probe and sampler exit cost
# ---------------------------------------------------------------------------


def test_default_mem_probe_is_the_unified_memory_helper():
    """The named default resolves to the real unified-memory helper.

    Asserted by identity rather than by calling it, so the wiring is
    guarded on every platform: free_unified_memory_bytes shells out to
    macOS vm_stat and cannot run on a Linux CI box. A rename or move of
    the helper reds this test everywhere. The helper's own behaviour is
    covered in test_unified_memory.py.
    """
    assert abstraction_benchmark.DEFAULT_MEM_PROBE is unified_memory.free_unified_memory_bytes


async def test_run_benchmark_defaults_to_the_module_probe(monkeypatch):
    """Omitting mem_probe resolves DEFAULT_MEM_PROBE and actually uses it.

    The production caller never passes a probe, so the defaulting branch
    is the only one that runs in anger. Patching the module-level default
    substitutes it without reaching into the sampler's per-call internals.
    """
    chunks_by_doc = {
        "doc-0": [
            Chunk(
                document_id="doc-0",
                heading_path="Body",
                content="body of doc 0.",
                chunk_index=0,
            ),
        ]
    }
    services = MagicMock()
    services.content_store.get_all_chunks = AsyncMock(
        side_effect=lambda doc_id: chunks_by_doc[doc_id]
    )

    probe = CountingProbe([10_000, 9_000])
    monkeypatch.setattr(abstraction_benchmark, "DEFAULT_MEM_PROBE", probe)

    result = await run_benchmark(
        services=services,
        corpus=[_make_entry("doc-0", "adr", 100)],
        provider=RecordingProvider(output="abstract."),
        abstraction_config=_default_config(),
        repeats=1,
        poll_interval_s=0.005,
        warmup_calls=0,
    )

    # The defaulted probe reached the sampler: baseline on enter plus a
    # final reading on exit. A run that left mem_probe unresolved would
    # raise TypeError on the first probe call; one that resolved some
    # other probe would leave this counter at zero.
    assert probe.calls >= 2
    assert len(result.measurements) == 1


async def test_sampler_exit_does_not_block_the_caller():
    """Shutdown does not wait out the pending poll interval.

    The poll interval here is 20x the provider's work, so a sampler that
    drained by sleeping the interval out would hold ``measure_one`` for
    ~200 ms rather than ~10 ms. This asserts on the wall time of the
    call itself, not on the record: with the timing window narrowed to
    the provider call, a slow drain no longer shows up in
    ``wall_clock_ms`` at all, so the record cannot witness this defect.
    """
    provider = RecordingProvider(output="abstract.", sleep_s=0.01)
    probe = CountingProbe([10_000, 10_000])

    t0 = time.perf_counter()
    record = await measure_one(
        provider=provider,
        projection_text="ignored",
        doc_type=None,
        abstraction_config=_default_config(),
        mem_probe=probe,
        poll_interval_s=0.2,
    )
    total_ms = (time.perf_counter() - t0) * 1000.0

    assert total_ms < 100
    assert record.wall_clock_ms >= 10  # the provider's own work still counts


async def test_probe_cost_stays_out_of_the_measured_latency():
    """Wall-clock covers the provider call, not the sampler's readings.

    The probe costs 50 ms per reading and is called twice per
    measurement -- baseline on enter, final on exit -- against a
    provider doing 10 ms of work. Timing across the enclosing
    ``async with`` would report ~110 ms instead of ~10 ms, an 11x
    overstatement. This matters because the default probe really is
    this expensive: it spawns a subprocess. The upper bound is the
    complement of the lower bound asserted in
    test_measure_one_records_wall_clock_tokens_memory_and_output.
    """
    provider = RecordingProvider(output="abstract.", sleep_s=0.01)
    probe = SlowProbe(value=10_000, cost_s=0.05)

    record = await measure_one(
        provider=provider,
        projection_text="ignored",
        doc_type=None,
        abstraction_config=_default_config(),
        mem_probe=probe,
        # Longer than the provider call, so the only probe readings are
        # the enter/exit pair and the cost is exactly 2 x cost_s.
        poll_interval_s=0.2,
    )

    assert probe.calls == 2
    assert record.wall_clock_ms >= 10  # the provider's own work is counted
    assert record.wall_clock_ms < 60  # ... and neither probe reading is


# ---------------------------------------------------------------------------
# 8. Vault safety
# ---------------------------------------------------------------------------


async def test_harness_does_not_invoke_ingestion_reabstract():
    chunks_by_doc = {
        f"doc-{i}": [
            Chunk(
                document_id=f"doc-{i}",
                heading_path=SYNTHETIC_HEADER_HEADING_PATH,
                content="header chunk that should be filtered out",
                chunk_index=0,
            ),
            Chunk(
                document_id=f"doc-{i}",
                heading_path="Body",
                content=f"body of doc {i}.",
                chunk_index=1,
            ),
        ]
        for i in range(3)
    }

    services = MagicMock()
    services.content_store.get_all_chunks = AsyncMock(
        side_effect=lambda doc_id: chunks_by_doc[doc_id]
    )
    services.ingestion_service.reabstract = AsyncMock()
    services.ingestion_service._reabstract_background = AsyncMock()

    corpus = [_make_entry(f"doc-{i}", "adr", 100 + i) for i in range(3)]
    provider = RecordingProvider(output="abstract.")
    probe = CountingProbe([0, 0])

    result = await run_benchmark(
        services=services,
        corpus=corpus,
        provider=provider,
        abstraction_config=_default_config(),
        repeats=1,
        mem_probe=probe,
        poll_interval_s=0.005,
    )

    # Production reabstract path must NOT be touched.
    services.ingestion_service.reabstract.assert_not_called()
    services.ingestion_service._reabstract_background.assert_not_called()

    # Synthetic-header chunks must be filtered out before joining.
    for call in provider.calls:
        assert "header chunk that should be filtered out" not in call["text"]

    # Result captures one record per doc.
    assert isinstance(result, BenchmarkResult)
    assert len(result.measurements) == 3


# ---------------------------------------------------------------------------
# 8b. Warmup discipline (framework §3.2)
# ---------------------------------------------------------------------------


async def test_warmup_calls_are_excluded_from_measurements():
    chunks_by_doc = {
        f"doc-{i}": [
            Chunk(
                document_id=f"doc-{i}",
                heading_path="Body",
                content=f"body of doc {i}.",
                chunk_index=0,
            ),
        ]
        for i in range(3)
    }
    services = MagicMock()
    services.content_store.get_all_chunks = AsyncMock(
        side_effect=lambda doc_id: chunks_by_doc[doc_id]
    )

    corpus = [_make_entry(f"doc-{i}", "adr", 100 + i) for i in range(3)]
    provider = RecordingProvider(output="abstract.")
    probe = CountingProbe([0, 0])

    result = await run_benchmark(
        services=services,
        corpus=corpus,
        provider=provider,
        abstraction_config=_default_config(),
        repeats=1,
        mem_probe=probe,
        poll_interval_s=0.005,
        warmup_calls=2,
    )

    # The provider is called once per warmup PLUS once per (doc × repeat).
    # With 3 docs × 1 repeat + 2 warmups, that's 5 total calls.
    assert len(provider.calls) == 5
    # Only the measured calls land in the result; warmup outputs are discarded.
    assert len(result.measurements) == 3
    # Per-doc determinism map matches the corpus, not the warmup.
    assert set(result.determinism_verdicts.keys()) == {"doc-0", "doc-1", "doc-2"}


# ---------------------------------------------------------------------------
# 9. Statistics
# ---------------------------------------------------------------------------


def test_aggregate_latency_reports_mean_median_p95_p99():
    # 19 records in 10_000-15_000 ms + one outlier at 60_000.
    latencies_ms = [10_000 + 250 * i for i in range(19)] + [60_000]
    stats = aggregate_latency(latencies_ms)

    assert 11_000 <= stats["median"] <= 14_000
    # p99 is pulled toward the outlier by statistics.quantiles' inclusive
    # interpolation. The exact value depends on method, but it must be
    # well above the median band (mean-only mutation produces ~12_375;
    # the real p99 sits >> 30_000).
    assert stats["p99"] > 30_000
    assert stats["max"] == 60_000
    assert stats["median"] < stats["p95"] <= 60_000
    assert stats["mean"] > stats["median"]  # outlier pulls mean up
    assert stats["min"] == 10_000


# ---------------------------------------------------------------------------
# 10-11. Scorecard rendering
# ---------------------------------------------------------------------------


def _sample_result(model_id: str = "qwen3-8b") -> BenchmarkResult:
    measurements = [
        MeasurementRecord(
            doc_id=f"doc-{i}",
            doc_type="adr",
            word_count=200,
            max_tokens=154,
            wall_clock_ms=12_000 + 100 * i,
            tokens_generated=80,
            output_text=f"Abstract for doc {i}.",
            memory_delta_bytes=1_000_000,
            peak_used_bytes_during_call=2_500_000,
        )
        for i in range(5)
    ]
    return BenchmarkResult(
        candidate_model_id=model_id,
        corpus_size=5,
        repeats=2,
        measurements=measurements,
        determinism_verdicts={m.doc_id: "identical" for m in measurements},
        alt_outputs={m.doc_id: [m.output_text] for m in measurements},
        latency_stats=aggregate_latency([m.wall_clock_ms for m in measurements]),
        memory_stats=aggregate_latency([m.peak_used_bytes_during_call for m in measurements]),
        started_at="2026-05-19T00:00:00Z",
        finished_at="2026-05-19T00:10:00Z",
    )


def test_scorecard_includes_all_seven_decision_criteria_sections():
    result = _sample_result()
    md = render_scorecard(result)

    expected_headings = [
        "## Candidate",
        "## Latency",
        "## Memory footprint",
        "## Determinism",
        "## Prompt verdict",
        "## Decision criteria (§7)",
        "## Blind review",
    ]
    for heading in expected_headings:
        assert heading in md, f"missing heading: {heading}"

    # Decision-criteria block has five enumerated checkboxes.
    decision_section_start = md.index("## Decision criteria (§7)")
    decision_section = md[decision_section_start:]
    # Each criterion is on its own line with a checkbox.
    checkbox_lines = [
        line for line in decision_section.splitlines() if line.startswith(("- [ ]", "- [x]"))
    ]
    assert len(checkbox_lines) >= 5


def test_outputs_renderer_masks_provider_identity_in_blind_review_block():
    result = _sample_result(model_id="mlx-community/Qwen3-8B-Instruct-2507-4bit")
    # Baseline text is intentionally neutral — the renderer's job is to
    # avoid injecting model identifiers, not to censor the abstract text
    # itself.
    baseline_outputs = {
        f"doc-{i}": f"This document specifies the canonical baseline approach for item {i}."
        for i in range(5)
    }
    md = render_outputs_for_blind_review(result, baseline_outputs=baseline_outputs)

    # Split body from reveal.
    if "## Reveal" in md:
        body, reveal = md.split("## Reveal", 1)
    else:
        pytest.fail("renderer must emit a '## Reveal' section")

    # Body must NOT name the providers.
    forbidden_in_body = ["Qwen3-8B", "Qwen3-30B", "30B", "8B", "qwen3-8b", "qwen3-30b"]
    for needle in forbidden_in_body:
        assert needle not in body, f"body must not contain {needle!r}"

    # Body must use masked labels.
    assert "Card A" in body
    assert "Card B" in body

    # Reveal must map labels to actual provider ids.
    assert "Card A" in reveal
    assert "Card B" in reveal
    assert ("Qwen3-8B" in reveal) or ("qwen3-8b" in reveal.lower())


# ---------------------------------------------------------------------------
# 12. Determinism
# ---------------------------------------------------------------------------


async def test_determinism_check_detects_drift_and_identity():
    config = _default_config()
    probe = CountingProbe([0, 0])

    # Case A: flaky provider → drift.
    flaky = FlakyProvider()
    record_a, verdict_a, alts_a = await measure_with_determinism_check(
        provider=flaky,
        projection_text="some text.",
        doc_type="adr",
        abstraction_config=config,
        mem_probe=probe,
        poll_interval_s=0.005,
        repeats=2,
    )
    assert verdict_a == "drift"
    assert len(alts_a) == 2
    assert alts_a[0] != alts_a[1]

    # Case B: deterministic provider → identical.
    deterministic = RecordingProvider(output="same output every time.")
    record_b, verdict_b, alts_b = await measure_with_determinism_check(
        provider=deterministic,
        projection_text="some text.",
        doc_type="adr",
        abstraction_config=config,
        mem_probe=probe,
        poll_interval_s=0.005,
        repeats=2,
    )
    assert verdict_b == "identical"
    assert len(alts_b) == 2
    assert alts_b[0] == alts_b[1]


# ---------------------------------------------------------------------------
# Latency-record capture
# ---------------------------------------------------------------------------


TIMING_LOGGER_NAME = "sage.abstraction.timing"


class TimingEmittingProvider(AbstractionProvider):
    """Emits a structured latency record the way the local MLX provider does.

    One JSON payload per call on the timing logger, published synchronously
    before the call returns. ``payloads`` supplies a distinct record per call
    so a test can tell which call a captured value came from.
    """

    def __init__(self, *payloads: dict, output: str = "stub abstract.") -> None:
        self.payloads = list(payloads)
        self.output = output
        self.call_count = 0
        self.calls: list[dict] = []

    async def generate_abstract(self, text: str, max_tokens: int, doc_type: str | None) -> str:
        self.calls.append({"text": text, "max_tokens": max_tokens, "doc_type": doc_type})
        i = min(self.call_count, len(self.payloads) - 1)
        self.call_count += 1
        logging.getLogger(TIMING_LOGGER_NAME).info(json.dumps(self.payloads[i]))
        return self.output


def _timing_payload(**overrides) -> dict:
    payload = {
        "layer": "abstraction",
        "label": "abstract.mlx",
        "model": "stub-model",
        "document_chars": 91234,
        "input_tokens": 40000,
        "retained_tokens": 31000,
        "prompt_tokens": 31120,
        "generated_tokens": 200,
        "prefill_ms": 1234.5,
        "prefill_tps": 500.0,
        "decode_ms": 6789.0,
        "decode_tps": 25.0,
    }
    payload.update(overrides)
    return payload


async def _measure(provider, text: str = "word " * 400, doc_type: str | None = "adr"):
    return await measure_one(
        provider=provider,
        projection_text=text,
        doc_type=doc_type,
        abstraction_config=_default_config(),
        mem_probe=CountingProbe([1000, 1000]),
        poll_interval_s=0.01,
        doc_id="doc-1",
    )


def test_capture_restores_logger_state_on_exit():
    """Handlers, level, and propagation are left exactly as they were found.

    The capture attaches to a logger the rest of the process shares, so a
    handler or a level left behind would silently alter logging for every
    later call in the same run.
    """
    logger = logging.getLogger(TIMING_LOGGER_NAME)
    before = (list(logger.handlers), logger.level, logger.propagate)

    with LatencyRecordCapture():
        pass

    assert (list(logger.handlers), logger.level, logger.propagate) == before


def test_capture_restores_logger_state_on_exception():
    """The restore survives an exception raised inside the block."""
    logger = logging.getLogger(TIMING_LOGGER_NAME)
    before = (list(logger.handlers), logger.level, logger.propagate)

    with pytest.raises(ValueError):
        with LatencyRecordCapture():
            raise ValueError("boom")

    assert (list(logger.handlers), logger.level, logger.propagate) == before


def test_capture_reads_records_the_emitter_logs_at_info():
    """The record is captured even when nothing has configured the logger.

    The provider publishes at INFO and the harness runs with no logging setup,
    so a capture that only attached a handler would see nothing: the default
    effective level discards the record before any handler runs.
    """
    logging.getLogger(TIMING_LOGGER_NAME).setLevel(logging.NOTSET)

    with LatencyRecordCapture() as capture:
        logging.getLogger(TIMING_LOGGER_NAME).info(json.dumps(_timing_payload()))
        assert capture.record is not None
        assert capture.record["prefill_ms"] == 1234.5


@pytest.mark.asyncio
async def test_measure_one_captures_prefill_and_decode_from_timing_record():
    """Phase figures on the record are the ones the provider published.

    Every value is distinctive, so a measurement that filled these in from
    defaults, from zeros, or from the wall-clock it already had cannot
    reproduce them.
    """
    provider = TimingEmittingProvider(_timing_payload())

    record = await _measure(provider)

    assert record.prefill_ms == 1234.5
    assert record.prefill_tps == 500.0
    assert record.decode_ms == 6789.0
    assert record.decode_tps == 25.0
    assert record.input_tokens == 40000
    assert record.retained_tokens == 31000
    assert record.prompt_tokens == 31120
    assert record.reported_generated_tokens == 200


@pytest.mark.asyncio
async def test_measure_one_reads_post_rename_field_names():
    """The capture is pinned to the current record contract, not its ancestor.

    A payload carrying only the superseded ``tokens_per_second`` name leaves
    the decode rate unmeasured rather than silently populated, so a reader
    that has fallen behind a rename shows up as an absent column instead of a
    plausible-looking number.
    """
    payload = _timing_payload()
    payload["tokens_per_second"] = payload.pop("decode_tps")
    payload["input_chars"] = payload.pop("document_chars")
    provider = TimingEmittingProvider(payload)

    record = await _measure(provider)

    assert record.decode_tps is None
    assert record.prefill_tps == 500.0


@pytest.mark.asyncio
async def test_measure_one_leaves_phase_fields_none_when_no_record_emitted():
    """A provider that publishes no record measures as unmeasured, not zero.

    Hosted providers share no vocabulary below wall-clock time, so absence is
    the honest reading -- and it has to stay distinguishable from a real zero.
    """
    record = await _measure(RecordingProvider(output="stub abstract."))

    assert record.prefill_ms is None
    assert record.decode_tps is None
    assert record.retained_tokens is None
    assert record.wall_clock_ms > 0
    assert record.output_text == "stub abstract."


@pytest.mark.asyncio
async def test_capture_attributes_one_record_per_call():
    """Each measured call carries its own call's figures.

    A capture that accumulated across calls would pass a single-call test and
    then attribute the previous document's prefill to every later one, which
    no aggregate over the run would reveal.
    """
    provider = TimingEmittingProvider(
        _timing_payload(prefill_ms=100.0, retained_tokens=1000),
        _timing_payload(prefill_ms=200.0, retained_tokens=2000),
    )

    first = await _measure(provider)
    second = await _measure(provider)

    assert (first.prefill_ms, first.retained_tokens) == (100.0, 1000)
    assert (second.prefill_ms, second.retained_tokens) == (200.0, 2000)


# ---------------------------------------------------------------------------
# Resident memory
# ---------------------------------------------------------------------------


def _single_chunk_services(doc_count: int):
    chunks_by_doc = {
        f"doc-{i}": [
            Chunk(
                document_id=f"doc-{i}",
                heading_path="Body",
                content=f"body of doc {i}.",
                chunk_index=0,
            ),
        ]
        for i in range(doc_count)
    }
    services = MagicMock()
    services.content_store.get_all_chunks = AsyncMock(
        side_effect=lambda doc_id: chunks_by_doc[doc_id]
    )
    return services


@pytest.mark.asyncio
async def test_benchmark_result_records_peak_rss_and_machine_total():
    """The run reports a resident footprint and the capacity to read it against.

    A footprint without its denominator cannot answer the budget question the
    scorecard exists to settle, so both travel together on the result.
    """
    result = await run_benchmark(
        services=_single_chunk_services(2),
        corpus=[_make_entry(f"doc-{i}", "adr", 100 + i) for i in range(2)],
        provider=RecordingProvider(output="abstract."),
        abstraction_config=_default_config(),
        repeats=1,
        mem_probe=CountingProbe([0, 0]),
        poll_interval_s=0.005,
        warmup_calls=0,
        rss_probe=CountingProbe([5_000, 7_000]),
        total_memory_probe=lambda: 68_719_476_736,
    )

    assert result.peak_rss_bytes > 0
    assert result.machine_total_bytes == 68_719_476_736


@pytest.mark.asyncio
async def test_peak_rss_is_monotonic_across_the_run():
    """The reported figure is the run's maximum, not its final reading.

    The probe rises and then falls, so an implementation that keeps the last
    sample reports 6_000 and an implementation that keeps the first reports
    1_000; only a running maximum yields the peak. Resident memory does fall
    back after a large allocation is released, which is exactly why the last
    reading is the wrong one to publish.
    """
    result = await run_benchmark(
        services=_single_chunk_services(4),
        corpus=[_make_entry(f"doc-{i}", "adr", 100 + i) for i in range(4)],
        provider=RecordingProvider(output="abstract."),
        abstraction_config=_default_config(),
        repeats=1,
        mem_probe=CountingProbe([0, 0]),
        poll_interval_s=0.005,
        warmup_calls=0,
        rss_probe=CountingProbe([1_000, 4_000, 9_000, 6_000]),
        total_memory_probe=lambda: 68_719_476_736,
    )

    assert result.peak_rss_bytes == 9_000


# ---------------------------------------------------------------------------
# Long-context prefill probe
# ---------------------------------------------------------------------------


class RaisingProvider(AbstractionProvider):
    """Raises the way a Metal allocation failure surfaces during prefill."""

    def __init__(self, exc: BaseException) -> None:
        self.exc = exc

    async def generate_abstract(self, text: str, max_tokens: int, doc_type: str | None) -> str:
        raise self.exc


def _bulk_services(doc_count: int, chunk_chars: int):
    chunks_by_doc = {
        f"doc-{i}": [
            Chunk(
                document_id=f"doc-{i}",
                heading_path="Body",
                content="word " * (chunk_chars // 5),
                chunk_index=0,
            ),
        ]
        for i in range(doc_count)
    }
    services = MagicMock()
    services.content_store.get_all_chunks = AsyncMock(
        side_effect=lambda doc_id: chunks_by_doc[doc_id]
    )
    return services


async def _probe(provider, target_tokens: int = 131072, doc_count: int = 4):
    return await probe_context_window(
        services=_bulk_services(doc_count, chunk_chars=200_000),
        corpus=[_make_entry(f"doc-{i}", "adr", 200_000) for i in range(doc_count)],
        provider=provider,
        abstraction_config=_default_config(),
        target_tokens=target_tokens,
        mem_probe=CountingProbe([0, 0]),
        poll_interval_s=0.005,
    )


@pytest.mark.asyncio
async def test_context_probe_reports_ok_when_target_tokens_are_actually_retained():
    """A prefill that genuinely reached the window is the only 'ok'.

    The retained count sits just under the target because the window also has
    to hold the generation budget and the chat template; that shortfall is a
    rounding difference, not a clamp.
    """
    provider = TimingEmittingProvider(
        _timing_payload(retained_tokens=129_472, prefill_ms=48_000.0, prefill_tps=2_697.0)
    )

    outcome = await _probe(provider, target_tokens=131_072)

    assert outcome.verdict == "ok"
    assert outcome.retained_tokens == 129_472
    assert outcome.prefill_ms == 48_000.0
    assert outcome.prefill_tps == 2_697.0


@pytest.mark.asyncio
async def test_context_probe_reports_short_when_input_was_silently_truncated():
    """Truncation to a smaller window is a finding, not a success.

    The provider truncates to fit by design and then generates perfectly well,
    so nothing raises and the output looks ordinary. Reading the verdict off
    the absence of an exception would report a 128K prefill that never ran --
    the exact claim the scorecard would then carry into an adopt decision.
    """
    provider = TimingEmittingProvider(_timing_payload(retained_tokens=31_000))

    outcome = await _probe(provider, target_tokens=131_072)

    assert outcome.verdict == "short"
    assert outcome.retained_tokens == 31_000
    assert outcome.target_tokens == 131_072


@pytest.mark.asyncio
async def test_context_probe_reports_failed_and_preserves_the_exception():
    """A Metal allocation failure is captured with enough detail to act on.

    A bare verdict cannot distinguish an out-of-memory prefill from a model
    that failed to load, and those lead to opposite recommendations.
    """
    provider = RaisingProvider(RuntimeError("[metal::malloc] Insufficient Memory"))

    outcome = await _probe(provider, target_tokens=131_072)

    assert outcome.verdict == "failed"
    assert outcome.error_type == "RuntimeError"
    assert "Insufficient Memory" in outcome.error_message


@pytest.mark.asyncio
async def test_context_probe_reports_unknown_when_no_record_was_published():
    """No published record means the window was not measured either way.

    Reporting 'short' here would assert a truncation nothing observed.
    """
    outcome = await _probe(RecordingProvider(output="abstract."), target_tokens=131_072)

    assert outcome.verdict == "unknown"
    assert outcome.retained_tokens is None


@pytest.mark.asyncio
async def test_context_probe_assembles_input_to_the_target_window():
    """The probe supplies enough text that the window is what limits the prompt.

    If the assembled input were the shorter side, a 'short' verdict would
    describe the corpus rather than the model, and the probe would measure
    nothing about the context window at all.
    """
    provider = TimingEmittingProvider(_timing_payload(retained_tokens=129_472))

    await _probe(provider, target_tokens=131_072)

    assert len(provider.calls) == 1
    # One token never spans more than a handful of characters, so text this
    # long cannot fail to reach the target on token count.
    assert len(provider.calls[0]["text"]) >= 131_072 * 4


# ---------------------------------------------------------------------------
# Scorecard sections for the long-context evaluation
# ---------------------------------------------------------------------------


def _sample_result_with_phases(model_id: str = "qwen3.5-9b") -> BenchmarkResult:
    measurements = [
        MeasurementRecord(
            doc_id=f"doc-{i}",
            doc_type="adr",
            word_count=200,
            max_tokens=154,
            wall_clock_ms=12_000 + 100 * i,
            tokens_generated=80,
            output_text=f"Abstract for doc {i}.",
            memory_delta_bytes=1_000_000,
            peak_used_bytes_during_call=2_500_000,
            prefill_ms=800.0 + 10 * i,
            prefill_tps=2_500.0,
            decode_ms=11_000.0,
            decode_tps=24.0,
            input_tokens=5_000,
            retained_tokens=5_000,
            prompt_tokens=5_120,
            reported_generated_tokens=80,
        )
        for i in range(5)
    ]
    return BenchmarkResult(
        candidate_model_id=model_id,
        corpus_size=5,
        repeats=2,
        measurements=measurements,
        determinism_verdicts={m.doc_id: "identical" for m in measurements},
        alt_outputs={m.doc_id: [m.output_text] for m in measurements},
        latency_stats=aggregate_latency([m.wall_clock_ms for m in measurements]),
        memory_stats=aggregate_latency([m.peak_used_bytes_during_call for m in measurements]),
        started_at="2026-08-25T00:00:00Z",
        finished_at="2026-08-25T00:10:00Z",
        peak_rss_bytes=14_000_000_000,
        machine_total_bytes=68_719_476_736,
        configured_context_window=131_072,
        native_context_window=262_144,
        effective_context_window=131_072,
        context_probe=ContextProbeOutcome(
            verdict="ok",
            target_tokens=131_072,
            retained_tokens=129_472,
            prefill_ms=48_000.0,
            prefill_tps=2_697.0,
            input_chars=1_048_576,
        ),
    )


def test_scorecard_includes_context_window_prefill_decode_and_resident_sections():
    """Every criterion the evaluation has to answer gets its own section."""
    md = render_scorecard(_sample_result_with_phases())

    for heading in (
        "## Context window",
        "## Prefill vs decode",
        "## Resident memory",
        "## Long-context prefill probe",
        "## Runtime pin",
    ):
        assert heading in md, f"missing heading: {heading}"


def test_scorecard_reports_prefill_throughput_alongside_duration():
    """Throughput is what makes two different window sizes comparable.

    Prefill duration at 128K is an order of magnitude above 32K by definition;
    only the rate says whether the model degraded or simply had more to read.
    """
    md = render_scorecard(_sample_result_with_phases())

    prefill_section = md[md.index("## Prefill vs decode") : md.index("## Resident memory")]
    assert "tokens/s" in prefill_section
    assert "2500" in prefill_section.replace(",", "").replace(".0", "")


def test_scorecard_reports_resident_footprint_against_machine_total():
    """The footprint is rendered with its denominator, not on its own."""
    md = render_scorecard(_sample_result_with_phases())

    resident = md[md.index("## Resident memory") : md.index("## Long-context prefill probe")]
    assert "14.0 GiB" in resident or "13.0 GiB" in resident
    assert "64.0 GiB" in resident


def test_scorecard_records_the_probe_verdict_and_the_tokens_behind_it():
    """The verdict travels with the retained count that produced it.

    A bare 'ok' is unauditable; the reader has to be able to see that the
    prompt actually reached the window.
    """
    md = render_scorecard(_sample_result_with_phases())

    probe = md[md.index("## Long-context prefill probe") : md.index("## Runtime pin")]
    assert "ok" in probe
    assert "129,472" in probe or "129472" in probe
    assert "131,072" in probe or "131072" in probe


def test_scorecard_renders_unmeasured_phase_fields_as_absent_not_zero():
    """A provider that published no phases renders as unmeasured, never as zero.

    A 0.0 ms prefill row would read as an instantaneous prefill -- a specific
    and very wrong claim -- where the truth is that nothing was measured.
    """
    md = render_scorecard(_sample_result())

    prefill_section = md[md.index("## Prefill vs decode") : md.index("## Resident memory")]
    assert "not measured" in prefill_section.lower()
    assert "0.0" not in prefill_section
