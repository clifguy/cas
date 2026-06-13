// CAS cloud deployment — application managed identities module.
//
// Provisions the two user-assigned managed identities the cloud deployment
// profile (CAS-ADR-042) consumes: one for SAGE, one for the CAS BFF. They are
// created once here and shared — the Key Vault module grants them data-plane
// read, the relational-store module grants the SAGE identity a database role,
// and the container apps attach them at deploy time. Each identity's resource
// id, principal id, and client id are exposed as outputs so every downstream
// module composes against a stable principal rather than minting its own.
//
// Resource-group scoped (the Bicep default): the orchestrator deploys it with
// scope: rg.

@description('Azure region for the managed identities.')
param location string

@description('Short environment name, e.g. prod. Used in resource naming.')
param environmentName string

@description('Tags applied to every identity resource.')
param tags object

// User-assigned identity the SAGE container app runs as: reads its secrets from
// Key Vault and authenticates to Postgres, both by this identity.
resource sageIdentity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: 'id-sage-${environmentName}'
  location: location
  tags: tags
}

// User-assigned identity the CAS BFF container app runs as.
resource bffIdentity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: 'id-cas-bff-${environmentName}'
  location: location
  tags: tags
}

@description('Resource id of the SAGE managed identity (container apps attach this).')
output sageIdentityId string = sageIdentity.id

@description('Principal id of the SAGE managed identity (granted role assignments).')
output sageIdentityPrincipalId string = sageIdentity.properties.principalId

@description('Client id of the SAGE managed identity (runtime token acquisition).')
output sageIdentityClientId string = sageIdentity.properties.clientId

@description('Resource id of the CAS BFF managed identity (container apps attach this).')
output bffIdentityId string = bffIdentity.id

@description('Principal id of the CAS BFF managed identity (granted role assignments).')
output bffIdentityPrincipalId string = bffIdentity.properties.principalId

@description('Client id of the CAS BFF managed identity (runtime token acquisition).')
output bffIdentityClientId string = bffIdentity.properties.clientId
