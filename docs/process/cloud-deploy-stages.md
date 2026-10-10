# Cloud deployment — staged bring-up ordering

Bringing a cloud tenant up is not a single-pass `az deployment` followed by a
working system. Several steps create live state that the Bicep deployment either
depends on or cannot express, and a few cross a cloud boundary (Azure emits a
value; the operator publishes a DNS record; a binding then completes). This
document fixes the **order** those steps run in, so a fresh tenant converges
predictably rather than by repeated re-runs.

It is the staging companion to the per-tenant bootstrap scripts (`deploy/bootstrap/`)
and the parameter template (`infra/main.bicepparam.example`). The CI deploy
pipeline orchestrates these stages from committed code; this document is the
ordering it encodes. Authority: CAS-ADR-042 (deployment profiles), CAS-ADR-043
(document-store vault source), and the *CAS Cloud Deployment Discipline* steering
document. Per that discipline, operational provisioning is idempotent code, and
every stage below re-runs without corruption.

**What CI runs versus the manual floor.** A single operator-triggered dispatch of
the `infra` workflow (see [`azure-deployment.md`](azure-deployment.md)) carries
out **Stage 1 (provision + bootstrap job), Stage 3 (converge), and Stage 5
(preflight)** end to end for the selected tenant. **Stages 0, 2, and 4** — the
Entra registrations, the secret/vault-source seed, and the DNS publication —
remain the irreducible manual floor: they need directory rights or a tenant DNS
provider, so an operator runs them and folds the coordinates they emit into the
tenant's Environment variables.

## Why ordering is explicit

The dependency edges that force the sequence:

- The **Entra registrations** are directory objects created with directory-admin
  rights, deliberately outside the Bicep/CI path; their app ids are *inputs* to
  the deployment (the SAGE audience and the BFF client id).
- The **Key Vault secrets and certificate** can only be loaded after the vault
  exists, which is after the deployment.
- The **vault-source seed** can only grant the SAGE managed identity its
  site-scoped permission after that identity exists, which is after the
  deployment.
- The **cas DNS records** depend on the BFF container app's ingress FQDN and
  domain-ownership token, which exist only once the app is deployed.

## Stages

### Stage 0 — One-time directory bootstrap (pre-deploy)

Run once per tenant by an operator with directory-admin rights, before the first
deployment. Produces the identity coordinates the parameter set needs.

- `deploy/bootstrap/entra-app-registrations.sh` — creates the SAGE resource
  server, the CAS BFF confidential client, and the public MCP client, grants
  admin consent, and **emits `sageAudience`, `bffOidcClientId`, and
  `mcpClientId`** for the parameter set.
- The CI deploy identity's own OIDC federation is established here too (it needs
  directory rights); see `docs/process/azure-deployment.md`.

Fill `infra/main.bicepparam.example` → `main.<tenant>.bicepparam` with the
emitted coordinates and the remaining tenant values.

### Stage 1 — Infrastructure deployment

