# Azure deployment

The CAS cloud deployment profile (CAS-ADR-042) is provisioned with Bicep
under [`infra/`](../../infra/) and deployed by the
[`infra`](../../.github/workflows/infra.yml) GitHub Actions workflow. Deploys
run from committed code through CI: an operator triggers the workflow and selects
the target tenant, and the run carries out the full staged bring-up. This runbook
is the deployment entry point and the one-time per-tenant setup the workflow
depends on. The staged ordering the run encodes is in
[`cloud-deploy-stages.md`](cloud-deploy-stages.md).

The orchestrator [`infra/main.bicep`](../../infra/main.bicep) targets the
**subscription** scope: it creates the resource group and deploys each
hosting-environment module into it. The workflow's `validate` job (Bicep compile
+ lint) runs on every change; the `build` and `deploy` jobs run only on an
operator-triggered dispatch and stay **dormant** until the selected tenant's
deploy identity is configured (they are gated on its `AZURE_CLIENT_ID` variable).

The *deploy* identity bootstrapped below is distinct from the *runtime* auth
identities. The Entra app registrations the cloud profile authenticates with —
SAGE as an OAuth resource server and the CAS BFF as a confidential client — are
a separate one-time bootstrap documented in
[`entra-app-registrations.md`](entra-app-registrations.md).

## Per-tenant parameterization

One tenant = one parameter set (the CAS Cloud Deployment Discipline). Everything
tenant-specific — the tenant and subscription identifiers, the deploy-identity
client id, the resource group, region, domain, audiences, object ids, and
document-library coordinates — is **configuration carried by the tenant's GitHub
Environment**, never code. Identity GUIDs and secrets stay out of the repository
and are supplied at deploy time. [`infra/main.bicepparam.example`](../../infra/main.bicepparam.example)
documents the full parameter surface; the deploy supplies each value from an
environment-scoped variable. Adding a tenant is authoring an Environment and its
variable set, not editing the stack.

## Deployment entry point

The authoritative deploy is **operator-triggered, per tenant, from committed
code**. From the repository's **Actions → infra → Run workflow**, select the
tenant's GitHub Environment and run it. The pipeline then, in order: builds and
pushes that tenant's images (version baked), runs a what-if gate, applies the
Bicep, starts and waits on the in-VNet Postgres bootstrap job, converges the app
tier, and runs the post-deploy preflight. Re-triggering is the convergence
vehicle — every stage is idempotent — so a multi-pass bring-up is re-running the
workflow, not hand-running deploys.

The PR and push-to-`main` runs are **validate-only** (`az bicep build`); the
apply happens only on dispatch, gated behind the selected Environment's required
reviewer.

The break-glass fallback — a manual apply from a clean checkout, not a
hand-patched working copy — runs the same template directly, passing the same
parameters inline from the tenant's values:

```bash
az deployment sub create \
  --name "$ENVIRONMENT_NAME" \
  --location "$LOCATION" \
  --template-file infra/main.bicep \
  --parameters \
    environmentName="$ENVIRONMENT_NAME" \
    location="$LOCATION" \
    resourceGroupName="$RESOURCE_GROUP_NAME" \
    sageAudience="$SAGE_AUDIENCE" \
    publisherEmail="$PUBLISHER_EMAIL" \
    imageTag="$IMAGE_TAG" \
    bffOidcClientId="$BFF_OIDC_CLIENT_ID" \
    mcpClientId="$MCP_CLIENT_ID" \
    baseDomain="$BASE_DOMAIN"
```

