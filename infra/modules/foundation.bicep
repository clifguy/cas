// CAS cloud deployment — hosting foundation module.
//
// Provisions the shared, multi-app hosting foundation for the cloud
// deployment profile (CAS-ADR-042): a virtual network with the subnets the
// platform needs, a VNet-integrated Azure Container Apps environment, a Log
// Analytics workspace wired to that environment, and an Azure Container
// Registry. No application-specific resources live here — the foundation is
// reusable across every container app that later deploys into it, which
// compose against the outputs at the bottom of this file.
//
// Resource-group scoped (the Bicep default): the orchestrator deploys it with
// scope: rg. The VNet carries two delegated subnets — one for the ACA
// environment, one a managed Postgres Flexible Server integrates into for
// private connectivity — so all address-space management stays in one place.

@description('Azure region for all foundation resources.')
param location string

@description('Short environment name, e.g. prod. Used in resource naming.')
param environmentName string

@description('Tags applied to every foundation resource.')
param tags object

@description('Address space of the hosting virtual network.')
param vnetAddressPrefix string = '10.20.0.0/16'

@description('Prefix of the ACA infrastructure subnet (delegated to Microsoft.App/environments).')
param acaInfraSubnetPrefix string = '10.20.0.0/23'

@description('Prefix of the delegated subnet a managed Postgres Flexible Server integrates into.')
param postgresSubnetPrefix string = '10.20.2.0/24'

@description('Log Analytics workspace retention in days.')
param logAnalyticsRetentionDays int = 30

@description('SKU name of the Azure Container Registry.')
param acrSku string = 'Basic'

// ACR names are globally unique and alphanumeric; derive a stable one from the
// resource group id rather than taking it as a parameter.
var acrName = toLower('acr${uniqueString(resourceGroup().id)}')

// The virtual network. Subnets are declared inline so they are created as part
// of the single VNet deployment — this avoids the concurrent-subnet-write
// conflict that nested child subnets hit on a shared network. Subnet 0 is the
// ACA infrastructure subnet; subnet 1 is the delegated Postgres subnet.
resource vnet 'Microsoft.Network/virtualNetworks@2024-05-01' = {
  name: 'vnet-${environmentName}'
  location: location
  tags: tags
  properties: {
    addressSpace: {
      addressPrefixes: [
        vnetAddressPrefix
      ]
    }
    subnets: [
      {
        name: 'aca-infra'
        properties: {
          addressPrefix: acaInfraSubnetPrefix
          delegations: [
            {
              name: 'aca-delegation'
              properties: {
                serviceName: 'Microsoft.App/environments'
              }
            }
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
              properties: {
                serviceName: 'Microsoft.DBforPostgreSQL/flexibleServers'
              }
            }
          ]
        }
      }
    ]
  }
}

// Log Analytics workspace that backs the ACA environment's application logs.
resource logAnalytics 'Microsoft.OperationalInsights/workspaces@2023-09-01' = {
  name: 'log-${environmentName}'
  location: location
  tags: tags
  properties: {
    sku: {
      name: 'PerGB2018'
    }
    retentionInDays: logAnalyticsRetentionDays
  }
}

// The Azure Container Apps environment, VNet-integrated into the ACA subnet and
// streaming application logs to the workspace above. A single Consumption
// workload profile gives scale-to-zero today and leaves room for dedicated
// profiles later. Public ingress (internal: false); apps enforce their own auth.
resource acaEnvironment 'Microsoft.App/managedEnvironments@2024-03-01' = {
  name: 'cae-${environmentName}'
  location: location
  tags: tags
  properties: {
    appLogsConfiguration: {
      destination: 'log-analytics'
      logAnalyticsConfiguration: {
        customerId: logAnalytics.properties.customerId
        sharedKey: logAnalytics.listKeys().primarySharedKey
      }
    }
    vnetConfiguration: {
      infrastructureSubnetId: vnet.properties.subnets[0].id
      internal: false
    }
    workloadProfiles: [
      {
        name: 'Consumption'
        workloadProfileType: 'Consumption'
      }
    ]
  }
}

// The container registry the platform pulls app images from. The admin user is
// disabled — pulls authenticate via managed identity / RBAC, never a stored
// admin credential — keeping the no-stored-secret posture of the deploy path.
resource registry 'Microsoft.ContainerRegistry/registries@2023-07-01' = {
  name: acrName
  location: location
  tags: tags
  sku: {
    name: acrSku
  }
  properties: {
    adminUserEnabled: false
  }
}

@description('Resource id of the hosting virtual network.')
output vnetId string = vnet.id

@description('Resource id of the Azure Container Apps environment.')
output acaEnvironmentId string = acaEnvironment.id

@description('Name of the Azure Container Apps environment (referenced as an existing parent by environment-level child resources such as custom-domain certificates).')
output acaEnvironmentName string = acaEnvironment.name

@description('Default domain of the Azure Container Apps environment.')
output acaEnvironmentDefaultDomain string = acaEnvironment.properties.defaultDomain

@description('Login server host of the Azure Container Registry.')
output acrLoginServer string = registry.properties.loginServer

@description('Name of the Azure Container Registry (referenced as an existing resource by the container-apps module to scope its AcrPull grants).')
output acrName string = registry.name

@description('Resource id of the ACA infrastructure subnet.')
output acaInfraSubnetId string = vnet.properties.subnets[0].id

@description('Resource id of the delegated Postgres subnet.')
output postgresSubnetId string = vnet.properties.subnets[1].id
