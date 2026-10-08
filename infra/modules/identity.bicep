// CAS cloud deployment — application managed identities module.
//
// Provisions the user-assigned managed identities the cloud deployment profile
// (CAS-ADR-042) consumes: one for SAGE, one for the CAS BFF, one for the API
// Management gateway, and the relational store's bootstrap identity. They are
// created once here and shared — the Key Vault module grants each the secrets
// it reads, the relational-store module grants the SAGE identity a database
// role, and the container apps and gateway attach them at deploy time. Each identity's resource
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

// User-assigned identity the API Management gateway runs as. It reads only the
// TLS certificate and the ingress key it injects, and publishes telemetry. A
// separate identity, so that SAGE -- which parses untrusted documents -- holds
// neither the certificate's private key nor the gateway's credentials.
resource apimIdentity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: 'id-apim-${environmentName}'
  location: location
  tags: tags
}

// Dedicated bootstrap identity for the relational store: the relational-store
// module sets it as the server's Entra administrator, and the in-VNet bootstrap
// job runs as it to enrol the application managed-identity roles and pre-create
// the extensions. Least standing privilege — it administers nothing else, so the
// running application identities never hold a server-administration role.
resource bootstrapIdentity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: 'id-pg-bootstrap-${environmentName}'
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

@description('Resource id of the API Management gateway managed identity (the gateway attaches this).')
output apimIdentityId string = apimIdentity.id

@description('Principal id of the API Management gateway managed identity (granted role assignments).')
output apimIdentityPrincipalId string = apimIdentity.properties.principalId

@description('Client id of the API Management gateway managed identity (Key Vault and telemetry token acquisition).')
output apimIdentityClientId string = apimIdentity.properties.clientId

@description('Resource id of the Postgres bootstrap managed identity (the bootstrap job attaches this).')
output bootstrapIdentityId string = bootstrapIdentity.id

@description('Principal id of the Postgres bootstrap managed identity (set as the server Entra administrator; granted AcrPull).')
output bootstrapIdentityPrincipalId string = bootstrapIdentity.properties.principalId

@description('Client id of the Postgres bootstrap managed identity (selects it for DefaultAzureCredential in the bootstrap job).')
output bootstrapIdentityClientId string = bootstrapIdentity.properties.clientId

@description('Name of the Postgres bootstrap managed identity (the database user the bootstrap job connects as, and the Entra admin display name).')
output bootstrapIdentityName string = bootstrapIdentity.name
