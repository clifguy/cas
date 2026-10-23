param acrName string
param keyVaultName string
param servicePrincipalId string
param bootstrapPrincipalId string
resource acr 'Microsoft.ContainerRegistry/registries@2023-07-01' existing = { name: acrName }
resource kv 'Microsoft.KeyVault/vaults@2023-07-01' existing = { name: keyVaultName }
resource secret 'Microsoft.KeyVault/vaults/secrets@2023-07-01' existing = { parent: kv, name: 'anthropic-api-key' }
resource pulls 'Microsoft.Authorization/roleAssignments@2022-04-01' = [for principal in [servicePrincipalId,bootstrapPrincipalId]: {
  name: guid(acr.id,principal,'perf-pull')
  scope: acr
  properties: {
    principalId: principal
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions','7f951dda-4ed3-4680-a7ca-43fe172d538d') // gitleaks:allow public AcrPull role
  }
}]
resource key 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(secret.id,servicePrincipalId,'perf-secret')
  scope: secret
  properties: {
    principalId: servicePrincipalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions','4633458b-17de-408a-b874-0445c86b69e6') // gitleaks:allow public Key Vault Secrets User role
  }
}
var pullIds = [for (principal,i) in [servicePrincipalId,bootstrapPrincipalId]: pulls[i].id]
output roleIds array = concat(pullIds,[key.id])
