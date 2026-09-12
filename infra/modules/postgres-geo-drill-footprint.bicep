// Destination-region footprint for a geo restore drill of the serving PostgreSQL
// server (CAS-ADR-042).
//
// Azure will not geo-restore a private-access server onto a public endpoint, so
// the restore needs a delegated subnet and a private DNS zone in the region it
// lands in, and the read-only verification job needs compute that can reach
// them. This module creates all of it from nothing, in a resource group the
// restore-verification driver creates for one drill run and deletes afterwards.
//
// Deployed out of band by that driver, never by the serving template, and it
// carries no deletion lock: it is drill scaffolding, not a serving resource. It
// references no existing resource, because the other networks in the destination
// region belong to other workloads. Every resource carries the drill's ownership
// tag, because the driver refuses to delete a group holding anything untagged.
@description('Destination region of the geo restore.')
param location string
@description('Environment name.')
param environmentName string
@description('Drill run id; names every resource and is written as the ownership tag.')
param runId string
@description('Address space of the drill network. Chosen apart from the serving network so a later peering stays possible.')
param vnetAddressPrefix string = '10.30.0.0/16'
@description('Prefix of the Container Apps infrastructure subnet.')
param acaInfraSubnetPrefix string = '10.30.0.0/23'
@description('Prefix of the delegated subnet the restored server integrates into.')
param postgresSubnetPrefix string = '10.30.2.0/24'

var tags = { casRestoreDrill: runId }
var suffix = '${environmentName}-geodrill-${runId}'

// Subnets are declared inline so they are created with the network in one write.
// Subnet 0 hosts the Container Apps environment; subnet 1 the restored server.
resource vnet 'Microsoft.Network/virtualNetworks@2024-05-01' = {
  name: 'vnet-${suffix}'
  location: location
  tags: tags
  properties: {
    addressSpace: { addressPrefixes: [vnetAddressPrefix] }
    subnets: [
      {
        name: 'aca-infra'
        properties: {
          addressPrefix: acaInfraSubnetPrefix
          delegations: [
            { name: 'aca-delegation', properties: { serviceName: 'Microsoft.App/environments' } }
          ]
        }
      }
      {
        name: 'postgres'
        properties: {
          addressPrefix: postgresSubnetPrefix
          delegations: [
            {
              name: 'postgres-delegation'
              properties: { serviceName: 'Microsoft.DBforPostgreSQL/flexibleServers' }
            }
          ]
        }
      }
    ]
  }
}

// The restore writes the server's A record here. A zone of the drill's own keeps
// the serving zone's record set untouched.
resource zone 'Microsoft.Network/privateDnsZones@2020-06-01' = {
  name: '${suffix}.private.postgres.database.azure.com'
  location: 'global'
  tags: tags
}

resource zoneLink 'Microsoft.Network/privateDnsZones/virtualNetworkLinks@2020-06-01' = {
  parent: zone
  name: 'link-${suffix}'
  location: 'global'
  tags: tags
  properties: {
    virtualNetwork: { id: vnet.id }
    registrationEnabled: false
  }
}

// Backs the environment's console logs, where the verification report is read.
resource logs 'Microsoft.OperationalInsights/workspaces@2023-09-01' = {
  name: 'log-${suffix}'
  location: location
  tags: tags
  properties: {
    sku: { name: 'PerGB2018' }
    retentionInDays: 30
  }
}

// Internal: the drill runs a manual job and serves nothing, so no public ingress.
resource environment 'Microsoft.App/managedEnvironments@2024-03-01' = {
  name: 'cae-${suffix}'
  location: location
  tags: tags
  properties: {
    appLogsConfiguration: {
      destination: 'log-analytics'
      logAnalyticsConfiguration: {
        customerId: logs.properties.customerId
        sharedKey: logs.listKeys().primarySharedKey
      }
    }
    vnetConfiguration: {
      infrastructureSubnetId: vnet.properties.subnets[0].id
      internal: true
    }
    workloadProfiles: [
      { name: 'Consumption', workloadProfileType: 'Consumption' }
    ]
  }
}

@description('Delegated subnet the geo restore is given.')
output postgresSubnetId string = vnet.properties.subnets[1].id
@description('Private DNS zone the geo restore is given.')
output privateDnsZoneId string = zone.id
@description('Container Apps environment the verification job runs in.')
output environmentId string = environment.id
@description('Region the footprint was built in.')
output location string = location
