#!/usr/bin/env bash
# Grant the SAGE managed identity the least-privilege, site-scoped Microsoft
# Graph permission and seed the validation vault's configuration into the
# document library (CAS-ADR-043). This is the executable substance of
# docs/process/sharepoint-vault-source.md — the one-time vault-source bootstrap
# for the stateless cloud compute.
#
# Idempotent: each grant is looked up first and posted only when absent, and the
# config upload is a create-or-replace PUT, so a re-run converges. Directory
# objects are selected by exact match, a lookup that finds nothing stops the
# run, and so does any failed call -- a refused grant is never mistaken for one
# already in place.
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

# Print the single value an `az` lookup returns, stopping the run when it
# returns none or more than one. Callers pass an exact match -- an OData `eq`
# filter, never `--display-name`, which matches by prefix -- and a query that
# projects every candidate rather than taking the first.
#
# This runs inside command substitutions, where bash does not apply `set -e`, so
# each failure exits explicitly: a failed lookup must stop the run, never read
# as "absent".
lookup_one() {
  local what="$1"
  shift
  local found
  found="$("$@" -o tsv)" || exit 1
  if [ -z "${found}" ]; then
    echo "ERROR: found no ${what}; resolve it before re-running." >&2
    exit 1
  fi
  if [ "$(printf '%s' "${found}" | grep -c .)" -gt 1 ]; then
    echo "ERROR: expected exactly one ${what}, found several; resolve the duplicate" \
      "before re-running." >&2
    exit 1
  fi
  printf '%s' "${found}"
}

# Resolve identity coordinates at run time -- no GUID is baked into this script.
# The Microsoft Graph service principal is selected by its exact display name
# rather than its well-known app id, so no identifier literal lives here.
SAGE_MI_CLIENT_ID="$(lookup_one "client id for identity ${SAGE_IDENTITY_NAME}" \
  az identity show -g "${RG}" -n "${SAGE_IDENTITY_NAME}" --query clientId)"
SAGE_MI_SP_ID="$(lookup_one "service principal for identity ${SAGE_IDENTITY_NAME}" \
  az ad sp list --filter "appId eq '${SAGE_MI_CLIENT_ID}'" --query '[].id')"
GRAPH_SP_ID="$(lookup_one "Microsoft Graph service principal" \
  az ad sp list --filter "displayName eq 'Microsoft Graph'" --query '[].id')"
SITES_SELECTED_ROLE_ID="$(lookup_one "Sites.Selected role on Microsoft Graph" \
  az ad sp show --id "${GRAPH_SP_ID}" --query "appRoles[?value=='Sites.Selected'].id")"

# 1. Resolve the SharePoint site and library coordinates.
SITE_ID="$(lookup_one "site ${SITE_HOSTNAME}${SITE_PATH}" \
  az rest --method GET \
  --uri "https://graph.microsoft.com/v1.0/sites/${SITE_HOSTNAME}:${SITE_PATH}" \
  --query id)"
DRIVE_ID="$(lookup_one "document library ${LIBRARY_NAME}" \
  az rest --method GET \
  --uri "https://graph.microsoft.com/v1.0/sites/${SITE_ID}/drives" \
  --query "value[?name=='${LIBRARY_NAME}'].id")"

# 2. Grant the Sites.Selected application role to the SAGE identity, unless the
# identity already holds it.
existing_role="$(az rest --method GET \
  --uri "https://graph.microsoft.com/v1.0/servicePrincipals/${SAGE_MI_SP_ID}/appRoleAssignments" \
  --query "value[?resourceId=='${GRAPH_SP_ID}' && appRoleId=='${SITES_SELECTED_ROLE_ID}'].id" \
  -o tsv)"
if [ -z "${existing_role}" ]; then
  az rest --method POST \
    --uri "https://graph.microsoft.com/v1.0/servicePrincipals/${SAGE_MI_SP_ID}/appRoleAssignments" \
    --body "{\"principalId\":\"${SAGE_MI_SP_ID}\",\"resourceId\":\"${GRAPH_SP_ID}\",\"appRoleId\":\"${SITES_SELECTED_ROLE_ID}\"}" \
    >/dev/null
fi

# 3. Grant the per-site write permission, scoped to the single site, unless the
# site already grants the identity write.
existing_grant="$(az rest --method GET \
  --uri "https://graph.microsoft.com/v1.0/sites/${SITE_ID}/permissions" \
  --query "value[?contains(grantedToIdentitiesV2[].application.id || \`[]\`, '${SAGE_MI_CLIENT_ID}') && contains(roles || \`[]\`, 'write')].id" \
  -o tsv)"
if [ -z "${existing_grant}" ]; then
  az rest --method POST \
    --uri "https://graph.microsoft.com/v1.0/sites/${SITE_ID}/permissions" \
    --body "{\"roles\":[\"write\"],\"grantedToIdentities\":[{\"application\":{\"id\":\"${SAGE_MI_CLIENT_ID}\"}}]}" \
    >/dev/null
fi

# 4. Seed the validation vault's configuration (create-or-replace upload). The
# committed seed is deploy/test-vault/vault_config.yaml; the :/content PUT
# creates the intermediate folders implicitly. The folder name below must match
# the seed's vault.id: discovery registers the vault under the id the config
# declares, not under the folder it was found in, so a mismatch loads a vault
# whose sources are addressed elsewhere rather than failing. A test holds the two
# together.
az rest --method PUT \
  --uri "https://graph.microsoft.com/v1.0/drives/${DRIVE_ID}/root:/${VAULT_SOURCE_ROOT_PATH}/cloud_validation/vault_config.yaml:/content" \
  --headers "Content-Type=text/yaml" \
  --body "@${repo_root}/deploy/test-vault/vault_config.yaml"

# Emit the coordinates for the deployment parameter set (main.bicepparam).
echo "# Paste into the tenant parameter set:"
echo "param sharepointSiteId = '${SITE_ID}'"
echo "param sharepointDriveId = '${DRIVE_ID}'"
