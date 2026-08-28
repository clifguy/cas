// CAS cloud deployment — orchestrator.
//
// Targets the subscription so it can create the resource group that every
// hosting-environment module deploys into. Hosting-environment modules live
// under infra/modules/ and are wired in below as the cloud deployment
// profile (CAS-ADR-042) is built out. The foundation module is wired first; it
// establishes the shared network and compute environment that later modules
// consume through its outputs.

targetScope = 'subscription'

@description('Short environment name, e.g. prod. Used in resource naming and tags.')
param environmentName string

@description('Azure region for the resource group and the resources within it.')
param location string

@description('Name of the resource group that holds the deployment.')
param resourceGroupName string

@description('Tags applied to the resource group and inherited by modules.')
param tags object = {
  project: 'CAS'
  environment: environmentName
  managedBy: 'bicep'
}

@description('SAGE resource-server audience the facade JWT policy validates (api://<app-id>).')
param sageAudience string

@description('Publisher email for the API Management service (administrative contact).')
param publisherEmail string

@description('SKU of the API Management facade. Consumption is serverless and scale-to-zero.')
param apimSku string = 'Consumption'

@description('Immutable container image tag both apps pin to ({version}-{short-sha}); supplied at deploy time, never `latest`.')
param imageTag string

@description('Application (client) id of the CAS BFF confidential client registration (supplied at deploy time).')
param bffOidcClientId string

@description('Application (client) id of the pre-provisioned public MCP client (auth-code + PKCE, no secret) the DCR-compatibility facade registers back at /register (supplied at deploy time, CAS-ADR-042).')
param mcpClientId string

@description('Claude model identifier the hosted abstraction provider generates abstracts with.')
param abstractionModel string = 'claude-haiku-4-5'

@description('Owned base domain the custom hostnames derive from (e.g. example.com). The wildcard certificate *.<base-domain> covers both the cas and sage hostnames.')
param baseDomain string

@description('Object id of the Entra principal granted Postgres administrator. Supplied at deploy time; empty leaves the binding unset.')
param postgresAadAdminObjectId string = ''

@description('Display name of the Postgres Entra administrator principal.')
param postgresAadAdminPrincipalName string = ''

@description('Type of the Postgres Entra administrator principal: User, Group, or ServicePrincipal.')
param postgresAadAdminPrincipalType string = 'Group'

@description('Microsoft Graph site id of the SharePoint site that hosts the cloud vault tree (the document-store vault-source binding, CAS-ADR-043). Supplied at deploy time; empty leaves the binding unconfigured (the runbook grants the site-scoped permission and resolves this id).')
param sharepointSiteId string = ''

@description('Microsoft Graph drive id of the SharePoint document library that holds the cloud vault tree (CAS-ADR-043). Supplied at deploy time alongside the site id.')
param sharepointDriveId string = ''

@description('Folder path within the SharePoint document library the vault tree is rooted at (CAS-ADR-043).')
param vaultSourceRootPath string = 'vaults'

// Custom-domain hostnames derive from the owned base domain; the wildcard
// certificate (*.${baseDomain}) covers both. The certificate's Key Vault secret
// URL is built once from the vault module's outputs and shared by both bindings
// — versionless, so each binding follows certificate rotation.
var casHostname = 'cas.${baseDomain}'
var sageHostname = 'sage.${baseDomain}'
var tlsCertSecretUri = '${keyvault.outputs.keyVaultUri}secrets/${keyvault.outputs.tlsCertificateName}'

resource rg 'Microsoft.Resources/resourceGroups@2024-03-01' = {
  name: resourceGroupName
  location: location
  tags: tags
}

// Hosting-environment modules deploy into the resource group above, scoped to
// rg. The foundation module is first: it establishes the shared network and
// compute environment (VNet, ACA environment, Log Analytics, ACR) that later
// modules consume through its outputs.
module foundation 'modules/foundation.bicep' = {
  name: 'foundation'
  scope: rg
  params: {
    location: location
    environmentName: environmentName
    tags: tags
    bffIdentityId: identity.outputs.bffIdentityId
  }
}

// The API Management facade: the public edge for SAGE's REST and MCP surfaces.
// It validates Entra JWTs uniformly across every surface (the maintenance mount
// included) and serves the MCP OAuth discovery handshake. The backend hostname
// and audience are resolved when the SAGE container app and Entra registration
// are concrete.
module apim 'modules/apim.bicep' = {
  name: 'apim'
  scope: rg
  params: {
    location: location
    environmentName: environmentName
    tags: tags
    sageBackendHostname: containerApps.outputs.sageFqdn
    sageAudience: sageAudience
    mcpClientId: mcpClientId
    casAppUrl: 'https://${casHostname}'
    publisherEmail: publisherEmail
    apimSku: apimSku
    sageCustomDomain: sageHostname
    sageIdentityId: identity.outputs.sageIdentityId
    sageIdentityClientId: identity.outputs.sageIdentityClientId
    sageIdentityPrincipalId: identity.outputs.sageIdentityPrincipalId
    tlsCertSecretUri: tlsCertSecretUri
    logAnalyticsWorkspaceId: foundation.outputs.logAnalyticsWorkspaceId
  }
}

