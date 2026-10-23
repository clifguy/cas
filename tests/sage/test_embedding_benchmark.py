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
