// CAS cloud deployment — Key Vault module.
//
// Provisions the secrets vault for the cloud deployment profile (CAS-ADR-042).
// The vault holds the hosted abstraction provider's API key and the owned
// wildcard TLS certificate, the BFF client secret and the gateway's ingress
// key; the database connection authenticates by managed identity, so no
// database password is stored. The access model is Azure RBAC, granted per
// secret: each identity reads exactly the secrets it consumes and nothing else
// in the vault, which may hold other workloads' secrets. Secret values are loaded out of band
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

@description('Principal id of the API Management gateway managed identity granted certificate and ingress-key read.')
param apimPrincipalId string

@description('Whether the secrets are loaded. A grant scoped to one secret needs that secret to exist, and secrets are loaded after the first deployment of a new tenant; deploy that first time with false, load the secrets, then deploy again with true.')
param keyVaultSecretsLoaded bool = true

@description('Enable purge protection. Off by default: the setting is irreversible — Azure refuses false once it has been applied — and vault-wide, so it binds every workload whose secrets share the vault rather than only this one. On, it hardens against secret loss but blocks deletion for the full soft-delete window. While it is off, soft delete is the recovery path.')
param enablePurgeProtection bool = false

// Key Vault names are globally unique and alphanumeric; derive a stable one from
// the resource group id rather than taking it as a parameter (mirrors the ACR
// naming in the foundation module).
var kvName = 'kv${uniqueString(resourceGroup().id)}'

// Built-in Azure role (a public, fixed constant — not an identity coordinate).
// Key Vault Secrets User reads a secret's value; a certificate's private key is
// read through its backing secret, of the same name. Certificate User is not
// used: it carries secrets/getSecret, so even a narrow scope grants more than
// the read this vault's consumers need.
var keyVaultSecretsUserRoleId = '4633458b-17de-408a-b874-0445c86b69e6' // gitleaks:allow public role id

// Canonical names of the secrets the deployment consumes. Downstream modules
// build Key Vault references from the outputs below; use them verbatim.
var anthropicSecretName = 'anthropic-api-key'
var bffClientSecretName = 'bff-client-secret'
var tlsCertificateName = 'wildcard-tls'
var ingressKeySecretName = 'sage-ingress-key'

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

// The secrets the grants are scoped to. Loaded out of band, never created here.
resource anthropicSecret 'Microsoft.KeyVault/vaults/secrets@2023-07-01' existing = {
  parent: keyVault
  name: anthropicSecretName
}

resource bffClientSecret 'Microsoft.KeyVault/vaults/secrets@2023-07-01' existing = {
  parent: keyVault
  name: bffClientSecretName
}

resource tlsCertificateSecret 'Microsoft.KeyVault/vaults/secrets@2023-07-01' existing = {
  parent: keyVault
  name: tlsCertificateName
}

resource ingressKeySecret 'Microsoft.KeyVault/vaults/secrets@2023-07-01' existing = {
  parent: keyVault
  name: ingressKeySecretName
}

// Data-plane grants, one per identity and secret. Nothing is granted at vault
// scope, and nothing is granted write access to secret material from here.
// SAGE: the hosted abstraction key, and the ingress key it checks requests for.
resource sageAnthropicRead 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (keyVaultSecretsLoaded) {
  scope: anthropicSecret
  name: guid(anthropicSecret.id, sagePrincipalId, keyVaultSecretsUserRoleId)
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', keyVaultSecretsUserRoleId)
    principalId: sagePrincipalId
    principalType: 'ServicePrincipal'
  }
}

resource sageIngressKeyRead 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (keyVaultSecretsLoaded) {
  scope: ingressKeySecret
  name: guid(ingressKeySecret.id, sagePrincipalId, keyVaultSecretsUserRoleId)
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', keyVaultSecretsUserRoleId)
    principalId: sagePrincipalId
    principalType: 'ServicePrincipal'
  }
}

// BFF: its client secret, and the certificate the container-apps environment
// binds to the cas custom domain (read as the BFF identity).
resource bffClientSecretRead 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (keyVaultSecretsLoaded) {
  scope: bffClientSecret
  name: guid(bffClientSecret.id, bffPrincipalId, keyVaultSecretsUserRoleId)
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', keyVaultSecretsUserRoleId)
    principalId: bffPrincipalId
    principalType: 'ServicePrincipal'
  }
}

resource bffCertificateRead 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (keyVaultSecretsLoaded) {
  scope: tlsCertificateSecret
  name: guid(tlsCertificateSecret.id, bffPrincipalId, keyVaultSecretsUserRoleId)
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', keyVaultSecretsUserRoleId)
    principalId: bffPrincipalId
    principalType: 'ServicePrincipal'
  }
}

// Gateway: the certificate for the sage custom domain, and the ingress key it
// injects on every request it forwards.
resource apimCertificateRead 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (keyVaultSecretsLoaded) {
  scope: tlsCertificateSecret
  name: guid(tlsCertificateSecret.id, apimPrincipalId, keyVaultSecretsUserRoleId)
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', keyVaultSecretsUserRoleId)
    principalId: apimPrincipalId
    principalType: 'ServicePrincipal'
  }
}

resource apimIngressKeyRead 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (keyVaultSecretsLoaded) {
  scope: ingressKeySecret
  name: guid(ingressKeySecret.id, apimPrincipalId, keyVaultSecretsUserRoleId)
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', keyVaultSecretsUserRoleId)
    principalId: apimPrincipalId
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
output anthropicSecretName string = anthropicSecretName

@description('Canonical certificate name the owned wildcard TLS certificate is loaded under.')
output tlsCertificateName string = tlsCertificateName

@description('Canonical secret name the BFF confidential-client secret is loaded under.')
output bffClientSecretName string = bffClientSecretName

@description('Canonical secret name the gateway ingress key is loaded under.')
output ingressKeySecretName string = ingressKeySecretName