// The relational store deploys into the same resource group and composes
// through the foundation's outputs: it integrates into the delegated Postgres
// subnet and links its private DNS zone to the hosting VNet. By default its
// Entra administrator is the dedicated bootstrap identity (so the bootstrap job's
// token can administer the database, fully reproducibly with no deploy-time GUID);
// an operator-supplied `postgresAadAdminObjectId` overrides it when set.
module postgres 'modules/postgres.bicep' = {
  name: 'postgres'
  scope: rg
  params: {
    location: location
    environmentName: environmentName
    tags: tags
    delegatedSubnetId: foundation.outputs.postgresSubnetId
    vnetId: foundation.outputs.vnetId
    aadAdminObjectId: empty(postgresAadAdminObjectId)
      ? identity.outputs.bootstrapIdentityPrincipalId
      : postgresAadAdminObjectId
    aadAdminPrincipalName: empty(postgresAadAdminObjectId)
      ? identity.outputs.bootstrapIdentityName
      : postgresAadAdminPrincipalName
    aadAdminPrincipalType: empty(postgresAadAdminObjectId)
      ? 'ServicePrincipal'
      : postgresAadAdminPrincipalType
  }
}

// The application managed identities the container apps run as. Created before
// the Key Vault module so the vault can grant them data-plane read; their ids
// are also consumed by the relational-store and container-app modules.
module identity 'modules/identity.bicep' = {
  name: 'identity'
  scope: rg
  params: {
    location: location
    environmentName: environmentName
    tags: tags
  }
}

// The secrets vault and its RBAC access model. Composes through the orchestrator:
// it consumes the identity module's principal ids rather than reaching across the
// module boundary.
module keyvault 'modules/keyvault.bicep' = {
  name: 'keyvault'
  scope: rg
  params: {
    location: location
    tags: tags
    sagePrincipalId: identity.outputs.sageIdentityPrincipalId
    bffPrincipalId: identity.outputs.bffIdentityPrincipalId
  }
}

// The custom-domain certificate binding: imports the owned wildcard certificate
// into the ACA environment from Key Vault, by reference, for the CAS BFF
// container app to attach to its custom domain at deploy time. The companion
// `sage` hostname binding lives on the APIM facade above. Composes through the
// foundation, identity, and Key Vault module outputs.
module customDomains 'modules/custom-domains.bicep' = {
  name: 'custom-domains'
  scope: rg
  params: {
    location: location
    tags: tags
    acaEnvironmentName: foundation.outputs.acaEnvironmentName
    tlsCertSecretUri: tlsCertSecretUri
    tlsCertificateName: keyvault.outputs.tlsCertificateName
    bffIdentityId: identity.outputs.bffIdentityId
  }
}

// The SAGE and CAS BFF container apps. Composes through every hosting-environment
// module's outputs: it runs the images on the foundation's ACA environment,
// attaches the identity module's managed identities and grants them AcrPull on
// the foundation's registry, reads its cloud config from the Postgres and Key
// Vault coordinates, and binds the BFF custom domain to the custom-domains
// certificate. SAGE's resulting FQDN resolves the APIM facade backend above.
module containerApps 'modules/container-apps.bicep' = {
  name: 'container-apps'
  scope: rg
  params: {
    location: location
    environmentName: environmentName
    tags: tags
    acaEnvironmentId: foundation.outputs.acaEnvironmentId
    acrLoginServer: foundation.outputs.acrLoginServer
    acrName: foundation.outputs.acrName
    imageTag: imageTag
    sageIdentityId: identity.outputs.sageIdentityId
    sageIdentityClientId: identity.outputs.sageIdentityClientId
    sageIdentityPrincipalId: identity.outputs.sageIdentityPrincipalId
    bffIdentityId: identity.outputs.bffIdentityId
    bffIdentityClientId: identity.outputs.bffIdentityClientId
    bffIdentityPrincipalId: identity.outputs.bffIdentityPrincipalId
    keyVaultUri: keyvault.outputs.keyVaultUri
    bffClientSecretName: keyvault.outputs.bffClientSecretName
    postgresServerFqdn: postgres.outputs.postgresServerFqdn
    postgresDatabaseName: postgres.outputs.postgresDatabaseName
    sageAudience: sageAudience
    bffOidcClientId: bffOidcClientId
    sageHostname: sageHostname
    casHostname: casHostname
    casCertificateId: customDomains.outputs.casCertificateId
    abstractionModel: abstractionModel
    sharepointSiteId: sharepointSiteId
    sharepointDriveId: sharepointDriveId
    vaultSourceRootPath: vaultSourceRootPath
  }
}

