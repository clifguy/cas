using './main.bicep'

// Single cloud environment. The deployment what-if/create commands pass
// --location to record the subscription-scope deployment; keep that value
// in step with the location below. Additional environments are added by
// copying this file to main.<env>.bicepparam and selecting it in the
// workflow — see docs/process/azure-deployment.md.

param environmentName = 'prod'
param location = 'eastus2'
param resourceGroupName = 'rg-cas-prod'
