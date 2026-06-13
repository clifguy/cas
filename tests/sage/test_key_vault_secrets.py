"""Tests for Key Vault secret resolution (cloud profile, CAS-ADR-042).

The Azure SDK is mocked throughout -- no network, no credentials. ``fetch_secret``
defers its azure imports to call time, so patching ``SecretClient`` on the
``azure.keyvault.secrets`` module is picked up when the function runs (mirrors the
hosted Anthropic SDK pattern). A caller-injected ``credential`` keeps the real
``DefaultAzureCredential`` out of the construction path.

Test IDs follow KV-NNN.
"""

import pytest

from sage.secrets.key_vault import (
    ANTHROPIC_SECRET_NAME,
    KEY_VAULT_URI_ENV_VAR,
    fetch_secret,
    resolve_vault_uri,
)


def test_kv_001_resolve_vault_uri_returns_env_value():
    """The vault URI is read from the injected environment mapping."""
    env = {KEY_VAULT_URI_ENV_VAR: "https://kv.example/"}
    assert resolve_vault_uri(env) == "https://kv.example/"


def test_kv_002_resolve_vault_uri_missing_fails_closed():
    """An unset vault URI raises, naming the variable -- no partial startup.

    Anti-coincidental-pass: a resolver that returned an empty string or None
    would let a later fetch fail with an opaque SDK error; this asserts it
    raises here and names the coordinate the operator must set.
    """
    with pytest.raises(RuntimeError) as excinfo:
        resolve_vault_uri({})
    assert KEY_VAULT_URI_ENV_VAR in str(excinfo.value)


class _FakeSecret:
    def __init__(self, value: str | None) -> None:
        self.value = value


class _RecordingSecretClient:
    """Fake SecretClient capturing its vault/credential and requested names."""

    instances: list["_RecordingSecretClient"] = []

    def __init__(self, vault_url, credential, value="sk-test-value") -> None:
        self.vault_url = vault_url
        self.credential = credential
        self._value = value
        self.requested: list[str] = []
        _RecordingSecretClient.instances.append(self)

    def get_secret(self, name):
        self.requested.append(name)
        return _FakeSecret(self._value)


def test_kv_003_fetch_secret_returns_value_via_injected_client(monkeypatch):
    """fetch_secret builds a SecretClient over the credential and returns the
    secret's value, requesting exactly the asked-for name.

    Anti-coincidental-pass: the fake records the vault URI, credential, and the
    secret name it was asked for. A function that hard-coded a value, dropped
    the credential, or requested a different name would fail one of the
    assertions.
    """
    _RecordingSecretClient.instances = []
    sentinel_cred = object()
    monkeypatch.setattr("azure.keyvault.secrets.SecretClient", _RecordingSecretClient)

    value = fetch_secret("https://kv.example/", ANTHROPIC_SECRET_NAME, credential=sentinel_cred)

    assert value == "sk-test-value"
    client = _RecordingSecretClient.instances[-1]
    assert client.vault_url == "https://kv.example/"
    assert client.credential is sentinel_cred
    assert client.requested == [ANTHROPIC_SECRET_NAME]


def test_kv_004_fetch_secret_fails_closed_on_azure_error(monkeypatch):
    """An Azure SDK error is re-raised as a RuntimeError naming the vault and
    secret, with the original error chained as __cause__.

    Positive control: KV-003 proves a working client returns the value, so this
    test isolates the failure path rather than passing because nothing happened.
    """
    from azure.core.exceptions import ResourceNotFoundError

    class _FailingSecretClient:
        def __init__(self, vault_url, credential) -> None:
            pass

        def get_secret(self, name):
            raise ResourceNotFoundError("secret not found")

    monkeypatch.setattr("azure.keyvault.secrets.SecretClient", _FailingSecretClient)

    with pytest.raises(RuntimeError) as excinfo:
        fetch_secret("https://kv.example/", ANTHROPIC_SECRET_NAME, credential=object())

    message = str(excinfo.value)
    assert "https://kv.example/" in message
    assert ANTHROPIC_SECRET_NAME in message
    assert isinstance(excinfo.value.__cause__, ResourceNotFoundError)


def test_kv_005_empty_secret_fails_closed(monkeypatch):
    """A secret that resolves to an empty value fails closed rather than handing
    back a blank credential to the abstraction provider."""

    class _EmptySecretClient:
        def __init__(self, vault_url, credential) -> None:
            pass

        def get_secret(self, name):
            return _FakeSecret("")

    monkeypatch.setattr("azure.keyvault.secrets.SecretClient", _EmptySecretClient)

    with pytest.raises(RuntimeError) as excinfo:
        fetch_secret("https://kv.example/", ANTHROPIC_SECRET_NAME, credential=object())
    assert ANTHROPIC_SECRET_NAME in str(excinfo.value)
