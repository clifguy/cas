// CAS cloud deployment — orchestrator.
//
// Targets the subscription so it can create the resource group that every
// hosting-environment module deploys into. Hosting-environment modules live
// under infra/modules/ and are wired in below as the cloud deployment
// profile (CAS-ADR-042) is built out. The foundation module is wired first; it
// establishes the shared network and compute environment that later modules
// consume through its outputs.

targetScope = 'subscription'

@description('Short environment name, e.g. prod. Used in resource naming and tags.')
param environmentName string

@description('Azure region for the resource group and the resources within it.')
param location string

@description('Name of the resource group that holds the deployment.')
param resourceGroupName string

@description('Tags applied to the resource group and inherited by modules.')
param tags object = {
  project: 'CAS'
  environment: environmentName
  managedBy: 'bicep'
}

resource rg 'Microsoft.Resources/resourceGroups@2024-03-01' = {
  name: resourceGroupName
  location: location
  tags: tags
}

// Hosting-environment modules deploy into the resource group above, scoped to
// rg. The foundation module is first: it establishes the shared network and
// compute environment (VNet, ACA environment, Log Analytics, ACR) that later
// modules consume through its outputs.
module foundation 'modules/foundation.bicep' = {
  name: 'foundation'
  scope: rg
  params: {
    location: location
    environmentName: environmentName
    tags: tags
  }
}

@description('Provisioned resource group name, consumed by module deployments.')
output deployedResourceGroupName string = rg.name

@description('Resource id of the Azure Container Apps environment.')
output acaEnvironmentId string = foundation.outputs.acaEnvironmentId

@description('Login server host of the Azure Container Registry.')
output acrLoginServer string = foundation.outputs.acrLoginServer
