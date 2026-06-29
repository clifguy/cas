# Infrastructure modules

Hosting-environment Bicep modules for the CAS cloud deployment profile
(CAS-ADR-042). The orchestrator at [`../main.bicep`](../main.bicep) targets
the subscription, creates the resource group, and deploys each module into
that group.

## Authoring convention

- **One concern per module.** Each Azure concern is a single
  `modules/<concern>.bicep` file (foundation networking + compute
  environment, the relational store, the secrets vault, the API facade, …).
- **Resource-group scoped.** Modules declare resources at resource-group
  scope. The orchestrator deploys them with `scope: rg`; a module does not
  re-create the resource group.
- **Parameterized, never hardcoded.** Take `location` and `tags` from the
  orchestrator. Identity coordinates (subscription, tenant, client ids) and
  secrets are never written into a module — they arrive as parameters,
  deployment-time variables, or managed-identity references.
- **Foundation first.** The foundation module establishes the shared
  network and compute environment that later modules consume via outputs;
  it is the first module wired into the orchestrator.
- **Composed through outputs.** A module that depends on another consumes
  the producer's `output` values through the orchestrator rather than
  reaching across module boundaries.

## Wiring a module

Add the module to [`../main.bicep`](../main.bicep), scoped to the resource
group, passing the orchestrator's `location`, `environmentName`, and `tags`:

```bicep
module foundation 'modules/foundation.bicep' = {
  name: 'foundation'
  scope: rg
  params: {
    location: location
    environmentName: environmentName
    tags: tags
  }
}
```

The foundation module exposes `vnetId`, `acaEnvironmentId`,
`acaEnvironmentDefaultDomain`, `acrLoginServer`, `acaInfraSubnetId`, and
`postgresSubnetId` as outputs for later modules (a relational store, an API
facade, …) to consume.

The relational-store module (`postgres.bicep`) deploys into the delegated
Postgres subnet and exposes `postgresServerFqdn`, `postgresDatabaseName`, and
`postgresServerName` for the cloud profile configuration to consume.

The identity module (`identity.bicep`) provisions the two user-assigned managed
identities the container apps run as (SAGE and the CAS BFF) and exposes, for
each, its resource id, principal id, and client id. The Key Vault module grants
those principals data-plane read; the relational-store module grants the SAGE
principal a database role; the container apps attach the identities by resource
id at deploy time.

The Key Vault module (`keyvault.bicep`) provisions the secrets vault under Azure
RBAC, granting the SAGE and CAS BFF identities read of the secrets and
certificates they consume, and exposes `keyVaultUri`, `keyVaultName`, and the
canonical `anthropicSecretName` / `tlsCertificateName` / `bffClientSecretName`.
Secret values and the wildcard TLS certificate are loaded out of band per
[`../../docs/process/key-vault-secrets.md`](../../docs/process/key-vault-secrets.md);
no secret material is committed.

## The API facade

`apim.bicep` provisions the public edge for SAGE (its REST and MCP surfaces):
an API Management service whose inbound policy validates Entra-issued JWTs
uniformly across every surface — the REST API, the ordinary MCP mount, and the
maintenance mount alike — and serves the MCP OAuth discovery handshake
(`/.well-known/oauth-protected-resource` plus the `WWW-Authenticate` challenge).
Authentication and authorization are the sole admin control; no surface is
denied at the edge. The CAS BFF does not go through the facade — it uses the
container ingress directly.

The module takes the orchestrator's `location`, `environmentName`, and `tags`,
plus `sageBackendHostname` (the SAGE container app the facade routes to),
`sageAudience` (the resource-server audience the JWT policy checks), and
`publisherEmail`; the SKU is the `apimSku` parameter (default `Consumption`).
The issuing tenant is derived from the deployment context — no identity GUID is
written into the module. The inbound policy is authored as versioned XML under
[`../policies/`](../policies/) and loaded with `loadTextContent`; the
environment-specific coordinates reach it as API Management named values.

