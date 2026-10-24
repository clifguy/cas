param location string
param runId string
param tags object
param imageDigest string
param acrName string
param serviceId string
param serviceClientId string
param exportUrl string
@minValue(1)
@maxValue(900)
param perRunSeconds int
resource env 'Microsoft.App/managedEnvironments@2024-03-01' = {
  name: 'cae-perf-${runId}'
  location: location
  tags: tags
  properties: {
    workloadProfiles: [{ name: 'Consumption', workloadProfileType: 'Consumption' }]
  }
}
output environmentId string = env.id
resource benchmarkJob 'Microsoft.App/jobs@2024-03-01' = {
  name: 'bench-${runId}'
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
        resources: { cpu: 2, memory: '4Gi' }
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
