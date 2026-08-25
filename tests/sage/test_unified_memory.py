"""unified-memory helper tests.

The helper queries macOS ``vm_stat`` and exposes the operator-tunable
threshold that gates Qwen3 generation. These tests verify the helper
in isolation; the integration with the abstraction provider is tested
in test_abstraction_qwen3_guardrail.py.
"""

import sys

import pytest

from sage.utils import unified_memory


@pytest.mark.skipif(sys.platform != "darwin", reason="vm_stat is macOS-only")
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


@pytest.mark.skipif(sys.platform != "darwin", reason="sysctl hw.memsize is macOS-only")
def test_total_unified_memory_bytes_reports_machine_total():
    """Helper reports installed physical memory, which bounds the free reading.

    The benchmark scorecard reports a resident footprint against the machine
    total, so the total must be a real capacity figure rather than anything
    derived from current availability.
    """
    total = unified_memory.total_unified_memory_bytes()
    assert isinstance(total, int)
    assert total > 0
    assert total > unified_memory.free_unified_memory_bytes()


def test_total_unified_memory_bytes_parses_sysctl_output(monkeypatch):
    """The byte count is read from sysctl rather than inferred.

    A stubbed reading that matches no real machine pins the parse: an
    implementation returning a constant, or one reading a different sysctl
    key, cannot produce this number.
    """

    def fake_check_output(argv, text=False):
        assert argv == ["/usr/sbin/sysctl", "-n", "hw.memsize"]
        return "17179869184\n"

    monkeypatch.setattr(unified_memory.subprocess, "check_output", fake_check_output)
    assert unified_memory.total_unified_memory_bytes() == 17179869184


def test_total_unified_memory_bytes_raises_on_unparseable_output(monkeypatch):
    """Unparseable output fails loudly rather than yielding a bogus capacity.

    A silent zero would render the scorecard's headroom figure as a division
    by zero or an infinite ratio, neither of which reads as a measurement
    failure to whoever consumes the card.
    """

    def fake_check_output(argv, text=False):
        return "not-a-number\n"

    monkeypatch.setattr(unified_memory.subprocess, "check_output", fake_check_output)
    with pytest.raises(RuntimeError, match="hw.memsize"):
        unified_memory.total_unified_memory_bytes()
