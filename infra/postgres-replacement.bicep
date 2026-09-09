// Provision only the replacement inside the existing resource group. No app
// coordinates or existing server settings change in this deployment.
@description('Azure region of the existing environment.')
param location string
@description('Existing environment name used by the shared network and DNS zone.')
param environmentName string
@description('Database name from the existing deployment.')
param databaseName string
@description('Tags for the replacement.')
param tags object = { managedBy: 'bicep' }
@minLength(1)
@maxLength(12)
@description('Stable nonempty generation, reused at cutover.')
param serverGeneration string
@description('Existing delegated Postgres subnet resource id.')
param delegatedSubnetId string
@description('Existing VNet resource id.')
param vnetId string
@description('Entra admin identity object id, supplied by deployment outputs.')
param aadAdminObjectId string
@description('Entra admin identity name.')
param aadAdminPrincipalName string
@description('Entra admin principal type.')
param aadAdminPrincipalType string = 'ServicePrincipal'
var versions = loadJsonContent('../versions.json')
module replacement 'modules/postgres.bicep' = {
  name: 'postgres-replacement-${serverGeneration}'
  params: {
    location: location
    environmentName: environmentName
    databaseName: databaseName
    tags: tags
    serverGeneration: serverGeneration
    postgresVersion: versions.postgres.dev_major
    geoRedundantBackup: 'Enabled'
    delegatedSubnetId: delegatedSubnetId
    vnetId: vnetId
    aadAdminObjectId: aadAdminObjectId
    aadAdminPrincipalName: aadAdminPrincipalName
    aadAdminPrincipalType: aadAdminPrincipalType
  }
}
@description('Replacement FQDN; production consumers are unchanged.')
output postgresServerFqdn string = replacement.outputs.postgresServerFqdn
@description('Replacement server identity.')
output postgresServerName string = replacement.outputs.postgresServerName
