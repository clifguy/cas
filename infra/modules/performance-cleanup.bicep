param location string
param runId string
param tags object
resource account 'Microsoft.Automation/automationAccounts@2024-10-23' = {
  name: 'aa-perf-${runId}'
  location: location
  tags: tags
  identity: { type: 'SystemAssigned' }
  properties: { sku: { name: 'Basic' }, publicNetworkAccess: true }
}
resource store 'Microsoft.Storage/storageAccounts@2023-05-01' = {
  name: 'stperf${uniqueString(resourceGroup().id)}'
  location: location
  tags: tags
  kind: 'StorageV2'
  sku: { name: 'Standard_LRS' }
  properties: { allowBlobPublicAccess: false, allowSharedKeyAccess: false, supportsHttpsTrafficOnly: true, minimumTlsVersion: 'TLS1_2' }
}
resource blobs 'Microsoft.Storage/storageAccounts/blobServices@2023-05-01' = {
  parent: store
  name: 'default'
}
resource evidence 'Microsoft.Storage/storageAccounts/blobServices/containers@2023-05-01' = {
  parent: blobs
  name: 'evidence'
  properties: { publicAccess: 'None' }
}
output principalId string = account.identity.principalId
output automationName string = account.name
output storageName string = store.name
output exportUrl string = '${store.properties.primaryEndpoints.blob}${evidence.name}/${runId}'
