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

**Codified as [`deploy/bootstrap/seed-vault-source.sh`](../../deploy/bootstrap/seed-vault-source.sh).** That script is the executable substance of this procedure (CAS Cloud Deployment Discipline, Principle 3); this runbook documents it.

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

## Privilege required

These are Microsoft Graph directory and SharePoint operations, distinct from the
Azure RBAC the deploy identity holds (`azure-deployment.md`):

- **The `Sites.Selected` app-role assignment (step 2) is itself the admin
  consent** for that application permission — assigning the app role to the
  identity's service principal *is* granting it. There is no separate
  `az ad app permission admin-consent` call (that command consents delegated
  scopes, as in `entra-app-registrations.md`). The POST to
  `servicePrincipals/.../appRoleAssignments` therefore requires a directory role
  that can consent to application permissions: **Privileged Role
  Administrator**, **Application Administrator**, or **Global Administrator**.
- **The per-site permission (step 3) and the seed upload (step 4)** require write
  access to the target site — a **site owner / member** or a SharePoint
  administrator role that confers it.

Run these while that elevated access is in hand; the grant then persists with
the managed identity and needs no standing elevation afterward.

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

### 0. Create the SharePoint site and document library

Create (or designate) the SharePoint site and the document library within it
that will hold the vault tree, then record the coordinates the later steps
consume:

- Create a SharePoint site through the SharePoint admin center (or
  `https://<tenant>.sharepoint.com` → *Create site*). Record its hostname
  (`<tenant>.sharepoint.com`) and server-relative path (`/sites/<name>`) as
  `SITE_HOSTNAME` and `SITE_PATH`.
- In that site, create the **document library** that holds the vault tree (the
  default *Documents* library is acceptable). Record its display name as
  `LIBRARY_NAME`.

The library is the root of the vault tree; each vault lives at
`<vaultSourceRootPath>/<vault_id>/` within it (the `vaultSourceRootPath` default
is `vaults`). No vault folders are created here by hand beyond the seed in
step 4 — SAGE is the sole writer of the tree thereafter (CAS-ADR-043).

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

### 4. Seed the test vault's configuration

Provisioning a cloud vault is an act against the store (CAS-ADR-043): place a
schema-valid `vault_config.yaml` directly into the library at
`<vaultSourceRootPath>/<vault_id>/vault_config.yaml` by an authorized writer, so
the sole-writer posture holds and the declaration is canonical on first
discovery. The committed seed for the disposable `test` vault is
[`deploy/test-vault/vault_config.yaml`](../../deploy/test-vault/vault_config.yaml);
its schema validity is gated by `tests/sage/test_cloud_test_vault_seed_config.py`.

```bash
az rest --method PUT \
  --uri "https://graph.microsoft.com/v1.0/drives/${DRIVE_ID}/root:/vaults/test/vault_config.yaml:/content" \
  --headers "Content-Type=text/yaml" \
  --body "@deploy/test-vault/vault_config.yaml"

# Confirm it landed:
az rest --method GET \
  --uri "https://graph.microsoft.com/v1.0/drives/${DRIVE_ID}/root:/vaults/test/vault_config.yaml" \
  --query name -o tsv
```

The `:/path:/content` upload creates the intermediate `vaults/test/` folders
implicitly. No source-hash or chain-head provenance attaches to the config
declaration itself — provenance tracks ingested documents, not the vault config.
(The path above assumes the default `vaults` root; adjust if `vaultSourceRootPath`
is overridden.)

### 5. Deploy and verify

Deploy with the resolved coordinates (`sharepointSiteId = ${SITE_ID}`,
`sharepointDriveId = ${DRIVE_ID}`) so the SAGE config selects
`vault_source_backend: document_store`. At startup SAGE enumerates the library,
finds `vaults/test/`, and loads its `vault_config.yaml` — the seeded vault is
discovered with no local vault root involved. Confirm:

- `admin_list_vaults` includes `test`, and `admin_get_vault_config` returns its
  configuration.
- Restart (or roll a new revision of) the SAGE container app and confirm `test`
  is rediscovered — the live proof that the vault survives the stateless
  compute's restart, read back from the document store rather than an ephemeral
  local root.

## Rotation and teardown

The grant follows the managed identity: deleting the SAGE identity removes the
service principal and with it the app-role assignment. To revoke access to a site
without deleting the identity, remove the per-site permission (the inverse of
step 3) — the `Sites.Selected` role alone then grants nothing.
