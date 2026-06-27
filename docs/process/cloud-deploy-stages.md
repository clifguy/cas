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
  server and the CAS BFF confidential client, grants admin consent, and **emits
  `sageAudience` and `bffOidcClientId`** for the parameter set.
- The CI deploy identity's own OIDC federation is established here too (it needs
  directory rights); see `docs/process/azure-deployment.md`.

Fill `infra/main.bicepparam.example` → `main.<tenant>.bicepparam` with the
emitted coordinates and the remaining tenant values.

### Stage 1 — Infrastructure deployment

`az deployment sub create` against `infra/main.bicep` with the tenant parameter
set. This provisions the resource group, network, identities, Key Vault,
Postgres, APIM, the container apps, and the custom-domain certificate binding.
The in-VNet Postgres role/schema bootstrap runs as part of this deployment (it is
data-plane SQL that cannot be declared as ARM). The deployment is idempotent;
re-running reconciles.

### Stage 2 — Post-deploy seed (secrets and vault source)

After the vault and the SAGE identity exist:

- `deploy/bootstrap/load-key-vault-secrets.sh` — loads the abstraction-provider
  key, the BFF client secret, and the wildcard TLS certificate into Key Vault.
- `deploy/bootstrap/seed-vault-source.sh` — grants the SAGE identity the
  site-scoped Microsoft Graph permission and seeds the test vault's
  configuration into the document library (CAS-ADR-043); **emits
  `sharepointSiteId` and `sharepointDriveId`**.

If the SharePoint ids were not known at Stage 1, fold them into the parameter set
and redeploy Stage 1 so the SAGE config binds the document-store vault source.

### Stage 3 — Convergence

Roll (or restart) the container apps so they pick up the loaded secrets and
self-bootstrap their database schema over their managed-identity connections. The
apps fail closed with a clear error if a required secret is missing, so an
unseeded vault surfaces immediately rather than degrading silently.

### Stage 4 — DNS publication (provider-agnostic, manual)

Azure has now emitted every hostname and the cas-side ownership token:

- `deploy/bootstrap/emit-dns-records.sh` — computes and prints the `sage` and
  `cas` CNAMEs and the `asuid` domain-ownership TXT.
- The operator publishes those records in the tenant's own DNS provider. No
  provider API is scripted; the tenants in scope span more than one provider.

The bindings complete once the records resolve: APIM serves `sage` and the
container ingress serves `cas`, each over the wildcard certificate.

### Stage 5 — Preflight

A single preflight probe checks every layer — edge routing, authentication,
storage, secrets, vault load, source store — and reports all failures at once,
each paired with an anti-coincidental control. Serial discovery of one broken
layer per redeploy is the failure mode the staged ordering and the preflight
exist to kill.

## Re-running

Every stage is idempotent. A re-run of any script reconciles rather than
duplicating: the Entra script looks up before creating, the secret load sets a
new version, the vault seed tolerates a pre-existing grant and replaces the
config in place, and the DNS emitter only reads and prints. Re-running the whole
sequence on an already-converged tenant is a no-op that re-proves convergence.
