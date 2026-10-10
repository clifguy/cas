#!/usr/bin/env bash
# Load the cloud profile's Key Vault secrets and wildcard TLS certificate
# (CAS-ADR-042). The Bicep Key Vault module provisions the vault and its RBAC
# access model but commits no secret material; this script is the executable
# substance of docs/process/key-vault-secrets.md — the one-time operator load.
#
# Secret material is read from the environment and unset immediately, never
# passed as a literal or committed, and never placed on a command line, where
# any local process could read it: each value reaches the Azure CLI and
# OpenSSL through a file readable only by this user, in a scratch directory
# removed when the script exits. Re-running sets a new secret version (the
# rotation path) for every secret but the ingress key, which is generated once
# and kept, so the gateway and SAGE keep agreeing on it.
#
# After each certificate import the expiry notice is set again
# (set-certificate-expiry-notice.sh, from CERT_EXPIRY_CONTACTS and the optional
# CERT_EXPIRY_NOTICE_DAYS), so a renewal re-asserts it.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
NOTICE="${SCRIPT_DIR}/set-certificate-expiry-notice.sh"

# The vault name is the keyVaultName deployment output. Supply it directly via
# KEY_VAULT_NAME, or set DEPLOYMENT_NAME to resolve it from the deployment.
KV="${KEY_VAULT_NAME:-}"
if [ -z "${KV}" ]; then
  : "${DEPLOYMENT_NAME:?set KEY_VAULT_NAME, or DEPLOYMENT_NAME to resolve it from deployment outputs}"
  KV="$(az deployment sub show --name "${DEPLOYMENT_NAME}" \
    --query 'properties.outputs.keyVaultName.value' -o tsv)"
fi

: "${ANTHROPIC_API_KEY:?set ANTHROPIC_API_KEY to the hosted abstraction provider key}"
: "${BFF_CLIENT_SECRET:?set BFF_CLIENT_SECRET to the BFF confidential-client secret}"
: "${WILDCARD_TLS_PFX_PATH:?set WILDCARD_TLS_PFX_PATH to the wildcard certificate bundle}"
: "${WILDCARD_TLS_PFX_PASSWORD:?set WILDCARD_TLS_PFX_PASSWORD to the bundle password}"
: "${CERT_EXPIRY_CONTACTS:?set CERT_EXPIRY_CONTACTS to the email addresses of the certificate owner, comma-separated}"
# Refuse malformed notice input now, before anything is written.
"${BASH:-bash}" "${NOTICE}" --validate-only

umask 077
# An explicit template: without one, macOS mktemp ignores TMPDIR.
scratch="$(mktemp -d "${TMPDIR:-/tmp}/kv-load.XXXXXX")"
trap 'rm -rf "${scratch}"' EXIT

# Write a value to a scratch file. printf is a shell builtin, so the value
# never appears in a process argument list.
to_file() {
  printf '%s' "$2" >"${scratch}/$1"
}

set_secret() {
  az keyvault secret set --vault-name "${KV}" --name "$1" \
    --file "${scratch}/$2" --encoding utf-8 --output none
}

# Abstraction-provider API key (read by SAGE at runtime via its managed identity).
to_file anthropic "${ANTHROPIC_API_KEY}"
unset ANTHROPIC_API_KEY
set_secret anthropic-api-key anthropic

# BFF OIDC client secret (read by the BFF at runtime via its managed identity).
to_file bff "${BFF_CLIENT_SECRET}"
unset BFF_CLIENT_SECRET
set_secret bff-client-secret bff

# Gateway ingress key: injected by API Management on every request it forwards
# and required by SAGE, so SAGE's public container ingress admits only the
# gateway. Generated when the vault holds none; an existing key is kept, since
# replacing it outside a planned rotation would refuse traffic until both sides
# pick up the new value. A failed lookup stops the run rather than reading as
# "absent".
existing_ingress_key="$(az keyvault secret list --vault-name "${KV}" \
  --query "[?name=='sage-ingress-key'].id" -o tsv)"
if [ -z "${existing_ingress_key}" ]; then
  # Through a variable, so the generator's trailing newline is dropped: the key
  # travels as a header value, which cannot carry one.
  ingress_key="$(openssl rand -hex 32)"
  to_file ingress "${ingress_key}"
  unset ingress_key
  set_secret sage-ingress-key ingress
fi

# Owned wildcard TLS certificate, imported from a PFX/PKCS#12 bundle. Both the
# bundle path and its password arrive through the environment; the password is
# handed on as a file (OpenSSL's file: source, the Azure CLI's @file value).
to_file pfx-password "${WILDCARD_TLS_PFX_PASSWORD}"
unset WILDCARD_TLS_PFX_PASSWORD

# The bundle MUST carry the full chain (leaf + issuing intermediate). Azure
# Container Apps serves the bound environment certificate's PFX bytes verbatim,
# so a leaf-only bundle makes the BFF custom domain fail strict TLS clients
# (curl error 60: certificate not trusted) even though APIM masks it for the
# SAGE host by rebuilding the chain. Refuse a leaf-only bundle here rather than
# import an endpoint that silently fails verification. See
# docs/process/key-vault-secrets.md for how to build a full-chain PFX.
_tls_cert_count="$(openssl pkcs12 -nokeys -in "$WILDCARD_TLS_PFX_PATH" \
  -passin "file:${scratch}/pfx-password" 2>/dev/null | grep -c 'BEGIN CERTIFICATE' || true)"
if [ "${_tls_cert_count:-0}" -lt 2 ]; then
  echo "ERROR: read ${_tls_cert_count:-0} certificate(s) from WILDCARD_TLS_PFX_PATH; a full chain (leaf + intermediate) is required so ACA serves a trusted chain. Rebuild the bundle per docs/process/key-vault-secrets.md (an older export may need 'openssl pkcs12 ... -legacy')." >&2
  exit 1
fi

az keyvault certificate import --vault-name "${KV}" --name wildcard-tls \
  --file "$WILDCARD_TLS_PFX_PATH" --password "@${scratch}/pfx-password" --output none

# Have the vault email the certificate's owner before expiry, naming this
# deployment as a holder. Set after every import, whether or not a new version
# keeps the previous policy.
KEY_VAULT_NAME="${KV}" "${BASH:-bash}" "${NOTICE}"
