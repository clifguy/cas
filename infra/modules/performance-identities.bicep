param location string
param runId string
param tags object
param createBootstrap bool = true
resource service 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: 'id-perf-${runId}'
  location: location
  tags: tags
}
resource bootstrap 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = if (createBootstrap) {
  name: 'id-perf-pg-${runId}'
  location: location
  tags: tags
}
output serviceId string = service.id
output serviceClientId string = service.properties.clientId
output servicePrincipalId string = service.properties.principalId
output bootstrapId string = createBootstrap ? bootstrap!.id : ''
output bootstrapClientId string = createBootstrap ? bootstrap!.properties.clientId : ''
output bootstrapPrincipalId string = createBootstrap ? bootstrap!.properties.principalId : ''
