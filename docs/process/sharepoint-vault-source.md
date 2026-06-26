# SharePoint document-store vault-source binding — operator runbook

The cloud deployment profile persists each vault's configuration declaration to a
Microsoft 365 SharePoint document library over the Microsoft Graph API, reached
under the SAGE workload's managed identity (CAS-ADR-043). This is the durable
vault-source seam for the stateless cloud compute: with it in place, a cloud
vault survives a container restart or a new revision, where the local container
filesystem does not.

This runbook is the one-time, hand-run procedure that grants the SAGE managed
identity the **least-privilege, site-scoped** Microsoft Graph permission and
resolves the site and library coordinates the deployment needs. It is the
shippable, reviewable record of the grant even though the directory objects are
created against the tenant by hand. The infrastructure-as-code carries only the
non-secret coordinates (`sharepointSiteId`, `sharepointDriveId`,
`vaultSourceRootPath`), threaded into the SAGE cloud config by the container-apps
module.

## Why a runbook, not Bicep

The Microsoft Graph application-permission grant — the `Sites.Selected`
application role assigned to the SAGE identity's service principal, and the
per-site permission that scopes it to one site — is a Microsoft Graph directory
operation, not an Azure Resource Manager role assignment. Azure RBAC role
assignments are declarable in Bicep; Graph app-role assignments are not, in the
resource providers this deployment uses. The **Microsoft Graph Bicep extension**
(`Microsoft.Graph/appRoleAssignedTo`) was considered and **not adopted**: it is a
preview provider, this repository configures no Graph Bicep extension, and
introducing it would put the authoritative `az bicep build` CI gate at risk. So
the grant is a scripted `az` / Microsoft Graph procedure, mirroring the existing
`entra-app-registrations.md` and `key-vault-secrets.md` runbooks.

## Least privilege

`Sites.Selected` grants **no** site access on its own — unlike the tenant-wide
`Sites.ReadWrite.All`, which is never granted here. Access is conferred only by
the explicit per-site permission in step 3, scoped to the **single site** that
hosts the vault tree. SAGE remains the sole writer of the vault tree (CAS-ADR-043);
the grant confers write access to no other site and to no other identity.

## Prerequisites

- The SAGE user-assigned managed identity exists (the identity module is
  deployed). Resolve its service-principal object id and its client id into shell
  variables from the deployment outputs — they are tenant coordinates resolved at
  run time, never written into this runbook.
- A SharePoint site and a document library within it that will hold the vault
  tree. Record the site hostname, the server-relative site path, and the library
  display name as shell variables.

```bash
# Resolve identity coordinates at run time (no GUID is baked into this runbook).
SAGE_MI_CLIENT_ID="$(az identity show -g "$RG" -n "$SAGE_IDENTITY_NAME" --query clientId -o tsv)"
SAGE_MI_SP_ID="$(az ad sp list --filter "appId eq '$SAGE_MI_CLIENT_ID'" --query '[0].id' -o tsv)"

# The Microsoft Graph service principal, resolved by display name rather than by
# its well-known app id, so no identifier literal lives in this file.
GRAPH_SP_ID="$(az ad sp list --display-name 'Microsoft Graph' --query '[0].id' -o tsv)"
SITES_SELECTED_ROLE_ID="$(az ad sp show --id "$GRAPH_SP_ID" \
  --query "appRoles[?value=='Sites.Selected'].id | [0]" -o tsv)"
```

## Steps

### 1. Resolve the SharePoint site and library coordinates

```bash
SITE_ID="$(az rest --method GET \
  --uri "https://graph.microsoft.com/v1.0/sites/${SITE_HOSTNAME}:/sites/${SITE_PATH}" \
  --query id -o tsv)"
DRIVE_ID="$(az rest --method GET \
  --uri "https://graph.microsoft.com/v1.0/sites/${SITE_ID}/drives" \
  --query "value[?name=='${LIBRARY_NAME}'].id | [0]" -o tsv)"
```

These are the values the deployment needs: set `sharepointSiteId = ${SITE_ID}`
and `sharepointDriveId = ${DRIVE_ID}` in `infra/main.bicepparam` (or pass them at
deploy time). `vaultSourceRootPath` defaults to `vaults`.

### 2. Grant the `Sites.Selected` application role to the SAGE identity

```bash
az rest --method POST \
  --uri "https://graph.microsoft.com/v1.0/servicePrincipals/${SAGE_MI_SP_ID}/appRoleAssignments" \
  --body "{\"principalId\":\"${SAGE_MI_SP_ID}\",\"resourceId\":\"${GRAPH_SP_ID}\",\"appRoleId\":\"${SITES_SELECTED_ROLE_ID}\"}"
```

### 3. Grant the per-site write permission, scoped to the single site

```bash
az rest --method POST \
  --uri "https://graph.microsoft.com/v1.0/sites/${SITE_ID}/permissions" \
  --body "{\"roles\":[\"write\"],\"grantedToIdentities\":[{\"application\":{\"id\":\"${SAGE_MI_CLIENT_ID}\"}}]}"
```

This is the step that actually confers access, and it confers it to exactly one
site. Repeat steps 1 and 3 per site only if the deployment ever hosts vault trees
on more than one site; the default is a single site.

### 4. Deploy and verify

Deploy with the resolved coordinates, then confirm a cloud vault survives a
restart: create a vault through the maintenance surface, restart (or roll) the
SAGE container app, and confirm the vault is rediscovered at startup (it is read
back from the document store, not the ephemeral local root). The SAGE config's
`vault_source_backend: document_store` selection makes the document store the
durable seam; the container filesystem's vault root is no longer relied upon.

## Rotation and teardown

The grant follows the managed identity: deleting the SAGE identity removes the
service principal and with it the app-role assignment. To revoke access to a site
without deleting the identity, remove the per-site permission (the inverse of
step 3) — the `Sites.Selected` role alone then grants nothing.
