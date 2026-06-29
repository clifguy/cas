"""Tests for the cloud deployment profile's secret-resolving bindings (CAS-ADR-042).

The cloud profile resolves its secrets from the managed secret store via the
workload's managed identity. These tests mock the Key Vault fetch, the Anthropic
SDK, and the managed-identity credential so nothing touches Azure or a database;
they prove the abstraction binding passes a fetched key straight to the provider
(never the environment) and fails closed, and that the storage binding
authenticates by token with no env password.

Test IDs follow CLD-NNN.
"""

import os

import pytest

from sage.adapters.stubs import StubAbstractionProvider
from sage.config import (
    SageCoreConfig,
    StackAbstractionConfig,
    StackPostgresConfig,
)
from sage.mcp_init import (
    _cloud_abstraction_binding,
    _cloud_auth_binding,
    _cloud_storage_binding,
)


def _anthropic_cloud_config() -> SageCoreConfig:
    return SageCoreConfig(
        profile="cloud",
        abstraction=StackAbstractionConfig(provider="anthropic", model="claude-haiku-4-5"),
    )


# ---------------------------------------------------------------------------
# Cloud abstraction binding
# ---------------------------------------------------------------------------


def test_cld_001_abstraction_binding_fetches_key_and_passes_it_not_env(monkeypatch):
    """The cloud abstraction binding fetches the key from Key Vault and hands it
    to the Anthropic provider directly -- and never writes it to the environment.

    Anti-coincidental-pass: the recorder captures the model id and api_key handed
    to the provider, and the test asserts ANTHROPIC_API_KEY is absent from the
    environment afterward. A binding that exported the key to os.environ (the
    'no secrets in the environment' criterion's failure mode) would fail the last
    assertion; one that ignored the fetched key would fail the api_key assertion.
    """
    monkeypatch.delenv("SAGE_TEST_STUB_PROVIDERS", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr(
        "sage.secrets.key_vault.resolve_vault_uri", lambda environ=None: "https://kv.example/"
    )
    monkeypatch.setattr(
        "sage.secrets.key_vault.fetch_secret",
        lambda vault_uri, secret_name, **kw: "sk-from-vault",
    )

    captured: dict = {}

    class _RecordingProvider:
        def __init__(self, model_id, api_key=None):
            captured["model_id"] = model_id
            captured["api_key"] = api_key

    monkeypatch.setattr(
        "sage.adapters.abstraction_anthropic.AnthropicAbstractionProvider", _RecordingProvider
    )

    provider = _cloud_abstraction_binding(_anthropic_cloud_config())

    assert isinstance(provider, _RecordingProvider)
    assert captured["model_id"] == "claude-haiku-4-5"
    assert captured["api_key"] == "sk-from-vault"
    assert "ANTHROPIC_API_KEY" not in os.environ


def test_cld_002_abstraction_binding_honors_stub_override_no_key_vault(monkeypatch):
    """Under SAGE_TEST_STUB_PROVIDERS=1 the binding returns the stub provider and
    never reaches Key Vault -- keeping the suite offline (F-8).

    Anti-coincidental-pass: fetch_secret is rigged to raise; reaching it (a
    missing stub short-circuit) would surface as that error instead of a stub.
    """
    monkeypatch.setenv("SAGE_TEST_STUB_PROVIDERS", "1")

    def _boom(*args, **kwargs):
        raise AssertionError("fetch_secret must not be called under the stub override")

    monkeypatch.setattr("sage.secrets.key_vault.fetch_secret", _boom)

    provider = _cloud_abstraction_binding(_anthropic_cloud_config())
    assert isinstance(provider, StubAbstractionProvider)


def test_cld_003_abstraction_binding_fails_closed_on_key_vault_error(monkeypatch):
    """A Key Vault fetch failure propagates as a clear error -- the cloud stack
    fails to start rather than running without an abstraction key."""
    monkeypatch.delenv("SAGE_TEST_STUB_PROVIDERS", raising=False)
    monkeypatch.setattr(
        "sage.secrets.key_vault.resolve_vault_uri", lambda environ=None: "https://kv.example/"
    )

    def _fail(vault_uri, secret_name, **kw):
        raise RuntimeError(f"failed to read secret {secret_name!r} from Key Vault {vault_uri!r}")

    monkeypatch.setattr("sage.secrets.key_vault.fetch_secret", _fail)

    with pytest.raises(RuntimeError):
        _cloud_abstraction_binding(_anthropic_cloud_config())


def test_cld_004_abstraction_binding_rejects_non_hosted_provider(monkeypatch):
    """A non-hosted abstraction provider (e.g. local-mlx) fails closed: the cloud
    image ships no local MLX runtime, so the misconfiguration surfaces at
    startup rather than attempting an impossible load."""
    monkeypatch.delenv("SAGE_TEST_STUB_PROVIDERS", raising=False)
    cfg = SageCoreConfig(
        profile="cloud",
        abstraction=StackAbstractionConfig(provider="local-mlx", model="mlx-community/test"),
    )
    with pytest.raises(ValueError) as excinfo:
        _cloud_abstraction_binding(cfg)
    assert "anthropic" in str(excinfo.value)


# ---------------------------------------------------------------------------
# Cloud storage binding
# ---------------------------------------------------------------------------


def test_cld_005_storage_binding_uses_token_auth_and_suppresses_env_password(monkeypatch):
    """The cloud storage binding builds a Postgres provisioner with a token-auth
    connection class and no env password -- managed-identity auth, not a fallback
    to $SAGE_PG_PASSWORD.

    Anti-coincidental-pass: SAGE_PG_PASSWORD is set in the environment; the
    composed conn kwargs are asserted to carry no password (and the configured
    user is preserved). A binding that left read_env_password=True would leak the
    env password into the kwargs and fail.
    """
    monkeypatch.delenv("SAGE_TEST_STORAGE_BACKEND", raising=False)
    monkeypatch.setenv("SAGE_PG_PASSWORD", "envpw")
    # Avoid constructing a real azure credential; the connection class is never
    # exercised in this structural test.
    monkeypatch.setattr(
        "sage.storage.postgres.managed_identity.get_postgres_credential", lambda: object()
    )

    from sage.storage.postgres.pool import build_conn_kwargs
    from sage.storage_binding import (
        PostgresVaultStorageProvisioner,
        build_stack_storage_provisioner,
    )

    cfg = SageCoreConfig(
        profile="cloud",
        storage_backend="postgres",
        postgres=StackPostgresConfig(host="db.example", user="svc", sslmode="require"),
    )
    prov = build_stack_storage_provisioner(cfg, managed_identity=True)

    assert isinstance(prov, PostgresVaultStorageProvisioner)
    assert prov._connection_class is not None
    assert prov._read_env_password is False

    kwargs = build_conn_kwargs(prov._connection_params(), prov._conn_environ)
    assert "password" not in kwargs
    assert kwargs["user"] == "svc"


def test_cld_005a_storage_binding_skips_workload_extension_creation(monkeypatch):
    """The cloud storage binding builds the provisioner with extension creation
    OFF: under managed identity the unprivileged workload cannot issue an
    untrusted CREATE EXTENSION (Azure rejects it from any role outside
    azure_pg_admin, even with IF NOT EXISTS, even when the extension is already
    present), so the per-vault self-bootstrap relies on the admin-pre-created
    extensions and creates only its schema and tables.

    Anti-coincidental-pass: paired with STO-007 (the local binding leaves
    creation ON), a binding that did not flip the flag under managed identity
    would leave `_create_extensions` True and fail here.
    """
    monkeypatch.delenv("SAGE_TEST_STORAGE_BACKEND", raising=False)
    # Avoid constructing a real azure credential; the connection class is never
    # exercised in this structural test.
    monkeypatch.setattr(
        "sage.storage.postgres.managed_identity.get_postgres_credential", lambda: object()
    )

    from sage.storage_binding import (
        PostgresVaultStorageProvisioner,
        build_stack_storage_provisioner,
    )

    cfg = SageCoreConfig(
        profile="cloud",
        storage_backend="postgres",
        postgres=StackPostgresConfig(host="db.example", user="svc", sslmode="require"),
    )
    prov = build_stack_storage_provisioner(cfg, managed_identity=True)

    assert isinstance(prov, PostgresVaultStorageProvisioner)
    assert prov._create_extensions is False


def test_cld_006_storage_binding_honors_embedded_override(monkeypatch):
    """With the embedded storage override set, the cloud storage binding returns
    the embedded provisioner -- the test suite never needs a managed identity."""
    monkeypatch.setenv("SAGE_TEST_STORAGE_BACKEND", "embedded")

    from sage.storage_binding import (
        EmbeddedVaultStorageProvisioner,
        build_stack_storage_provisioner,
    )

    cfg = SageCoreConfig(
        profile="cloud",
        storage_backend="postgres",
        postgres=StackPostgresConfig(host="db.example", user="svc"),
    )
    prov = build_stack_storage_provisioner(cfg, managed_identity=True)
    assert isinstance(prov, EmbeddedVaultStorageProvisioner)


def test_cld_008_storage_binding_delegates_with_managed_identity(monkeypatch):
    """`_cloud_storage_binding` routes through `build_stack_storage_provisioner`
    with `managed_identity=True` -- the selector that turns on token auth.

    Anti-coincidental-pass: the recorder captures the `managed_identity` kwarg by
    late binding (mirroring PRF-008); a binding that dropped the flag (falling
    back to the env-password Postgres path) would record `False`.
    """
    captured: dict = {}

    def _recorder(stack_config, *, managed_identity=False):
        captured["managed_identity"] = managed_identity
        return "PROVISIONER_SENTINEL"

    monkeypatch.setattr("sage.mcp_init.build_stack_storage_provisioner", _recorder)

    result = _cloud_storage_binding(_anthropic_cloud_config())
    assert result == "PROVISIONER_SENTINEL"
    assert captured["managed_identity"] is True


# ---------------------------------------------------------------------------
# Cloud auth binding
# ---------------------------------------------------------------------------


def test_cld_007_auth_binding_delegates_to_build_auth_validator(monkeypatch):
    """The cloud auth binding routes through build_auth_validator (the same
    factory the local binding uses; no secret material) by module-global name.

    Anti-coincidental-pass: a captured-reference binding would not return the
    monkeypatched sentinel, mirroring the delegation guards PRF-006/008 pin for
    the abstraction and storage seams.
    """
    sentinel = object()
    monkeypatch.setattr("sage.mcp_init.build_auth_validator", lambda _auth: sentinel)

    assert _cloud_auth_binding(SageCoreConfig(profile="cloud")) is sentinel
