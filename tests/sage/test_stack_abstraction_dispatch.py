"""Tests for the SAGE-stack-wide abstraction provider dispatch helper
(CAS-ADR-030).

The factory dispatch that used to live inside `initialize_services`
(per-vault) is re-anchored at stack scope by ADR-030. The helper under
test is `sage.mcp_init.build_stack_abstraction_provider`. It implements:

    1. SAGE_TEST_STUB_PROVIDERS=1 -> Stub (env override)
    2. stack.abstraction.provider == "stub" -> Stub (explicit opt-out)
    3. stack.abstraction.provider == "local-mlx"
       and stack.abstraction.model is None -> raise ConfigError
    4. stack.abstraction.provider == "local-mlx"
       and stack.abstraction.model is not None -> local MLX provider (factory)
    5. stack.abstraction.provider == "anthropic"
       and stack.abstraction.model is None -> raise ConfigError
    6. stack.abstraction.provider == "anthropic"
       and stack.abstraction.model is not None -> hosted Claude provider

Test IDs follow STK-NNN (Stack abstraction dispatch).
"""

import sys

import pytest

from sage.adapters.stubs import StubAbstractionProvider
from sage.config import SageCoreConfig, StackAbstractionConfig
from sage.mcp_init import build_stack_abstraction_provider, resolve_stack_abstraction_provider


def _stack_config(**abstraction_kwargs) -> SageCoreConfig:
    return SageCoreConfig(abstraction=StackAbstractionConfig(**abstraction_kwargs))


