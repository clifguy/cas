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

### 3. Assign a least-privilege role

The deploy identity needs to create the resource group and the resources
within it. `Contributor` at the subscription scope is the least built-in
role that can create a resource group; once the group exists you may narrow
later deployments to a resource-group-scoped assignment.

```bash
az role assignment create \
  --assignee "$APP_ID" \
  --role Contributor \
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

## Adding an environment

The scaffold models a single cloud environment. To add another:

1. Copy `infra/main.bicepparam` to `infra/main.<env>.bicepparam` and set its
   `environmentName` / `resourceGroupName` / `location`.
2. Add a matching GitHub environment and a federated credential whose
   subject is `repo:<OWNER>/<REPO>:environment:<env>`.
3. Extend the workflow to select the new `.bicepparam` for that environment.