Subscription-scope deployments require a `--location` (it records the deployment
metadata; the resource group's own region is the `location` parameter). Keep the
`--location` flag in step with the `location` value.

## Per-tenant setup (one-time)

Run once per tenant by an operator with directory rights, before that tenant's
first deploy. Resolve the repository slug dynamically rather than hardcoding it,
and pick the Environment name you will deploy under:

```bash
REPO="$(gh repo view --json nameWithOwner -q .nameWithOwner)"   # <OWNER>/<REPO>
ENVIRONMENT="<env>"             # the tenant's GitHub Environment name
SUBSCRIPTION_ID="<SUBSCRIPTION_ID>"
```

### 1. Create the Entra application

The CI deploy identity is a Microsoft Entra workload identity federated to this
repository's GitHub OIDC token — there is **no stored client secret**. It cannot
be created by the pipeline that needs it, so create it once by hand in the
tenant's directory:

```bash
APP_ID="$(az ad app create --display-name "cas-deploy-${ENVIRONMENT}" --query appId -o tsv)"
az ad sp create --id "$APP_ID"
```

### 2. Add the federated credential

One subject, for the environment-gated deploy, templated to the resolved slug and
the Environment name — never a literal owner/repo pair:

```bash
az ad app federated-credential create --id "$APP_ID" --parameters "{
  \"name\": \"github-environment-${ENVIRONMENT}\",
  \"issuer\": \"https://token.actions.githubusercontent.com\",
  \"subject\": \"repo:${REPO}:environment:${ENVIRONMENT}\",
  \"audiences\": [\"api://AzureADTokenExchange\"]
}"
```

The subject, shown as a placeholder, is `repo:<OWNER>/<REPO>:environment:<env>`.

### 3. Assign the deployment roles

The deploy identity creates the resource group and the resources within it —
**and** the role assignments those resources carry (the Key Vault module grants
the SAGE and CAS BFF managed identities data-plane read on the vault, and the
container-app module grants them `AcrPull`). Creating a resource needs
`Contributor`; creating a role assignment needs
`Microsoft.Authorization/roleAssignments/write`, which `Contributor` does **not**
include. The deploy therefore needs both capabilities, or it fails with
`AuthorizationFailed` on the first module that assigns a role.

Grant `Contributor` **and** `User Access Administrator` at the subscription
scope — the least-privilege built-in pair that can both create the resource
group and assign roles within it (`Owner` is the single-role alternative, but
broader than required). Once the group exists you may narrow later deployments
to a resource-group-scoped assignment of the same pair.

```bash
az role assignment create \
  --assignee "$APP_ID" \
  --role Contributor \
  --scope "/subscriptions/${SUBSCRIPTION_ID}"
az role assignment create \
  --assignee "$APP_ID" \
  --role "User Access Administrator" \
  --scope "/subscriptions/${SUBSCRIPTION_ID}"
```

### 4. Create the Environment and set its variables

In the repository's **Settings → Environments**, create an environment named
`${ENVIRONMENT}` and add yourself as a required reviewer; the `deploy` job binds
this environment, so every apply waits for your approval, and its name must match
the federated subject from step 2.

Then set the tenant's parameter set as **environment-scoped variables** (not
secrets — these are non-sensitive identifiers — and not repository-wide, so each
tenant's set is isolated). The deploy identity and the Bicep parameters all live
here:

```bash
TENANT_ID="$(az account show --query tenantId -o tsv)"
gh variable set AZURE_CLIENT_ID       --env "$ENVIRONMENT" --body "$APP_ID"
gh variable set AZURE_TENANT_ID       --env "$ENVIRONMENT" --body "$TENANT_ID"
gh variable set AZURE_SUBSCRIPTION_ID --env "$ENVIRONMENT" --body "$SUBSCRIPTION_ID"

# Bicep parameter set (one value per tenant coordinate):
gh variable set ENVIRONMENT_NAME      --env "$ENVIRONMENT" --body "<environmentName>"
gh variable set LOCATION              --env "$ENVIRONMENT" --body "<region>"
gh variable set RESOURCE_GROUP_NAME   --env "$ENVIRONMENT" --body "<resourceGroupName>"
gh variable set SAGE_AUDIENCE         --env "$ENVIRONMENT" --body "<api://sage-app-id>"
gh variable set PUBLISHER_EMAIL       --env "$ENVIRONMENT" --body "<ops@example.org>"
gh variable set BFF_OIDC_CLIENT_ID    --env "$ENVIRONMENT" --body "<bff-client-id>"
gh variable set BASE_DOMAIN           --env "$ENVIRONMENT" --body "<example.org>"
# Optional coordinates (set once the bootstrap scripts emit them):
gh variable set POSTGRES_AAD_ADMIN_OBJECT_ID      --env "$ENVIRONMENT" --body "<object-id>"
gh variable set POSTGRES_AAD_ADMIN_PRINCIPAL_NAME --env "$ENVIRONMENT" --body "<principal-name>"
gh variable set SHAREPOINT_SITE_ID    --env "$ENVIRONMENT" --body "<site-id>"
gh variable set SHAREPOINT_DRIVE_ID   --env "$ENVIRONMENT" --body "<drive-id>"
# Preflight expectations:
gh variable set PREFLIGHT_EXPECTED_VAULTS --env "$ENVIRONMENT" --body "cas"
gh variable set PREFLIGHT_VAULT_SOURCE    --env "$ENVIRONMENT" --body "document_store"
```

Once the deploy identity variables are in place the `build` and `deploy` jobs
activate on the next dispatch, and the first run provisions the resource group —
exercising the full GitHub-OIDC → Entra workload-identity → `az deployment` chain
end to end.

### 5. Grant the deploy identity read on SAGE

The post-deploy preflight authenticates to SAGE *as the deploy identity*: it mints
a v2 `.default` token for the SAGE audience and reads `/sage_vaults` to prove the
backend is reachable. That token clears the APIM edge on issuer and audience
alone, but the SAGE backend additionally requires the token to carry a configured
role or scope. A service principal acquires no delegated scope, so the deploy
identity must hold an **application role** on the SAGE resource server, or the
preflight's authenticated read is rejected — and the `.default` mint can itself
fail when the principal holds no permission on SAGE at all.

Grant the **read-only `Sage.Reader`** application role — the minimal role the
preflight read needs. This is **least-privilege**: `Sage.Reader` confers read
across the SAGE surfaces and nothing more; the deploy identity is **not** made an
owner of the SAGE registration and is granted no write or admin role. Run this
after the [Entra app-registrations bootstrap](entra-app-registrations.md)
([Stage 0](cloud-deploy-stages.md)) has created the SAGE resource server, whose
service principal owns the role:

```bash
SAGE_APP_ID="$(az ad app list --display-name sage-resource-server --query '[0].appId' -o tsv)"
SAGE_SP_ID="$(az ad sp list --filter "appId eq '${SAGE_APP_ID}'" --query '[0].id' -o tsv)"
SAGE_READER_ROLE_ID="$(az ad sp show --id "${SAGE_SP_ID}" \
  --query "appRoles[?value=='Sage.Reader'].id | [0]" -o tsv)"
DEPLOY_SP_ID="$(az ad sp list --filter "appId eq '${APP_ID}'" --query '[0].id' -o tsv)"

az rest --method POST \
  --uri "https://graph.microsoft.com/v1.0/servicePrincipals/${DEPLOY_SP_ID}/appRoleAssignments" \
  --body "{\"principalId\":\"${DEPLOY_SP_ID}\",\"resourceId\":\"${SAGE_SP_ID}\",\"appRoleId\":\"${SAGE_READER_ROLE_ID}\"}" \
  2>/dev/null || true
```

The grant is idempotent — re-running tolerates a pre-existing assignment
(`|| true`) — so a re-provision or a fresh-tenant bring-up reaches the same state
without a manual hand-grant.

## Container images and the ACR push

The [`CI`](../../.github/workflows/ci.yml) workflow builds the SAGE and CAS BFF
images and runs the container smoke tests on every push and pull request, via the
shared reusable [`build-images`](../../.github/workflows/build-images.yml)
workflow with the push **disabled** — CI is the regression gate, not a publisher.
The push to a registry happens in the deploy pipeline, because each tenant has
its own Azure Container Registry: the `infra` workflow's `build` job calls the
same reusable workflow with the push **enabled** and the tenant's Environment, so
the artifact it provisions is built from the committed commit and lands in that
tenant's registry.

Images are tagged `{version}-{short-sha}` (the version is
`sage.build_info.RELEASE_VERSION`, so the registry tag matches the stamp the
running container reports) plus a moving `latest`; deployments pin the immutable
`{version}-{short-sha}` tag, never `latest`.

The push is dormant until the tenant's `AZURE_CLIENT_ID` and `ACR_LOGIN_SERVER`
variables are set. The registry login host is a Bicep output (`acrLoginServer`),
so it exists only after the first deploy. On a brand-new tenant the first
dispatch provisions the registry with the push dormant; set the variable from the
output and re-trigger (idempotent) to push and converge:

```bash
ACR_LOGIN_SERVER="$(az deployment sub show \
  --name "$ENVIRONMENT_NAME" \
  --query properties.outputs.acrLoginServer.value -o tsv)"
gh variable set ACR_LOGIN_SERVER --env "$ENVIRONMENT" --body "$ACR_LOGIN_SERVER"
```

### Push authorization

The registry's admin user is disabled by design (see
[`infra/modules/foundation.bicep`](../../infra/modules/foundation.bicep)), so
the push authenticates via the OIDC deploy identity rather than a stored
credential — `az acr login` exchanges the workload-identity token for a registry
token. The subscription-scope `Contributor` role assigned in the bootstrap
already grants data-plane push, so the push works as soon as the variable above
is set. **If you later narrow that grant** — the bootstrap's §3 anticipates
scoping it down once the resource group exists — re-grant push explicitly with
the least-privilege `AcrPush` data role scoped to the registry:

