// CAS cloud deployment — relational store module.
//
// Provisions the managed Azure Database for PostgreSQL Flexible Server that
// backs SAGE's content and graph stores in the cloud deployment profile
// (CAS-ADR-042). The server integrates privately into the delegated subnet the
// foundation module owns — no public endpoint — and resolves through a private
// DNS zone linked to the hosting virtual network. Authentication is Entra-only:
// Active Directory auth is enabled and password auth is disabled, so no database
// password exists to store anywhere in the application path.
//
// The server parameters reproduce what the Postgres storage adapters expect: the
// `azure.extensions` allowlist carries pgvector (the content store's embedding
// column) and pgstattuple (bloat measurement), since a managed server refuses
// `CREATE EXTENSION` for anything not allowlisted. Native `ts_rank` full-text
// search needs no server parameter. High availability is deferred (single-zone).
//
// Resource-group scoped (the Bicep default): the orchestrator deploys it with
// scope: rg, composing the delegated-subnet and VNet ids from the foundation's
// outputs.

@description('Azure region for the server (the private DNS zone is global).')
param location string

@description('Short environment name, e.g. prod. Used in resource naming.')
param environmentName string

@description('Tags applied to every resource in this module.')
param tags object

@description('Resource id of the delegated subnet the server integrates into.')
param delegatedSubnetId string

@description('Resource id of the virtual network the private DNS zone links to.')
param vnetId string

@description('Name of the database SAGE connects to within the server.')
param databaseName string = 'sage'

@description('Major PostgreSQL version of the Flexible Server.')
param postgresVersion string = '16'

@description('Compute SKU name of the Flexible Server.')
param skuName string = 'Standard_B2s'

@description('Compute SKU tier of the Flexible Server.')
param skuTier string = 'Burstable'

@description('Provisioned storage for the Flexible Server, in gibibytes.')
param storageSizeGB int = 32

@description('Object id of the Entra principal granted server administrator. Empty skips the binding so the principal can be supplied at deploy time.')
param aadAdminObjectId string = ''

@description('Display name of the Entra administrator principal.')
param aadAdminPrincipalName string = ''

@description('Type of the Entra administrator principal: User, Group, or ServicePrincipal.')
param aadAdminPrincipalType string = 'Group'

// Flexible Server names are globally unique DNS labels; derive a stable one from
// the resource group id rather than taking it as a parameter.
var serverName = 'psql-${environmentName}-${uniqueString(resourceGroup().id)}'

// Private DNS zone the VNet-integrated server registers in, linked to the
// hosting network so the server FQDN resolves privately from inside the VNet.
resource dnsZone 'Microsoft.Network/privateDnsZones@2020-06-01' = {
  name: '${environmentName}.private.postgres.database.azure.com'
  location: 'global'
  tags: tags
}

resource dnsLink 'Microsoft.Network/privateDnsZones/virtualNetworkLinks@2020-06-01' = {
  parent: dnsZone
  name: 'link-${environmentName}'
  location: 'global'
  tags: tags
  properties: {
    registrationEnabled: false
    virtualNetwork: {
      id: vnetId
    }
  }
}

// The managed PostgreSQL Flexible Server. Private access via the delegated
// subnet and the zone above; Entra-only authentication; single-zone (HA
// deferred). The zone link must exist before the server is created, hence the
// explicit dependsOn.
resource server 'Microsoft.DBforPostgreSQL/flexibleServers@2024-08-01' = {
  name: serverName
  location: location
  tags: tags
  sku: {
    name: skuName
    tier: skuTier
  }
  properties: {
    version: postgresVersion
    storage: {
      storageSizeGB: storageSizeGB
    }
    highAvailability: {
      mode: 'Disabled'
    }
    authConfig: {
      activeDirectoryAuth: 'Enabled'
      passwordAuth: 'Disabled'
      tenantId: subscription().tenantId
    }
    network: {
      delegatedSubnetResourceId: delegatedSubnetId
      privateDnsZoneArmResourceId: dnsZone.id
    }
  }
  dependsOn: [
    dnsLink
  ]
}

// Allowlist the extensions the schema bootstrap enables. pgvector backs the
// content store's embedding column; pgstattuple backs bloat measurement. Native
// ts_rank full-text search needs no server parameter.
resource extensions 'Microsoft.DBforPostgreSQL/flexibleServers/configurations@2024-08-01' = {
  parent: server
  name: 'azure.extensions'
  properties: {
    value: 'VECTOR,PGSTATTUPLE'
    source: 'user-override'
  }
}

// The database SAGE connects to. Vault schemas are created idempotently by the
// storage bootstrap at runtime within this database.
resource database 'Microsoft.DBforPostgreSQL/flexibleServers/databases@2024-08-01' = {
  parent: server
  name: databaseName
  properties: {
    charset: 'UTF8'
    collation: 'en_US.utf8'
  }
}

// Entra server administrator. Conditional so the module provisions cleanly
// before the principal is known; the object id is supplied at deploy time
// (never committed) when the deploy identity is activated.
resource aadAdmin 'Microsoft.DBforPostgreSQL/flexibleServers/administrators@2024-08-01' = if (!empty(aadAdminObjectId)) {
  parent: server
  name: aadAdminObjectId
  properties: {
    principalType: aadAdminPrincipalType
    principalName: aadAdminPrincipalName
    tenantId: subscription().tenantId
  }
}

@description('Fully qualified domain name of the Postgres Flexible Server.')
output postgresServerFqdn string = server.properties.fullyQualifiedDomainName

@description('Name of the database SAGE connects to.')
output postgresDatabaseName string = databaseName

@description('Name of the provisioned Postgres Flexible Server.')
output postgresServerName string = server.name