def test_stk_001_provider_local_mlx_calls_factory_with_model_id(monkeypatch):
    """provider="local-mlx" routes through `get_qwen3_abstraction_provider`
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

    cfg = _stack_config(provider="local-mlx", model="mlx-community/test-qwen3")
    provider = build_stack_abstraction_provider(cfg)

    assert provider is sentinel
    assert len(calls) == 1
    assert calls[0]["model_id"] == "mlx-community/test-qwen3"


def test_stk_002_provider_stub_does_not_call_qwen3_factory(monkeypatch):
    """provider="stub" returns `StubAbstractionProvider` even when `model`
    is non-null. The Qwen3 factory must NOT be called.

    Killer regression test for the refactor: without the explicit `"stub"`
    branch, a config with `model` set would fall through to the local-mlx
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
    provider="local-mlx" with a non-null model would otherwise dispatch to
    the local MLX provider (preserves the F-8 guardrail at the new layer).

    Anti-coincidental-pass: paired with STK-001 (same config minus the env
    var gets the local provider via factory). The difference between the two
    outcomes proves the env var is what flipped the decision. Belt-and-
    suspenders: the factory is monkeypatched to raise; an env-var-short-
    circuit regression fires loudly instead of silently loading MLX.
    """
    monkeypatch.setenv("SAGE_TEST_STUB_PROVIDERS", "1")

    def must_not_call(*args, **kwargs):
        raise AssertionError("Qwen3 factory must not be called when SAGE_TEST_STUB_PROVIDERS=1")

    monkeypatch.setattr(
        "sage.adapters.abstraction_qwen3.get_qwen3_abstraction_provider",
        must_not_call,
    )

    cfg = _stack_config(provider="local-mlx", model="mlx-community/test-qwen3")
    provider = build_stack_abstraction_provider(cfg)

    assert isinstance(provider, StubAbstractionProvider)


def test_stk_004_local_mlx_with_null_model_raises_config_error(monkeypatch):
    """provider="local-mlx" with `model is None` raises ValueError at stack
    startup. Cannot construct the local provider without a model identifier;
    ADR-030 promotes this from the old silent Stub fallback to a loud config
    error.

    Anti-coincidence: if a regression silently substitutes Stub instead of
    raising, the test fails. The error message must name the missing field
    so the operator can fix `sage/config.yaml` without spelunking.
    """
    monkeypatch.delenv("SAGE_TEST_STUB_PROVIDERS", raising=False)

    cfg = _stack_config(provider="local-mlx", model=None)
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

    cfg = _stack_config(provider="local-mlx", model="mlx-community/test-qwen3")
    first = build_stack_abstraction_provider(cfg)
    second = build_stack_abstraction_provider(cfg)
    assert first is second


def test_stk_006_provider_anthropic_constructs_provider(monkeypatch):
    """provider="anthropic" constructs an `AnthropicAbstractionProvider`
    carrying the configured Claude model id.

    Anti-coincidental-pass: STK-001 (provider="local-mlx", same config shape)
    routes to the Qwen3 factory instead. The two together prove the dispatch
    *key* — not the presence of a `model` value — selects the branch.
    """
    monkeypatch.delenv("SAGE_TEST_STUB_PROVIDERS", raising=False)

    from sage.adapters.abstraction_anthropic import AnthropicAbstractionProvider

    cfg = _stack_config(provider="anthropic", model="claude-haiku-4-5")
    provider = build_stack_abstraction_provider(cfg)

    assert isinstance(provider, AnthropicAbstractionProvider)
    assert provider._model_id == "claude-haiku-4-5"


def test_stk_007_provider_anthropic_with_null_model_raises(monkeypatch):
    """provider="anthropic" with `model is None` raises ValueError naming the
    missing `model` field (mirrors the local-mlx loud-config rule)."""
    monkeypatch.delenv("SAGE_TEST_STUB_PROVIDERS", raising=False)

    cfg = _stack_config(provider="anthropic", model=None)
    with pytest.raises(ValueError) as excinfo:
        build_stack_abstraction_provider(cfg)
    assert "model" in str(excinfo.value).lower()


def test_stk_008_anthropic_construction_imports_no_mlx(monkeypatch):
    """Constructing the `anthropic` provider through the full profile-seam
    resolver imports no MLX module.

    This is the platform-decoupling proof that runs on the Linux CI runner:
    the suite executes without `mlx`/`mlx-lm` installed, so any eager MLX
    import leaking into the abstraction construction path would raise
    ImportError there. On macOS (where mlx *is* installed) the bare success
    would not catch a leak, so this also snapshots `sys.modules` and asserts
    no new `mlx*` module appeared as a result of construction.

    Routes through `resolve_stack_abstraction_provider` (the profile-seam
    accessor the lifespans use), not just the bare factory, so it exercises
    the real construction path.
    """
    monkeypatch.delenv("SAGE_TEST_STUB_PROVIDERS", raising=False)

    from sage.adapters.abstraction_anthropic import AnthropicAbstractionProvider

    before = {m for m in sys.modules if m == "mlx" or m.startswith(("mlx_", "mlx."))}

    cfg = _stack_config(provider="anthropic", model="claude-haiku-4-5")
    provider = resolve_stack_abstraction_provider(cfg)

    after = {m for m in sys.modules if m == "mlx" or m.startswith(("mlx_", "mlx."))}

    assert isinstance(provider, AnthropicAbstractionProvider)
    assert after == before, f"abstraction construction imported MLX modules: {after - before}"


def test_stk_009_context_window_passed_to_local_factory(monkeypatch):
    """A configured `context_window` reaches `get_qwen3_abstraction_provider`
    as a keyword argument on the local-mlx branch.

    Anti-coincidental-pass: STK-010 is the paired absent-field case. Together
    they prove the dispatch forwards whatever the config carries rather than
    hardcoding either the value or the module default.
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

    cfg = _stack_config(
        provider="local-mlx", model="mlx-community/test-qwen3", context_window=65536
    )
    provider = build_stack_abstraction_provider(cfg)

    assert provider is sentinel
    assert len(calls) == 1
    assert calls[0]["context_window"] == 65536


