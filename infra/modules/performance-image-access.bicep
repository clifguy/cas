param acrName string
param principalId string
var acrPullRole = '7f951dda-4ed3-4680-a7ca-43fe172d538d'
resource registry 'Microsoft.ContainerRegistry/registries@2023-07-01' existing = { name: acrName }
resource pull 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(registry.id, principalId, 'perf-image')
  scope: registry
  properties: {
    principalId: principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', acrPullRole)
  }
}
output roleIds array = [pull.id]