```bash
ACR_ID="$(az acr show --name "${ACR_LOGIN_SERVER%%.*}" --query id -o tsv)"
az role assignment create --assignee "$APP_ID" --role AcrPush --scope "$ACR_ID"
```

## Post-deploy preflight gate

Every deploy is gated by an automated preflight (CAS-ADR-042): after the stack
is applied, [`deploy/cloud-preflight.sh`](../../deploy/cloud-preflight.sh) probes
the live tenant and checks **every layer independently** — edge routing
(unauthenticated OAuth discovery `200`, unauthenticated `/mcp` `401`, an
authenticated request reaching the backend), SAGE liveness and vault-load count,
Postgres-backed retrieval, Key Vault secret resolution, provider-agnostic DNS
resolution, and SharePoint vault-source reachability — emitting a single
pass/fail matrix that names every failing layer. Each check carries an
anti-coincidental control (a `401`, for instance, is credited only when the
discovery-`200` control proves the edge is genuinely live, not a blanket edge
failure that makes everything look "as predicted"). It **reports; it never
fixes** — a verification artifact, not a deploy step.

The deploy pipeline runs it as the final stage, minting the bearer token from the
deploy identity. The harness is tenant-parameterized — no environment-specific
value is baked in. To run it by hand, supply the tenant's public host and an
operator-obtained bearer token by environment; the probe never mints a token of
its own:

