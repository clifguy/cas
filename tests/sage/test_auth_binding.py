"""Auth-seam binding/resolution tests (CAS-ADR-042).

The token validator is a deployment-profile binding on the new auth seam,
assembled exactly like the abstraction and storage seams. These guard that
the seam is registered for the local profile, that resolution returns the
pass-through validator by default and the Entra validator when auth is
enabled, and that the binding delegates to ``build_auth_validator`` by
module-global name so a monkeypatch is honored through the resolver.
"""

from __future__ import annotations

from sage import profiles
from sage.auth import EntraTokenValidator, NoAuthValidator
from sage.config import SageCoreConfig, StackAuthConfig
from sage.mcp_init import resolve_stack_auth_validator

_ENABLED = SageCoreConfig(
    auth=StackAuthConfig(enabled=True, tenant_id="tid", audience="api://sage")
)


def test_d1_auth_seam_registered_for_local_profile() -> None:
    assert profiles.AUTH_SEAM in profiles._REGISTRY[profiles.LOCAL_PROFILE]


def test_d2_default_resolves_to_noauth() -> None:
    assert isinstance(resolve_stack_auth_validator(SageCoreConfig()), NoAuthValidator)


def test_d3_enabled_resolves_to_entra_validator() -> None:
    assert isinstance(resolve_stack_auth_validator(_ENABLED), EntraTokenValidator)


def test_d4_monkeypatch_of_factory_is_honored(monkeypatch) -> None:
    """Patching sage.mcp_init.build_auth_validator reaches the resolver.

    Guards the by-name delegation in ``_local_auth_binding`` (the same
    discipline as the storage/abstraction bindings): a captured-reference
    binding would silently bypass the patch.
    """
    sentinel = object()
    monkeypatch.setattr("sage.mcp_init.build_auth_validator", lambda _cfg: sentinel)
    assert resolve_stack_auth_validator(SageCoreConfig()) is sentinel
