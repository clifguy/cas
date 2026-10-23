param location string
param runId string
param tags object
resource service 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: 'id-perf-${runId}'
  location: location
  tags: tags
}
resource bootstrap 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: 'id-perf-pg-${runId}'
  location: location
  tags: tags
}
output serviceId string = service.id
output serviceClientId string = service.properties.clientId
output servicePrincipalId string = service.properties.principalId
output bootstrapId string = bootstrap.id
output bootstrapClientId string = bootstrap.properties.clientId
output bootstrapPrincipalId string = bootstrap.properties.principalId
