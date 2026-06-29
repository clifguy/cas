#!/usr/bin/env bash
# Create (or reconcile) the two Entra app registrations the cloud auth model
# depends on (CAS-ADR-042): SAGE as an OAuth resource server, and the CAS BFF as
# a confidential client that calls SAGE on-behalf-of an interactive user. This is
# the executable substance of docs/process/entra-app-registrations.md.
#
# One-time per tenant, run by an operator with directory-admin rights. Idempotent:
# every create is guarded by a lookup, and the scope/role ids are stable across
# runs when passed in the environment. On success it emits the sageAudience and
# bffOidcClientId coordinates for the deployment parameter set.
set -euo pipefail

# The BFF cloud hostname and OIDC callback path the redirect URI is built from.
# BFF_HOSTNAME is the default ingress FQDN until the custom domain is bound;
# AUTH_CALLBACK_PATH is fixed by the BFF login implementation.
: "${BFF_HOSTNAME:?set BFF_HOSTNAME to the BFF cloud hostname (e.g. the default ingress FQDN)}"
AUTH_CALLBACK_PATH="${AUTH_CALLBACK_PATH:-auth/callback}"

# 1. SAGE resource-server registration (lookup-then-create keeps it idempotent).
SAGE_APP_ID="$(az ad app list --display-name sage-resource-server \
  --query '[0].appId' -o tsv)"
if [ -z "${SAGE_APP_ID}" ]; then
  SAGE_APP_ID="$(az ad app create --display-name sage-resource-server \
    --sign-in-audience AzureADMyOrg --query appId -o tsv)"
fi
az ad app update --id "${SAGE_APP_ID}" --identifier-uris "api://${SAGE_APP_ID}"
az ad sp create --id "${SAGE_APP_ID}" 2>/dev/null || true

# Expose the single delegated scope and app role that authorize both the REST
# and MCP surfaces, and pin the access-token version to v2 so a token minted for
# this resource via the /.default scope endpoint carries the tenant's v2.0 issuer
# -- the issuer APIM validate-jwt and the SAGE backend require. Generate the
# scope/role ids once; keep them stable across runs by passing ACCESS_SCOPE_ID /
# SAGE_READER_ROLE_ID in the environment.
SAGE_OBJECT_ID="$(az ad app show --id "${SAGE_APP_ID}" --query id -o tsv)"
ACCESS_SCOPE_ID="${ACCESS_SCOPE_ID:-$(uuidgen)}"
SAGE_READER_ROLE_ID="${SAGE_READER_ROLE_ID:-$(uuidgen)}"

az rest --method PATCH \
  --url "https://graph.microsoft.com/v1.0/applications/${SAGE_OBJECT_ID}" \
  --headers 'Content-Type=application/json' \
  --body "{
    \"api\": {
      \"requestedAccessTokenVersion\": 2,
      \"oauth2PermissionScopes\": [{
        \"id\": \"${ACCESS_SCOPE_ID}\",
        \"value\": \"Sage.Access\",
        \"type\": \"User\",
        \"adminConsentDisplayName\": \"Access SAGE\",
        \"adminConsentDescription\": \"Access SAGE on behalf of the signed-in user.\",
        \"isEnabled\": true
      }]
    },
    \"appRoles\": [{
      \"id\": \"${SAGE_READER_ROLE_ID}\",
      \"allowedMemberTypes\": [\"User\"],
      \"value\": \"Sage.Reader\",
      \"displayName\": \"Sage.Reader\",
      \"description\": \"Read across the SAGE REST and MCP surfaces.\",
      \"isEnabled\": true
    }]
  }"

# 2. CAS BFF confidential-client registration (lookup-then-create).
BFF_APP_ID="$(az ad app list --display-name cas-bff --query '[0].appId' -o tsv)"
if [ -z "${BFF_APP_ID}" ]; then
  BFF_APP_ID="$(az ad app create --display-name cas-bff \
    --sign-in-audience AzureADMyOrg \
    --web-redirect-uris "https://${BFF_HOSTNAME}/${AUTH_CALLBACK_PATH}" \
    --query appId -o tsv)"
else
  az ad app update --id "${BFF_APP_ID}" \
    --web-redirect-uris "https://${BFF_HOSTNAME}/${AUTH_CALLBACK_PATH}"
fi
az ad sp create --id "${BFF_APP_ID}" 2>/dev/null || true

# Grant the BFF the delegated API permission onto SAGE, then admin-consent it —
# this is what makes the on-behalf-of exchange possible.
az ad app permission add --id "${BFF_APP_ID}" \
  --api "${SAGE_APP_ID}" \
  --api-permissions "${ACCESS_SCOPE_ID}=Scope"
az ad app permission admin-consent --id "${BFF_APP_ID}"

# Emit the coordinates for the deployment parameter set (main.bicepparam).
echo "# Paste into the tenant parameter set:"
echo "param sageAudience = 'api://${SAGE_APP_ID}'"
echo "param bffOidcClientId = '${BFF_APP_ID}'"
