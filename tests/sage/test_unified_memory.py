"""T-0029 unified-memory helper tests.

The helper queries macOS ``vm_stat`` and exposes the operator-tunable
threshold that gates Qwen3 generation. These tests verify the helper
in isolation; the integration with the abstraction provider is tested
in test_abstraction_qwen3_guardrail.py.
"""

from sage.utils import unified_memory


def test_t0029_free_unified_memory_bytes_returns_positive_int():
    """Helper parses vm_stat and returns a positive int (bytes).

    The page size on Apple Silicon machines varies (4 KiB on Intel
    Macs, 16 KiB on Apple Silicon); the helper must read it from the
    vm_stat header rather than assume.
    """
    free = unified_memory.free_unified_memory_bytes()
    assert isinstance(free, int)
    assert free > 0


def test_t0029_min_free_bytes_default_when_env_unset(monkeypatch):
    """Threshold falls back to DEFAULT_MIN_FREE_GIB when the env var
    is unset.
    """
    monkeypatch.delenv("SAGE_MIN_FREE_UNIFIED_MEMORY_GIB", raising=False)
    assert unified_memory.min_free_bytes() == unified_memory.DEFAULT_MIN_FREE_GIB * 1024**3


def test_t0029_min_free_bytes_env_var_override(monkeypatch):
    """SAGE_MIN_FREE_UNIFIED_MEMORY_GIB overrides the default.

    Operator tunability without redeploy: when the threshold needs
    adjustment in response to observed memory pressure, the env var is
    the lever.
    """
    monkeypatch.setenv("SAGE_MIN_FREE_UNIFIED_MEMORY_GIB", "8")
    assert unified_memory.min_free_bytes() == 8 * 1024**3
