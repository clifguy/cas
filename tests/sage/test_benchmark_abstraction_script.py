"""CLI wiring tests for the abstraction benchmark script.

The harness itself is covered in test_abstraction_benchmark.py. These tests
cover only the seam between the command line and the run: whether the
window a caller asks for is the window the provider is constructed with,
which model an unqualified run measures, and where its artifacts land.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

import sage.adapters.abstraction_qwen3 as qwen3_module
from sage.adapters.stubs import StubAbstractionProvider
from sage.vault_management import default_vault_root
from scripts.benchmark_abstraction import DEFAULT_OUTPUT_DIR, _build_provider, _parse_args


@pytest.fixture
def stack_config(monkeypatch, tmp_path):
    """Point stack-config resolution at a caller-written config file."""

    def write(doc: dict) -> Path:
        path = tmp_path / "stack_config.yaml"
        path.write_text(yaml.safe_dump(doc))
        monkeypatch.setenv("SAGE_CONFIG_PATH", str(path))
        return path

    return write


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
    assert _parse_args(["cas", "--model", "stub"]).context_probe is False
    assert _parse_args(["cas", "--model", "stub", "--context-probe"]).context_probe is True


def test_context_window_rejects_a_non_positive_value():
    """A window of zero or less is a typo, not a configuration."""
    with pytest.raises(SystemExit):
        _parse_args(["cas", "--model", "stub", "--context-window", "0"])


def test_model_defaults_to_the_configured_stack_model(stack_config):
    """An unqualified run measures the model this stack abstracts with.

    A default frozen into the script drifts the moment the configured model
    moves, and the run still succeeds -- reporting a scorecard for a model
    the stack does not use, unless the caller happened to pass --model.
    """
    stack_config(
        {"abstraction": {"provider": "local-mlx", "model": "mlx-community/Configured-4bit"}}
    )

    assert _parse_args(["cas"]).model == "mlx-community/Configured-4bit"


def test_an_explicit_model_still_wins_over_the_configured_one(stack_config):
    """Benchmarking a candidate the stack is not configured for is the point."""
    stack_config(
        {"abstraction": {"provider": "local-mlx", "model": "mlx-community/Configured-4bit"}}
    )

    assert _parse_args(["cas", "--model", "candidate"]).model == "candidate"


def test_model_is_required_when_the_stack_names_none(stack_config, capsys):
    """No configured model means no default -- ask, rather than guess.

    The exit alone does not discriminate: every argparse failure reachable
    from this argv raises SystemExit, so a later change that drops the
    model check while erroring for some other reason would keep this green.
    The message is what pins the exit to this cause.
    """
    stack_config({"abstraction": {"provider": "stub"}})

    with pytest.raises(SystemExit):
        _parse_args(["cas"])

    assert "--model is required" in capsys.readouterr().err


def test_default_output_dir_is_outside_every_vault_tree():
    """Benchmark artifacts never default into SAGE-managed storage.

    Everything under a vault tree is SAGE's to write; a harness dropping
    files there directly desynchronizes the graph from what is on disk, and
    a vault-shaped directory for a vault that does not exist is worse still.

    The root comes from ``default_vault_root`` rather than a literal so the
    assertion tracks wherever vaults actually live (CAS-ADR-043): spelled
    ``~/sage_vaults`` here, it would stay green against a relocated root
    that the output directory had come to sit inside.
    """
    vault_root = default_vault_root().resolve()
    default = DEFAULT_OUTPUT_DIR.expanduser().resolve()

    assert vault_root not in default.parents
    assert default != vault_root

    parsed = _parse_args(["cas", "--model", "stub"]).output_dir.expanduser().resolve()
    assert parsed == default
