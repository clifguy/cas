// Temporary read-only verification job for an isolated restore target.
//
// Deployed out of band by the restore-verification driver, never by the serving
// template, and deleted with the restore server it inspects. It carries no
// deletion lock for exactly that reason: it is drill scaffolding, not a serving
// resource. The name deliberately avoids the migration-job prefix the tenant
// deploy guard scans, so an in-flight drill never blocks ordinary deployment or
// maintenance.
@description('Region of the existing Container Apps environment.')
param location string
@description('Environment name.')
param environmentName string
@description('Existing Container Apps environment resource id.')
param acaEnvironmentId string
@description('Immutable SAGE image reference. The driver rejects an unpinned tag.')
param image string
@description('Existing registry host.')
param acrLoginServer string
@description('Existing bootstrap identity resource id, already granted AcrPull.')
param identityId string
@description('Bootstrap identity client id.')
param identityClientId string
@description('Bootstrap identity database role name.')
param adminUser string
@description('FQDN of the isolated restore target this job inspects.')
param restoreFqdn string
@description('FQDN of the serving server, supplied so the job can refuse it.')
param servingFqdn string
@description('Database name.')
param databaseName string
@description('SAGE database role.')
param sageRole string
@description('BFF database role.')
param bffRole string
@description('Recovery point the restore was taken to, in UTC ISO 8601.')
param restorePoint string
@description('Schema holding the two bracketing sentinel documents.')
param sentinelSchema string
@description('Document written before the recovery point; must be present.')
param sentinelBeforeId string
@description('Document written after the recovery point; must be absent.')
param sentinelAfterId string
var versions = loadJsonContent('../../versions.json')
resource restoreVerifyJob 'Microsoft.App/jobs@2024-03-01' = {
  name: 'job-pg-restore-verify-${environmentName}'
  location: location
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: { '${identityId}': {} }
  }
  properties: {
    environmentId: acaEnvironmentId
    configuration: {
      triggerType: 'Manual'
      replicaTimeout: 3600
      replicaRetryLimit: 0
      manualTriggerConfig: { parallelism: 1, replicaCompletionCount: 1 }
      registries: [{ server: acrLoginServer, identity: identityId }]
    }
    template: {
      containers: [{
        name: 'restore-verify'
        image: image
        command: ['python', '-m', 'sage.maintenance.postgres_restore_verify']
        resources: { cpu: 1, memory: '2Gi' }
        env: [
          { name: 'AZURE_CLIENT_ID', value: identityClientId }
          { name: 'PG_ADMIN_USER', value: adminUser }
          { name: 'PG_FQDN', value: restoreFqdn }
          { name: 'PG_SERVING_FQDN', value: servingFqdn }
          { name: 'PG_DATABASE', value: databaseName }
          { name: 'SAGE_DB_ROLE', value: sageRole }
          { name: 'BFF_DB_ROLE', value: bffRole }
          { name: 'PG_EXPECTED_MAJOR', value: versions.postgres.deploy_major }
          { name: 'PG_RESTORE_POINT', value: restorePoint }
          { name: 'PG_SENTINEL_SCHEMA', value: sentinelSchema }
          { name: 'PG_SENTINEL_BEFORE_ID', value: sentinelBeforeId }
          { name: 'PG_SENTINEL_AFTER_ID', value: sentinelAfterId }
        ]
      }]
    }
  }
}
@description('Name of the verification job, started separately by the driver.')
output jobName string = restoreVerifyJob.name
