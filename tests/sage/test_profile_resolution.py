"""Tests for the deployment-profile seam (CAS-ADR-042).

The seam is a stack-scope `profile` marker plus the binding-registration
contract in `sage.profiles`: each adapter binding registers itself against a
profile, and `resolve_profile` assembles the registered bindings for the active
profile once at stack startup. The abstraction provider is the keystone seam --
its `local` binding is the whole `build_stack_abstraction_provider` factory --
so resolving the local profile reproduces today's behavior exactly.

Test IDs follow PRF-NNN (PRofile resolution).
"""

import pytest

from sage import profiles
from sage.adapters.stubs import StubAbstractionProvider
from sage.config import SageCoreConfig, StackAbstractionConfig
from sage.mcp_init import (
    build_stack_abstraction_provider,
    resolve_stack_abstraction_provider,
    resolve_stack_profile,
)


def _stub_stack_config() -> SageCoreConfig:
    """A `local`-profile stack config whose abstraction binding is the stub.

    `provider="stub"` is deterministic and provider-rename-proof (`stub` is
    stable across provider renames), so resolving the profile assembles
    without loading MLX (CLAUDE.md RAM rule / F-8).
    """
    return SageCoreConfig(abstraction=StackAbstractionConfig(provider="stub", model=None))


def test_prf_001_resolve_stack_profile_assembles_local_abstraction():
    """`resolve_stack_profile` on the default (`local`) profile assembles a
    `ResolvedProfile` carrying the abstraction-seam binding.

    Happy path for AC#3 (the profile resolves at startup). The delegation and
    registration guarantees that keep this from passing coincidentally on a
    hardcoded stub live in PRF-004 / PRF-005.
    """
    resolved = resolve_stack_profile(_stub_stack_config())

    assert resolved.profile == "local"
    assert profiles.ABSTRACTION_SEAM in resolved.bindings
    assert isinstance(resolved.binding(profiles.ABSTRACTION_SEAM), StubAbstractionProvider)


def test_prf_002_resolve_profile_unknown_value_raises_loud():
    """`resolve_profile` on a profile with no registered bindings raises
    `ValueError`, naming the unknown profile and the registered set.

    Anti-coincidental-pass: a resolver that returned an empty `ResolvedProfile`
    for an unknown profile would pass a weaker test; this asserts it RAISES and
    that the message is actionable (names `cloud` and lists the registered
    profiles, which include `local`).
    """
    with pytest.raises(ValueError) as excinfo:
        profiles.resolve_profile("cloud", _stub_stack_config())

    message = str(excinfo.value)
    assert "cloud" in message
    assert "local" in message


def test_prf_003_register_binding_then_resolve_calls_factory_once_with_config():
    """The generic contract: `register_binding` attaches a factory to a
    `(profile, seam)`, and `resolve_profile` calls it exactly once with the
    stack config and files the result under the seam name.

    This is AC#2 -- a future binding is added by registering it. Uses a
    throwaway profile/seam so it exercises the mechanism without touching the
    real `local`/abstraction wiring.

    Anti-coincidental-pass: a resolver that ignored the registry would not
    surface the binding; one that dropped the config argument would fail the
    `calls == [cfg]` assertion.
    """
    cfg = _stub_stack_config()
    sentinel = object()
    calls: list[SageCoreConfig] = []

    def fake_factory(stack_config: SageCoreConfig) -> object:
        calls.append(stack_config)
        return sentinel

    profiles.register_binding("prf_test_profile", "prf_test_seam", fake_factory)
    resolved = profiles.resolve_profile("prf_test_profile", cfg)

    assert resolved.binding("prf_test_seam") is sentinel
    assert calls == [cfg]


