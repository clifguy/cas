# Azure deployment

The CAS cloud deployment profile (CAS-ADR-042) is provisioned with Bicep
under [`infra/`](../../infra/) and deployed by the
[`infra`](../../.github/workflows/infra.yml) GitHub Actions workflow. This
runbook is the deployment entry point: the `az deployment` invocation and
parameter conventions, plus the one-time identity bootstrap the workflow
depends on.

The orchestrator [`infra/main.bicep`](../../infra/main.bicep) targets the
**subscription** scope: it creates the resource group and (as modules land)
deploys each hosting-environment module into it. Until the deploy identity
is bootstrapped, the workflow's `validate` job (Bicep compile + lint) runs
on every change, while the `what-if` and `deploy` jobs stay **dormant** —
they are gated on the `AZURE_CLIENT_ID` repository variable and are skipped
until it is set.

The *deploy* identity bootstrapped below is distinct from the *runtime* auth
identities. The Entra app registrations the cloud profile authenticates with —
SAGE as an OAuth resource server and the CAS BFF as a confidential client — are
a separate one-time bootstrap documented in
[`entra-app-registrations.md`](entra-app-registrations.md).

## Deployment entry point

Parameters live in `.bicepparam` files next to the orchestrator. The single
environment today is [`infra/main.bicepparam`](../../infra/main.bicepparam);
a second environment is a copy named `main.<env>.bicepparam` selected by the
workflow.

Subscription-scope deployments require a `--location` (it records the
deployment metadata; the resource group's own region is the `location`
parameter). Keep the `--location` flag in step with the `location` set in
the `.bicepparam`.

```bash
# Preview (no changes applied):
az deployment sub what-if \
  --location eastus2 \
  --template-file infra/main.bicep \
  --parameters infra/main.bicepparam

# Apply:
az deployment sub create \
  --location eastus2 \
  --template-file infra/main.bicep \
  --parameters infra/main.bicepparam
```

The workflow runs `what-if` on pull requests and `create` on pushes to
`main` (and manual dispatch), the apply gated behind the `azure-prod`
environment's required reviewer.

## One-time identity bootstrap

The CI deploy identity is a Microsoft Entra workload identity federated to
this repository's GitHub OIDC token — there is **no stored client secret**.
The identity cannot be created by the very pipeline that needs it, so this
bootstrap is run once, by hand, in your Azure tenant. Resolve the repository
slug dynamically rather than hardcoding it:

```bash
REPO="$(gh repo view --json nameWithOwner -q .nameWithOwner)"   # <OWNER>/<REPO>
SUBSCRIPTION_ID="<SUBSCRIPTION_ID>"
```

### 1. Create the Entra application

```bash
APP_ID="$(az ad app create --display-name cas-deploy --query appId -o tsv)"
az ad sp create --id "$APP_ID"
```

### 2. Add the federated credentials

Two subjects: one for the pull-request `what-if`, one for the
environment-gated deploy. Both are templated to the resolved slug — never a
literal owner/repo pair.

```bash
az ad app federated-credential create --id "$APP_ID" --parameters "{
  \"name\": \"github-pull-request\",
  \"issuer\": \"https://token.actions.githubusercontent.com\",
  \"subject\": \"repo:${REPO}:pull_request\",
  \"audiences\": [\"api://AzureADTokenExchange\"]
}"

az ad app federated-credential create --id "$APP_ID" --parameters "{
  \"name\": \"github-environment-azure-prod\",
  \"issuer\": \"https://token.actions.githubusercontent.com\",
  \"subject\": \"repo:${REPO}:environment:azure-prod\",
  \"audiences\": [\"api://AzureADTokenExchange\"]
}"
```

The two subjects, shown as placeholders, are:

- `repo:<OWNER>/<REPO>:pull_request`
- `repo:<OWNER>/<REPO>:environment:azure-prod`

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

### 4. Set the repository variables

These are repository **variables**, not secrets — they are non-sensitive
identifiers, and the workflow's dormant-job gate reads `AZURE_CLIENT_ID`.

```bash
TENANT_ID="$(az account show --query tenantId -o tsv)"
gh variable set AZURE_CLIENT_ID --body "$APP_ID"
gh variable set AZURE_TENANT_ID --body "$TENANT_ID"
gh variable set AZURE_SUBSCRIPTION_ID --body "$SUBSCRIPTION_ID"
```

### 5. Create the approval environment

In the repository's **Settings → Environments**, create an environment named
`azure-prod` and add yourself as a required reviewer. The `deploy` job binds
this environment, so every apply waits for your approval. The environment's
name must match the federated subject from step 2.

Once these are in place the `what-if` and `deploy` jobs activate on the next
run, and the first `az deployment sub create` provisions the resource group
— exercising the full GitHub-OIDC → Entra workload-identity → `az deployment`
chain end to end.

## Container image push to ACR

The [`CI`](../../.github/workflows/ci.yml) workflow's `container` job builds the
SAGE and CAS BFF images and runs the container smoke tests on every push and
pull request. Pushing the resulting tagged images to the Azure Container
Registry created by the foundation module is **dormant** until both the
`AZURE_CLIENT_ID` (set in the bootstrap above) and `ACR_LOGIN_SERVER`
repository variables are set — the same dormant-until-configured pattern the
`infra` workflow uses. Images are tagged `{version}-{short-sha}` (the version
is `sage.build_info.RELEASE_VERSION`, so the registry tag matches the stamp the
running container reports) plus a moving `latest`; pin deployments to the
immutable `{version}-{short-sha}` tag, never `latest`.

The registry login host is a Bicep output (`acrLoginServer`), so it exists only
after the first deploy. Set the variable from that output once the registry is
provisioned:

```bash
ACR_LOGIN_SERVER="$(az deployment sub show \
  --name main \
  --query properties.outputs.acrLoginServer.value -o tsv)"
gh variable set ACR_LOGIN_SERVER --body "$ACR_LOGIN_SERVER"
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

## Adding an environment

The scaffold models a single cloud environment. To add another:

1. Copy `infra/main.bicepparam` to `infra/main.<env>.bicepparam` and set its
   `environmentName` / `resourceGroupName` / `location`.
2. Add a matching GitHub environment and a federated credential whose
   subject is `repo:<OWNER>/<REPO>:environment:<env>`.
3. Extend the workflow to select the new `.bicepparam` for that environment.
