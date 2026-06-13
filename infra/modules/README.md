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
canonical `anthropicSecretName` / `tlsCertificateName`. Secret values and the
wildcard TLS certificate are loaded out of band per
[`../../docs/process/key-vault-secrets.md`](../../docs/process/key-vault-secrets.md);
no secret material is committed.

## The API facade

`apim.bicep` provisions the public edge for SAGE (its REST and MCP surfaces):
an API Management service whose inbound policy validates Entra-issued JWTs,
serves the MCP OAuth discovery handshake (`/.well-known/oauth-protected-resource`
plus the `WWW-Authenticate` challenge), and denies the maintenance mount so it
never reaches the backend. The CAS BFF does not go through the facade — it uses
the container ingress directly.

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
(`baseDomain`) and the wildcard certificate it covers are published into AWS
Route 53 by the operator per
[`../../docs/process/custom-domains-dns.md`](../../docs/process/custom-domains-dns.md).
