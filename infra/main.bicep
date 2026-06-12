// CAS cloud deployment — orchestrator.
//
// Targets the subscription so it can create the resource group that every
// hosting-environment module deploys into. Hosting-environment modules live
// under infra/modules/ and are wired in below as the cloud deployment
// profile (CAS-ADR-042) is built out; at this scaffold stage the template
// deploys only the resource group and its tags, which gives the pipeline a
// real, idempotent footprint to plan and apply.

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

// Hosting-environment modules deploy into the resource group above. As each
// lands it is wired here, scoped to rg. The foundation module is first:
//
//   module foundation 'modules/foundation.bicep' = {
//     name: 'foundation'
//     scope: rg
//     params: {
//       location: location
//       tags: tags
//     }
//   }

@description('Provisioned resource group name, consumed by module deployments.')
output deployedResourceGroupName string = rg.name
