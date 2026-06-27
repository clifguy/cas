#!/usr/bin/env bash
# Load the cloud profile's Key Vault secrets and wildcard TLS certificate
# (CAS-ADR-042). The Bicep Key Vault module provisions the vault and its RBAC
# access model but commits no secret material; this script is the executable
# substance of docs/process/key-vault-secrets.md — the one-time operator load.
#
# Secret material is read from the environment and unset immediately, never
# passed as a literal or committed. Re-running sets a new secret version (the
# rotation path); the apps resolve the current version, so a re-run converges.
set -euo pipefail

# The vault name is the keyVaultName deployment output. Supply it directly via
# KEY_VAULT_NAME, or set DEPLOYMENT_NAME to resolve it from the deployment.
KV="${KEY_VAULT_NAME:-}"
if [ -z "${KV}" ]; then
  : "${DEPLOYMENT_NAME:?set KEY_VAULT_NAME, or DEPLOYMENT_NAME to resolve it from deployment outputs}"
  KV="$(az deployment sub show --name "${DEPLOYMENT_NAME}" \
    --query 'properties.outputs.keyVaultName.value' -o tsv)"
fi

# Abstraction-provider API key (read by SAGE at runtime via its managed identity).
: "${ANTHROPIC_API_KEY:?set ANTHROPIC_API_KEY to the hosted abstraction provider key}"
az keyvault secret set --vault-name "${KV}" --name anthropic-api-key \
  --value "$ANTHROPIC_API_KEY"
unset ANTHROPIC_API_KEY

# BFF OIDC client secret (read by the BFF at runtime via its managed identity).
: "${BFF_CLIENT_SECRET:?set BFF_CLIENT_SECRET to the BFF confidential-client secret}"
az keyvault secret set --vault-name "${KV}" --name bff-client-secret \
  --value "$BFF_CLIENT_SECRET"
unset BFF_CLIENT_SECRET

# Owned wildcard TLS certificate, imported from a PFX/PKCS#12 bundle. Both the
# bundle path and its password arrive through the environment.
: "${WILDCARD_TLS_PFX_PATH:?set WILDCARD_TLS_PFX_PATH to the wildcard certificate bundle}"
: "${WILDCARD_TLS_PFX_PASSWORD:?set WILDCARD_TLS_PFX_PASSWORD to the bundle password}"
az keyvault certificate import --vault-name "${KV}" --name wildcard-tls \
  --file "$WILDCARD_TLS_PFX_PATH" --password "$WILDCARD_TLS_PFX_PASSWORD"
unset WILDCARD_TLS_PFX_PASSWORD
