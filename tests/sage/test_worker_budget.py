"""The ``-n auto`` worker budget: CPU count bounded by the Postgres connection ceiling."""

from __future__ import annotations

import pytest

from tests.helpers.workers import worker_budget


@pytest.mark.parametrize(
    ("cpu_count", "max_connections", "expected"),
    [
        (14, 100, 6),  # a 14-core workstation against a default ceiling: connections bind
        (4, 100, 4),  # a 4-vCPU CI runner: CPUs bind
        (14, 200, 14),  # a raised ceiling frees the CPUs
        (2, 40, 1),  # a tiny ceiling still yields one worker, never zero
        (14, None, 14),  # no server to ask: CPU count alone
        (None, None, 1),  # unknown CPU count: one worker
    ],
)
def test_wb_001_worker_budget(cpu_count, max_connections, expected):
    assert worker_budget(cpu_count, max_connections) == expected