def test_prf_004_resolve_stack_abstraction_provider_routes_through_factory(monkeypatch):
    """`resolve_stack_abstraction_provider` returns exactly what the registered
    `build_stack_abstraction_provider` returns -- proving the rewire routes the
    construction path through the resolver, not a parallel path.

    The Qwen3 factory is monkeypatched to a sentinel (no MLX load); both the
    resolver accessor and the direct factory call must return that sentinel.

    Anti-coincidental-pass: if the accessor bypassed the registered factory it
    would not return the sentinel. (The `"local-mlx"` provider value is the
    current enum literal; a later provider rename updates it in lockstep with
    the schema and config.)
    """
    monkeypatch.delenv("SAGE_TEST_STUB_PROVIDERS", raising=False)

    sentinel = StubAbstractionProvider()

    def fake_factory(*, model_id: str, **kwargs):
        return sentinel

    monkeypatch.setattr(
        "sage.adapters.abstraction_qwen3.get_qwen3_abstraction_provider",
        fake_factory,
    )

    cfg = SageCoreConfig(
        abstraction=StackAbstractionConfig(provider="local-mlx", model="mlx-community/test")
    )
    via_resolver = resolve_stack_abstraction_provider(cfg)
    via_factory = build_stack_abstraction_provider(cfg)

    assert via_resolver is sentinel
    assert via_resolver is via_factory


def test_prf_005_local_profile_registers_the_abstraction_binding():
    """Importing `sage.mcp_init` registers the abstraction-provider binding for
    the `local` profile (the module-level wiring line).

    Anti-coincidental-pass: if the `register_binding(LOCAL_PROFILE, ...)` line
    were dropped, `local` would be absent from the registry and
    `resolve_stack_profile` would raise -- so this asserts both the registry
    membership and that the resolved local profile carries the abstraction seam.
    """
    assert profiles.LOCAL_PROFILE in profiles.registered_profiles()

    resolved = resolve_stack_profile(_stub_stack_config())
    assert profiles.ABSTRACTION_SEAM in resolved.bindings


def test_prf_006_resolver_honors_monkeypatched_factory(monkeypatch):
    """The local abstraction binding resolves `build_stack_abstraction_provider`
    by late binding, so a monkeypatch of that module attribute is honored
    through `resolve_stack_abstraction_provider`.

    Regression guard: several lifespan/reload tests monkeypatch
    `sage.mcp_init.build_stack_abstraction_provider` to keep real providers out
    of the construction path. If the profile registry captured the bare
    function object at import time, those patches would be silently bypassed.

    Anti-coincidental-pass: a captured-reference registration returns the real
    (stub) provider, not the sentinel, so the identity assertion fails.
    """
    sentinel = StubAbstractionProvider()
    monkeypatch.setattr(
        "sage.mcp_init.build_stack_abstraction_provider",
        lambda _cfg: sentinel,
    )

    assert resolve_stack_abstraction_provider(_stub_stack_config()) is sentinel


def test_prf_007_local_profile_registers_the_storage_binding():
    """Importing `sage.mcp_init` registers the durable-storage binding for the
    `local` profile, and the resolved binding is a storage provisioner.

    Anti-coincidental-pass: if the `register_binding(LOCAL_PROFILE,
    STORAGE_SEAM, ...)` line were dropped, the seam would be absent from the
    resolved bindings and the `binding()` accessor would raise KeyError -- so
    this asserts both seam membership and the provisioner type. (Under the
    test session's SAGE_TEST_STORAGE_BACKEND=embedded pin the assembled
    provisioner is the embedded one regardless of the config key.)
    """
    from sage.storage_binding import VaultStorageProvisioner

    resolved = resolve_stack_profile(_stub_stack_config())
    assert profiles.STORAGE_SEAM in resolved.bindings
    assert isinstance(resolved.binding(profiles.STORAGE_SEAM), VaultStorageProvisioner)


def test_prf_008_storage_resolver_honors_monkeypatched_factory(monkeypatch):
    """The local storage binding resolves `build_stack_storage_provisioner` by
    late binding, so a monkeypatch of that `sage.mcp_init` attribute is honored
    through `resolve_stack_storage_provisioner` -- the same delegation guarantee
    PRF-006 pins for the abstraction seam.

    Anti-coincidental-pass: a captured-reference registration would return a
    real provisioner instead of the sentinel, failing the identity assertion.
    """
    from sage.mcp_init import resolve_stack_storage_provisioner

    sentinel = object()
    monkeypatch.setattr(
        "sage.mcp_init.build_stack_storage_provisioner",
        lambda _cfg: sentinel,
    )

    assert resolve_stack_storage_provisioner(_stub_stack_config()) is sentinel