def test_stk_010_context_window_absent_passes_none_to_local_factory(monkeypatch):
    """With `context_window` unset, the factory receives an explicit None --
    not the module default, and not an omitted keyword.

    None is the sentinel the provider resolves to its own
    `DEFAULT_CONTEXT_WINDOW`. Substituting 32768 here would satisfy STK-009
    while destroying the distinction that the non-local-provider rejection
    (STK-011, STK-012) depends on: "unset" and "explicitly set to the
    default" must not look the same to the dispatch.
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

    cfg = _stack_config(provider="local-mlx", model="mlx-community/test-qwen3")
    build_stack_abstraction_provider(cfg)

    assert len(calls) == 1
    assert "context_window" in calls[0], "context_window was not forwarded at all"
    assert calls[0]["context_window"] is None


def test_stk_011_context_window_against_anthropic_rejected(monkeypatch):
    """`context_window` set against the hosted provider fails loud at startup.

    The field's meaning is local-provider-specific: a hosted provider derives
    its input limit from its own configured model and would silently ignore
    the value. Rejecting is the difference between a visible misconfiguration
    and a knob that appears to work and does nothing.

    Anti-coincidental-pass: the positive control constructs the same hosted
    provider from the same config *without* the field, so a rejection that
    fired for every anthropic config would fail here.
    """
    monkeypatch.delenv("SAGE_TEST_STUB_PROVIDERS", raising=False)

    from sage.adapters.abstraction_anthropic import AnthropicAbstractionProvider

    cfg = _stack_config(provider="anthropic", model="claude-haiku-4-5", context_window=65536)
    with pytest.raises(ValueError) as excinfo:
        build_stack_abstraction_provider(cfg)
    message = str(excinfo.value)
    assert "context_window" in message
    assert "anthropic" in message

    # Positive control: the same provider/model without the field constructs.
    ok = _stack_config(provider="anthropic", model="claude-haiku-4-5")
    assert isinstance(build_stack_abstraction_provider(ok), AnthropicAbstractionProvider)


def test_stk_012_context_window_against_stub_rejected(monkeypatch):
    """`context_window` set against the explicit `stub` provider fails loud,
    and the env override still short-circuits above the rejection.

    Anti-coincidental-pass: the env-override half is the ordering guard. A
    rejection placed above the `SAGE_TEST_STUB_PROVIDERS` short-circuit would
    reject correctly here and break the test escape hatch that keeps the
    suite from loading the local MLX model alongside a running server -- so
    this asserts a local-mlx config carrying the field returns a stub without
    raising when the env var is set.
    """
    monkeypatch.delenv("SAGE_TEST_STUB_PROVIDERS", raising=False)

    cfg = _stack_config(provider="stub", model=None, context_window=65536)
    with pytest.raises(ValueError) as excinfo:
        build_stack_abstraction_provider(cfg)
    message = str(excinfo.value)
    assert "context_window" in message
    assert "stub" in message

    # Positive control: the same provider without the field constructs.
    ok = _stack_config(provider="stub", model=None)
    assert isinstance(build_stack_abstraction_provider(ok), StubAbstractionProvider)

    # Ordering guard: the env override wins over every config-driven branch,
    # including the rejection.
    monkeypatch.setenv("SAGE_TEST_STUB_PROVIDERS", "1")
    overridden = _stack_config(
        provider="local-mlx", model="mlx-community/test-qwen3", context_window=65536
    )
    assert isinstance(build_stack_abstraction_provider(overridden), StubAbstractionProvider)


def _committed_stack_config() -> SageCoreConfig:
    """Load the committed `sage/config.yaml`, bypassing the suite's pin.

    The autouse `_pin_test_stack_config` fixture points SAGE_CONFIG_PATH at a
    synthetic config carrying no `abstraction` block, so a bare
    `load_stack_config_or_default()` here would read that file instead of the
    shipped one. Passing the default path explicitly is what makes the two
    tests below assertions about what this repository actually ships.
    """
    from sage.mcp_init import _DEFAULT_STACK_CONFIG_PATH, load_stack_config_or_default

    return load_stack_config_or_default(path=_DEFAULT_STACK_CONFIG_PATH)


def test_stk_013_committed_config_pins_an_explicit_context_window():
    """The shipped stack config sets `context_window` rather than leaving it
    unset, so the effective window is the configured model's full capacity
    instead of the provider's conservative built-in floor.

    Anti-coincidental-pass: asserting only `is not None` would pass for any
    positive integer, including one *below* `DEFAULT_CONTEXT_WINDOW` -- a
    silent regression in the opposite direction to the one this pin exists to
    close. Comparing against the constant pins the direction of the decision
    without freezing the literal value, so a later model can raise the pin
    without editing this test.
    """
    from sage.adapters.abstraction_qwen3 import DEFAULT_CONTEXT_WINDOW

    cfg = _committed_stack_config()

    assert cfg.abstraction.context_window is not None, (
        "the committed sage/config.yaml leaves context_window unset; the local "
        "provider would fall back to its built-in default window"
    )
    assert cfg.abstraction.context_window >= DEFAULT_CONTEXT_WINDOW


def test_stk_014_committed_context_window_reaches_the_local_factory(monkeypatch):
    """The window the committed config carries survives the dispatch to
    `get_qwen3_abstraction_provider`.

    Anti-coincidental-pass: STK-009 proves an arbitrary configured value is
    forwarded and STK-010 proves an absent one is forwarded as None, but both
    build their config in-test. Neither notices the shipped file reverting to
    unset. Asserting equality against the loaded value -- rather than merely
    that the keyword was passed, which holds for None too -- is what binds the
    committed file to the live dispatch path.
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

    cfg = _committed_stack_config()
    build_stack_abstraction_provider(cfg)

    assert len(calls) == 1
    assert calls[0]["context_window"] is not None
    assert calls[0]["context_window"] == cfg.abstraction.context_window


