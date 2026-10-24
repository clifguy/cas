targetScope = 'subscription'

@minLength(3)
@maxLength(20)
@description('Validated isolated experiment suffix, never a serving environment name.')
param runId string
@description('Embedding canary excludes service, Postgres, source and provider dependencies.')
param embeddingOnly bool = false
@description('Concrete principals from the verified foundation receipt, used for access previews.')
param accessPrincipals object = {}
@description('Experiment region, matching the production reference.')
param location string
@description('UTC hard expiry verified by the independent controller.')
param expiresAt string
@description('Immutable baseline SAGE repository digest.')
param imageDigest string
@description('Existing source registry resource group.')
param sourceGroup string
@description('Existing registry name, read from the reference deployment.')
param acrName string
@description('Existing secret vault name, read from the reference deployment.')
param keyVaultName string = ''
@description('Explicit SharePoint site scope.')
param siteId string = ''
@description('Explicit SharePoint drive scope.')
param driveId string = ''
@description('Existing SAGE authentication audience.')
param abstractionModel string = ''
@minValue(1)
@maxValue(30)
param maxProviderCalls int = 1
@minValue(1)
@maxValue(900)
param perRunSeconds int
param audience string = ''
@description('Existing Entra tenant.')
param tenantId string = ''
@description('OIDC operator principal exporting experiment evidence.')
param operatorPrincipalId string
@description('Create billable stack only after the independent cleanup schedule passes preflight.')
param deployStack bool = false
@description('Grant exact access only after identities exist and their IDs can be previewed.')
param configureAccess bool = false
@description('Start the service only after private database bootstrap and site grants succeed.')
param activateService bool = false
@allowed(['Consumption','D8'])
@description('Explicit experiment profile, not a production profile change.')
param profile string = 'Consumption'
@allowed(['2','4'])
@description('App CPU allocation for the selected arm.')
param cpu string = '2'
@allowed(['4Gi','8Gi'])
@description('App memory allocation; D8 comparisons hold this at eight GiB.')
param memory string = '4Gi'
@secure()
@description('Trial-only ingress key. Generated in memory, never retained in the plan.')
param ingressKey string = ''

var tags = { benchmarkRun: runId, expiresAt: expiresAt, purpose: 'ingest-performance' }
resource trial 'Microsoft.Resources/resourceGroups@2024-03-01' = {
  name: 'rg-ingest-perf-${runId}'
  location: location
  tags: tags
}
resource controller 'Microsoft.Resources/resourceGroups@2024-03-01' = {
  name: 'rg-ingest-perf-control-${runId}'
  location: location
  tags: tags
}
module identities './modules/performance-identities.bicep' = {
  name: 'perf-identities-${runId}'
  scope: trial
  params: { location: location, runId: runId, tags: tags, createBootstrap: !embeddingOnly }
}
module expiry './modules/performance-cleanup.bicep' = {
  name: 'perf-cleanup-${runId}'
  scope: controller
  params: { location: location, runId: runId, tags: tags }
}
module cleanupAccess './modules/performance-cleanup-access.bicep' = if (configureAccess) {
  name: 'perf-cleanup-access-${runId}'
  scope: trial
  params: { principalId: accessPrincipals.expiryPrincipalId }
}
module sourceAccess './modules/performance-access.bicep' = if (configureAccess && !embeddingOnly) {
  name: 'perf-source-access-${runId}'
  scope: resourceGroup(sourceGroup)
  params: {
    acrName: acrName
    keyVaultName: keyVaultName
    servicePrincipalId: accessPrincipals.servicePrincipalId
    bootstrapPrincipalId: accessPrincipals.bootstrapPrincipalId
  }
}
module evidenceAccess './modules/performance-evidence-access.bicep' = if (configureAccess) {
  name: 'perf-evidence-access-${runId}'
  scope: controller
  params: { storageName: expiry.outputs.storageName, servicePrincipalId: accessPrincipals.servicePrincipalId, operatorPrincipalId: operatorPrincipalId }
}
module stack './modules/performance-stack.bicep' = if (deployStack && !embeddingOnly) {
  name: 'perf-stack-${runId}'
  scope: trial
  params: {
    location: location
    runId: runId
    tags: tags
    imageDigest: imageDigest
    acrName: acrName
    serviceId: identities.outputs.serviceId
    serviceClientId: identities.outputs.serviceClientId
    servicePrincipalId: accessPrincipals.servicePrincipalId
    bootstrapId: identities.outputs.bootstrapId
    bootstrapClientId: identities.outputs.bootstrapClientId
    bootstrapPrincipalId: accessPrincipals.bootstrapPrincipalId
    keyVaultName: keyVaultName
    exportUrl: expiry.outputs.exportUrl
    siteId: siteId
    driveId: driveId
    abstractionModel: abstractionModel
    maxProviderCalls: maxProviderCalls
    perRunSeconds: perRunSeconds
    audience: audience
    tenantId: tenantId
    activateService: activateService
    profile: profile
    cpu: cpu
    memory: memory
    ingressKey: ingressKey
  }
  dependsOn: [ sourceAccess, evidenceAccess, cleanupAccess ]
}
output trialGroup string = trial.name
output controllerGroup string = controller.name
output automationName string = expiry.outputs.automationName
output exportUrl string = expiry.outputs.exportUrl
output serviceClientId string = identities.outputs.serviceClientId
output servicePrincipalId string = identities.outputs.servicePrincipalId
output bootstrapPrincipalId string = identities.outputs.bootstrapPrincipalId
output expiryPrincipalId string = expiry.outputs.principalId
var registryId = resourceId(subscription().subscriptionId, sourceGroup, 'Microsoft.ContainerRegistry/registries', acrName)
// Retain the exact prospective assignment before access apply, including uncertain outcomes.
output sourceRoleIds array = embeddingOnly ? [
  extensionResourceId(registryId, 'Microsoft.Authorization/roleAssignments', guid(registryId, identities.outputs.servicePrincipalId, 'perf-image'))
] : (configureAccess ? sourceAccess!.outputs.roleIds : [])
output stackOutputs object = deployStack ? (embeddingOnly ? {
  environmentId: embedding!.outputs.environmentId
  benchmarkJobName: embedding!.outputs.benchmarkJobName
} : {
  environmentId: stack!.outputs.environmentId
  bootstrapJob: stack!.outputs.bootstrapJob
  postgresId: stack!.outputs.postgresId
  postgresHost: stack!.outputs.postgresHost
  origin: stack!.outputs.origin
  appId: stack!.outputs.appId
  logCustomerId: stack!.outputs.logCustomerId
  verifyJobName: stack!.outputs.verifyJobName
  benchmarkJobName: stack!.outputs.benchmarkJobName
}) : {}

module imageAccess './modules/performance-image-access.bicep' = if (configureAccess && embeddingOnly) {
  name: 'perf-source-access-${runId}'
  scope: resourceGroup(sourceGroup)
  params: { acrName: acrName, principalId: accessPrincipals.servicePrincipalId }
}
module embedding './modules/performance-embedding.bicep' = if (deployStack && embeddingOnly) {
  name: 'perf-stack-${runId}'
  scope: trial
  params: {
    location: location
    runId: runId
    tags: tags
    imageDigest: imageDigest
    acrName: acrName
    serviceId: identities.outputs.serviceId
    serviceClientId: identities.outputs.serviceClientId
    exportUrl: expiry.outputs.exportUrl
    perRunSeconds: perRunSeconds
  }
  dependsOn: [imageAccess, evidenceAccess, cleanupAccess]
}
