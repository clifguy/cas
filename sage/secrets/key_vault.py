"""Key Vault secret resolution for the cloud deployment profile (CAS-ADR-042).

The cloud profile reads its hosted-abstraction key from an Azure Key Vault at
runtime using the workload's managed identity, so no secret value lives in the
environment, the container image, or the repository. The vault's data-plane URI
is supplied by the environment (the container app injects it from the Key Vault
deployment output); the secret name is fixed by the Key Vault module's contract.
The Azure SDK imports are deferred to call time, so an on-box local-profile
process never loads them, mirroring the hosted abstraction SDK's lazy import.
"""

from __future__ import annotations

import os

# Fixed secret name the Key Vault module publishes for the hosted abstraction
# provider's API key. Downstream resolution reads it verbatim -- the secret name
# is a stable part of the vault's contract, not a configurable value.
ANTHROPIC_SECRET_NAME = "anthropic-api-key"  # noqa: S105 -- secret name, not a secret

# Environment variable carrying the Key Vault data-plane URI (for example
# "https://kv<hash>.vault.azure.net/"). The container app injects it from the
# Key Vault module's deployment output; it is a coordinate, not a secret.
KEY_VAULT_URI_ENV_VAR = "SAGE_KEY_VAULT_URI"


def resolve_vault_uri(environ: dict[str, str] | None = None) -> str:
    """Return the Key Vault data-plane URI from the environment, or fail closed.

    The cloud profile cannot resolve any secret without the vault coordinate, so
    an unset variable raises rather than degrading to a partial startup.
    """
    env = os.environ if environ is None else environ
    vault_uri = env.get(KEY_VAULT_URI_ENV_VAR)
    if not vault_uri:
        raise RuntimeError(
            f"{KEY_VAULT_URI_ENV_VAR} is required for the cloud profile but is "
            "unset; the container app injects it from the Key Vault deployment "
            "output."
        )
    return vault_uri


def fetch_secret(
    vault_uri: str,
    secret_name: str,
    *,
    credential: object | None = None,
) -> str:
    """Read one secret's current value from Key Vault via managed identity.

    Builds a ``SecretClient`` over the workload's managed-identity credential
    (``DefaultAzureCredential`` resolves the user-assigned identity from
    ``AZURE_CLIENT_ID`` in the environment) and returns the secret's current
    value. Any failure -- authentication, a missing or empty secret, an
    unreachable vault -- fails closed as a ``RuntimeError`` naming the vault and
    the secret, so a deployment against an unseeded or unreachable vault
    surfaces immediately rather than degrading silently.
    """
    from azure.core.exceptions import AzureError
    from azure.identity import DefaultAzureCredential
    from azure.keyvault.secrets import SecretClient

    cred = credential if credential is not None else DefaultAzureCredential()
    try:
        client = SecretClient(vault_url=vault_uri, credential=cred)
        value = client.get_secret(secret_name).value
    except AzureError as exc:
        raise RuntimeError(
            f"failed to read secret {secret_name!r} from Key Vault {vault_uri!r} "
            f"via managed identity: {exc}"
        ) from exc
    if not value:
        raise RuntimeError(
            f"secret {secret_name!r} in Key Vault {vault_uri!r} resolved empty; "
            "the cloud profile requires a non-empty value."
        )
    return value