`az deployment sub create` against `infra/main.bicep` with the tenant parameter
set (supplied inline by CI from the Environment's variables). This provisions the
resource group, network, identities, Key Vault, Postgres, APIM, the container
apps, and the custom-domain certificate binding. The in-VNet Postgres role/schema
bootstrap is data-plane SQL that cannot be declared as ARM, so the deployment
declares it as an idempotent Container Apps job and CI **starts that job and waits
for it** as part of this stage. The deployment is idempotent; re-running
reconciles.

Key Vault access is granted per secret, and a grant scoped to a secret needs that
secret to exist. On a **new tenant**, the first run of this stage is an operator
run, because the CI deploy always uses the default: run the same
`az deployment sub create` command the workflow runs, with
`keyVaultSecretsLoaded=false` added to its parameters. The grants and the
gateway ingress key are held back, and SAGE receives no key and so checks none.
Load the secrets (Stage 2), then deploy through CI as usual.

### Stage 2 — Post-deploy seed (secrets and vault source)

After the vault and the SAGE identity exist:

- `deploy/bootstrap/load-key-vault-secrets.sh` — loads the abstraction-provider
  key, the BFF client secret, and the wildcard TLS certificate into Key Vault,
  and generates the gateway ingress key when the vault holds none. It also sets
  the certificate's expiry notice to `CERT_EXPIRY_CONTACTS`, the certificate
  owner's addresses, which it requires.
- `deploy/bootstrap/seed-vault-source.sh` — grants the SAGE identity the
  site-scoped Microsoft Graph permission and seeds the validation vault's
  configuration into the document library (CAS-ADR-043); **emits
  `sharepointSiteId` and `sharepointDriveId`**.

If the SharePoint ids were not known at Stage 1, fold them into the parameter set
and redeploy Stage 1 so the SAGE config binds the document-store vault source.

### Stage 3 — Convergence

CI restarts each container app's active revision so they pick up the loaded
secrets and self-bootstrap their database schema over their managed-identity
connections — now possible because Stage 1 created their database roles. The apps
fail closed with a clear error if a required secret is missing, so an unseeded
vault surfaces immediately rather than degrading silently.

### Stage 4 — DNS publication (provider-agnostic, manual)

Azure has now emitted every hostname and the cas-side ownership token:

- `deploy/bootstrap/emit-dns-records.sh` — computes and prints the `sage` and
  `cas` CNAMEs and the `asuid` domain-ownership TXT.
- The operator publishes those records in the tenant's own DNS provider. No
  provider API is scripted; the tenants in scope span more than one provider.

The bindings complete once the records resolve: APIM serves `sage` and the
container ingress serves `cas`, each over the wildcard certificate.

### Stage 5 — Preflight

CI runs a single preflight probe (`deploy/cloud-preflight.sh`) that checks every
layer — edge routing, authentication, storage, secrets, vault load, source
store — and reports all failures at once, each paired with an anti-coincidental
control; a non-zero exit fails the deploy. Serial discovery of one broken layer
per redeploy is the failure mode the staged ordering and the preflight exist to
kill.

## Moving an existing tenant to per-secret grants

A tenant deployed before Key Vault access was granted per secret holds vault-wide
**Key Vault Secrets User** and **Key Vault Certificate User** grants for the SAGE
and BFF identities, and its gateway runs as the SAGE identity. Moving it is a
one-time sequence:

1. **Before deploying,** run `deploy/bootstrap/load-key-vault-secrets.sh`. It
   generates `sage-ingress-key`, which the deployment's grants, the gateway's
   named value and SAGE's secret reference all name.
2. **Deploy** (Stage 1). The gateway moves to its own identity, every identity
   gains its per-secret grants, and SAGE starts requiring the ingress key that
   the gateway now injects.
3. **Remove the grants the template no longer declares.** An Azure deployment
   adds and updates but never deletes, so they stay in force until removed: the
   SAGE and BFF identities' vault-scope Key Vault grants, and the SAGE
   identity's Monitoring Metrics Publisher grant on the gateway's Application
   Insights resource, which the gateway's own identity now holds. Delete only
   those: the vault may hold other workloads' grants:

   ```bash
   KV_ID="$(az keyvault show -n "$KV" --query id -o tsv)"
   for identity in "id-sage-$ENV" "id-cas-bff-$ENV"; do
     principal="$(az identity show -g "$RG" -n "$identity" --query principalId -o tsv)"
     az role assignment delete --assignee "$principal" --scope "$KV_ID" \
       --role "Key Vault Secrets User"
     az role assignment delete --assignee "$principal" --scope "$KV_ID" \
       --role "Key Vault Certificate User"
   done
   sage_principal="$(az identity show -g "$RG" -n "id-sage-$ENV" --query principalId -o tsv)"
   az role assignment delete --assignee "$sage_principal" \
     --scope "$(az resource show -g "$RG" -n "appi-$ENV" \
       --resource-type Microsoft.Insights/components --query id -o tsv)" \
     --role "Monitoring Metrics Publisher"
   ```

   The gateway's identities need the same attention. API Management keeps a
   user-assigned identity the template no longer lists, so after the deployment
   the SAGE identity is still attached beside the gateway's own, and the gateway
   can still act as SAGE. Detach it once none of the places the deployment
   configures with an identity names it: the custom-domain certificate, the Key
   Vault-backed named values and the logger credential. The block below reads
   all three and detaches only when every read succeeded and none names the
   SAGE identity. A logger credential holds the client id directly or as a
   `{{named value}}` reference, so references are resolved before comparing.
   The identity's key is taken from the gateway's identity list exactly as
   Azure returns it. The block needs `jq` as well as the Azure CLI:

   ```bash
   APIM="$(az apim list -g "$RG" --query '[0].name' -o tsv)"
   APIM_ID="$(az apim show -g "$RG" -n "$APIM" --query id -o tsv)"
   SAGE_CLIENT_ID="$(az identity show -g "$RG" -n "id-sage-$ENV" --query clientId -o tsv)"
   blocked=""
   [ -n "$APIM_ID" ] && [ -n "$SAGE_CLIENT_ID" ] || blocked="$blocked gateway or identity lookup failed;"
   hosts="$(az apim show -g "$RG" -n "$APIM" \
     --query "hostnameConfigurations[?identityClientId=='$SAGE_CLIENT_ID'].hostName" -o tsv)" \
     || blocked="$blocked hostname read failed;"
   values="$(az apim nv list -g "$RG" --service-name "$APIM" \
     --query "[?keyVault.identityClientId=='$SAGE_CLIENT_ID'].name" -o tsv)" \
     || blocked="$blocked named-value read failed;"
   loggers="$(az rest --method get --url "$APIM_ID/loggers?api-version=2022-08-01" \
     --query "value[].properties.credentials.identityClientId" -o tsv)" \
     || blocked="$blocked logger read failed;"
   logger_ids="$(printf '%s\n' "$loggers" | while read -r ref; do
     case "$ref" in
       ("{{"*"}}")
         ref="${ref#"{{"}"
         v="$(az apim nv show-secret -g "$RG" --service-name "$APIM" \
           --named-value-id "${ref%"}}"}" --query value -o tsv)" && [ -n "$v" ] \
           && printf '%s\n' "$v" || echo "unresolved" ;;
       (*) printf '%s\n' "$ref" ;;
     esac
   done)"
   printf '%s\n' "$logger_ids" | grep -qx unresolved \
     && blocked="$blocked logger credential unresolved;"
   [ -n "$SAGE_CLIENT_ID" ] && printf '%s\n' "$logger_ids" | grep -qxF "$SAGE_CLIENT_ID" \
     && blocked="$blocked a logger uses id-sage-$ENV;"
   [ -z "$hosts$values" ] || blocked="$blocked still in use: $hosts $values;"
   if [ -n "$blocked" ]; then
     echo "Not detaching: $blocked" >&2
   else
     sage_key=""
     ids="$(az apim show -g "$RG" -n "$APIM" \
       --query identity.userAssignedIdentities -o json)" \
       && sage_key="$(printf '%s' "$ids" | jq -r --arg n "/id-sage-$ENV" \
         '(. // {}) | keys[] | select(endswith($n))')" \
       || blocked="identity read failed"
     if [ -n "$blocked" ]; then
       echo "Not detaching: $blocked" >&2
     elif [ -z "$sage_key" ]; then
       echo "id-sage-$ENV is not attached to the gateway"
     elif [ "$(printf '%s\n' "$sage_key" | wc -l)" -ne 1 ]; then
       echo "Not detaching: more than one attached identity ends in /id-sage-$ENV" >&2
     else
       az rest --method patch --url "$APIM_ID?api-version=2022-08-01" \
         --body "$(jq -n --arg k "$sage_key" \
           '{identity: {type: "UserAssigned", userAssignedIdentities: {($k): null}}}')" \
         --query "identity.userAssignedIdentities" -o json
     fi
   fi
   ```

   When it detaches, the response lists only `id-apim-$ENV`.

4. **Verify:** `az role assignment list --scope "$KV_ID" --assignee <principal>`
   returns nothing for either identity, the SAGE identity holds nothing on
   `appi-$ENV`, the gateway carries only `id-apim-$ENV`
   (`az apim show -g "$RG" -n "$APIM" --query identity.userAssignedIdentities`),
   the apps and both custom domains still serve, and a request to SAGE's
   container hostname without the ingress key is refused with 403.

## Re-running

Every stage is idempotent. A re-run of any script reconciles rather than
duplicating: the Entra script looks up before creating, the secret load sets a
new version, the vault seed tolerates a pre-existing grant and replaces the
config in place, and the DNS emitter only reads and prints. Re-running the whole
sequence on an already-converged tenant is a no-op that re-proves convergence.
