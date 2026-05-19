"""Tests for the abstraction-provider benchmark harness (T-0084).

The harness exercises a candidate ``AbstractionProvider`` against a
stratified corpus and emits a per-candidate scorecard. The core is
provider-agnostic (tested here with stubs) and never invokes
``IngestionService.reabstract`` -- the benchmark is read-only against
any vault graph.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from sage.adapters.abstraction_utils import compute_max_tokens, trim_to_sentence_boundary
from sage.adapters.interfaces import SYNTHETIC_HEADER_HEADING_PATH, AbstractionProvider, Chunk
from sage.config import AbstractionConfig
from sage.utils.abstraction_benchmark import (
    BenchmarkResult,
    CatalogEntry,
    MeasurementRecord,
    aggregate_latency,
    measure_one,
    measure_with_determinism_check,
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


def _make_entry(doc_id: str, doc_type: str, length: int) -> CatalogEntry:
    return CatalogEntry(doc_id=doc_id, doc_type=doc_type, length_bytes=length)


def _default_config() -> AbstractionConfig:
    return AbstractionConfig(
        enabled=True,
        model="test-model",
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
