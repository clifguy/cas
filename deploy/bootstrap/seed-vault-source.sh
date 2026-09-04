#!/usr/bin/env bash
# Grant the SAGE managed identity the least-privilege, site-scoped Microsoft
# Graph permission and seed the validation vault's configuration into the
# document library (CAS-ADR-043). This is the executable substance of
# docs/process/sharepoint-vault-source.md — the one-time vault-source bootstrap
# for the stateless cloud compute.
#
# Idempotent: the app-role assignment and per-site grant tolerate a pre-existing
# state, and the config upload is a create-or-replace PUT, so a re-run converges.
# Run by an operator holding a directory role that can consent application
# permissions plus write access to the target site.
set -euo pipefail

: "${RG:?set RG to the resource group holding the SAGE identity}"
: "${SAGE_IDENTITY_NAME:?set SAGE_IDENTITY_NAME to the SAGE managed identity name}"
: "${SITE_HOSTNAME:?set SITE_HOSTNAME to <tenant>.sharepoint.com}"
: "${SITE_PATH:?set SITE_PATH to the server-relative site path, e.g. /sites/<name>}"
: "${LIBRARY_NAME:?set LIBRARY_NAME to the document library display name}"
VAULT_SOURCE_ROOT_PATH="${VAULT_SOURCE_ROOT_PATH:-vaults}"

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

# Resolve identity coordinates at run time — no GUID is baked into this script.
# The Microsoft Graph service principal is resolved by display name rather than
# its well-known app id, so no identifier literal lives here.
SAGE_MI_CLIENT_ID="$(az identity show -g "${RG}" -n "${SAGE_IDENTITY_NAME}" --query clientId -o tsv)"
SAGE_MI_SP_ID="$(az ad sp list --filter "appId eq '${SAGE_MI_CLIENT_ID}'" --query '[0].id' -o tsv)"
GRAPH_SP_ID="$(az ad sp list --display-name 'Microsoft Graph' --query '[0].id' -o tsv)"
SITES_SELECTED_ROLE_ID="$(az ad sp show --id "${GRAPH_SP_ID}" \
  --query "appRoles[?value=='Sites.Selected'].id | [0]" -o tsv)"

# 1. Resolve the SharePoint site and library coordinates.
SITE_ID="$(az rest --method GET \
  --uri "https://graph.microsoft.com/v1.0/sites/${SITE_HOSTNAME}:/sites/${SITE_PATH}" \
  --query id -o tsv)"
DRIVE_ID="$(az rest --method GET \
  --uri "https://graph.microsoft.com/v1.0/sites/${SITE_ID}/drives" \
  --query "value[?name=='${LIBRARY_NAME}'].id | [0]" -o tsv)"

# 2. Grant the Sites.Selected application role to the SAGE identity. A re-run
# hits an already-assigned conflict, which is tolerated.
az rest --method POST \
  --uri "https://graph.microsoft.com/v1.0/servicePrincipals/${SAGE_MI_SP_ID}/appRoleAssignments" \
  --body "{\"principalId\":\"${SAGE_MI_SP_ID}\",\"resourceId\":\"${GRAPH_SP_ID}\",\"appRoleId\":\"${SITES_SELECTED_ROLE_ID}\"}" \
  || true

# 3. Grant the per-site write permission, scoped to the single site.
az rest --method POST \
  --uri "https://graph.microsoft.com/v1.0/sites/${SITE_ID}/permissions" \
  --body "{\"roles\":[\"write\"],\"grantedToIdentities\":[{\"application\":{\"id\":\"${SAGE_MI_CLIENT_ID}\"}}]}" \
  || true

# 4. Seed the validation vault's configuration (create-or-replace upload). The
# committed seed is deploy/test-vault/vault_config.yaml; the :/content PUT
# creates the intermediate folders implicitly. The folder name below must match
# the seed's vault.id — discovery pairs them — and a test holds the two together.
az rest --method PUT \
  --uri "https://graph.microsoft.com/v1.0/drives/${DRIVE_ID}/root:/${VAULT_SOURCE_ROOT_PATH}/cloud_validation/vault_config.yaml:/content" \
  --headers "Content-Type=text/yaml" \
  --body "@${repo_root}/deploy/test-vault/vault_config.yaml"

# Emit the coordinates for the deployment parameter set (main.bicepparam).
echo "# Paste into the tenant parameter set:"
echo "param sharepointSiteId = '${SITE_ID}'"
echo "param sharepointDriveId = '${DRIVE_ID}'"
