param storageName string
param servicePrincipalId string
param operatorPrincipalId string
resource store 'Microsoft.Storage/storageAccounts@2023-05-01' existing = { name: storageName }
resource blobs 'Microsoft.Storage/storageAccounts/blobServices@2023-05-01' existing = { parent: store, name: 'default' }
resource container 'Microsoft.Storage/storageAccounts/blobServices/containers@2023-05-01' existing = { parent: blobs, name: 'evidence' }
resource grants 'Microsoft.Authorization/roleAssignments@2022-04-01' = [for principal in [servicePrincipalId,operatorPrincipalId]: {
  name: guid(container.id,principal,'perf-evidence')
  scope: container
  properties: {
    principalId: principal
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions','ba92f5b4-2d11-453d-a403-e96b0029c9fe') // gitleaks:allow public Blob Data Contributor role
  }
}]
