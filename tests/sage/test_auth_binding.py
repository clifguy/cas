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
from sage.auth import (
    EntraTokenValidator,
    NoAuthValidator,
    _accepted_audiences,
    build_auth_validator,
)
from sage.config import SageCoreConfig, StackAuthConfig
from sage.mcp_init import resolve_stack_auth_validator

_GUID = "11111111-2222-3333-4444-555555555555"

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


def test_accepted_audiences_derives_guid_from_app_id_uri() -> None:
    # The App ID URI and its bare application-id GUID are both admitted, the URI
    # first; a v2.0 access token carries the bare GUID as its aud.
    forms = _accepted_audiences(f"api://{_GUID}")
    assert forms == [f"api://{_GUID}", _GUID]


def test_accepted_audiences_passthrough_for_non_uri() -> None:
    # A non-URI audience has no GUID suffix to derive -- returned unchanged.
    assert _accepted_audiences("custom-audience") == ["custom-audience"]


def test_build_auth_validator_accepts_guid_audience() -> None:
    # The binding broadens the single configured App ID URI into both accepted
    # forms, so the bound validator admits the v2 bare-GUID aud. (Network-free:
    # the JWKS client is constructed lazily and never fetched here.)
    cfg = StackAuthConfig(enabled=True, tenant_id="tid", audience=f"api://{_GUID}")
    validator = build_auth_validator(cfg)
    assert isinstance(validator, EntraTokenValidator)
    # A *sequence* of both forms -- not the bare configured string. (A bare string
    # would pass the membership checks below by coincidental substring matching,
    # which is exactly the un-broadened state this guards against.)
    assert isinstance(validator._audience, (list, tuple)), validator._audience
    assert list(validator._audience) == [f"api://{_GUID}", _GUID]
