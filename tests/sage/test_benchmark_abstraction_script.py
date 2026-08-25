"""CLI wiring tests for the abstraction benchmark script.

The harness itself is covered in test_abstraction_benchmark.py. These tests
cover only the seam between the command line and the provider: whether the
window a caller asks for is the window the provider is constructed with.
"""

from __future__ import annotations

import pytest

import sage.adapters.abstraction_qwen3 as qwen3_module
from sage.adapters.stubs import StubAbstractionProvider
from scripts.benchmark_abstraction import _build_provider, _parse_args


@pytest.fixture
def recorded_factory(monkeypatch):
    """Capture the arguments the provider factory is constructed with."""
    calls: list[dict] = []

    def fake_factory(model_id: str, context_window: int | None = None):
        calls.append({"model_id": model_id, "context_window": context_window})
        return object()

    monkeypatch.setattr(qwen3_module, "get_qwen3_abstraction_provider", fake_factory)
    return calls


def test_context_window_flag_is_forwarded_to_the_provider(recorded_factory):
    """A window asked for on the command line reaches the provider.

    Without the forwarding the flag parses cleanly, the run proceeds, and the
    provider quietly uses its built-in default -- so the scorecard would
    report a long-context evaluation that ran at the default window.
    """
    args = _parse_args(["cas", "--model", "some-model", "--context-window", "131072"])
    _build_provider(args.model, args.context_window)

    assert recorded_factory == [{"model_id": "some-model", "context_window": 131072}]


def test_context_window_defaults_to_none(recorded_factory):
    """Omitting the flag reproduces the prior behavior exactly.

    The unconfigured sentinel has to reach the provider unchanged, so a
    baseline re-run stays comparable with runs recorded before the flag
    existed.
    """
    args = _parse_args(["cas", "--model", "some-model"])
    _build_provider(args.model, args.context_window)

    assert recorded_factory == [{"model_id": "some-model", "context_window": None}]


def test_stub_model_ignores_context_window():
    """The dry-run path stays usable with the flag present."""
    args = _parse_args(["cas", "--model", "stub", "--context-window", "131072"])
    provider = _build_provider(args.model, args.context_window)

    assert isinstance(provider, StubAbstractionProvider)


def test_context_probe_flag_defaults_off():
    """The probe is opt-in; an ordinary benchmark run does not fire it."""
    assert _parse_args(["cas"]).context_probe is False
    assert _parse_args(["cas", "--context-probe"]).context_probe is True


def test_context_window_rejects_a_non_positive_value():
    """A window of zero or less is a typo, not a configuration."""
    with pytest.raises(SystemExit):
        _parse_args(["cas", "--context-window", "0"])
