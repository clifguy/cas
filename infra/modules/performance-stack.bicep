param location string
param runId string
param tags object
param imageDigest string
param acrName string
param serviceId string
param serviceClientId string
param servicePrincipalId string
param bootstrapId string
param bootstrapClientId string
param bootstrapPrincipalId string
param keyVaultName string
param exportUrl string
param siteId string
param driveId string
param abstractionModel string
param maxProviderCalls int
@minValue(1)
@maxValue(900)
param perRunSeconds int
param audience string
param tenantId string
param activateService bool
param profile string
param cpu string
param memory string
@secure()
param ingressKey string

var appName = 'ingest-perf-${runId}'
resource vnet 'Microsoft.Network/virtualNetworks@2024-05-01' = {
  name: 'vnet-perf-${runId}'
  location: location
  tags: tags
  properties: {
    addressSpace: { addressPrefixes: ['10.41.0.0/16'] }
    subnets: [
      { name: 'aca', properties: { addressPrefix: '10.41.0.0/23', delegations: [{ name: 'aca', properties: { serviceName: 'Microsoft.App/environments' } }] } }
      { name: 'postgres', properties: { addressPrefix: '10.41.2.0/24', delegations: [{ name: 'postgres', properties: { serviceName: 'Microsoft.DBforPostgreSQL/flexibleServers' } }] } }
    ]
  }
}
resource logs 'Microsoft.OperationalInsights/workspaces@2023-09-01' = {
  name: 'log-perf-${runId}'
  location: location
  tags: tags
  properties: { sku: { name: 'PerGB2018' }, retentionInDays: 30, workspaceCapping: { dailyQuotaGb: 1 } }
}
resource env 'Microsoft.App/managedEnvironments@2024-03-01' = {
  name: 'cae-perf-${runId}'
  location: location
  tags: tags
  properties: {
    appLogsConfiguration: { destination: 'log-analytics', logAnalyticsConfiguration: { customerId: logs.properties.customerId, sharedKey: logs.listKeys().primarySharedKey } }
    vnetConfiguration: { infrastructureSubnetId: vnet.properties.subnets[0].id, internal: false }
    workloadProfiles: [
      { name: 'Consumption', workloadProfileType: 'Consumption' }
      { name: 'D8', workloadProfileType: 'D8', minimumCount: 1, maximumCount: 1 }
    ]
  }
}
module pg './postgres.bicep' = {
  name: 'perf-postgres'
  params: {
    location: location
    environmentName: 'perf-${runId}'
    tags: tags
    delegatedSubnetId: vnet.properties.subnets[1].id
    vnetId: vnet.id
    aadAdminObjectId: bootstrapPrincipalId
    aadAdminPrincipalName: last(split(bootstrapId,'/'))
    aadAdminPrincipalType: 'ServicePrincipal'
    protectDeletion: false
    backupRetentionDays: 7
  }
}
module bootstrap './postgres-bootstrap.bicep' = {
  name: 'perf-pg-bootstrap'
  params: {
    location: location
    environmentName: 'perf-${runId}'
    tags: tags
    acaEnvironmentId: env.id
    acrLoginServer: '${acrName}${environment().suffixes.acrLoginServer}'
    acrName: acrName
    imageTag: ''
    imageDigest: imageDigest
    manageAcrPull: false
    jobName: 'pg-perf-${runId}'
    bootstrapIdentityId: bootstrapId
    bootstrapIdentityClientId: bootstrapClientId
    bootstrapIdentityPrincipalId: bootstrapPrincipalId
    bootstrapIdentityName: last(split(bootstrapId,'/'))
    postgresServerFqdn: pg.outputs.postgresServerFqdn
    postgresDatabaseName: pg.outputs.postgresDatabaseName
    sageIdentityId: serviceId
    bffIdentityId: serviceId
  }
}
var serviceName = last(split(serviceId,'/'))
var origin = 'https://${appName}.${env.properties.defaultDomain}'
var config = join([
  'profile: cloud'
  'storage_backend: postgres'
  'vault_source_backend: document_store'
  'abstraction:'
  '  provider: anthropic'
  '  model: ${abstractionModel}'
  'postgres:'
  '  host: ${pg.outputs.postgresServerFqdn}'
  '  database: ${pg.outputs.postgresDatabaseName}'
  '  user: ${serviceName}'
  '  sslmode: require'
  'document_store:'
  '  site_id: ${siteId}'
  '  drive_id: ${driveId}'
  '  root_path: performance/${runId}'
  'auth:'
  '  enabled: true'
  '  tenant_id: ${tenantId}'
  '  audience: ${audience}'
  'transfer:'
  '  public_base_url: ${origin}'
], '\n')
resource app 'Microsoft.App/containerApps@2024-03-01' = if (activateService) {
  name: appName
  location: location
  tags: tags
  identity: { type: 'UserAssigned', userAssignedIdentities: { '${serviceId}': {} } }
  properties: {
    managedEnvironmentId: env.id
    workloadProfileName: profile
    configuration: {
      activeRevisionsMode: 'Single'
      ingress: { external: true, targetPort: 8000, transport: 'auto', allowInsecure: false }
      registries: [{ server: '${acrName}${environment().suffixes.acrLoginServer}', identity: serviceId }]
      secrets: [
        { name: 'sage-cloud-config', value: config }
        { name: 'trial-ingress', value: ingressKey }
        #disable-next-line use-secure-value-for-secure-inputs // Authored non-secret trial code mounted as a file.
        { name: 'trial-runtime', value: loadTextContent('../../deploy/performance_runtime.py') }
        #disable-next-line use-secure-value-for-secure-inputs // Authored non-secret trial code mounted as a file.
        { name: 'trial-reconcile', value: loadTextContent('../../deploy/performance_reconcile.py') }
      ]
    }
    template: {
      containers: [{
        name: 'sage'
        image: imageDigest
        command: ['python','/etc/perf/performance_runtime.py']
        resources: { cpu: json(cpu), memory: memory }
        env: [
          { name: 'AZURE_CLIENT_ID', value: serviceClientId }
          { name: 'SAGE_CONFIG_PATH', value: '/etc/perf/config.yaml' }
          { name: 'SAGE_KEY_VAULT_URI', value: 'https://${keyVaultName}${environment().suffixes.keyvaultDns}' }
          { name: 'SAGE_INGRESS_KEY', secretRef: 'trial-ingress' }
          { name: 'PERF_RUN_ID', value: runId }
          { name: 'PERF_EXPIRES_AT', value: tags.expiresAt }
          { name: 'PERF_PER_RUN_SECONDS', value: string(perRunSeconds) }
          { name: 'PERF_EXPORT_URL', value: exportUrl }
          { name: 'PERF_PROVIDER_LIMIT', value: string(maxProviderCalls) }
        ]
        volumeMounts: [{ volumeName: 'config', mountPath: '/etc/perf' }]
        probes: [
          { type: 'Startup', httpGet: { path: '/health', port: 8000 }, initialDelaySeconds: 10, periodSeconds: 10, failureThreshold: 60 }
          { type: 'Readiness', httpGet: { path: '/health', port: 8000 }, periodSeconds: 10, failureThreshold: 3 }
        ]
      }]
      scale: { minReplicas: 1, maxReplicas: 1 }
      volumes: [{ name: 'config', storageType: 'Secret', secrets: [
        { secretRef: 'sage-cloud-config', path: 'config.yaml' }
        { secretRef: 'trial-runtime', path: 'performance_runtime.py' }
        { secretRef: 'trial-reconcile', path: 'reconcile.py' }
      ] }]
    }
  }
}
// Reconciliation runs as the service role, not the bootstrap administrator.
// It is created/started after service measurements, so it cannot contend on D8.
output environmentId string = env.id
output bootstrapJob string = bootstrap.outputs.bootstrapJobName
output postgresId string = resourceId('Microsoft.DBforPostgreSQL/flexibleServers',pg.outputs.postgresServerName)
output postgresHost string = pg.outputs.postgresServerFqdn
output origin string = origin
output appId string = activateService ? app!.id : ''
output logCustomerId string = logs.properties.customerId
output servicePrincipalId string = servicePrincipalId
resource verifyJob 'Microsoft.App/jobs@2024-03-01' = if (activateService) {
  name: 'verify-${runId}'
  location: location
  tags: tags
  identity: { type: 'UserAssigned', userAssignedIdentities: { '${serviceId}': {} } }
  properties: {
    environmentId: env.id
    workloadProfileName: 'Consumption'
    configuration: {
      triggerType: 'Manual'
      replicaTimeout: perRunSeconds
      replicaRetryLimit: 0
      manualTriggerConfig: { parallelism: 1, replicaCompletionCount: 1 }
      registries: [{ server: '${acrName}${environment().suffixes.acrLoginServer}', identity: serviceId }]
      secrets: [
        { name: 'sage-cloud-config', value: config }
        #disable-next-line use-secure-value-for-secure-inputs // Authored non-secret trial code mounted as a file.
        { name: 'trial-runtime', value: loadTextContent('../../deploy/performance_runtime.py') }
        #disable-next-line use-secure-value-for-secure-inputs // Authored non-secret trial code mounted as a file.
        { name: 'trial-reconcile', value: loadTextContent('../../deploy/performance_reconcile.py') }
      ]
    }
    template: {
      containers: [{
        name: 'verify'
        image: imageDigest
        command: ['python','/etc/perf/reconcile.py']
        resources: { cpu: 2, memory: '4Gi' }
        env: [
          { name: 'AZURE_CLIENT_ID', value: serviceClientId }
          { name: 'SAGE_CONFIG_PATH', value: '/etc/perf/config.yaml' }
          { name: 'PERF_RUN_ID', value: runId }
          { name: 'PERF_EXPIRES_AT', value: tags.expiresAt }
          { name: 'PERF_PER_RUN_SECONDS', value: string(perRunSeconds) }
          { name: 'PERF_EXPORT_URL', value: exportUrl }
        ]
        volumeMounts: [{ volumeName: 'config', mountPath: '/etc/perf' }]
      }]
      volumes: [{ name: 'config', storageType: 'Secret', secrets: [
        { secretRef: 'sage-cloud-config', path: 'config.yaml' }
        { secretRef: 'trial-runtime', path: 'performance_runtime.py' }
        { secretRef: 'trial-reconcile', path: 'reconcile.py' }
      ] }]
    }
  }
}
output verifyJobName string = activateService ? verifyJob!.name : ''
resource benchmarkJob 'Microsoft.App/jobs@2024-03-01' = {
  name: 'bench-${runId}'
  location: location
  tags: tags
  identity: { type: 'UserAssigned', userAssignedIdentities: { '${serviceId}': {} } }
  properties: {
    environmentId: env.id
    workloadProfileName: 'D8'
    configuration: {
      triggerType: 'Manual'
      replicaTimeout: perRunSeconds
      replicaRetryLimit: 0
      manualTriggerConfig: { parallelism: 1, replicaCompletionCount: 1 }
      registries: [{ server: '${acrName}${environment().suffixes.acrLoginServer}', identity: serviceId }]
      secrets: [
        #disable-next-line use-secure-value-for-secure-inputs // Authored non-secret trial code mounted as a file.
        { name: 'trial-runtime', value: loadTextContent('../../deploy/performance_runtime.py') }
        #disable-next-line use-secure-value-for-secure-inputs // Authored non-secret trial code mounted as a file.
        { name: 'trial-benchmark', value: loadTextContent('../../deploy/performance_benchmark.py') }
      ]
    }
    template: {
      containers: [{
        name: 'benchmark'
        image: imageDigest
        command: ['python','/etc/perf/benchmark.py']
        resources: { cpu: 2, memory: '8Gi' }
        env: [
          { name: 'AZURE_CLIENT_ID', value: serviceClientId }
          { name: 'PERF_RUN_ID', value: runId }
          { name: 'PERF_EXPIRES_AT', value: tags.expiresAt }
          { name: 'PERF_PER_RUN_SECONDS', value: string(perRunSeconds) }
          { name: 'PERF_EXPORT_URL', value: exportUrl }
          { name: 'BENCHMARK_IMAGE_DIGEST', value: imageDigest }
          { name: 'PERF_BENCH_CASE', value: '{"shape":"short","batch":1,"threads":2,"mode":"direct"}' }
        ]
        volumeMounts: [{ volumeName: 'config', mountPath: '/etc/perf' }]
      }]
      volumes: [{ name: 'config', storageType: 'Secret', secrets: [
        { secretRef: 'trial-runtime', path: 'performance_runtime.py' }
        { secretRef: 'trial-benchmark', path: 'benchmark.py' }
      ] }]
    }
  }
}
output benchmarkJobName string = benchmarkJob.name