// The in-VNet Postgres bootstrap job: an idempotent Container Apps Job that
// creates the application managed-identity database roles and pre-creates the
// extensions, from inside the VNet (the server has no public endpoint). It runs
// as the bootstrap identity — the server's Entra administrator (wired above) —
// and reuses the SAGE image. Composes through the foundation (ACA environment,
// registry), identity (bootstrap + application identities), and relational-store
// (server FQDN, database) module outputs.
module postgresBootstrap 'modules/postgres-bootstrap.bicep' = {
  name: 'postgres-bootstrap'
  scope: rg
  params: {
    location: location
    environmentName: environmentName
    tags: tags
    acaEnvironmentId: foundation.outputs.acaEnvironmentId
    acrLoginServer: foundation.outputs.acrLoginServer
    acrName: foundation.outputs.acrName
    imageTag: imageTag
    bootstrapIdentityId: identity.outputs.bootstrapIdentityId
    bootstrapIdentityClientId: identity.outputs.bootstrapIdentityClientId
    bootstrapIdentityPrincipalId: identity.outputs.bootstrapIdentityPrincipalId
    bootstrapIdentityName: identity.outputs.bootstrapIdentityName
    postgresServerFqdn: postgres.outputs.postgresServerFqdn
    postgresDatabaseName: postgres.outputs.postgresDatabaseName
    sageIdentityId: identity.outputs.sageIdentityId
    bffIdentityId: identity.outputs.bffIdentityId
  }
}

// In-VNet maintenance job (CAS-ADR-043/029). Declared by the deploy and started
// out-of-band by the dedicated maintenance workflow, which selects the operation
// (vault teardown, document purge, bulk abstract recovery) per invocation; runs
// as the SAGE identity,
// which already holds every grant the operations need (schema owner + SharePoint
// writer).
module maintenanceJob 'modules/maintenance-job.bicep' = {
  name: 'maintenance-job'
  scope: rg
  params: {
    location: location
    environmentName: environmentName
    tags: tags
    acaEnvironmentId: foundation.outputs.acaEnvironmentId
    acrLoginServer: foundation.outputs.acrLoginServer
    imageTag: imageTag
    sageIdentityId: identity.outputs.sageIdentityId
    sageIdentityClientId: identity.outputs.sageIdentityClientId
    postgresServerFqdn: postgres.outputs.postgresServerFqdn
    postgresDatabaseName: postgres.outputs.postgresDatabaseName
    sharepointSiteId: sharepointSiteId
    sharepointDriveId: sharepointDriveId
    vaultSourceRootPath: vaultSourceRootPath
    keyVaultUri: keyvault.outputs.keyVaultUri
    abstractionModel: abstractionModel
  }
}

@description('Provisioned resource group name, consumed by module deployments.')
output deployedResourceGroupName string = rg.name

@description('Resource id of the Azure Container Apps environment.')
output acaEnvironmentId string = foundation.outputs.acaEnvironmentId

@description('Login server host of the Azure Container Registry.')
output acrLoginServer string = foundation.outputs.acrLoginServer

@description('Public gateway URL of the API Management facade.')
output apimGatewayUrl string = apim.outputs.apimGatewayUrl

@description('Fully qualified domain name of the Postgres Flexible Server.')
output postgresServerFqdn string = postgres.outputs.postgresServerFqdn

@description('Name of the database SAGE connects to.')
output postgresDatabaseName string = postgres.outputs.postgresDatabaseName

@description('Data-plane URI of the Key Vault, consumed by the cloud profile configuration.')
output keyVaultUri string = keyvault.outputs.keyVaultUri

@description('Name of the Key Vault.')
output keyVaultName string = keyvault.outputs.keyVaultName

@description('Client id of the SAGE managed identity (runtime token acquisition).')
output sageIdentityClientId string = identity.outputs.sageIdentityClientId

@description('Client id of the CAS BFF managed identity (runtime token acquisition).')
output bffIdentityClientId string = identity.outputs.bffIdentityClientId

@description('Custom domain hostname of the container-ingress-fronted CAS BFF (the operator publishes its DNS records at deploy time).')
output casCustomDomain string = casHostname

@description('Custom domain hostname of the APIM-fronted SAGE edge.')
output sageCustomDomain string = sageHostname

@description('Resource id of the ACA environment wildcard certificate, attached to the CAS BFF custom domain at deploy time.')
output casCertificateId string = customDomains.outputs.casCertificateId

@description('Deterministic FQDN of the SAGE container app — the value the APIM facade backend resolves from.')
output sageContainerAppFqdn string = containerApps.outputs.sageFqdn

@description('Name of the in-VNet Postgres bootstrap job — the deploy pipeline starts it after the apply to create the application managed-identity database roles and pre-create the extensions.')
output bootstrapJobName string = postgresBootstrap.outputs.bootstrapJobName

@description('Name of the in-VNet maintenance job — the dedicated maintenance workflow starts it out-of-band after applying the per-invocation request to its environment.')
output maintenanceJobName string = maintenanceJob.outputs.maintenanceJobName

@description('Name of the SAGE container app — the deploy pipeline restarts it to converge the app tier after the bootstrap job runs.')
output sageContainerAppName string = containerApps.outputs.sageContainerAppName

@description('Name of the CAS BFF container app — the deploy pipeline restarts it to converge the app tier after the bootstrap job runs.')
output bffContainerAppName string = containerApps.outputs.bffContainerAppName
