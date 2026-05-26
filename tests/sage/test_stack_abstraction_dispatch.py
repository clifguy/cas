"""Tests for the SAGE-stack-wide abstraction provider dispatch helper
(CAS-ADR-030).

The factory dispatch that used to live inside `initialize_services`
(per-vault) is re-anchored at stack scope by ADR-030. The helper under
test is `sage.mcp_init.build_stack_abstraction_provider`. It implements:

    1. SAGE_TEST_STUB_PROVIDERS=1 -> Stub (env override)
    2. stack.abstraction.provider == "stub" -> Stub (explicit opt-out)
    3. stack.abstraction.provider == "qwen3-mlx"
       and stack.abstraction.model is None -> raise ConfigError
    4. stack.abstraction.provider == "qwen3-mlx"
       and stack.abstraction.model is not None -> Qwen3 (factory)

Test IDs follow STK-NNN (Stack abstraction dispatch).
"""

import pytest

from sage.adapters.stubs import StubAbstractionProvider
from sage.config import SageCoreConfig, StackAbstractionConfig
from sage.mcp_init import build_stack_abstraction_provider


def _stack_config(**abstraction_kwargs) -> SageCoreConfig:
    return SageCoreConfig(abstraction=StackAbstractionConfig(**abstraction_kwargs))


def test_stk_001_provider_qwen3_mlx_calls_factory_with_model_id(monkeypatch):
    """provider="qwen3-mlx" routes through `get_qwen3_abstraction_provider`
    with the configured `model_id`. We monkeypatch the factory to a sentinel
    so the real MLX load is not triggered (CLAUDE.md RAM-budget rule).

    Anti-coincidental-pass: assertion checks both (a) the sentinel is the
    instance returned by the helper and (b) the factory was called once
    with the configured model_id. STK-002 is the negative pair
    (provider="stub" with the same config must NOT call the factory).
    """
    monkeypatch.delenv("SAGE_TEST_STUB_PROVIDERS", raising=False)

    sentinel = StubAbstractionProvider()
    calls: list[dict] = []

    def fake_factory(*, model_id: str, **kwargs):
        calls.append({"model_id": model_id, **kwargs})
        return sentinel

    monkeypatch.setattr(
        "sage.adapters.abstraction_qwen3.get_qwen3_abstraction_provider",
        fake_factory,
    )

    cfg = _stack_config(provider="qwen3-mlx", model="mlx-community/test-qwen3")
    provider = build_stack_abstraction_provider(cfg)

    assert provider is sentinel
    assert len(calls) == 1
    assert calls[0]["model_id"] == "mlx-community/test-qwen3"


def test_stk_002_provider_stub_does_not_call_qwen3_factory(monkeypatch):
    """provider="stub" returns `StubAbstractionProvider` even when `model`
    is non-null. The Qwen3 factory must NOT be called.

    Killer regression test for the refactor: without the explicit `"stub"`
    branch, a config with `model` set would fall through to the qwen3
    branch. The monkeypatched factory raises if invoked, so the regression
    fires immediately.
    """
    monkeypatch.delenv("SAGE_TEST_STUB_PROVIDERS", raising=False)

    def must_not_call(*args, **kwargs):
        raise AssertionError(
            "get_qwen3_abstraction_provider must not be called when "
            "stack.abstraction.provider == 'stub'"
        )

    monkeypatch.setattr(
        "sage.adapters.abstraction_qwen3.get_qwen3_abstraction_provider",
        must_not_call,
    )

    cfg = _stack_config(provider="stub", model="mlx-community/test-qwen3")
    provider = build_stack_abstraction_provider(cfg)

    assert isinstance(provider, StubAbstractionProvider)


def test_stk_003_env_var_short_circuits_before_provider_field(monkeypatch):
    """`SAGE_TEST_STUB_PROVIDERS=1` short-circuits to Stub even when
    provider="qwen3-mlx" with a non-null model would otherwise dispatch to
    Qwen3 (preserves / F-8 guardrail at the new layer).

    Anti-coincidental-pass: paired with STK-001 (same config minus the env
    var gets Qwen3 via factory). The difference between the two outcomes
    proves the env var is what flipped the decision. Belt-and-suspenders:
    the factory is monkeypatched to raise; an env-var-short-circuit
    regression fires loudly instead of silently loading MLX.
    """
    monkeypatch.setenv("SAGE_TEST_STUB_PROVIDERS", "1")

    def must_not_call(*args, **kwargs):
        raise AssertionError("Qwen3 factory must not be called when SAGE_TEST_STUB_PROVIDERS=1")

    monkeypatch.setattr(
        "sage.adapters.abstraction_qwen3.get_qwen3_abstraction_provider",
        must_not_call,
    )

    cfg = _stack_config(provider="qwen3-mlx", model="mlx-community/test-qwen3")
    provider = build_stack_abstraction_provider(cfg)

    assert isinstance(provider, StubAbstractionProvider)


def test_stk_004_qwen3_mlx_with_null_model_raises_config_error(monkeypatch):
    """provider="qwen3-mlx" with `model is None` raises ValueError at stack
    startup. Cannot construct Qwen3 without a model identifier; ADR-030
    promotes this from the old silent Stub fallback to a loud config error.

    Anti-coincidence: if a regression silently substitutes Stub instead of
    raising, the test fails. The error message must name the missing field
    so the operator can fix `sage/config.yaml` without spelunking.
    """
    monkeypatch.delenv("SAGE_TEST_STUB_PROVIDERS", raising=False)

    cfg = _stack_config(provider="qwen3-mlx", model=None)
    with pytest.raises(ValueError) as excinfo:
        build_stack_abstraction_provider(cfg)
    assert "model" in str(excinfo.value).lower()


def test_stk_005_repeated_calls_return_same_singleton_instance(monkeypatch):
    """`get_qwen3_abstraction_provider` enforces a process-wide singleton.
    Calling `build_stack_abstraction_provider` twice with the same config
    must return the same instance (consistent with the singleton
    invariant in F-8).
    """
    monkeypatch.delenv("SAGE_TEST_STUB_PROVIDERS", raising=False)

    sentinel = StubAbstractionProvider()
    call_count = {"n": 0}

    def fake_factory(*, model_id: str, **kwargs):
        call_count["n"] += 1
        return sentinel  # same instance every call (simulates singleton)

    monkeypatch.setattr(
        "sage.adapters.abstraction_qwen3.get_qwen3_abstraction_provider",
        fake_factory,
    )

    cfg = _stack_config(provider="qwen3-mlx", model="mlx-community/test-qwen3")
    first = build_stack_abstraction_provider(cfg)
    second = build_stack_abstraction_provider(cfg)
    assert first is second