def test_stk_015_opener_constraint_passed_to_local_factory(monkeypatch):
    """A configured `opener_constraint` reaches the local factory.

    The knob controls a decoding-time constraint inside the local
    provider's sampling loop, so a value that never reaches the factory
    leaves generation unchanged while the config claims otherwise.

    Anti-coincidental-pass: STK-016 is the negative pair, asserting the
    unset field still forwards -- explicitly False, not absent.
    """
    monkeypatch.delenv("SAGE_TEST_STUB_PROVIDERS", raising=False)

    calls: list[dict] = []

    def fake_factory(*, model_id: str, **kwargs):
        calls.append({"model_id": model_id, **kwargs})
        return StubAbstractionProvider()

    monkeypatch.setattr(
        "sage.adapters.abstraction_qwen3.get_qwen3_abstraction_provider",
        fake_factory,
    )

    cfg = _stack_config(
        provider="local-mlx", model="mlx-community/test-qwen3", opener_constraint=True
    )
    build_stack_abstraction_provider(cfg)

    assert calls[0]["opener_constraint"] is True


def test_stk_016_opener_constraint_unset_forwards_false(monkeypatch):
    monkeypatch.delenv("SAGE_TEST_STUB_PROVIDERS", raising=False)

    calls: list[dict] = []

    def fake_factory(*, model_id: str, **kwargs):
        calls.append({"model_id": model_id, **kwargs})
        return StubAbstractionProvider()

    monkeypatch.setattr(
        "sage.adapters.abstraction_qwen3.get_qwen3_abstraction_provider",
        fake_factory,
    )

    cfg = _stack_config(provider="local-mlx", model="mlx-community/test-qwen3")
    build_stack_abstraction_provider(cfg)

    assert "opener_constraint" in calls[0], "opener_constraint was not forwarded at all"
    assert calls[0]["opener_constraint"] is False


