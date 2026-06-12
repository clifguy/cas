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

@description('Public hostname of the SAGE container app the APIM facade routes to.')
param sageBackendHostname string

@description('SAGE resource-server audience the facade JWT policy validates (api://<app-id>).')
param sageAudience string

@description('Publisher email for the API Management service (administrative contact).')
param publisherEmail string

@description('SKU of the API Management facade. Consumption is serverless and scale-to-zero.')
param apimSku string = 'Consumption'

@description('Object id of the Entra principal granted Postgres administrator. Supplied at deploy time; empty leaves the binding unset.')
param postgresAadAdminObjectId string = ''

@description('Display name of the Postgres Entra administrator principal.')
param postgresAadAdminPrincipalName string = ''

@description('Type of the Postgres Entra administrator principal: User, Group, or ServicePrincipal.')
param postgresAadAdminPrincipalType string = 'Group'

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
  }
}

// The API Management facade: the public edge for SAGE's REST and MCP surfaces.
// It validates Entra JWTs, serves the MCP OAuth discovery handshake, and keeps
// the maintenance mount off the public edge. The backend hostname and audience
// are resolved when the SAGE container app and Entra registration are concrete.
module apim 'modules/apim.bicep' = {
  name: 'apim'
  scope: rg
  params: {
    location: location
    environmentName: environmentName
    tags: tags
    sageBackendHostname: sageBackendHostname
    sageAudience: sageAudience
    publisherEmail: publisherEmail
    apimSku: apimSku
  }
}

// The relational store deploys into the same resource group and composes
// through the foundation's outputs: it integrates into the delegated Postgres
// subnet and links its private DNS zone to the hosting VNet.
module postgres 'modules/postgres.bicep' = {
  name: 'postgres'
  scope: rg
  params: {
    location: location
    environmentName: environmentName
    tags: tags
    delegatedSubnetId: foundation.outputs.postgresSubnetId
    vnetId: foundation.outputs.vnetId
    aadAdminObjectId: postgresAadAdminObjectId
    aadAdminPrincipalName: postgresAadAdminPrincipalName
    aadAdminPrincipalType: postgresAadAdminPrincipalType
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