```bash
AUTH_TOKEN="$(az account get-access-token \
  --scope "$SAGE_AUDIENCE/.default" --query accessToken -o tsv)" \
  BASE_DOMAIN=example.org \
  PREFLIGHT_EXPECTED_VAULTS=cas \
  PREFLIGHT_VAULT_SOURCE=document_store \
  deploy/cloud-preflight.sh
```

`SAGE_FQDN`/`CAS_FQDN` default to `sage.<BASE_DOMAIN>`/`cas.<BASE_DOMAIN>`; the
DNS verification record and Key Vault wildcard certificate are checked against
`BASE_DOMAIN`. Run `deploy/cloud-preflight.sh --dry-run` to print the full check
inventory (each check's prediction and its anti-coincidental control) without
touching the network; the script header documents every parameter and seam. A
non-zero exit means at least one layer failed — read the matrix, fix the layer,
redeploy, and re-run.

## Adding a tenant

Adding a tenant is configuration, not a code change: there is no per-tenant file
in the repository and no workflow edit.

1. Run the [per-tenant setup](#per-tenant-setup-one-time) for the new tenant —
   create its deploy identity and federated credential, assign the roles, and
   create its GitHub Environment with its variable set.
2. Dispatch the `infra` workflow against the new Environment. The first run may
   leave the image push dormant (the registry does not exist yet); set
   `ACR_LOGIN_SERVER` from the deploy output and re-trigger.
3. Run the bootstrap scripts ([`cloud-deploy-stages.md`](cloud-deploy-stages.md)
   Stages 0/2/4) for the manual floor — Entra registrations, secret/vault-source
   seed, and DNS publication — folding their emitted coordinates into the
   Environment's variables. Once the Stage 0 Entra registrations exist, complete
   the deploy-identity `Sage.Reader` grant (per-tenant setup step 5) so the
   preflight's authenticated read clears the SAGE backend.
4. Confirm the [post-deploy preflight gate](#post-deploy-preflight-gate) reports
   every layer green before treating the tenant as live.