def test_stk_017_opener_constraint_against_anthropic_rejected(monkeypatch):
    """The constraint is a property of a sampling loop the hosted provider
    does not have, so setting it there fails loud at startup.

    Clause (f) coverage is therefore provider-dependent by construction:
    the hosted provider keeps the prompt directive and the post-generation
    check, and gains no prevention. That asymmetry is stated here rather
    than discovered from an abstract that breached anyway.

    Anti-coincidental-pass: the positive control constructs the same
    hosted provider from the same config without the field, so a
    rejection firing for every anthropic config would fail here.
    """
    monkeypatch.delenv("SAGE_TEST_STUB_PROVIDERS", raising=False)

    from sage.adapters.abstraction_anthropic import AnthropicAbstractionProvider

    cfg = _stack_config(provider="anthropic", model="claude-haiku-4-5", opener_constraint=True)
    with pytest.raises(ValueError) as excinfo:
        build_stack_abstraction_provider(cfg)
    message = str(excinfo.value)
    assert "opener_constraint" in message
    assert "anthropic" in message

    ok = _stack_config(provider="anthropic", model="claude-haiku-4-5")
    assert isinstance(build_stack_abstraction_provider(ok), AnthropicAbstractionProvider)


def test_stk_018_opener_constraint_false_against_anthropic_is_accepted(monkeypatch):
    """Only an *enabled* constraint is a misconfiguration.

    A rejection keyed on the field's presence rather than its value would
    make a stack config that spells out the default unloadable on the
    hosted provider.
    """
    monkeypatch.delenv("SAGE_TEST_STUB_PROVIDERS", raising=False)

    from sage.adapters.abstraction_anthropic import AnthropicAbstractionProvider

    cfg = _stack_config(provider="anthropic", model="claude-haiku-4-5", opener_constraint=False)
    assert isinstance(build_stack_abstraction_provider(cfg), AnthropicAbstractionProvider)


def test_stk_019_committed_config_enables_the_opener_constraint():
    """The shipped stack config turns the clause (f) decoding constraint on.

    The constraint is pinned here rather than shipped as the schema default
    because it is rejected against a non-local provider: a `true` default
    would make the container (`stub`) and cloud (`anthropic`) stacks fail
    at startup. The committed file is the one stack that names `local-mlx`,
    so it is the only place the decision can be recorded.

    Anti-coincidental-pass: STK-015 proves an arbitrary configured value is
    forwarded and STK-016 proves an absent one is forwarded as False, but
    both build their config in-test, so neither notices the shipped file
    reverting to unset. This asserts the loaded value, and STK-020 carries
    it through the dispatch.
    """
    cfg = _committed_stack_config()

    assert cfg.abstraction.provider == "local-mlx", (
        "the committed config no longer names the local provider; the "
        "opener constraint is rejected against every other one"
    )
    assert cfg.abstraction.opener_constraint is True, (
        "the committed sage/config.yaml leaves opener_constraint unset; "
        "generation would run unconstrained against CAS-ADR-020 clause (f)"
    )


def test_stk_020_committed_opener_constraint_reaches_the_local_factory(monkeypatch):
    """The pin the committed config carries survives the dispatch.

    Anti-coincidental-pass: asserting the config value alone (STK-019) would
    pass for a dispatch that read the field and dropped it, which is exactly
    the failure the rejection guard cannot catch -- a dropped `true` looks
    identical to a stack that never enabled it. Equality against the loaded
    value, rather than a hardcoded True, keeps the pair honest if the pin is
    ever deliberately reverted.
    """
    monkeypatch.delenv("SAGE_TEST_STUB_PROVIDERS", raising=False)

    calls: list[dict] = []

    def fake_factory(*, model_id: str, **kwargs):
        calls.append({"model_id": model_id, **kwargs})
        return StubAbstractionProvider()

    monkeypatch.setattr(
        "sage.adapters.abstraction_qwen3.get_qwen3_abstraction_provider",
        fake_factory,
    )

    cfg = _committed_stack_config()
    build_stack_abstraction_provider(cfg)

    assert len(calls) == 1
    assert calls[0]["opener_constraint"] == cfg.abstraction.opener_constraint