The facade also binds the `sage` custom domain on its gateway endpoint: a
hostname configuration served with the owned wildcard certificate, referenced
versionlessly from Key Vault and read via the SAGE user-assigned managed identity
(attached to the service for this purpose). It takes `sageCustomDomain`,
`sageIdentityId`, `sageIdentityClientId`, and the certificate's `tlsCertSecretUri`.

## Custom domains and TLS

`custom-domains.bicep` binds the owned wildcard certificate to the Azure
Container Apps environment for the `cas` hostname: it references the environment
as `existing` and imports the certificate from Key Vault by reference
(`certificateKeyVaultProperties`), authenticated by the CAS BFF managed identity
— no certificate material is committed. It takes the orchestrator's `location`
and `tags` plus `acaEnvironmentName` (from the foundation), `tlsCertSecretUri`
and `tlsCertificateName` (from the Key Vault module), and `bffIdentityId` (from
the identity module), and exposes `casCertificateId` for the CAS BFF container
app to attach to its custom domain at deploy time. The `sage` side of the same
concern is the gateway hostname configuration in `apim.bicep` (above), since APIM
— not the container ingress — is SAGE's public edge. The owned base domain
(`baseDomain`) and the wildcard certificate it covers are published into the
operator's DNS provider per
[`../../docs/process/custom-domains-dns.md`](../../docs/process/custom-domains-dns.md).

## The container apps

`container-apps.bicep` declares the two Azure Container Apps the profile runs —
SAGE and the CAS BFF — into the foundation's ACA environment. Each app runs as
its own user-assigned managed identity (from the identity module) and pulls its
image from the registry by that identity: the module grants each identity
`AcrPull` on the foundation's ACR (referenced existing by `acrName`) and binds
the registry in the app's `registries` block, so no stored registry credential
is used. Images are pinned to the immutable `{registry}/{repo}:{imageTag}` form
(`imageTag` supplied at deploy time), never `latest`.

Ingress mirrors the facade decision. SAGE takes external container ingress on
port 8000; the module exposes its resulting `sageFqdn`, which the orchestrator
feeds to `apim.bicep`'s `sageBackendHostname` so the facade backend resolves from
a real value rather than a hand-substituted placeholder. The BFF takes external
container ingress on port 8001 and attaches its custom domain through the
environment certificate (`casCertificateId`) the custom-domains module produced.

Each app pins a warm minimum replica (`scale.minReplicas`, the `minReplicas`
parameter, default 1) — Azure Container Apps treats an unset value as 0, which
would idle the app at zero replicas and let the post-deploy preflight probe a
cold-starting (briefly unavailable) replica.

The SAGE container is explicitly sized at 2.0 vCPU / 4.0 GiB (a valid ACA
Consumption CPU:memory combo) so it can load the embedding model — which
initializes lazily during lifespan startup, on first vault content embedding —
without being OOM-killed. The ACA default of 0.5 vCPU / 1 GiB is too small for
the model's working set (the loaded model plus a worst-case embed batch peaks
around 2.5 GiB) and SIGKILLs the process before startup completes. The BFF
carries no model and stays at the ACA default.

Cloud-profile configuration is a YAML file the module assembles from the hosting
modules' outputs (profile, `storage_backend`, the abstraction provider/model, the
`postgres` block, and the `auth` audience/tenant) and projects into each app as a
mounted secret volume; `SAGE_CONFIG_PATH` points the runtime at it. The injected
config keys stay a subset of
[`../../docs/fs/sage/sage_core_config.schema.json`](../../docs/fs/sage/sage_core_config.schema.json)
(a drift guard in `tests/infra/test_container_apps.py` enforces this). Only
non-secret coordinates ride in the environment — `SAGE_KEY_VAULT_URI`, the
managed-identity `AZURE_CLIENT_ID`, and the BFF's Entra/SAGE-upstream
coordinates; the confidential credentials (the abstraction key, the Postgres
token, the BFF client secret) resolve from Key Vault or the managed identity at
runtime, never from the template. The module exposes `sageFqdn`, `bffFqdn`, and
each app's resource id for the orchestrator to surface.
