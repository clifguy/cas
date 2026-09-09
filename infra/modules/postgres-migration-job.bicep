@description('Region of the existing Container Apps environment.')
param location string
@description('Environment name.')
param environmentName string
@description('Existing Container Apps environment resource id.')
param acaEnvironmentId string
@description('Immutable SAGE image reference from the current deployment.')
param image string
@description('Existing registry host.')
param acrLoginServer string
@description('Existing bootstrap identity resource id, already granted AcrPull.')
param identityId string
@description('Bootstrap identity client id.')
param identityClientId string
@description('Bootstrap identity database role name.')
param adminUser string
@description('Source server FQDN.')
param sourceFqdn string
@description('Replacement server FQDN.')
param targetFqdn string
@description('Database name.')
param databaseName string
@description('SAGE database role.')
param sageRole string
@description('BFF database role.')
param bffRole string
@description('Stable migration identity. A retry must reuse it.')
param runId string
var versions = loadJsonContent('../../versions.json')
resource migrationJob 'Microsoft.App/jobs@2024-03-01' = {
  name: 'job-pg-migration-${environmentName}'
  location: location
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: { '${identityId}': {} }
  }
  properties: {
    environmentId: acaEnvironmentId
    configuration: {
      triggerType: 'Manual'
      replicaTimeout: 7200
      replicaRetryLimit: 0
      manualTriggerConfig: { parallelism: 1, replicaCompletionCount: 1 }
      registries: [{ server: acrLoginServer, identity: identityId }]
    }
    template: {
      containers: [{
        name: 'migration'
        image: image
        command: ['python', '-m', 'sage.maintenance.postgres_migration']
        resources: { cpu: 1, memory: '2Gi' }
        env: [
          { name: 'AZURE_CLIENT_ID', value: identityClientId }
          { name: 'PG_ADMIN_USER', value: adminUser }
          { name: 'PG_SOURCE_FQDN', value: sourceFqdn }
          { name: 'PG_TARGET_FQDN', value: targetFqdn }
          { name: 'PG_DATABASE', value: databaseName }
          { name: 'SAGE_DB_ROLE', value: sageRole }
          { name: 'BFF_DB_ROLE', value: bffRole }
          { name: 'PG_MIGRATION_RUN_ID', value: runId }
          { name: 'PG_SOURCE_MAJOR', value: versions.postgres.deploy_major }
          { name: 'PG_TARGET_MAJOR', value: versions.postgres.dev_major }
        ]
      }]
    }
  }
}
@description('Name of the migration job, started separately after preparation.')
output jobName string = migrationJob.name
