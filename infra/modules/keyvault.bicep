// CAS cloud deployment — Key Vault module.
//
// Provisions the secrets vault for the cloud deployment profile (CAS-ADR-042).
// The vault holds the hosted abstraction provider's API key and the owned
// wildcard TLS certificate; the database connection authenticates by managed
// identity, so no database password is stored. The access model is Azure RBAC:
// the SAGE and CAS BFF managed identities are granted data-plane read of the
// secrets and certificates they consume. Secret values are loaded out of band
// by a documented operator step (see docs/process/key-vault-secrets.md), so no
// secret material is committed to the repository.
//
// Resource-group scoped (the Bicep default): the orchestrator deploys it with
// scope: rg, passing the principal ids the identity module produced.

@description('Azure region for the Key Vault.')
param location string

@description('Tags applied to the Key Vault.')
param tags object

@description('Tenant id that owns the vault and its RBAC assignments.')
param tenantId string = subscription().tenantId

@description('Principal id of the SAGE managed identity granted secret/certificate read.')
param sagePrincipalId string

@description('Principal id of the CAS BFF managed identity granted secret/certificate read.')
param bffPrincipalId string

@description('Enable purge protection. Off by default (the vault is recreatable in the experimental profile); on hardens against secret loss but blocks deletion for the soft-delete window.')
param enablePurgeProtection bool = false

// Key Vault names are globally unique and alphanumeric; derive a stable one from
// the resource group id rather than taking it as a parameter (mirrors the ACR
// naming in the foundation module).
var kvName = 'kv${uniqueString(resourceGroup().id)}'

// Built-in Azure roles (public, fixed constants — not identity coordinates).
// Key Vault Secrets User: read secret values. Key Vault Certificate User: read
// certificates and their backing secret.
var keyVaultSecretsUserRoleId = '4633458b-17de-408a-b874-0445c86b69e6' // gitleaks:allow public role id
var keyVaultCertificateUserRoleId = 'db79e9a7-68ee-4b58-9aeb-b90e7c24fcba'

resource keyVault 'Microsoft.KeyVault/vaults@2023-07-01' = {
  name: kvName
  location: location
  tags: tags
  properties: {
    sku: {
      family: 'A'
      name: 'standard'
    }
    tenantId: tenantId
    // RBAC, not the legacy access-policy array: the no-stored-credential posture.
    enableRbacAuthorization: true
    enableSoftDelete: true
    // Azure accepts only true or an absent property here; never an explicit false.
    enablePurgeProtection: enablePurgeProtection ? true : null
    publicNetworkAccess: 'Enabled'
    networkAcls: {
      defaultAction: 'Allow'
      bypass: 'AzureServices'
    }
  }
}

// Data-plane grants. Each managed identity reads the secrets and certificates it
// consumes; nothing is granted write access to secret material from here.
resource sageSecretsUser 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: keyVault
  name: guid(keyVault.id, sagePrincipalId, keyVaultSecretsUserRoleId)
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', keyVaultSecretsUserRoleId)
    principalId: sagePrincipalId
    principalType: 'ServicePrincipal'
  }
}

resource sageCertificateUser 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: keyVault
  name: guid(keyVault.id, sagePrincipalId, keyVaultCertificateUserRoleId)
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', keyVaultCertificateUserRoleId)
    principalId: sagePrincipalId
    principalType: 'ServicePrincipal'
  }
}

resource bffSecretsUser 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: keyVault
  name: guid(keyVault.id, bffPrincipalId, keyVaultSecretsUserRoleId)
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', keyVaultSecretsUserRoleId)
    principalId: bffPrincipalId
    principalType: 'ServicePrincipal'
  }
}

resource bffCertificateUser 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: keyVault
  name: guid(keyVault.id, bffPrincipalId, keyVaultCertificateUserRoleId)
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', keyVaultCertificateUserRoleId)
    principalId: bffPrincipalId
    principalType: 'ServicePrincipal'
  }
}

@description('Name of the Key Vault.')
output keyVaultName string = keyVault.name

@description('Data-plane URI of the Key Vault (downstream modules build references from this).')
output keyVaultUri string = keyVault.properties.vaultUri

@description('Resource id of the Key Vault.')
output keyVaultResourceId string = keyVault.id

@description('Canonical secret name the hosted abstraction provider key is loaded under.')
output anthropicSecretName string = 'anthropic-api-key'

@description('Canonical certificate name the owned wildcard TLS certificate is loaded under.')
output tlsCertificateName string = 'wildcard-tls'
