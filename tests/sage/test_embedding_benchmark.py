"""Benchmark results must describe actual invocations and delivered vectors."""

import pytest

from sage.utils.embedding_benchmark import benchmark_case, summarize


class Provider:
    def __init__(self):
        self.calls = []
        self.public_calls = 0

    def _embed_sync(self, texts):
        self.calls.append(list(texts))
        return [[1.0, 0.0] for _ in texts]

    async def embed(self, texts):
        self.public_calls += 1
        return [row for text in texts for row in self._embed_sync([text])]


def test_direct_batch_cardinality_and_warmup_exclusion():
    provider = Provider()
    rows = benchmark_case(provider, ["one", "two", "three"], batch_size=2, repeats=2, warmups=1)
    assert len(rows) == 2
    assert [len(c) for c in provider.calls] == [2, 1] * 3
    assert all(r["texts"] == 3 and r["invocations"] == 2 for r in rows)
    assert all(r["actual_batch_sizes"] == [2, 1] for r in rows)
    assert summarize(rows)["repetitions"] == 2


def test_fair_baseline_exercises_provider_public_dispatch():
    provider = Provider()
    rows = benchmark_case(provider, ["one", "two"], batch_size=1, repeats=1, warmups=0, fair=True)
    assert provider.calls == [["one"], ["two"]]
    assert provider.public_calls == 1
    assert rows[0]["mode"] == "fair_dispatch"


def test_missing_vectors_refuse_success():
    provider = Provider()
    provider._embed_sync = lambda texts: []
    with pytest.raises(ValueError, match="vector"):
        benchmark_case(provider, ["one"], batch_size=1, repeats=1, warmups=0)


def test_wrong_vector_association_refuses_success():
    provider = Provider()
    reference = [[0.0, 1.0]]
    with pytest.raises(ValueError, match="association"):
        benchmark_case(provider, ["one"], batch_size=1, repeats=1, warmups=0, reference=reference)


@pytest.mark.parametrize("vectors", [[[2.0, 0.0]], [[float("nan"), 0.0]]])
def test_non_normalized_or_invalid_vectors_refuse_success(vectors):
    provider = Provider()
    provider._embed_sync = lambda texts: vectors
    with pytest.raises(ValueError, match="vector"):
        benchmark_case(provider, ["one"], batch_size=1, repeats=1, warmups=0)


def test_fair_reports_observed_calls_instead_of_requested_batch_size():
    provider = Provider()
    rows = benchmark_case(
        provider, ["one", "two", "three"], batch_size=2, repeats=1, warmups=0, fair=True
    )
    assert provider.public_calls == 1
    assert rows[0]["actual_batch_sizes"] == [1, 1, 1]
    assert rows[0]["invocations"] == 3


def test_fair_semantic_samples_show_latency_and_bulk_progress():
    import asyncio

    class TrafficProvider(Provider):
        async def embed(self, texts):
            self.public_calls += 1
            result = []
            for text in texts:
                await asyncio.sleep(0)
                result.extend(self._embed_sync([text]))
            return result

    provider = TrafficProvider()
    rows = benchmark_case(
        provider,
        ["one", "two", "three"],
        batch_size=2,
        repeats=2,
        warmups=1,
        fair=True,
        semantic_text="query",
    )
    assert len(rows) == 2
    assert provider.public_calls > 3
    for row in rows:
        assert row["actual_batch_sizes"] == [1, 1, 1]
        assert row["semantic_sample_count"] > 0
        assert row["semantic_max_seconds"] >= row["semantic_p95_seconds"] > 0
        assert any(
            q["bulk_completed_after"] > q["bulk_completed_before"] for q in row["semantic_samples"]
        )


def test_bulk_and_query_tokens_are_observed_separately():
    import asyncio

    import numpy as np

    class Model:
        def tokenize(self, texts):
            return {
                "attention_mask": np.array([[1] * (9 if text == "query" else 3) for text in texts])
            }

        def encode(self, texts, **kwargs):
            self.tokenize(texts)
            return [[1.0, 0.0] for _ in texts]

    class ObservedProvider:
        def __init__(self):
            self._model = Model()

        async def embed(self, texts):
            result = []
            for text in texts:
                await asyncio.sleep(0)
                result.extend(self._model.encode([text]))
            return result

    rows = benchmark_case(
        ObservedProvider(),
        ["one", "two"],
        batch_size=2,
        repeats=1,
        warmups=0,
        fair=True,
        semantic_text="query",
    )
    row = rows[0]
    assert row["bulk_tokens"] == 6
    assert row["query_tokens"] == 9 * row["semantic_sample_count"] > 0
    assert row["tokens_per_second"] == row["bulk_tokens"] / row["elapsed_seconds"]
    assert row["actual_token_lengths"] == [[3], [3]]


def test_squared_difference_cannot_hide_inside_componentwise_tolerance():
    import numpy as np

    from sage.utils.embedding_benchmark import _validate

    reference = np.ones((1, 768)) / np.sqrt(768)
    candidate = reference + np.array([[0.00004, -0.00004] * 384])
    assert np.allclose(candidate, reference, atol=1e-4, rtol=1e-3)
    assert np.sum((candidate - reference) ** 2) >= 1e-6
    with pytest.raises(ValueError, match="association"):
        _validate(candidate.tolist(), 1, reference.tolist())


def test_semantic_text_collision_cannot_misclassify_bulk_work():
    with pytest.raises(ValueError, match="semantic text"):
        benchmark_case(
            Provider(),
            ["query"],
            batch_size=1,
            repeats=1,
            warmups=0,
            fair=True,
            semantic_text="query",
        )
